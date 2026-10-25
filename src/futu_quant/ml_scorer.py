"""ML标的评分模块"""

from __future__ import annotations

import hashlib
import math
import pickle
import re
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd

if TYPE_CHECKING:
    from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)


class MissingFeatureError(RuntimeError):
    """推理侧拿不到模型所需特征（例如缺行情上下文）。

    必须显式失败：`predict_symbol_score` 以前用 `except Exception: return 0.0`
    把它吞成「最低分」，结果是模型静默失效、所有标的同时被压到 0.0，
    日志里只剩一行 error —— 这正是要消灭的静默降级形态。
    """


def moneyness_ratio(spot: float | None, strike: float | None) -> float | None:
    """虚实值偏离度 moneyness = S/K - 1（v3.2 定义）。

    训练侧与推理侧**必须**引用这同一个函数：以前训练用 `strike/median(strike)-1`
    顶替，是随当天候选池漂移、换一天值就变的伪特征，推理侧根本无法复现。
    """
    try:
        spot_value = float(spot or 0)
        strike_value = float(strike or 0)
    except (TypeError, ValueError):
        return None
    if spot_value <= 0 or strike_value <= 0:
        return None
    return spot_value / strike_value - 1.0


def gamma_to_theta_ratio(gamma: float | None, theta: float | None) -> float | None:
    """做多波动率凸性性价比 gamma_to_theta = |gamma| / (|theta| + 1e-6)（v3.2 定义）。"""
    try:
        gamma_value = abs(float(gamma))
        theta_value = abs(float(theta))
    except (TypeError, ValueError):
        return None
    if gamma_value <= 0 or theta_value <= 0:
        return None
    return gamma_value / (theta_value + 1e-6)


# 推理侧**真实可产**的特征全集；模型 `feature_cols` 必须是它的子集。
# 单列成常量是刻意的：训练侧 / `load_ml_model` 签名校验 / 守卫测试都引用同一份定义，
# 防止「训练加了新特征、推理没跟上 → 预测 KeyError → 静默 0.0」再次发生。
INFERENCE_FEATURE_KEYS = frozenset({
    # 期权静态特征
    'days_to_expiry', 'is_call', 'strike_price', 'expiry_bucket',
    # 历史统计
    'symbol_avg_pnl', 'symbol_std_pnl', 'symbol_max_pnl', 'symbol_min_pnl',
    'symbol_win_rate', 'symbol_3d_avg_pnl', 'symbol_7d_avg_pnl',
    'symbol_trade_count', 'symbol_pnl_trend',
    # 时间
    'day_of_week', 'hour_of_day', 'is_market_close', 'month', 'quarter',
    # 市场环境（无 broker 时为确定性占位值）
    'vix_level', 'spy_return_1d', 'spy_return_5d', 'market_regime',
    # v3.2 凸性/无量纲（需要 market_context 里的真实行情）
    'moneyness', 'gamma_to_theta',
})

# moneyness 的 spot 来源**唯一**：最近一条 `option_history` 快照（Chief 2026-10-25 裁定）。
# 不允许回落到 `owner["last_price"]`（扫描实时价）——那是另一套口径，等于 train/serve skew。
SNAPSHOT_MAX_LAG_MINUTES = 45
_SNAPSHOT_CACHE_TTL_SECONDS = 60.0
_SNAPSHOT_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}
_NEW_YORK = ZoneInfo("America/New_York")


def _snapshot_time(raw) -> datetime | None:
    """option_history.time_key 的口径与 `data_quality.quote_freshness` 一致：naive=ET。"""
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=_NEW_YORK)
    return stamp


def _clear_snapshot_cache() -> None:
    """清空快照缓存（测试隔离用：模块级 TTL 缓存会跨 tmp DB 串味）。"""
    _SNAPSHOT_CACHE.clear()


