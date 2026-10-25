"""中立 tie-break（兜底打分器）验收守卫 B1~B4。

背景（Analyst 2026-10-25 重算）：`BuiltinRuleModel`（模型加载失败时的生产兜底打分器）
旧的 L2 破并列按 `(days_to_expiry, expiry_bucket, -is_call, strike_price, 行号)` 升序赋偏移，
**并列组内的配对 AUC = 0.4121（分位 0.1%）——比随机还差**。修法：并列组内改用
**对称哈希（blake2b）+ 合约身份** 的次序，可复现但无方向。

口径（Chief 2026-10-25 裁定）：旧并列组内（size≥2 且组内 `window_net_pnl` 有差异）的配对
AUC = 赢家（pnl 高者）排在输家之前的比例；随机期望 0.5；分位 = 实测 AUC 在 N=1000 次
**组内随机排列** 的 AUC 分布中的 midrank 位置。

为什么守卫按「多种盐的 pooled 分位」而不是「出厂单个盐的分位」：
- 哈希次序是对「合约身份空间」的一次固定随机排列；L2 作用组口径实测 12 个盐 pooled AUC
  0.4411~0.5449，其中 AUC=0.4937 的盐 pooled 分位 41.9%，但该盐的「每日分位中位数」却是
  0.0%（当日 AUC 离散化伪影）。用单个盐的分位当验收阈值 = 用抽签结果当门槛。
- 所以机制层判据用 **S 个预注册盐（与数据无关的固定常量）的 pooled 分位分布**：
  中位落 [40,60]，且不能系统性偏低。出厂盐本身是其中一个抽签结果，不做数据挑选
  （按历史数据挑盐 = 拿结果反选排列，等于把 look-ahead 塞进 tie-break）。
- Chief 2026-10-25 裁定 A：验收只看机制；出厂盐 realised 分位只记录、标注
  「单次抽样、无统计效力」；盐池与出厂盐指纹由 B5 冻结。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from futu_quant.ml_scorer import (
    _RULE_TIE_FUSION_K,
    _RULE_TIE_FUSION_SALTS,
    _RULE_TIE_PROBE_SALTS,
    _RULE_TIE_SALT,
    _RULE_TIE_STEP,
    _TIE_IDENTITY_KEYS,
    _continuous_components,
    _fused_tie_order,
    _neutral_tie_rank,
    _tie_identity_text,
    BuiltinRuleModel,
    rule_tiebreak_realised_quality,
)

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"

FEATURE_COLS = [
    "symbol_win_rate", "symbol_avg_pnl", "symbol_pnl_trend", "symbol_std_pnl",
    "symbol_max_pnl", "symbol_min_pnl", "symbol_3d_avg_pnl", "symbol_7d_avg_pnl",
    "symbol_trade_count", "days_to_expiry", "expiry_bucket", "is_call", "strike_price",
]
PNL_COL = "window_net_pnl"
N_PERM = 1000
# 预注册盐池：写死在 `ml_scorer._RULE_TIE_PROBE_SALTS`，与数据无关，**不得事后调整**。
# 验收用它们的分布检验机制无方向，而不是用出厂盐的单次落地值（Chief 2026-10-25 裁定 A）。
PROBE_SALTS = _RULE_TIE_PROBE_SALTS
# 冻结指纹：改出厂盐 / 盐池 / 融合参数 = 必须显式改本守卫并说明理由（防止按数据挑盐）。
SHIPPED_SALT_SHA256 = "3032f2105f489576c5316f43ae3462b2701b1116ec1fa78c1f0b45f349787b39"
PROBE_SALTS_SHA256 = "0e9e6b74a7cb2cf49623a42deff5719fd88130d1f44b84039463a319551b9120"
FUSION_SALTS_SHA256 = "ab2f5d99605c89cc71939d68783471de921033e9cbf933887c4e17115498115e"


# --------------------------------------------------------------------- 数据/度量
def _load_frame() -> pd.DataFrame:
    if not CSV_PATH.exists():
        pytest.skip(f"缺少训练样本 {CSV_PATH}（历史 CSV 不在本 worktree）")
    frame = pd.read_csv(CSV_PATH)
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame.dropna(subset=["trade_date"]).reset_index(drop=True)


def _old_discrete_score(row) -> float:
    """旧实现（离散版），用来划分「旧实现能分辨 / 旧实现并列」的配对。"""

    def _num(key: str, default: float) -> float:
        value = row.get(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return default if number != number else number

    score = 0.50 + (_num("symbol_win_rate", 0.50) - 0.50) * 0.35
    trend = _num("symbol_pnl_trend", 0.0)
    score += 0.05 if trend > 0 else (-0.05 if trend < 0 else 0.0)
    if _num("symbol_avg_pnl", 0.0) > 0:
        score += 0.05
    return max(0.05, min(0.95, score))


def _base_scores_before_l2(frame: pd.DataFrame) -> list[float]:
    """复刻 Layer A + Layer B（L2 之前）的分数，用来验证 L2 不改变任何可分辨配对。"""
    rows = list(frame.iterrows())
    components = [_continuous_components(row) for _, row in rows]
    base = [item["score_a"] for item in components]
    groups: dict[float, list[int]] = {}
    for index, value in enumerate(base):
        groups.setdefault(value, []).append(index)
    for value, indices in groups.items():
        if len(indices) < 2:
            continue
        for index in indices:
            base[index] = value + components[index]["score_b"]
    return base


def _tie_groups(frame: pd.DataFrame) -> list[list[int]]:
    """L2 实际作用组：base（A+B）分数完全相等的组。"""
    base = _base_scores_before_l2(frame)
    groups: dict[float, list[int]] = {}
    for index, value in enumerate(base):
        groups.setdefault(value, []).append(index)
    return [indices for indices in groups.values() if len(indices) >= 2]


def _old_discrete_tie_groups(frame: pd.DataFrame) -> list[list[int]]:
    """Chief/Analyst 口径组：**旧离散热分值**并列的组（tie-break 的评估总体）。"""
    old = frame.apply(_old_discrete_score, axis=1).to_numpy()
    groups: dict[float, list[int]] = {}
    for index, value in enumerate(old):
        groups.setdefault(value, []).append(index)
    return [indices for indices in groups.values() if len(indices) >= 2]


def _pair_auc(pnl: np.ndarray, positions: dict[int, int], group: list[int]) -> tuple[int, int]:
    ok = total = 0
    for first in range(len(group)):
        for second in range(first + 1, len(group)):
            left, right = group[first], group[second]
            if abs(pnl[left] - pnl[right]) < 1e-12:
                continue
            winner, loser = (left, right) if pnl[left] > pnl[right] else (right, left)
            total += 1
            ok += int(positions[winner] < positions[loser])
    return ok, total


def _day_contexts(
    frame: pd.DataFrame, groups_fn=_old_discrete_tie_groups
) -> list[tuple[pd.DataFrame, list[list[int]]]]:
    contexts = []
    for _, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        groups = groups_fn(day)
        if groups:
            contexts.append((day, groups))
    return contexts


def _neutral_order(salt: bytes):
    """按指定盐给出组内中立次序（与生产实现同一套「日期 + 合约身份」+ blake2b）。"""

    def order(row, day_id: str = "") -> int:
        identity = "|".join(
            [day_id or _tie_identity_text(row, "trade_date")]
            + [_tie_identity_text(row, key) for key in _TIE_IDENTITY_KEYS]
        )
        digest = hashlib.blake2b(salt + identity.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big")

    return order


def _semantic_order(row, day_id: str = "") -> tuple:
    """变异：旧的方向性排序键（必须被守卫抓到转红）。"""

    def _num(key: str, default: float) -> float:
        value = row.get(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return default if number != number else number

    return (
        _num("days_to_expiry", 9999.0),
        _num("expiry_bucket", 9.0),
        -_num("is_call", 0.0),
        _num("strike_price", 1e12),
    )


def _pooled_auc(contexts, key_fn) -> tuple[float, int]:
    ok = total = 0
    for day, groups in contexts:
        pnl = day[PNL_COL].to_numpy(dtype=float)
        rows = list(day.iterrows())
        day_id = str(day["trade_date"].iloc[0]) if "trade_date" in day.columns else ""
        for group in groups:
            ranks = {
                index: rank
                for rank, index in enumerate(
                    sorted(group, key=lambda i, _r=rows: key_fn(_r[i][1], day_id))
                )
            }
            group_ok, group_total = _pair_auc(pnl, ranks, group)
            ok += group_ok
            total += group_total
    return (ok / total if total else float("nan")), total


def _permutation_null(contexts, n_perm: int = N_PERM, seed: int = 20261025) -> np.ndarray:
    """组内随机排列的 AUC 零分布（一次生成，所有盐复用做分位）。"""
    rng = np.random.default_rng(seed)
    pnl_by_day = [day[PNL_COL].to_numpy(dtype=float) for day, _ in contexts]
    max_group = max(len(group) for _, groups in contexts for group in groups)
    uniform = rng.random((n_perm, max_group))
    values = np.empty(n_perm)
    for k in range(n_perm):
        ok = total = 0
        for (day, groups), pnl in zip(contexts, pnl_by_day):
            for group in groups:
                perm = np.argsort(uniform[k, : len(group)])
                ranks = {group[int(perm[pos])]: pos for pos in range(len(group))}
                group_ok, group_total = _pair_auc(pnl, ranks, group)
                ok += group_ok
                total += group_total
        values[k] = ok / total if total else 0.5
    return values


def _percentile(value: float, null: np.ndarray) -> float:
    less = int(np.sum(null < value - 1e-15))
    equal = int(np.sum(np.abs(null - value) <= 1e-15))
    return (less + 0.5 * equal) / len(null) * 100.0


# ------------------------------------------------------------------ B1 逐值不变
def test_B1_l2_tiebreak_never_flips_old_distinguishable_pairs():
    """A4 的硬版本：旧实现能分辨的配对，L2 之后的先后必须与 L2 之前**逐值相同**。"""
    frame = _load_frame()
    model = BuiltinRuleModel()
    compared = 0
    for _, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        base = _base_scores_before_l2(day)
        new = model.predict_proba(day)[:, 1]
        old = day.apply(_old_discrete_score, axis=1).to_numpy()
        for i in range(len(day)):
            for j in range(i + 1, len(day)):
                if abs(old[i] - old[j]) < 1e-12:
                    continue
                compared += 1
                assert np.sign(new[i] - new[j]) == np.sign(base[i] - base[j]), (
                    f"L2 改变了旧可分辨配对 ({i},{j}) 的先后："
                    f"base={base[i]:.9f}/{base[j]:.9f} new={new[i]:.9f}/{new[j]:.9f}"
                )
    assert compared > 1000, f"可检验配对仅 {compared} 个"


def test_B1b_scores_stable_across_calls_and_row_permutation():
    """同一输入重复调用、以及打乱行序后，逐合约分数必须完全一致（次序不依赖行号）。"""
    frame = _load_frame().head(300).reset_index(drop=True)
    model = BuiltinRuleModel()
    first = model.predict_proba(frame)[:, 1]
    second = model.predict_proba(frame)[:, 1]
    np.testing.assert_array_equal(first, second)

    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    third = model.predict_proba(shuffled)[:, 1]
    key = [
        "symbol", "underlying_symbol", "strike_price", "is_call",
        "days_to_expiry", "expiry_bucket",
    ]
    left = frame.assign(_score=first).groupby(key)["_score"].apply(
        lambda values: np.sort(values.to_numpy())
    )
    right = shuffled.assign(_score=third).groupby(key)["_score"].apply(
        lambda values: np.sort(values.to_numpy())
    )
    assert len(left) == len(right)
    for identity, values in left.items():
        np.testing.assert_allclose(values, right.loc[identity], atol=1e-15)


# ------------------------------------------------------- B2 机制无方向（多种盐）
def test_B2_hash_mechanism_is_direction_neutral_across_salts():
    """Chief 口径（旧离散分并列组）内、**纯 tie-break 次序**的多盐 pooled 分位：
    中位落 [40,60]，且不得系统性偏低。

    总体：旧离散热分值并列组 58 组 / 535 行 / 36 天（Analyst 的 53 组是他额外
    要求「组内 pnl 有差异」后的分母，口径一致）。零分布 sd≈0.020。
    判据对象是**机制**（多个与数据无关的盐），不是出厂那一次抽签——见模块 docstring。
    """
    contexts = _day_contexts(_load_frame())
    null = _permutation_null(contexts)
    percentiles = []
    for salt in PROBE_SALTS:
        auc, pairs = _pooled_auc(contexts, _neutral_order(salt))
        assert pairs > 3000
        percentiles.append(_percentile(auc, null))
    percentiles = np.array(percentiles)
    median = float(np.median(percentiles))
    low_share = float(np.mean(percentiles < 25.0))
    assert 40.0 <= median <= 60.0, (
        f"{len(PROBE_SALTS)} 个盐的 pooled 分位中位 = {median:.1f}%，不在 [40,60] → "
        f"tie-break 机制带方向（实际分位: {np.round(percentiles, 1).tolist()}）"
    )
    assert low_share <= 0.30, (
        f"{low_share:.0%} 的盐分位低于随机 25% 分位 → 机制系统性偏低"
        f"（实际分位: {np.round(percentiles, 1).tolist()}）"
    )


def test_B2b_mutation_semantic_key_must_be_red():
    """变异：把次序换回旧的方向性排序键 → 分位必须低于随机 25% 分位（守卫红）。

    实测旧键 AUC ≈ 0.4748，分位 ≈ 10.8%；修复前 Analyst 报 0.1%（口径差异见
    `data/qa/rule_neutral_tiebreak_report.md`），方向一致：都低于 25%。
    """
    contexts = _day_contexts(_load_frame())
    null = _permutation_null(contexts)
    auc, pairs = _pooled_auc(contexts, _semantic_order)
    assert pairs > 3000
    percentile = _percentile(auc, null)
    assert percentile < 25.0, (
        f"旧方向性键分位 = {percentile:.1f}%（AUC={auc:.4f}）没有低于随机 25% 分位 → "
        "守卫抓不到方向性 tie-break，形同虚设"
    )


def test_B2c_l2_scope_groups_are_neutral_too():
    """补充口径：L2 **实际作用组**（base 完全相等，77 组 / 421 行）内多盐分位也居中。"""
    contexts = _day_contexts(_load_frame(), groups_fn=_tie_groups)
    null = _permutation_null(contexts)
    percentiles = np.array([
        _percentile(_pooled_auc(contexts, _neutral_order(salt))[0], null)
        for salt in PROBE_SALTS
    ])
    median = float(np.median(percentiles))
    assert 40.0 <= median <= 60.0, (
        f"L2 作用组内多盐分位中位 = {median:.1f}% 不在 [40,60]："
        f"{np.round(percentiles, 1).tolist()}"
    )


# ------------------------------------------------- B3 与语义字段无秩相关
def _pooled_within_group_spearman(pairs: list[tuple[np.ndarray, np.ndarray]]) -> float:
    """组内秩相关（逐组中心化后拼接求 Pearson；等价于按组加权的组内 Spearman）。

    不能对 size=2 的组做 pooled 原始秩相关（每组必为 ±1，会造出假相关，实测 pooled
    ρ=+0.25），也不能只对每组算 ρ 再平均（size=3 组方差大）。中心化拼接的估计
    随机期望 0，且用满全部行数。
    """
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for first, second in pairs:
        xs.append(pd.Series(first).rank().to_numpy() - (len(first) + 1) / 2.0)
        ys.append(pd.Series(second).rank().to_numpy() - (len(second) + 1) / 2.0)
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def test_B3_no_rank_correlation_with_semantic_fields():
    """tie-break 名次与行序 / strike / DTE 的**组内**秩相关 ≈ 0。

    同 B2 的理由：单个盐的 realized ρ 也是一次抽签（本数据集 n≈421 行，零分布 sd≈0.05），
    所以判据取 S 个预置盐的**平均 ρ** 与上界，变异必须红。实测出厂盐 ρ(row/strike/dte)
    = -0.052 / +0.108 / -0.027 —— 与随机抽签一致（单盐零分布 sd≈0.05），不是系统性方向。
    """
    frame = _load_frame()
    day_groups: list[tuple[str, list, list[list[int]]]] = []
    groups_total = 0
    for day_id, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        rows = list(day.iterrows())
        grids = _tie_groups(day)
        day_groups.append((str(day_id), rows, grids))
        groups_total += len(grids)
    assert groups_total > 50, f"可检验并列组仅 {groups_total} 个"

    def collect(key_fn) -> dict[str, list[tuple[np.ndarray, np.ndarray]]]:
        samples: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {
            "row": [], "strike": [], "dte": [],
        }
        for day_id, rows, grids in day_groups:
            for group in grids:
                ranks = sorted(group, key=lambda i: key_fn(rows[i][1], day_id))
                position = {index: pos for pos, index in enumerate(ranks)}
                tie_rank = np.array([position[i] for i in group], dtype=float)
                for name, extract in (
                    ("row", lambda i: i),
                    ("strike", lambda i: float(rows[i][1].get("strike_price", 0.0))),
                    ("dte", lambda i: float(rows[i][1].get("days_to_expiry", 0.0))),
                ):
                    series = np.array([extract(i) for i in group], dtype=float)
                    if len(np.unique(series)) < 2:
                        continue
                    samples[name].append((tie_rank, series))
        return samples

    for name in ("row", "strike", "dte"):
        neutral_rhos = [
            _pooled_within_group_spearman(collect(_neutral_order(salt))[name])
            for salt in PROBE_SALTS
        ]
        mean_rho = float(np.mean(neutral_rhos))
        median_abs = float(np.median(np.abs(neutral_rhos)))
        assert abs(mean_rho) < 0.10, (
            f"tie-break 与 {name} 的多盐平均组内秩相关 = {mean_rho:+.3f}，|ρ| 超 0.1 → 次序带语义"
        )
        assert median_abs < 0.15, (
            f"{name}：{len(PROBE_SALTS)} 个盐的 |ρ| 中位 = {median_abs:.3f} ≥ 0.15 → 次序带语义"
        )


def test_B3b_mutation_semantic_key_shows_rank_correlation():
    """变异：旧方向性键（按 strike 升序赋偏移）与 strike 的秩相关必须接近 +1 → B3 判据红。"""
    frame = _load_frame()
    samples: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {
        "row": [], "strike": [], "dte": [],
    }
    for day_id, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        rows = list(day.iterrows())
        for group in _tie_groups(day):
            ranks = sorted(group, key=lambda i: _semantic_order(rows[i][1]))
            position = {index: pos for pos, index in enumerate(ranks)}
            tie_rank = np.array([position[i] for i in group], dtype=float)
            for name, extract in (
                ("row", lambda i: i),
                ("strike", lambda i: float(rows[i][1].get("strike_price", 0.0))),
                ("dte", lambda i: float(rows[i][1].get("days_to_expiry", 0.0))),
            ):
                series = np.array([extract(i) for i in group], dtype=float)
                if len(np.unique(series)) < 2:
                    continue
                samples[name].append((tie_rank, series))
    rho_strike = _pooled_within_group_spearman(samples["strike"])
    assert abs(rho_strike) > 0.15, (
        f"旧方向性键与 strike 的秩相关只有 {rho_strike:+.3f}，B3 抓不到方向性 tie-break"
    )


# ------------------------------------------------------------------ B4 冻结契约
def test_B4_identity_and_step_contract():
    """身份字段集合与步长冻结：改身份字段 = 换口径，必须显式改设计与守卫。"""
    assert _TIE_IDENTITY_KEYS == (
        "underlying_symbol", "strike_price", "is_call", "days_to_expiry", "expiry_bucket",
    )
    assert _RULE_TIE_STEP == 1e-9
    row = pd.Series({
        "underlying_symbol": "US.QQQ", "strike_price": 400.0, "is_call": 1,
        "days_to_expiry": 3, "expiry_bucket": 1,
    })
    assert _neutral_tie_rank(row) == _neutral_tie_rank(row.copy())
    other = row.copy()
    other["strike_price"] = 405.0
    assert _neutral_tie_rank(row) != _neutral_tie_rank(other)


def test_B4b_tie_rank_is_not_semantic_ordering():
    """身份字段单独变动时，哈希名次的升降不定向（方向性 key 会让 strike 单调）。"""
    strikes = [380.0, 385.0, 390.0, 395.0, 400.0, 405.0, 410.0, 415.0]
    ranks = [
        _neutral_tie_rank(pd.Series({
            "underlying_symbol": "US.SPY", "strike_price": strike, "is_call": 1,
            "days_to_expiry": 2, "expiry_bucket": 1,
        }))
        for strike in strikes
    ]
    diffs = np.diff(ranks)
    assert np.any(diffs > 0) and np.any(diffs < 0), (
        f"strike 单调递增时名次也单调 → 哈希退化为按 strike 排序：{ranks}"
    )


# ------------------------------------------------- B5 盐池预注册 / 冻结指纹
def test_B5_salt_pool_is_preregistered_and_frozen():
    """盐池与出厂盐冻结：任何改动都必须显式改本守卫并说明理由。

    Chief 2026-10-25 裁定 A：**禁止按历史样本表现挑选/调整盐**——挑盐 = 用结果反选
    随机排列，等于把 look-ahead 塞进 rule 对照组。验收判「实现无偏」（多盐分布），
    出厂盐的 realised 分位只记录、不进门槛：它是**单次抽样、无统计效力**
    （24 盐分位实测 1.8%~100%；出厂盐 AUC 0.4413 / 0.0% 属 2.9σ 不利抽签）。
    """
    assert len(_RULE_TIE_PROBE_SALTS) == 24
    assert hashlib.sha256(_RULE_TIE_SALT).hexdigest() == SHIPPED_SALT_SHA256, (
        "出厂 tie-break 盐变了：如果是按历史表现调盐，等于用结果反选排列（禁止）；"
        "若确属口径变更，请先改设计并说明理由，再更新本指纹"
    )
    assert (
        hashlib.sha256(b"|".join(_RULE_TIE_PROBE_SALTS)).hexdigest() == PROBE_SALTS_SHA256
    ), "预注册盐池被改动：事后调盐池 = 事后调门槛，必须显式改设计"
    assert _RULE_TIE_FUSION_K == 8, "融合 K 变了：必须显式改设计（K 也属于预注册参数）"
    assert _RULE_TIE_FUSION_SALTS == _RULE_TIE_PROBE_SALTS[:8], (
        "生产融合盐必须取自预注册池前缀，不许另起一套或按数据挑选"
    )
    assert (
        hashlib.sha256(b"|".join(_RULE_TIE_FUSION_SALTS)).hexdigest()
        == FUSION_SALTS_SHA256
    ), "融合盐池被改动：必须显式改设计并说明理由"


# ------------------------------------------------- B6 K 盐融合（Chief 裁定 ①）
def test_B6_fusion_order_is_mean_rank_of_preregistered_salts():
    """结构守卫：融合次序必须**等于** 8 个预注册盐的平均名次次序（不许退回单盐）。"""
    strikes = [380.0, 385.0, 390.0, 395.0, 400.0, 405.0, 410.0, 415.0]
    rows = [
        pd.Series({
            "underlying_symbol": "US.SPY", "strike_price": strike, "is_call": 1,
            "days_to_expiry": 2, "expiry_bucket": 1, "trade_date": "2026-08-05",
        })
        for strike in strikes
    ]
    indices = list(range(len(rows)))
    fused = _fused_tie_order(rows, indices)

    totals = {index: 0.0 for index in indices}
    baseline: dict[int, int] = {}
    for position, salt in enumerate(_RULE_TIE_FUSION_SALTS):
        ordered = sorted(indices, key=lambda i: _neutral_tie_rank(rows[i], salt))
        for rank, index in enumerate(ordered):
            totals[index] += rank
            if position == 0:
                baseline[index] = rank
    expected = sorted(
        indices, key=lambda i: (totals[i], baseline[i], _neutral_tie_rank(rows[i]))
    )
    assert fused == expected, "融合次序不等于预注册 8 盐的平均名次 → 实现被改（如退回单盐）"
    single = sorted(indices, key=lambda i: _neutral_tie_rank(rows[i]))
    assert fused != single, "融合次序与单盐完全一致 → 融合没有生效，变异必须红"


def test_B6b_mutation_single_salt_must_be_red(monkeypatch):
    """变异：把融合退回单盐（K=1）→ B6 的结构判据必须不成立（守卫会红）。"""
    import futu_quant.ml_scorer as ms

    strikes = [380.0, 385.0, 390.0, 395.0, 400.0, 405.0, 410.0, 415.0]
    rows = [
        pd.Series({
            "underlying_symbol": "US.SPY", "strike_price": strike, "is_call": 1,
            "days_to_expiry": 2, "expiry_bucket": 1, "trade_date": "2026-08-05",
        })
        for strike in strikes
    ]
    indices = list(range(len(rows)))
    fused = _fused_tie_order(rows, indices)
    monkeypatch.setattr(ms, "_RULE_TIE_FUSION_SALTS", (_RULE_TIE_PROBE_SALTS[0],))
    mutated = _fused_tie_order(rows, indices)
    assert mutated != fused, "退回单盐后次序没变化 → 守卫抓不到单盐回退"
    assert ms._RULE_TIE_FUSION_K == 8, "K 常量未随实现回退 → 守卫必须依赖 K 冻结"


def test_B6c_fusion_reduces_realised_dispersion_vs_single_salt():
    """监控口径：8 盐融合把 realised 分位的**抽样分散度**收窄，且中位居中。

    注意：融合**不能**让单份数据的 realised 分位收敛到中位——realised 分位是
    「一个排序的一次落地值」，LLN 不适用（实测 K=4/8/16/24 的 all 值 42.9/70.4/18.0/84.1）。
    可测的改进只有分散度：单盐 realised 分位 sd≈32（24 盐），8 盐融合 sd≈27（24 组预注册子集）。
    因此本守卫只断言「分散度收窄 + 中位居中」，不要求单次值落 [40,60]。
    """
    frame = _load_frame()
    pool = list(_RULE_TIE_PROBE_SALTS)
    rng = np.random.default_rng(20261025)
    subsets = [tuple(rng.choice(len(pool), size=8, replace=False)) for _ in range(24)]
    import futu_quant.ml_scorer as ms

    original = ms._RULE_TIE_FUSION_SALTS
    try:
        single_percentiles = []
        fused_percentiles = []
        for index in range(24):
            ms._RULE_TIE_FUSION_SALTS = (pool[index],)
            quality = rule_tiebreak_realised_quality(frame, include_segments=False)
            assert quality["status"] == "ok", quality
            single_percentiles.append(quality["fused_percentile"])
            ms._RULE_TIE_FUSION_SALTS = tuple(pool[j] for j in subsets[index])
            quality = rule_tiebreak_realised_quality(frame, include_segments=False)
            fused_percentiles.append(quality["fused_percentile"])
    finally:
        ms._RULE_TIE_FUSION_SALTS = original

    single_sd = float(np.std(single_percentiles))
    fused_sd = float(np.std(fused_percentiles))
    assert fused_sd < single_sd, (
        f"融合后 realised 分位分散度 sd={fused_sd:.1f} 未小于单盐 sd={single_sd:.1f} → 融合无收益"
    )
    assert 40.0 <= float(np.median(single_percentiles)) <= 60.0
    assert 40.0 <= float(np.median(fused_percentiles)) <= 60.0


def test_B6e_monitoring_segments_are_contiguous_and_explicit():
    """监控分段：按交易日**顺序**切 5 段、逐段给出融合/单盐分位，不随机切。

    分段只作监控粒度（不作 [40,60] 门槛——单段 realised 分位同样是抽样值）；
    每段必须要么给出两个分位、要么显式 `unavailable` + 原因，不许静默缺段。
    """
    frame = _load_frame()
    quality = rule_tiebreak_realised_quality(frame)
    assert quality["status"] == "ok", quality
    segments = quality["segments"]
    assert len(segments) == 5, f"分段数 {len(segments)} != 5"
    assert sum(segment["days"] for segment in segments) == quality["days"], (
        "分段未覆盖全部有并列组的交易日（漏段或重复计数）"
    )
    for segment in segments:
        assert segment["days"] > 0
        assert segment["status"] in {"ok", "unavailable"}
        if segment["status"] == "ok":
            assert 0.0 <= segment["fused_percentile"] <= 100.0
            assert 0.0 <= segment["single_salt_percentile"] <= 100.0
        else:
            assert segment.get("reason") == "insufficient_sample"


def test_B6d_realised_quality_is_explicit_when_unavailable():
    """监控不可用时必须显式点名（不许静默给 0 或假绿）。"""
    frame = _load_frame()
    stripped = frame.drop(columns=["underlying_symbol"])
    quality = rule_tiebreak_realised_quality(stripped)
    assert quality["status"] == "unavailable"
    assert "underlying_symbol" in quality["missing"]
    tiny = frame.head(5)
    quality = rule_tiebreak_realised_quality(tiny)
    assert quality["status"] == "unavailable"
    assert quality["reason"] in {"insufficient_sample", "missing_columns"}
