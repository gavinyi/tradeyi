"""实盘开仓「容量预检」（2026-11-02）。

**要解决的问题**：实盘账户权益 $6,646、6% 压力损失预算只有 $398，而波动率策略
单笔权利金预算默认 $1,000 —— 于是**每一笔标准开仓都必然被风控拒绝**（2026-11-02
22:41 实盘 AMZN261113C250000 被 `portfolio_allocator.strategy_budget_allowed` 挡在
「压力损失预算将超过上限」）。风控没错，错在**选约时不看账户撑不撑得起**，让不可成交
的候选一路走到下单才被拒。

所以本模块只做一件事：**在选约阶段先筛掉「买不起 / 流动性不够」的合约**。
它**不碰**任何风控阈值（6% 预算、单笔 $1,000、港币口径一律不动），
**不碰** SELL / 退出管理（只作用于新开仓 BUY 的候选列表）。

判定六条（Product 2026-11-02 定案，默认值即上线值）：

1. 容量线：单张最小权利金 ≤ `实盘 USD 权益 × 3%`；
2. 最小权利金 = `ask × 100 × 最小下单数量`，**用 ask 不用 mid**（按最坏成交价估）；
3. 价差 `(ask − bid) / mid ≤ 10%`；
4. 深度 `bid_size ≥ 5×lot` 且 `ask_size ≥ 5×lot`；
5. 报价新鲜度 ≤ 60 秒；
6. 币种必须明确 USD。

**权益只能有一个来源**：本模块**不接受**自己取账户快照，只接受调用方传入的
`equity` —— 且调用方必须用 `portfolio_allocator.strategy_budget_usage(store, broker)`
那一份（风控闸门算 6% 预算用的就是它）。否则「选约时的权益」和「下单时的权益」
会变成两个真值，容量线又会被悄悄放宽。

**第 6 条的落点**：币种门禁已经在 `portfolio_allocator` 里（三态 fail-closed）。
这里**不再重算**一遍币种，而是依赖「拿到 equity 就说明币种已通过」：
实盘 broker 币种 MISMATCH / UNKNOWN 时 `strategy_budget_usage` 会抛
`AccountCurrencyMismatch`，调用方拿不到 equity → 容量线为 0 → 一张都筛不出来。
再写一份币种判定只会多出一个会和门禁打架的真值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .data_quality import quote_freshness
from .volatility_pause import (
    set_volatility_new_entries_paused,
    volatility_new_entries_paused_for_tenant,
)

NY = ZoneInfo("America/New_York")

#: 容量线 = 权益 × 6%（2026-11-03 Product/Reviewer 定案：3% → 6%）。
#:
#: 为什么是 6% 而不是别的数：6% 就是**系统自身的压力损失预算**
#: （`strategy_risk_budgets.max_loss_ratio`，期权权利金全额计入），
#: 超过它的单本来也会被 `portfolio_allocator.strategy_budget_allowed` 拒绝。
#: 把预检对齐到 6%，等于「预检不再比风控更严」，**没有新增或放宽任何阈值**；
#: 3% 只是当初为了保守额外加的系数。
#:
#: 但**不得硬依赖这个常量**：`loss_limit` 是租户级可配的，真值在预算里。
#: 所以 `filter_candidates(..., loss_limit=...)` 会在运行时用 `min(权益×6%, loss_limit)` 收口 ——
#: 常量只是没有预算可读时的兜底，绝不能让它比风控更宽。
CAPACITY_EQUITY_RATIO = 0.06
#: 价差上限（相对 mid）。
MAX_SPREAD_RATIO = 0.10
#: 盘口深度下限：至少 5 倍最小下单数量。
DEPTH_MIN_MULTIPLE = 5
#: 报价新鲜度上限（秒）。
MAX_QUOTE_AGE_SECONDS = 60.0
#: 到期下限（天）：避开临期 gamma 跳空（Product 2026-11-02 定案）。
MIN_DTE = 14
#: 虚值上限（距现价比例）：$199 在 $250 标的上只存在于远虚值，
#: `+7%~+9%` 已接近彩票区，硬砍（Product/Reviewer 2026-11-02 定案）。
MAX_MONEYNESS_DISTANCE = 0.06
#: 小账户试探护栏：单笔 1 张、单日 1 笔（Product 定案）。
PROBE_LOT = 1
DAILY_ENTRY_LIMIT = 1
#: 成交打标（复盘与常规单分开统计）。
LOW_COST_PROBE_TAG = "low_cost_probe"
#: 最小下单数量（1 张）。
DEFAULT_LOT = 1
#: 美股期权合约乘数。
OPTION_MULTIPLIER = 100.0

#: 连续空轮计数（租户级）。
STREAK_KEY = "volatility_capacity_empty_streak:{tenant_id}"
#: 归因标记：这次「暂停新开仓」是容量预检自己置的，自动解除只能解它。
MARKER_KEY = "volatility_capacity_pause:{tenant_id}"
#: 置暂停的时间（用于总览展示「何时开始容量不足」）。
PAUSED_AT_KEY = "volatility_capacity_pause_at:{tenant_id}"
#: 连续几轮**买不起**才升级为 ERROR 告警（Product/Reviewer 2026-11-04 G 方案）。
#:
#: **它只决定「何时升级告警」，不决定「何时停机」**：容量预检不再自动暂停新开仓。
#: 原因（真实事故）：实盘权益 $6,640、容量线 $398，而 AMD 单张 $1,740 —— 结构上永远买不起。
#: 自动暂停把它当成异常熔断，结果是「账户小 → 暂停 → 只有开仓才能解除暂停」的死循环，
#: 两次人工解除后仍然复发。买不起的候选本来就被预检挡在下单链路之外，
#: 自动暂停只是冗余的第二道限制，代价却是整个策略停摆。
STREAK_TO_ALERT = 3
#: 当天是否已经发过「容量不足」ERROR 告警（每天最多一条，避免每轮刷屏）。
INSUFFICIENT_ALERT_KEY = "volatility_capacity_insufficient_alert_on:{tenant_id}"
#: 容量耗尽告警/事件类型（原 `VOLATILITY_CAPACITY_PAUSED`；不再暂停，故改名）。
INSUFFICIENT_EVENT = "VOLATILITY_CAPACITY_EXHAUSTED"
#: 买不起轮次的时间戳窗口（Reviewer 2026-11-04：频繁买不起要单独告警，**与暂停解耦**）。
BUYOUT_ROUNDS_KEY = "volatility_capacity_buyout_rounds_24h:{tenant_id}"
BUYOUT_WINDOW_HOURS = 24
#: 窗口内买不起达到该轮数 → 发 WARNING（独立于暂停，不动任何阈值）。
BUYOUT_WARN_THRESHOLD = 5
BUYOUT_WARN_EVENT = "VOLATILITY_CAPACITY_BUYOUT_FREQUENT"
#: 买不起 WARNING 的**小时聚合**键（Ops P1-8，2026-11-05 用户指派）：
#: 同因同租户**每小时最多一条**；同小时内的后续轮次只累加计数、不重复推送。
#:
#: 为什么不是「通配静音」：静音会把「结构性买不起」彻底藏起来。这里聚合后那一条
#: 仍然带**24h 轮次数**与**最近一轮时间**，逐轮明细照旧留在 `BUYOUT_ROUNDS_KEY`
#: 的时间戳窗口里（可查、可追、可还原到分钟）。
#: 背景：2026-11-04 该告警在 `futu_real_master` 上刷到 61 轮、约每分钟一条。
BUYOUT_WARN_HOUR_KEY = "volatility_capacity_buyout_warn_hour:{tenant_id}"
#: 小时桶按**北京时间**切分（运维与用户看的是中国时间，UTC 切分会跨到「昨天的后半天」）。
BUYOUT_WARN_TZ = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)

#: 拒绝原因（写进诊断/事件；**必须可区分**，否则「字段缺失」会伪装成「容量不足」）。
REASON_PREMIUM = "PREMIUM_OVER_CAPACITY"
REASON_SPREAD = "SPREAD_TOO_WIDE"
REASON_DEPTH = "DEPTH_TOO_THIN"
REASON_DEPTH_UNKNOWN = "DEPTH_UNKNOWN"
REASON_STALE = "QUOTE_STALE"
REASON_BOOK_MISSING = "BOOK_MISSING"
REASON_DTE = "DTE_TOO_SHORT"
REASON_DTE_UNKNOWN = "EXPIRY_UNKNOWN"
REASON_MONEYNESS = "MONEYNESS_TOO_FAR"
REASON_MONEYNESS_UNKNOWN = "MONEYNESS_UNKNOWN"

_REASON_TEXT = {
    REASON_PREMIUM: "单张最小权利金超过容量线",
    REASON_SPREAD: "盘口价差过宽",
    REASON_DEPTH: "盘口深度不足",
    REASON_DEPTH_UNKNOWN: "盘口深度字段缺失（无法确认深度）",
    REASON_STALE: "报价新鲜度不足",
    REASON_BOOK_MISSING: "双边盘口缺失",
    REASON_DTE: "到期日不足 14 天（临期 gamma 跳空）",
    REASON_DTE_UNKNOWN: "到期日无法确认",
    REASON_MONEYNESS: "虚值幅度超过 6%（彩票区）",
    REASON_MONEYNESS_UNKNOWN: "无法确认虚值幅度",
}


def is_real_account(broker) -> bool:
    """broker 是否实盘账户（`trading_env` 以 REAL 结尾）。"""
    return str(getattr(broker, "trading_env", "") or "").upper().endswith("REAL")


def _number(row: Any, *keys: str) -> float | None:
    """从行里取第一个可解析的数值；缺列/NaN/不可解析返回 ``None``（**不得当作 0**）。"""
    getter = getattr(row, "get", None)
    if getter is None:
        return None
    for key in keys:
        try:
            raw = getter(key)
        except Exception:  # noqa: BLE001 - 结构异常一律按「取不到」处理
            return None
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        return value
    return None


def capacity_equity_line(equity: float) -> float:
    """容量线（美元）。权益 ≤ 0 时返回 0 —— 拿不到权益就等于一张也买不起（fail-closed）。"""
    try:
        value = float(equity or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return value * CAPACITY_EQUITY_RATIO


def capacity_line(equity: float, loss_limit: float | None = None) -> float:
    """**实际生效**的容量线 = `min(权益 × 6%, 压力损失预算 loss_limit)`。

    为什么取 min：6% 是常量兜底，而 `loss_limit` 来自租户级
    `strategy_risk_budgets.max_loss_ratio`（可能被改成别的比例）。
    常量一旦大于预算，预检就会比风控更宽 —— 于是「预检放行、下单被拒」，
    正是本需求要消灭的那种失败。取 min 保证**预检永远不会比风控更宽**。
    """
    line = capacity_equity_line(equity)
    try:
        limit = float(loss_limit or 0)
    except (TypeError, ValueError):
        return line
    if limit <= 0:
        return line
    return min(line, limit)


def minimum_premium(row: Any, lot: int = DEFAULT_LOT) -> float | None:
    """单张最小权利金 = `ask × 100 × lot`（用 ask，按最坏成交价估）。"""
    ask = _number(row, "ask_price", "ask")
    if ask is None or ask <= 0:
        return None
    return ask * OPTION_MULTIPLIER * max(1, int(lot))


def _book_sizes(row: Any) -> tuple[float | None, float | None]:
    """取盘口深度（买一量、卖一量）。

    Futu 快照的字段名是 `bid_vol` / `ask_vol`；不同来源也可能叫 `bid_size` / `ask_size`。
    **取不到返回 None**（按未知处理，不当作「深度充足」）。
    """
    return (
        _number(row, "bid_vol", "bid_size", "bid_volume"),
        _number(row, "ask_vol", "ask_size", "ask_volume"),
    )


def _moneyness_distance(row: Any, spot: float) -> float | None:
    """**虚值幅度**（占现价比例）；实值返回 0。无法判定返回 None。

    只按虚值方向算：CALL 虚值 = 行权价高于现价的部分，PUT 虚值 = 低于现价的部分。
    **实值不按「距离远」拒绝** —— 实值合约权利金必然超标，由容量线那条去挡。
    """
    strike = _number(row, "strike_price", "strike")
    if strike is None or strike <= 0 or spot is None or spot <= 0:
        return None
    option_type = str(getattr(row, "get", lambda *_: "")("option_type") or "").upper()
    ratio = strike / spot - 1
    if option_type.startswith("C"):
        return max(0.0, ratio)
    if option_type.startswith("P"):
        return max(0.0, -ratio)
    return None


def _dte_days(row: Any, today: "date | None" = None) -> int | None:
    """距到期天数（按纽约日期）；无法解析返回 None。"""
    getter = getattr(row, "get", None)
    raw = getter("strike_time") if getter else None
    if raw is None or (isinstance(raw, float) and raw != raw) or str(raw).strip() == "":
        return None
    try:
        expiry = pd.to_datetime(raw, errors="coerce")
    except Exception:  # noqa: BLE001
        return None
    if pd.isna(expiry):
        return None
    base = today or datetime.now(NY).date()
    return int((expiry.date() - base).days)


def evaluate_row(
    row: Any, *, equity: float, lot: int = DEFAULT_LOT,
    now: datetime | None = None, spot: float | None = None,
    today: "date | None" = None, line: float | None = None,
) -> tuple[bool, str, str, float | None]:
    """单张合约的六条判定，返回 `(通过, 原因码, 原因文字, 最小权利金)`。

    判定顺序固定为「先看有没有数据、再看买不买得起、最后看流动性」——
    顺序会影响诊断里给出的原因码，实盘复盘时要能一眼看懂是**哪一条**挡的。

    `line` 传入时按它判（调用方已用 `capacity_line(equity, loss_limit)` 收口过），
    这样「批量筛」和「单张判」用的是**同一条线**，不会各自算一份。
    """
    line = capacity_line(equity) if line is None else float(line)
    premium = minimum_premium(row, lot)
    bid = _number(row, "bid_price", "bid")
    ask = _number(row, "ask_price", "ask")
    if premium is None or bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False, REASON_BOOK_MISSING, "双边盘口缺失或不合法（bid/ask 不可用）", premium
    if premium > line:
        return False, REASON_PREMIUM, (
            f"单张最小权利金 ${premium:.0f} 超过容量线 ${line:.0f}"
            f"（权益 ${float(equity or 0):.0f} × {CAPACITY_EQUITY_RATIO:.0%}）"
        ), premium
    # 到期下限：临期合约 gamma 跳空，止损额撑不住（Product 2026-11-02 定案 ≥14 天）。
    dte = _dte_days(row, today)
    if dte is None:
        return False, REASON_DTE_UNKNOWN, "无法确认到期日（strike_time 缺失/不可解析）", premium
    if dte < MIN_DTE:
        return False, REASON_DTE, f"距到期 {dte} 天，不足 {MIN_DTE} 天", premium
    # 虚值上限：$199 在 $250 标的上只存在于远虚值，超过 6% 属彩票区。
    distance = _moneyness_distance(row, spot)
    if distance is None:
        return False, REASON_MONEYNESS_UNKNOWN, "无法确认虚值幅度（缺行权价/现价/合约类型）", premium
    if distance > MAX_MONEYNESS_DISTANCE:
        return False, REASON_MONEYNESS, (
            f"虚值 {distance:.1%} 超过上限 {MAX_MONEYNESS_DISTANCE:.0%}（彩票区）"
        ), premium
    middle = (bid + ask) / 2
    spread_ratio = (ask - bid) / middle if middle > 0 else float("inf")
    if spread_ratio > MAX_SPREAD_RATIO:
        return False, REASON_SPREAD, (
            f"盘口价差 {spread_ratio:.1%} 超过 {MAX_SPREAD_RATIO:.0%}"
        ), premium
    bid_size, ask_size = _book_sizes(row)
    if bid_size is None or ask_size is None:
        # **不能当作深度充足**：字段缺失就说明「无法确认盘口接得住这 1 张」，
        # 但原因码必须和「深度确实不足」分开，否则运维会去查错方向。
        return False, REASON_DEPTH_UNKNOWN, (
            "快照没有盘口深度字段（bid_vol/ask_vol），无法确认深度"
        ), premium
    required = DEPTH_MIN_MULTIPLE * max(1, int(lot))
    if bid_size < required or ask_size < required:
        return False, REASON_DEPTH, (
            f"盘口深度不足：买一 {bid_size:.0f} 张 / 卖一 {ask_size:.0f} 张，"
            f"要求 ≥ {required} 张"
        ), premium
    fresh, fresh_reason, _age = quote_freshness(
        row, now, int(MAX_QUOTE_AGE_SECONDS),
    )
    if not fresh:
        return False, REASON_STALE, fresh_reason, premium
    return True, "", "", premium


@dataclass
class CapacityPrecheck:
    """一轮容量预检的结果（可空、可诊断、可入事件）。"""

    equity: float
    line: float
    lot: int = DEFAULT_LOT
    eligible: pd.DataFrame = field(default_factory=pd.DataFrame)
    reasons: dict[str, int] = field(default_factory=dict)
    cheapest_premium: float | None = None
    #: 拿不到美元权益（币种未通过 `portfolio_allocator` 门禁）时为真 ——
    #: 此时容量线按 0 处理，但**原因必须单独说**，不能伪装成「容量不足」。
    currency_blocked: bool = False
    #: 压力损失预算（租户级 `loss_limit`）；传入时容量线 = min(权益×6%, loss_limit)。
    loss_limit: float | None = None

    @property
    def empty(self) -> bool:
        return self.eligible is None or self.eligible.empty

    @property
    def needed_equity(self) -> float | None:
        """承载「最便宜的那张候选」所需的最低权益。"""
        if not self.cheapest_premium:
            return None
        return self.cheapest_premium / CAPACITY_EQUITY_RATIO

    def message(self) -> str:
        """给人看的一句话（总览页/决策日志同源）。"""
        if self.currency_blocked:
            return (
                "容量预检未通过：实盘账户资产快照币种无法确认为 USD，"
                "美元权益不可用，容量线按 0 处理（详情见币种门禁告警）"
            )
        if not self.empty:
            return (
                f"容量预检通过 {len(self.eligible)} 张（容量线 ${self.line:.0f}）"
            )
        needed = self.needed_equity
        cheapest = self.cheapest_premium
        if cheapest is None:
            # 纯质量拒绝轮没有容量证据：显示数量最多的真实拒绝原因，
            # 禁止误报为容量不足或盘口缺失。
            if self.reasons:
                top_reason, top_count = max(
                    self.reasons.items(), key=lambda item: item[1]
                )
                reason_text = _REASON_TEXT.get(top_reason, top_reason)
                return f"容量预检未通过：{reason_text}（{top_count} 张）"
            return "容量预检未通过：没有可评估的候选合约"
        detail = f"最便宜候选 ${cheapest:.0f}"
        if needed is not None:
            detail += f"，需要权益约 ${needed:,.0f} 才能承载"
        return (
            f"容量不足：{detail}（当前权益 ${self.equity:,.0f}，"
            f"容量线 ${self.line:.0f}）"
        )

    def as_payload(self) -> dict:
        return {
            "equity": round(float(self.equity), 2),
            "capacity_line": round(float(self.line), 2),
            "lot": int(self.lot),
            "eligible_count": 0 if self.empty else int(len(self.eligible)),
            "cheapest_premium": None if self.cheapest_premium is None
            else round(float(self.cheapest_premium), 2),
            "needed_equity": None if self.needed_equity is None
            else round(float(self.needed_equity), 2),
            "reasons": dict(self.reasons),
            "currency_blocked": bool(self.currency_blocked),
            "loss_limit": None if self.loss_limit is None else round(float(self.loss_limit), 2),
        }


def filter_candidates(
    candidates: pd.DataFrame, *, equity: float, lot: int = DEFAULT_LOT,
    now: datetime | None = None, spot: float | None = None,
    today: "date | None" = None, loss_limit: float | None = None,
) -> CapacityPrecheck:
    """按六条判定筛候选；**只读不写**（写状态在 `record_selection_round`）。

    `loss_limit` 传租户级压力损失预算时，容量线取 `min(权益×6%, loss_limit)` ——
    保证预检**永远不比风控更宽**（见 `capacity_line`）。
    """
    line = capacity_line(equity, loss_limit)
    result = CapacityPrecheck(
        equity=float(equity or 0), line=line, lot=int(lot),
        loss_limit=None if loss_limit is None else float(loss_limit or 0),
        eligible=pd.DataFrame(columns=list(candidates.columns)) if candidates is not None
        else pd.DataFrame(),
    )
    if candidates is None or candidates.empty:
        return result
    kept: list[int] = []
    for index, row in candidates.iterrows():
        ok, reason, _text, premium = evaluate_row(
            row, equity=equity, lot=lot, now=now, spot=spot, today=today, line=line,
        )
        if premium is not None:
            if result.cheapest_premium is None or premium < result.cheapest_premium:
                result.cheapest_premium = premium
        if ok:
            kept.append(index)
        else:
            result.reasons[reason] = result.reasons.get(reason, 0) + 1
    result.eligible = candidates.loc[kept] if kept else pd.DataFrame(
        columns=list(candidates.columns)
    )
    return result


def _tenant_of(tenant_id: str | None) -> str:
    tenant = str(tenant_id or "").strip()
    if not tenant:
        # 与 `set_volatility_new_entries_paused` 一致：拿不到租户就拒绝写。
        # 回落全局会把这一个租户的容量不足，变成**所有租户一起停**。
        raise ValueError("无法解析租户，拒绝写入容量预检状态键")
    return tenant


def counts_as_capacity_empty_round(result: CapacityPrecheck) -> bool:
    """这一轮空结果是否算**「容量不足」空轮**（唯一判定点，Reviewer 2026-11-04 口径）。

    只有「买不起」才计入连续空轮：`PREMIUM_OVER_CAPACITY`。

    为什么必须按原因区分（2026-11-02 的真实事故）：那一轮 `reasons` 是
    `{"PREMIUM_OVER_CAPACITY": 9, "MONEYNESS_TOO_FAR": 1}` —— 只要有一条虚值拒绝，
    整轮就算空轮，于是「质量门槛挡住的轮次」也被算进了「容量不足」，
    连续 3 轮就暂停了新开仓，实盘两天没开仓。**买得起 ≠ 该买**，
    质量类拒绝（虚值/DTE/价差/深度/盘口/报价过期）只记录、不计数、不触发暂停。
    """
    if result is None or not result.empty:
        return False
    try:
        bought_out = int(result.reasons.get(REASON_PREMIUM, 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return bought_out > 0


def _record_buyout_round(store, tenant: str, moment: datetime) -> int:
    """记一次「买不起」轮次，返回 24h 窗口内的轮数（滑动窗口，超窗自动丢弃）。

    为什么单独计：暂停用的 streak 表达的是**连续**；而「频繁买不起」是另一回事 ——
    买不起天天发生、但中间夹着质量轮次时 streak 会不断归零、永远不会暂停。
    Reviewer 要求用独立告警盯它（`BUYOUT_WARN_THRESHOLD`），不动任何阈值。
    """
    key = BUYOUT_ROUNDS_KEY.format(tenant_id=tenant)
    cutoff = moment - timedelta(hours=BUYOUT_WINDOW_HOURS)
    stamps: list[datetime] = []
    try:
        raw = store.get_setting(key, "") or ""
    except Exception as exc:  # noqa: BLE001 - 读失败按空窗口处理，不阻断闸门
        logger.warning("读取买不起轮次记录失败（按空窗口处理）：%s", exc)
        raw = ""
    if raw.strip():
        try:
            for item in json.loads(raw):
                parsed = datetime.fromisoformat(str(item))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed >= cutoff:
                    stamps.append(parsed)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("买不起轮次记录不可解析，已重置窗口：%s", exc)
            stamps = []
    stamps.append(moment)
    store.set_setting(key, json.dumps([stamp.isoformat() for stamp in stamps]))
    return len(stamps)


def _buyout_warn_bucket(moment: datetime) -> tuple[str, str]:
    """(小时桶, 北京时间显示串)。桶按**北京时间**切，与用户看到的时段对齐。"""
    ts = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    local = ts.astimezone(BUYOUT_WARN_TZ)
    return local.strftime("%Y-%m-%dT%H"), local.strftime("%m-%d %H:%M")


def _warn_frequent_buyouts(
    store, tenant: str, count: int, result: CapacityPrecheck, moment: datetime,
) -> bool:
    """24h 内买不起轮次达到阈值 → WARNING，**同因同租户每小时最多一条**。

    返回本次是否真的推送（守卫与验收用它断言聚合生效）。

    为什么聚合（Ops P1-8，2026-11-05）：该告警在 `futu_real_master` 上 5 小时刷了
    119 条（≈24 条/小时），把真正的信号埋了。聚合不是静音 ——
    推送的那一条仍带 **24h 轮次数** 与 **最近一轮时间**，逐轮时间戳照旧留在
    `BUYOUT_ROUNDS_KEY` 窗口里，可还原到分钟。

    读小时桶失败时按「本小时还没发过」处理（**宁可多一条，也不静默**）。
    """
    if count < BUYOUT_WARN_THRESHOLD:
        return False
    bucket, label = _buyout_warn_bucket(moment)
    key = BUYOUT_WARN_HOUR_KEY.format(tenant_id=tenant)
    sent_bucket = ""
    try:
        raw = str(store.get_setting(key, "") or "").strip()
        # 兼容两种历史格式：裸桶串 / `bucket|count|iso`
        sent_bucket = raw.split("|", 1)[0] if raw else ""
    except Exception as exc:  # noqa: BLE001 - 读不到按「未发」处理，宁可多一条也不静默
        logger.warning("读取买不起告警小时桶失败（按未发处理）：%s", exc)
        sent_bucket = ""
    if sent_bucket == bucket:
        # 同一小时已推送过：本轮只累加计数（已在 _record_buyout_round 里完成）
        return False
    try:
        from .alerts import AlertManager

        AlertManager(store).send(
            "WARNING", BUYOUT_WARN_EVENT,
            f"[{tenant}] 近 {BUYOUT_WINDOW_HOURS} 小时已有 {count} 轮因「买不起」筛空"
            f"（最近一轮 {label}；同因同租户每小时只聚合推送一条，"
            f"逐轮时间戳见 `{BUYOUT_ROUNDS_KEY.format(tenant_id=tenant)}`；"
            f"连续 {STREAK_TO_ALERT} 轮会升级为 ERROR 告警，不会自动暂停；"
            f"质量类拒绝不计入且会清零连续计数）："
            f"{result.message()}",
            incident_key=f"{BUYOUT_WARN_EVENT}:{tenant}:{moment.date().isoformat()}",
        )
    except Exception as exc:  # noqa: BLE001 - 告警失败不得把交易链路打断
        logger.warning("买不起频繁告警发送失败（计数照常）：%s", exc)
        return False
    try:
        store.set_setting(key, f"{bucket}|{count}|{moment.isoformat()}")
    except Exception as exc:  # noqa: BLE001 - 写失败只影响下一小时去重，不阻断交易
        logger.warning("写入买不起告警小时桶失败（下一轮可能重复推送）：%s", exc)
    return True


def _insufficient_alerted_today(store, tenant: str, moment: datetime) -> bool:
    """今天是否已经发过「容量耗尽」ERROR（每天最多一条，避免每轮刷屏）。"""
    try:
        last = str(store.get_setting(
            INSUFFICIENT_ALERT_KEY.format(tenant_id=tenant), "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - 读失败按「还没发过」处理，宁可多一条也不静默
        logger.warning("读取容量告警去重键失败（按未发过处理）：%s", exc)
        return False
    return last == moment.date().isoformat()


def record_selection_round(
    store, tenant_id: str | None, result: CapacityPrecheck,
    *, now: datetime | None = None, underlying: str = "",
) -> dict:
    """把一轮结果落成「连续空轮计数 / 告警 / 遗留暂停的自动解除」。

    **不再自动暂停**（2026-11-04 G 方案）：连续 ``STREAK_TO_ALERT`` 轮**买不起**只升级
    一条 ERROR 告警（每天最多一条），不写 `volatility_new_entries_paused`、不写 marker。

    自动解除只服务**遗留状态**：若 marker 仍为真（旧版本置的容量暂停），
    某一轮筛出合格候选时解除它；**人工/故障暂停（marker 为假）永远不被自动解除**。

    **只有「买不起」计入空轮**（见 :func:`counts_as_capacity_empty_round`）：
    质量类拒绝只记录原因，既不加一、也不清零（不能拿质量轮次掩盖真实的容量不足）。
    """
    tenant = _tenant_of(tenant_id)
    moment = now or datetime.now(timezone.utc)
    state = {
        "tenant_id": tenant,
        "eligible_count": 0 if result.empty else int(len(result.eligible)),
        "streak": 0,
        "paused": False,
        "action": "none",
        "message": result.message(),
    }
    if not result.empty:
        store.set_setting(STREAK_KEY.format(tenant_id=tenant), "0")
        if store.get_setting(MARKER_KEY.format(tenant_id=tenant), "").strip().lower() == "true":
            set_volatility_new_entries_paused(store, tenant, False)
            store.set_setting(MARKER_KEY.format(tenant_id=tenant), "false")
            store.set_setting(PAUSED_AT_KEY.format(tenant_id=tenant), "")
            store.audit("VOLATILITY_CAPACITY_PAUSE_RELEASED", {
                "tenant_id": tenant, "underlying": underlying,
                **result.as_payload(),
            }, "volatility_capacity", actor="service:gamma")
            state["action"] = "released"
            state["message"] = (
                f"容量已可承载：{result.message()}，已自动解除容量暂停"
            )
        return state
    streak = 0
    try:
        streak = int(float(store.get_setting(
            STREAK_KEY.format(tenant_id=tenant), "0") or 0))
    except (TypeError, ValueError):
        streak = 0
    if not counts_as_capacity_empty_round(result):
        # 质量类拒绝（虚值/DTE/价差/深度/盘口/过期）：只记录，且**连续计数归零** ——
        # streak 的语义是「连续买不起」，任何一轮不是容量问题就中断连续。
        # 不然买不起每三天来一次，九天后照样用过期行情锁死新开仓。
        store.set_setting(STREAK_KEY.format(tenant_id=tenant), "0")
        state["streak"] = 0
        # 可能本来就处在暂停中，必须回填真实状态，别让上层拿到「未暂停」的假值。
        state["paused"] = volatility_new_entries_paused_for_tenant(store, tenant)
        state["action"] = "quality_only"
        state["message"] = (
            f"{result.message()}（质量门槛拦截，不计入容量空轮；连续计数归零）"
        )
        return state
    streak += 1
    store.set_setting(STREAK_KEY.format(tenant_id=tenant), str(streak))
    state["streak"] = streak
    buyouts = _record_buyout_round(store, tenant, moment)
    _warn_frequent_buyouts(store, tenant, buyouts, result, moment)
    already_paused = volatility_new_entries_paused_for_tenant(store, tenant)
    state["paused"] = already_paused
    if streak < STREAK_TO_ALERT:
        return state
    if already_paused:
        # 人工/故障暂停：这里**一个字都不写**，也不重复告警 —— 暂停语义保持原样。
        return state
    # 【G 方案】只告警，不再自动暂停：买不起 = 账户规模与合约单价的量级差，
    # 不是需要熔断的异常；暂停只会造出「只有开仓才能解除暂停」的死循环。
    state["action"] = "insufficient"
    if _insufficient_alerted_today(store, tenant, moment):
        return state
    store.set_setting(
        INSUFFICIENT_ALERT_KEY.format(tenant_id=tenant), moment.date().isoformat(),
    )
    payload = {
        "tenant_id": tenant, "underlying": underlying,
        "streak": streak, "auto_paused": False, **result.as_payload(),
    }
    try:
        store.operational_event(
            "volatility_capacity", INSUFFICIENT_EVENT, "ERROR", payload,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断选约
        pass
    try:
        store.audit(
            INSUFFICIENT_EVENT, payload, "volatility_capacity",
            actor="service:gamma",
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from .alerts import AlertManager

        AlertManager(store).send(
            "ERROR", INSUFFICIENT_EVENT,
            f"[{tenant}] 波动率实盘连续 {streak} 轮因「买不起」筛空，"
            f"**未暂停新开仓**（账户规模与单张成本量级差，暂停只会让策略长期停摆）："
            f"{result.message()}",
            incident_key=f"{INSUFFICIENT_EVENT}:{tenant}:{moment.date().isoformat()}",
        )
    except Exception:  # noqa: BLE001 - 告警失败不得把交易链路打断
        pass
    return state


# --------------------------------------------------------------------------- #
# 小账户试探护栏（Product 2026-11-02 定案，与容量预检**同批**上线）
#
# 为什么护栏必须和过滤同批：只上护栏不上过滤 = 允许买临期深度虚值；只上过滤不上护栏 =
# 14 张同时可选时可能连开多张。两者缺一都是「比不做更危险」。
# --------------------------------------------------------------------------- #


def probe_entry_guard(
    todays_entries: list[dict], *, quantity: int, price: float,
    multiplier: float, premium_limit: float,
) -> tuple[bool, str, dict]:
    """单笔 1 张 / 单日 1 笔 / 当日累计权利金上限 三条护栏。

    `todays_entries`：今日该租户、该策略、REAL 环境下的 BUY 意图（含本单之前的全部），
    由调用方从 `order_intents` 取（不新建计数键，避免第二套真值）。

    `premium_limit` 直接传**压力损失预算**里的 `loss_limit`（权益 × max_loss_ratio），
    与 6% 风控同源 —— Product 明确「不新增第二套真值」。
    """
    if int(quantity) != PROBE_LOT:
        return False, f"小账户试探只允许单笔 {PROBE_LOT} 张，本单 {quantity} 张", {
            "quantity": int(quantity), "probe_lot": PROBE_LOT,
        }
    if len(todays_entries) >= DAILY_ENTRY_LIMIT:
        return False, (
            f"今日实盘已开 {len(todays_entries)} 笔，达到单日上限 {DAILY_ENTRY_LIMIT} 笔"
        ), {"today_entries": len(todays_entries), "daily_limit": DAILY_ENTRY_LIMIT}
    spent = 0.0
    for item in todays_entries:
        try:
            spent += abs(float(item.get("quantity") or 0)) * float(
                item.get("limit_price") or 0) * float(multiplier or 1)
        except (TypeError, ValueError):
            continue
    notional = float(quantity) * float(price) * float(multiplier or 1)
    if spent + notional > float(premium_limit or 0):
        return False, (
            f"当日累计权利金 ${spent + notional:,.0f} 将超过上限 ${float(premium_limit or 0):,.0f}"
            f"（已用 ${spent:,.0f} + 本单 ${notional:,.0f}）"
        ), {
            "premium_spent": round(spent, 2), "premium_notional": round(notional, 2),
            "premium_limit": round(float(premium_limit or 0), 2),
        }
    return True, "", {
        "premium_spent": round(spent, 2), "premium_notional": round(notional, 2),
        "premium_limit": round(float(premium_limit or 0), 2),
    }


def today_real_buy_entries(store, *, tenant_id: str, strategy_id: str,
                          today: "date | None" = None) -> list[dict]:
    """今日该租户 + 该策略 + REAL 的 BUY 意图（护栏的唯一数据源）。

    `intent_key` 带模式后缀（`:REAL`），据此**不再另建计数键** ——
    多一个计数器就多一处会和真实下单对不上的地方。
    """
    base = today or datetime.now(NY).date()
    prefix = str(base.isoformat())
    result: list[dict] = []
    try:
        rows = store.intents(limit=2000, tenant_id=tenant_id or None)
    except Exception:  # noqa: BLE001 - 读不到就不放行（fail-closed，见调用方）
        return result
    for item in rows:
        if str(item.get("strategy_id")) != strategy_id:
            continue
        if str(item.get("side")).upper() != "BUY":
            continue
        if not str(item.get("intent_key") or "").upper().endswith(":REAL"):
            continue
        if not str(item.get("created_at") or "").startswith(prefix):
            continue
        result.append(item)
    return result


def capacity_pause_state(store, tenant_id: str | None) -> dict:
    """给总览/页面读的只读快照（不写任何键）。"""
    try:
        tenant = _tenant_of(tenant_id)
    except ValueError:
        return {"tenant_id": "", "paused": False, "owned": False}
    return {
        "tenant_id": tenant,
        "paused": volatility_new_entries_paused_for_tenant(store, tenant),
        "owned": store.get_setting(MARKER_KEY.format(tenant_id=tenant), "").strip().lower()
        == "true",
        "streak": store.get_setting(STREAK_KEY.format(tenant_id=tenant), "0"),
        "paused_at": store.get_setting(PAUSED_AT_KEY.format(tenant_id=tenant), ""),
        # 2026-11-04 起容量预检不再自动暂停：`paused=True` 只可能来自人工或故障。
        # 买不起只体现在 streak 与 `volatility_capacity_insufficient_alert_on` 上。
        "insufficient_alert_on": store.get_setting(
            INSUFFICIENT_ALERT_KEY.format(tenant_id=tenant), "",
        ),
    }