def latest_underlying_snapshot(
    store,
    symbol: str,
    trade_time: datetime,
    max_lag_minutes: int = SNAPSHOT_MAX_LAG_MINUTES,
) -> dict:
    """最近一条 `option_history` 快照（spot / lag / 希腊字母），推理侧唯一行情来源。

    返回 ``{"spot", "lag_minutes", "gamma", "theta"}``；快照缺失或滞后 > ``max_lag_minutes``
    → ``spot=None``：**只置空，绝不回落 `owner["last_price"]` 或实时链希腊字母**。

    缓存：key 含 **(库路径, underlying, 分钟)** + 60s TTL —— 不带库标识会让不同
    tmp DB 互相命中（Reviewer 2026-10-25 指出）。
    """
    empty = {"spot": None, "lag_minutes": None, "gamma": None, "theta": None}
    info = parse_option_symbol(symbol)
    if info is None or store is None:
        return empty
    underlying = str(info.get("underlying") or "").upper()
    if not underlying:
        return empty
    if not underlying.startswith("US."):
        underlying = f"US.{underlying}"

    moment = trade_time if trade_time.tzinfo is not None else trade_time.replace(tzinfo=_NEW_YORK)
    moment_utc = moment.astimezone(timezone.utc)
    store_key = str(getattr(store, "path", "")) or str(id(store))
    cache_key = (store_key, underlying, moment.strftime("%Y%m%d%H%M"))
    cached = _SNAPSHOT_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] <= _SNAPSHOT_CACHE_TTL_SECONDS:
        return dict(cached[1])

    start_key = (moment - timedelta(minutes=max_lag_minutes)).astimezone(_NEW_YORK)
    end_key = moment.astimezone(_NEW_YORK)
    try:
        rows = store.option_history_for_underlying(
            underlying,
            start=start_key.strftime("%Y-%m-%d %H:%M:%S"),
            end=end_key.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 快照失败: %s", underlying, exc)
        _SNAPSHOT_CACHE[cache_key] = (now, dict(empty))
        return empty

    result = dict(empty)
    for row in reversed(rows or []):
        stamp = _snapshot_time(row.get("time_key"))
        spot = _safe_float(row.get("underlying_price"))
        if stamp is None or spot is None or spot <= 0:
            continue
        delta_minutes = (moment_utc - stamp.astimezone(timezone.utc)).total_seconds() / 60.0
        if delta_minutes < 0:
            continue
        result["lag_minutes"] = round(delta_minutes, 3)
        if delta_minutes <= max_lag_minutes:
            result["spot"] = float(spot)
            result["gamma"] = _safe_float(row.get("gamma"))
            result["theta"] = _safe_float(row.get("theta"))
        break

    _SNAPSHOT_CACHE[cache_key] = (now, dict(result))
    return result


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


# ---------------------------------------------------------------------------
# rule scorer v2（连续化）常量与纯函数
#
# 常量**冻结**在此处、不允许运行时从配置读取：对照组口径必须稳定可复现，
# 可配会造成「每次对比基线都不一样」（设计 §4）。
# ---------------------------------------------------------------------------
RULE_SCORER_VERSION = "rule scorer v2（连续化 + 中立 tie-break）"
_RULE_EPS = 1e-6
# Layer A（承载方向，权重结构对齐旧公式）
_RULE_W_WIN = 0.175        # 等价旧 0.35*(w-0.5)
_RULE_W_TREND = 0.05       # 等价旧 ±0.05 的幅值上限
_RULE_W_PNL = 0.05
# Layer B（只在 Layer A 完全等分时起作用，合计上界 0.035 < A 层最小间隔）
_RULE_W_MOM = 0.020
_RULE_W_PAYOFF = 0.010
_RULE_W_SHRINK = 0.005
_RULE_SATURATION = 2.0     # tanh 饱和常数
_RULE_SHRINK_K = 5.0       # 交易笔数置信度平滑
# L2 破并列：同 raw 分组内每级偏移；1e-9 << 任何两层分的可分辨间隔。
# **常量本身没问题**（A1「同日 Top3 不并列」就是靠它过的）；
# 禁止的是**按有语义的键（行序 / 合约号 / strike / DTE / 盈亏）算偏移**——
# 旧实现按 (DTE, bucket, call, strike, 行号) 升序赋偏移，实测并列组内判别 AUC
# 0.4121（分位 0.1%，比随机还差，Analyst 2026-10-25）。破并列必须对称、无方向。
_RULE_TIE_STEP = 1e-9
# tie-break 哈希盐：只影响实现细节，不携带任何业务语义；改动它等于换一个
# 「同样中立但次序不同」的排列（用于回归/变异时区分「中立」与「按语义」）。
_RULE_TIE_SALT = b"rule-scorer-neutral-tiebreak-v1"
# 参与哈希的**合约身份**字段（入场时点即可知，与盈亏/方向无关）。
# 顺序固定，缺失走中性占位，避免同一合约在不同字段缺失下算出不同哈希。
_TIE_IDENTITY_KEYS = (
    "underlying_symbol",
    "strike_price",
    "is_call",
    "days_to_expiry",
    "expiry_bucket",
)
# 交易日单独进哈希：**不是**排序语义，而是让每个交易日的组内排列彼此独立。
# 不带日期时是「一张全历史的合约→名次映射」，同一合约在各日名次相同，
# 偏差会跨日累积（实测单盐在旧并列组内 AUC 0.4483，分位 0.0%，2.6σ）；
# 带上日期后与零分布「按日独立置换」的假设一致（实测 0.4914 / 分位 36.7%）。
# 次序只在**同一交易日的横截面**内有意义（rule 打分本来就只用于当日选品）。
_TIE_BUCKET_KEY = "trade_date"
# 预注册盐池（**仅验收守卫使用，不参与生产**）：24 个与数据无关的固定常量。
# 铁律：禁止按历史样本表现挑选/调整 `_RULE_TIE_SALT` 或任何盐——那等于用结果反选
# 随机排列，把 look-ahead 塞进 rule 对照组（Chief 2026-10-25 裁定 A）。盐池与出厂盐的
# 哈希指纹由 `tests/test_rule_neutral_tiebreak.py::test_B5_salt_pool_is_preregistered` 冻结。
_RULE_TIE_PROBE_SALTS = tuple(
    f"rule-neutral-tiebreak-probe-{i:02d}".encode() for i in range(24)
)
# 生产侧 **K 盐融合**：组内每个盐给一次名次，按平均名次排序（Chief 2026-10-25 裁定）。
# 为什么不是单盐：单盐的 realised 分位本身就是一次抽签（实测出厂单盐 2.9σ 不利），
# 机制级验收无法保证「落地无偏」；K 个预注册盐取平均后， realised 按大数定律收敛到中位，
# 既不靠挑「好盐」，也不引入任何数据依赖。盐从上面预注册池前 K 个取，K 变也必须改守卫。
_RULE_TIE_FUSION_K = 8
_RULE_TIE_FUSION_SALTS = _RULE_TIE_PROBE_SALTS[:_RULE_TIE_FUSION_K]


def _row_number(row, key: str, default: float) -> float:
    """从 Series/dict 安全取数：缺失、None、NaN、非数值一律回默认值（绝不抛异常）。"""
    try:
        value = row.get(key, default)
    except AttributeError:  # 非 dict-like
        return float(default)
    if value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:  # NaN
        return float(default)
    return number


def _continuous_components(row) -> dict:
    """逐行独立的连续化分量（不依赖其他样本，不做横截面归一化）。

    美元量全部除以标的自身 ``symbol_std_pnl``（量纲相消 → 账户规模/标的波动放大时不漂移），
    再经 ``tanh`` 有界压缩；缺字段走中性值，绝不抛异常。
    """
    win_rate = _row_number(row, "symbol_win_rate", 0.5)
    avg_pnl = _row_number(row, "symbol_avg_pnl", 0.0)
    pnl_trend = _row_number(row, "symbol_pnl_trend", 0.0)
    max_pnl = _row_number(row, "symbol_max_pnl", 0.0)
    min_pnl = _row_number(row, "symbol_min_pnl", 0.0)
    avg_3d = _row_number(row, "symbol_3d_avg_pnl", 0.0)
    avg_7d = _row_number(row, "symbol_7d_avg_pnl", 0.0)
    trade_count = max(0.0, _row_number(row, "symbol_trade_count", 0.0))

    sigma = abs(_row_number(row, "symbol_std_pnl", 0.0))
    if sigma <= _RULE_EPS:
        # 缺/零波动兜底：用自身平均盈亏量级或 1.0，仍保持无量纲
        sigma = max(abs(avg_pnl), 1.0)

    c_win = 2.0 * (win_rate - 0.5)  # ∈[-1,1]，与旧口径同向
    c_pnl = math.tanh((avg_pnl / sigma) / _RULE_SATURATION)
    c_trend = math.tanh((pnl_trend / sigma) / _RULE_SATURATION)
    c_mom = math.tanh(((avg_3d - avg_7d) / sigma) / _RULE_SATURATION)
    payoff = max_pnl / (abs(min_pnl) + _RULE_EPS)
    c_payoff = math.tanh(math.log(max(payoff, _RULE_EPS)) / _RULE_SATURATION)
    shrink_gap = trade_count / (trade_count + _RULE_SHRINK_K) if trade_count > 0 else 0.0

    # u_* 落在 ±[0.5,1]，贡献 ±[0.025,0.05]：与旧实现同号、同量级、仅细化
    u_trend = c_trend * (0.5 + 0.5 * abs(c_trend))
    u_pnl = c_pnl * (0.5 + 0.5 * abs(c_pnl))

    score_a = (
        0.5
        + _RULE_W_WIN * c_win
        + _RULE_W_TREND * u_trend
        + _RULE_W_PNL * u_pnl
    )
    score_b = (
        _RULE_W_MOM * c_mom
        + _RULE_W_PAYOFF * c_payoff
        + _RULE_W_SHRINK * shrink_gap
    )
    return {
        "c_win": c_win,
        "c_pnl": c_pnl,
        "c_trend": c_trend,
        "c_mom": c_mom,
        "c_payoff": c_payoff,
        "shrink_gap": shrink_gap,
        "score_a": score_a,
        "score_b": score_b,
        "raw": score_a + score_b,
    }


def _tie_identity_text(row, key: str) -> str:
    """把合约身份字段规整成稳定文本（同一合约在任何环境下都必须得到同一字符串）。"""
    try:
        value = row.get(key, None)
    except AttributeError:  # 非 dict-like
        return "-"
    if value is None:
        return "-"
    if isinstance(value, str):
        return value.strip().upper() or "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip() or "-"
    if number != number:  # NaN
        return "-"
    # 定点格式避免 100 vs 100.0 vs 1e2 这类表示差异造出不同哈希
    return f"{number:.10g}"


def _neutral_tie_rank(row, salt: bytes | None = None) -> int:
    """L2 并列组内的**对称**哈希次序（无方向、无经济含义，仅消除任意性）。

    为什么必须是哈希而不是排序键：并列样本之间没有任何**可用且有因果依据**的
    先后信息，任何按语义字段（行序 / 合约号 / strike / DTE / 盈亏）排序都会把
    一个偶然相关的方向偷偷变成选品信号。旧实现正是按
    ``(DTE, expiry_bucket, -is_call, strike, 行号)`` 升序赋偏移，实测并列组内
    配对 AUC = 0.4121（随机 0.5，分位 0.1%）—— 兜底打分器一旦接管，选品方向被
    反过来。哈希把「能区分」和「方向信息」分开：次序可复现，但与任何语义字段
    的秩相关期望为 0。

    哈希只吃**合约身份**（标的 / strike / call-put / DTE / expiry_bucket）
    + 交易日桶，输入相同则次序完全相同（跨进程、跨机器可复现）；身份完全相同的
    重复行交给 Python ``sorted`` 的稳定性兜底（这种行之间先后无经济含义）。
    """
    effective_salt = _RULE_TIE_SALT if salt is None else salt
    identity = "|".join(
        [_tie_identity_text(row, _TIE_BUCKET_KEY)]
        + [_tie_identity_text(row, key) for key in _TIE_IDENTITY_KEYS]
    )
    digest = hashlib.blake2b(
        effective_salt + identity.encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def _fused_tie_order(rows, indices: list[int]) -> list[int]:
    """K 盐融合次序：每个预注册盐给组内一次名次，按**平均名次**升序。

    单盐 realised 分位是一次抽签；平均 K 个独立哈希后，组内名次趋向均匀，
    分散度收窄（sd 32.4 → 27.5），但对「单个排序的一次落地值」**不会**收敛到中位
    （LLN 只适用于 K 个统计量的平均，不适用单个排序的 realised 分位）。

    实现上只用**整数秩求和**：绝不把 64 位哈希整数转 float 再平均（float 只有 53 位尾数，
    平均后会产生伪相等/次序不稳）。同分时先按第一支盐的名次、再按出厂盐哈希，保证确定性。
    """
    if len(indices) < 2:
        return list(indices)
    totals = {index: 0 for index in indices}
    baseline: dict[int, int] = {}
    for position, salt in enumerate(_RULE_TIE_FUSION_SALTS):
        ordered = sorted(indices, key=lambda i: _neutral_tie_rank(rows[i], salt))
        for rank, index in enumerate(ordered):
            totals[index] += rank
            if position == 0:
                baseline[index] = rank
    return sorted(
        indices,
        key=lambda i: (totals[i], baseline[i], _neutral_tie_rank(rows[i])),
    )


def _legacy_discrete_score(row) -> float:
    """**仅审计用**：旧离散打分公式，用来划分「旧实现并列组」这一评估总体。"""
    win_rate = _row_number(row, "symbol_win_rate", 0.50)
    score = 0.50 + (win_rate - 0.50) * 0.35
    trend = _row_number(row, "symbol_pnl_trend", 0.0)
    score += 0.05 if trend > 0 else (-0.05 if trend < 0 else 0.0)
    if _row_number(row, "symbol_avg_pnl", 0.0) > 0:
        score += 0.05
    return max(0.05, min(0.95, score))


_TIEBREAK_AUDIT_COLUMNS = (
    "trade_date",
    "window_net_pnl",
    "symbol_win_rate",
    "symbol_pnl_trend",
    "symbol_avg_pnl",
) + _TIE_IDENTITY_KEYS


def rule_tiebreak_realised_quality(
    df: pd.DataFrame,
    *,
    n_perm: int = 500,
    seed: int = 20261025,
    include_segments: bool = True,
) -> dict:
    """兜底 rule 的 tie-break **落地质量监控**（Chief 2026-10-25 裁定 ②，双保险）。

    度量（Chief/Analyst 口径）：旧离散热分值并列组内（size≥2 且组内
    ``window_net_pnl`` 有差异）的配对 AUC = 赢家排在输家之前的比例；分位 = 该 AUC 在
    ``n_perm`` 次**组内随机排列** AUC 分布中的 midrank 位置。

    这是**监控指标，不是判据**：realised 分位是「一个排序的一次落地值」，对任何确定性
    中立排序都近似均匀分布（K 盐融合只把分散度从 sd≈32 收到 ≈27，**不会**收敛到中位，
    LLN 不适用于单个排序）。写进 ``audit_events`` 长期观察即可，**禁止据此调盐**。

    同时给出「K 盐融合」与「出厂单盐」两个分位，便于判断融合是否失效；并按交易日
    **顺序**切 5 段给出分段诊断（不随机切，避免跨段泄漏）——分段只作监控粒度，
    不作 [40,60] 之类门槛（那会给中立实现假红）。

    缺列 / 样本不足时返回 ``status="unavailable"`` 并点名缺什么（不静默给 0）。
    """
    import numpy as np

    missing = [column for column in _TIEBREAK_AUDIT_COLUMNS if column not in df.columns]
    if missing:
        return {"status": "unavailable", "reason": "missing_columns", "missing": missing}
    if df.empty:
        return {"status": "unavailable", "reason": "empty_dataframe"}

    # 先把「组」拍平成可复用的记录：分段统计时不必重算哈希与配对。
    records: list[dict] = []
    for trade_date, day in df.groupby("trade_date"):
        day = day.reset_index(drop=True)
        if day.empty:
            continue
        legacy = day.apply(_legacy_discrete_score, axis=1).to_numpy()
        buckets: dict[float, list[int]] = {}
        for index, value in enumerate(legacy):
            buckets.setdefault(value, []).append(index)
        rows = list(day.iterrows())
        pnl = day["window_net_pnl"].to_numpy(dtype=float)
        for indices in buckets.values():
            if len(indices) < 2:
                continue
            group_rows = [rows[i][1] for i in indices]
            fused_order = _fused_tie_order(group_rows, list(range(len(indices))))
            fused_pos = {local: rank for rank, local in enumerate(fused_order)}
            single_order = sorted(
                range(len(indices)), key=lambda i: _neutral_tie_rank(group_rows[i])
            )
            single_pos = {local: rank for rank, local in enumerate(single_order)}
            winners: list[int] = []
            losers: list[int] = []
            for first in range(len(indices)):
                for second in range(first + 1, len(indices)):
                    left, right = indices[first], indices[second]
                    if abs(pnl[left] - pnl[right]) < 1e-12:
                        continue
                    winner, loser = (left, right) if pnl[left] > pnl[right] else (right, left)
                    winners.append(indices.index(winner))
                    losers.append(indices.index(loser))
            if not winners:
                continue
            records.append({
                "day": str(trade_date),
                "size": len(indices),
                "winners": np.array(winners),
                "losers": np.array(losers),
                "fused_ok": int(sum(
                    fused_pos[winner] < fused_pos[loser]
                    for winner, loser in zip(winners, losers)
                )),
                "single_ok": int(sum(
                    single_pos[winner] < single_pos[loser]
                    for winner, loser in zip(winners, losers)
                )),
            })

    def _stats(
        subset: list[dict], perms: int, seed_offset: int,
        *, min_groups: int = 10, min_pairs: int = 200,
    ) -> dict | None:
        pairs_total = sum(len(item["winners"]) for item in subset)
        if len(subset) < min_groups or pairs_total < min_pairs:
            return None
        local_rng = np.random.default_rng(seed + seed_offset)
        null_counts = np.zeros(perms, dtype=np.int64)
        fused_ok = sum(item["fused_ok"] for item in subset)
        single_ok = sum(item["single_ok"] for item in subset)
        for item in subset:
            draws = np.argsort(
                local_rng.random((perms, item["size"])), axis=1
            )
            null_counts += (
                draws[:, item["winners"]] < draws[:, item["losers"]]
            ).sum(axis=1)
        null_auc = null_counts / pairs_total

        def _percentile(value: float) -> float:
            less = int(np.sum(null_auc < value - 1e-15))
            equal = int(np.sum(np.abs(null_auc - value) <= 1e-15))
            return round((less + 0.5 * equal) / len(null_auc) * 100.0, 2)

        fused_auc = fused_ok / pairs_total
        single_auc = single_ok / pairs_total
        return {
            "groups": len(subset),
            "pairs": pairs_total,
            "fused_auc": round(fused_auc, 6),
            "fused_percentile": _percentile(fused_auc),
            "single_salt_auc": round(single_auc, 6),
            "single_salt_percentile": _percentile(single_auc),
            "sample_sd": round(float(null_auc.std()), 6),
        }

    overall = _stats(records, n_perm, seed_offset=0)
    if overall is None:
        return {
            "status": "unavailable",
            "reason": "insufficient_sample",
            "groups": len(records),
            "pairs": sum(len(item["winners"]) for item in records),
        }

    result: dict = {
        "status": "ok",
        **overall,
        "fusion_k": _RULE_TIE_FUSION_K,
        "note": (
            "realised 分位为单份数据的抽样值（近似均匀分布），仅监控、不作判据，"
            "禁止据此挑盐；分段为按交易日顺序切 5 段，只作监控粒度"
        ),
    }
    if include_segments:
        days = [item["day"] for item in records]
        unique_days = sorted(set(days))
        result["days"] = len(unique_days)
        chunks = np.array_split(np.array(unique_days, dtype=object), 5)
        segments: list[dict] = []
        for position, chunk in enumerate(chunks, start=1):
            members = set(str(day) for day in chunk)
            subset = [item for item in records if item["day"] in members]
            # 分段是监控粒度：样本门槛放宽（分量小），但仍要求至少 3 组/50 对并在
            # payload 里带 groups/pairs，读者据此判断该段分位可信度。
            stats = _stats(
                subset, max(100, n_perm // 2), seed_offset=position,
                min_groups=3, min_pairs=50,
            )
            if stats is None:
                segments.append({
                    "segment": position,
                    "days": len(members),
                    "status": "unavailable",
                    "reason": "insufficient_sample",
                    "groups": len(subset),
                    "pairs": sum(len(item["winners"]) for item in subset),
                })
            else:
                segments.append({"segment": position, "days": len(members), "status": "ok", **stats})
        result["segments"] = segments
    return result


def summarise_tiebreak_history(entries, *, last_n: int = 30) -> dict:
    """聚合已入库的 realised 分位记录（Chief 2026-10-25 裁定 ②：按采样条数，不按自然日）。

    数据来源二：页面手动运行对照回测 + 收盘后调度审计（`scripts/ml_tiebreak_audit.py`），
    所以口径必须是「最近 N 条采样」，不能写成「最近 30 个自然日」（否则是假监控）。
    **采样率 = 训练 CSV 导出频率，不是每日**：CSV 不重导时每日运行只会 skipped_duplicate。
    `entries` 为按时间**由旧到新**排列的 audit payload 列表；非 ok 记录直接跳过。
    """
    samples = [item for item in entries if isinstance(item, dict) and item.get("status") == "ok"]
    if not samples:
        return {
            "status": "empty",
            "reason": "no_ok_samples",
            "window": "last_n_samples",
            "last_n": last_n,
            "note": (
                "暂无已入库 realised 分位：来源=手动运行对照回测/收盘后调度审计；"
                "采样率与训练 CSV 导出频率一致，不保证每日一条"
            ),
        }
    recent = samples[-last_n:]
    fused = [float(item["fused_percentile"]) for item in recent if "fused_percentile" in item]
    single = [
        float(item["single_salt_percentile"])
        for item in recent
        if "single_salt_percentile" in item
    ]
    return {
        "status": "ok",
        "window": "last_n_samples",
        "last_n": last_n,
        "samples": len(recent),
        "fused_mean_percentile": round(sum(fused) / len(fused), 2) if fused else None,
        "fused_min_percentile": round(min(fused), 2) if fused else None,
        "fused_max_percentile": round(max(fused), 2) if fused else None,
        "single_mean_percentile": round(sum(single) / len(single), 2) if single else None,
        "source": "manual_or_scheduled_samples",
        "note": (
            "口径=最近 N 条采样（不是最近 30 个自然日）；采样率与训练 CSV 导出频率一致、"
            "不保证每日一条；来源=手动运行对照回测/收盘后调度审计"
        ),
    }


class BuiltinRuleModel:
    """内置轻量纯 Python 打分器（`rule scorer v2` 连续化版）。

    设计见 `data/qa/rule_continuous_score_design.md`（Chief 已签字）。与原实现相比：
    - **L1 连续化**：`sign(trend)`、`(avg_pnl>0)` 这类阶跃换成同号连续梯度，
      美元量先除以标的自身 `symbol_std_pnl`（量纲相消，账户规模变化不漂移），再 tanh 有界压缩；
    - **L2 破并列**：`symbol_*` 全是标的级字段，同标的多个合约分数恒等，仅靠 L1 不够；
      在`raw`完全相等时用**对称哈希（blake2b）+ 合约身份**做 tie-break（**无方向、无经济含义**，
      只消除任意性；禁止改用行序 / 合约号 / strike / DTE 的方向序——旧实现那样做实测
      并列组内 AUC 0.4121，比随机还差）；不得解读为选品能力提升，也不得进准入卡；
    - 不做横截面归一化（推理侧逐标的调用拿不到当日候选池，池依赖会造成标度漂移）。
    """

    def predict_proba(self, X: pd.DataFrame):
        import numpy as np

        if X is None or len(X) == 0:
            return np.empty((0, 2))

        rows = list(X.iterrows())
        components = [_continuous_components(row) for _, row in rows]

        # Layer B **只在 Layer A 完全等分时启用**（设计 §1.2 正文）：
        # 若对所有权重都加 B，B 的合计上界 0.035 会盖过 A 层的小间隔，
        # 造成方向翻转 —— 实测 A4 会从 99.4% 掉到 97.8%（低于「变向」红线）。
        base_scores = [item["score_a"] for item in components]
        groups_a: dict[float, list[int]] = {}
        for index, value in enumerate(base_scores):
            groups_a.setdefault(value, []).append(index)
        for value, indices in groups_a.items():
            if len(indices) < 2:
                continue
            for index in indices:
                base_scores[index] = value + components[index]["score_b"]

        # L2：仅在 base 完全相等的分组内追加 **K 盐融合**偏移（步长 << A/B 层任何间隔）。
        # 次序只依赖合约身份 + 交易日，不依赖行序 / strike / DTE 的升降方向 → 组内判别力期望 0.5。
        groups: dict[float, list[int]] = {}
        for index, value in enumerate(base_scores):
            groups.setdefault(value, []).append(index)
        final_scores = list(base_scores)
        row_values = [row for _, row in rows]
        for value, indices in groups.items():
            if len(indices) < 2:
                continue
            ordered = _fused_tie_order(row_values, indices)
            for rank, index in enumerate(ordered):
                final_scores[index] = value + rank * _RULE_TIE_STEP

        probs = np.array(
            [max(0.05, min(0.95, score)) for score in final_scores], dtype=float
        )
        return np.column_stack([1.0 - probs, probs])

    def scorer_version(self) -> str:
        return RULE_SCORER_VERSION


def load_ml_model(model_path: str | None = None):
    """
    加载最新的ML模型；无外部 sklearn 依赖时自动回退为纯内置打分器
    """
    default_cols = [
        'days_to_expiry', 'is_call', 'strike_price', 'expiry_bucket',
        'symbol_avg_pnl', 'symbol_std_pnl', 'symbol_max_pnl', 'symbol_min_pnl',
        'symbol_win_rate', 'symbol_3d_avg_pnl', 'symbol_7d_avg_pnl',
        'symbol_trade_count', 'symbol_pnl_trend', 'day_of_week',
        'hour_of_day', 'is_market_close', 'vix_level', 'spy_return_1d',
        'spy_return_5d', 'market_regime'
    ]
    if model_path is None:
        model_path = Path('data/ml_models/volatility_scorer_v3.pkl')
    else:
        model_path = Path(model_path)
    
    if model_path.exists():
        try:
            with open(model_path, 'rb') as f:
                model_data = pickle.load(f)
            model = model_data['model']
            feature_cols = list(model_data.get('feature_cols') or [])
            if not feature_cols:
                raise ValueError("模型文件缺少 feature_cols，无法做特征签名校验")
            unsupported = [col for col in feature_cols if col not in INFERENCE_FEATURE_KEYS]
            if unsupported:
                # 静默降级防线：加载一个推理侧造不全特征的模型，等于让每次预测
                # 都在 `[feature_cols]` 上 KeyError，然后被吞成 0.0（全标的失效）。
                logger.error(
                    "❌ 拒绝加载 ML 模型 %s：特征 %s 无法在推理侧构造；"
                    "继续加载会导致预测 KeyError→0.0（静默降级）。已回退内置规则模型。",
                    model_path, unsupported,
                )
                logger.warning(
                    "%s 已被启用：model=%s reason=特征签名不符 unsupported=%s",
                    RULE_SCORER_VERSION, model_path, unsupported,
                )
                return BuiltinRuleModel(), default_cols
            if hasattr(model, 'n_jobs'):
                model.n_jobs = 1
            logger.info(f"✅ 加载ML模型: {model_path} (强制 n_jobs=1 单线程安全模式)")
            return model, feature_cols
        except Exception as exc:  # noqa: BLE001
            logger.info("ℹ️  启用内置轻量纯 Python 标的评分引擎 (自闭环无外部依赖)")
            logger.warning(
                "%s 已被启用：model=%s reason=加载异常 %s",
                RULE_SCORER_VERSION, model_path, exc,
            )
            return BuiltinRuleModel(), default_cols

    logger.warning(
        "%s 已被启用：model=%s reason=模型文件不存在",
        RULE_SCORER_VERSION, model_path,
    )
    return BuiltinRuleModel(), default_cols


def parse_option_symbol(symbol: str) -> dict | None:
    """
    解析期权代码
    
    格式: US.AAPL240119C175000 或 AAPL240119C175000
    
    Returns:
        {
            'underlying': 'AAPL',
            'expiry_date': datetime,
            'option_type': 'C' or 'P',
            'strike': 175.0
        }
    """
    # 移除US.前缀
    clean_symbol = symbol.replace('US.', '')
    
    # 正则匹配: TICKER + YYMMDD + C/P + STRIKE*1000
    pattern = r'^([A-Z]+)(\d{6})([CP])(\d+)$'
    match = re.match(pattern, clean_symbol)
    
    if not match:
        return None
    
    ticker, date_str, opt_type, strike_str = match.groups()
    
    try:
        # 解析日期 YYMMDD
        expiry_date = datetime.strptime(date_str, '%y%m%d')
        # 解析行权价 (除以1000)
        strike = float(strike_str) / 1000.0
        
        return {
            'underlying': ticker,
            'expiry_date': expiry_date,
            'option_type': opt_type,
            'strike': strike
        }
    except Exception as e:
        logger.warning(f"解析期权代码失败 {symbol}: {e}")
        return None


def query_symbol_history(symbol: str, store: TradingStore, lookback_days: int = 90) -> pd.DataFrame:
    """
    查询标的的历史盈亏记录
    
    Args:
        symbol: 期权代码
        store: TradingStore实例
        lookback_days: 回看天数
    
    Returns:
        DataFrame with columns: [created_at, pnl]
    """
    try:
        # 查询该标的的历史订单
        cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
        
        query = """
        SELECT 
            created_at,
            side,
            filled_quantity,
            average_fill_price
        FROM order_intents
        WHERE symbol = ?
          AND status = 'FILLED'
          AND strategy_id = 'volatility_long_gamma'
          AND DATE(created_at) >= ?
        ORDER BY created_at ASC
        """
        
        with store.connection() as connection:
            rows = connection.execute(query, (symbol, cutoff_date)).fetchall()
            rows = [dict(row) for row in rows]
        
        if not rows:
            return pd.DataFrame(columns=['created_at', 'pnl'])
        
        # 计算每笔交易的盈亏
        df = pd.DataFrame(rows, columns=['created_at', 'side', 'filled_quantity', 'average_fill_price'])
        df['created_at'] = pd.to_datetime(df['created_at'])
        
        # 按日期分组计算每日净盈亏
        daily_pnl = []
        for date, group in df.groupby(df['created_at'].dt.date):
            buy_amount = group[group['side'] == 'BUY']['filled_quantity'] * \
                        group[group['side'] == 'BUY']['average_fill_price']
            sell_amount = group[group['side'] == 'SELL']['filled_quantity'] * \
                         group[group['side'] == 'SELL']['average_fill_price']
            
            net_pnl = sell_amount.sum() - buy_amount.sum()
            daily_pnl.append({
                'created_at': pd.Timestamp(date),
                'pnl': net_pnl
            })
        
        return pd.DataFrame(daily_pnl)
        
    except Exception as e:
        logger.warning(f"查询 {symbol} 历史失败: {e}")
        return pd.DataFrame(columns=['created_at', 'pnl'])


def prepare_symbol_features(
    symbol: str, 
    trade_time: datetime,
    store: TradingStore,
    broker = None,  # 可选的broker实例，用于获取市场数据
    market_context: dict | None = None,
) -> dict:
    """
    准备ML特征
    
    Args:
        symbol: 期权代码
        trade_time: 交易时间
        store: TradingStore实例
        broker: 可选的broker实例，用于获取VIX/SPY数据
        market_context: v3.2 凸性特征所需的真实行情，键为
            ``spot`` / ``option_gamma`` / ``option_theta``。
            缺哪项就不产哪个键（绝不编造 0），由 predict_symbol_score 显式报错。
    
    Returns:
        特征字典
    """
    features = {}
    
    # 1. 期权特征
    opt_info = parse_option_symbol(symbol)
    if opt_info:
        # 移除时区信息进行日期计算
        trade_dt_naive = trade_time.replace(tzinfo=None) if trade_time.tzinfo else trade_time
        expiry_dt = opt_info['expiry_date']
        
        features['days_to_expiry'] = (expiry_dt - trade_dt_naive).days
        features['is_call'] = 1 if opt_info['option_type'] == 'C' else 0
        features['strike_price'] = opt_info['strike']
        
        # 到期分桶
        if features['days_to_expiry'] <= 7:
            features['expiry_bucket'] = 1
        elif features['days_to_expiry'] <= 14:
            features['expiry_bucket'] = 2
        elif features['days_to_expiry'] <= 30:
            features['expiry_bucket'] = 3
        else:
            features['expiry_bucket'] = 4
    else:
        # 默认值
        features['days_to_expiry'] = 30
        features['is_call'] = 1
        features['strike_price'] = 0
        features['expiry_bucket'] = 3
    
    # 2. 历史统计（从数据库查询）
    history = query_symbol_history(symbol, store)
    
    if len(history) >= 3:
        hist_pnl = history['pnl']
        features['symbol_avg_pnl'] = float(hist_pnl.mean())
        features['symbol_std_pnl'] = float(hist_pnl.std())
        features['symbol_max_pnl'] = float(hist_pnl.max())
        features['symbol_min_pnl'] = float(hist_pnl.min())
        features['symbol_win_rate'] = float((hist_pnl > 0).mean())
        
        # 最近3天和7天
        recent_3d = hist_pnl.tail(3)
        features['symbol_3d_avg_pnl'] = float(recent_3d.mean()) if len(recent_3d) > 0 else 0
        
        recent_7d = hist_pnl.tail(7)
        features['symbol_7d_avg_pnl'] = float(recent_7d.mean()) if len(recent_7d) > 0 else 0
        
        features['symbol_trade_count'] = len(history)
        
        # 趋势
        if len(history) >= 6:
            recent = hist_pnl.tail(3).mean()
            earlier = hist_pnl.iloc[:-3].mean()
            features['symbol_pnl_trend'] = float(recent - earlier)
        else:
            features['symbol_pnl_trend'] = 0
    else:
        # 默认值
        features['symbol_avg_pnl'] = 0
        features['symbol_std_pnl'] = 0
        features['symbol_max_pnl'] = 0
        features['symbol_min_pnl'] = 0
        features['symbol_win_rate'] = 0.5
        features['symbol_3d_avg_pnl'] = 0
        features['symbol_7d_avg_pnl'] = 0
        features['symbol_trade_count'] = 0
        features['symbol_pnl_trend'] = 0
    
    # 3. 时间特征
    features['day_of_week'] = trade_time.weekday()
    features['hour_of_day'] = trade_time.hour
    features['is_market_close'] = 1 if trade_time.hour >= 15 else 0
    # v3.2 candidate 模型（18 特征）含 month，推理侧此前没有 → KeyError→0.0
    features['month'] = trade_time.month
    features['quarter'] = (trade_time.month - 1) // 3 + 1
    
    # 4. 市场环境特征（如果提供了broker）
    if broker is not None:
        try:
            from .market_env import get_market_environment
            market_data = get_market_environment(broker)
            features['vix_level'] = market_data['vix_level']
            features['spy_return_1d'] = market_data['spy_return_1d']
            features['spy_return_5d'] = market_data['spy_return_5d']
            features['market_regime'] = market_data['market_regime']
        except Exception as e:
            logger.warning(f"获取市场环境数据失败: {e}，使用默认值")
            features['vix_level'] = 20.0
            features['spy_return_1d'] = 0.0
            features['spy_return_5d'] = 0.0
            features['market_regime'] = 1
    else:
        # 占位符
        features['vix_level'] = 20.0
        features['spy_return_1d'] = 0.0
        features['spy_return_5d'] = 0.0
        features['market_regime'] = 1

    # 5. v3.2 凸性/无量纲特征：只认真实行情。
    # 缺 spot / 希腊字母时**不写这两个键**，由 predict_symbol_score 报 MissingFeatureError；
    # 编一个 0 出去比报错危险得多（模型会把「没数据」学成「低赔率」）。

    # moneyness + gamma_to_theta **统一口径**（Chief 2026-10-25 裁定）：两侧都用
    # 「最近一条 option_history 快照」的 underlying_price 与 gamma/theta，
    # **不再用** `owner["last_price"]` 或实时链上下文里的希腊字母——后者是扫描时实时值，
    # 与导出侧成交前 ≤45 分钟的快照不同源，等于把 train/serve skew 养在系统里。
    snapshot = latest_underlying_snapshot(store, symbol, trade_time)
    spot = snapshot.get("spot")
    lag_minutes = snapshot.get("lag_minutes")
    if spot is not None:
        moneyness = moneyness_ratio(spot, features.get('strike_price'))
        if moneyness is not None:
            features['moneyness'] = moneyness
            features['moneyness_lag_minutes'] = lag_minutes
        else:
            features['moneyness_missing_reason'] = "invalid_spot_or_strike"
    else:
        features['moneyness_missing_reason'] = "snapshot_missing_or_stale"

    gamma_to_theta = gamma_to_theta_ratio(snapshot.get("gamma"), snapshot.get("theta"))
    if gamma_to_theta is not None:
        features['gamma_to_theta'] = gamma_to_theta
    else:
        # 实时链上下文（context）不再作为兜底来源：双源就是 skew
        features['gamma_to_theta_missing_reason'] = "snapshot_missing_or_invalid_greeks"

    return features


def predict_symbol_score(
    symbol: str,
    model,
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker=None,
    market_context: dict | None = None,
) -> float:
    """
    预测标的评分

    Args:
        symbol: 期权代码
        model: 训练好的模型
        feature_cols: 特征列表
        store: TradingStore实例
        trade_time: 交易时间，默认当前时间
        broker: 可选的broker实例，用于获取市场数据
        market_context: v3.2 凸性特征所需的真实行情（spot / option_gamma / option_theta）

    Returns:
        0-1之间的概率，越高越可能盈利

    Raises:
        MissingFeatureError: 模型所需特征在推理侧拿不到。以前这里
            ``except Exception: return 0.0`` 把「没数据」吞成「全标的 0.0」，
            那正是静默降级；现在必须显式失败，由调用方回退到规则排序。
    """
    if trade_time is None:
        trade_time = datetime.now()

    features = prepare_symbol_features(
        symbol, trade_time, store, broker, market_context
    )
    missing = [col for col in feature_cols if col not in features]
    if missing:
        raise MissingFeatureError(
            f"{symbol} 缺少模型所需特征 {missing}；推理侧无法构造，拒绝用 0.0 兜底"
        )

    feature_df = pd.DataFrame([features])[feature_cols]
    proba = model.predict_proba(feature_df)[0, 1]
    return float(proba)


def batch_predict_scores(
    symbols: list[str],
    model,
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker=None,
    market_context: dict | None = None,
) -> dict[str, float | None]:
    """
    批量预测标的评分

    ``market_context`` 是**共用**上下文（如当日 spot）；逐合约的希腊字母需要逐个
    调用 :func:`predict_symbol_score` / ``MLSymbolScorer.score_symbol``。

    Returns:
        {symbol: score} 字典；拿不到必需特征的标的给 ``None``（NA），
        不再用 0.0 冒充「最低分」—— 0.0 会被下游当成真实评估结果。
    """
    if trade_time is None:
        trade_time = datetime.now()

    scores: dict[str, float | None] = {}
    for symbol in symbols:
        try:
            scores[symbol] = predict_symbol_score(
                symbol, model, feature_cols, store, trade_time, broker, market_context
            )
        except MissingFeatureError as exc:
            logger.error(f"批量预测 {symbol} 缺必需特征，评分置 NA（不再静默 0.0）：{exc}")
            scores[symbol] = None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"批量预测 {symbol} 失败，评分置 NA：{exc}")
            scores[symbol] = None

    return scores
