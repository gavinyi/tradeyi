"""开仓门禁仲裁器（G4）—— **只聚合、不判定**。

设计：`docs/risk_budget_circuit_breaker_design.md` §D3 / §D3.1（HEAD `1482791`，Chief 2026-10-25 裁定方案 1）。

硬边界（违反即判红，V21）：
- 本模块**不查权益、不查快照、不调券商、不查库**；
- 既有 4 套拦截的判定与副作用**原地不动**（`risk.py` 的日亏损 / 日内回撤、对账锁、急停、只减仓）；
- 新熔断的 `dd_since_reset` / `dd_vs_peak` 由 `RiskEngine` 侧取数与计算，本模块**只接收数值/原因**；
- 只有 `side == "BUY"` 才调用本模块；SELL **永不经过**，任何等级都只停新开仓。

将来新增拦截点只改这里（加一个 `EntrySignals` 字段 + 一条原因），`submit_limit` 的门禁顺序不再被反复改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .risk_breaker import (
    ACCOUNT_HALT,
    DAILY_LOSS,
    DRAWDOWN_ABSOLUTE,
    DRAWDOWN_STOP,
    DRAWDOWN_WARN,
    RECONCILIATION_LOCK,
    RISK_BUDGET,
    STRATEGY_REDUCE_ONLY,
    BlockReason,
    BreakerUnsupportedEnv,
    resolve_equity_env,
)

__all__ = [
    "EntrySignals",
    "resolve_entry_block_reasons",
    "resolve_breaker_inputs",
    "audit_payload",
    "AGGREGATION_ORDER",
    "RISK_ENGINE_REJECT",
]

#: 兜底原因码：`RiskEngine` 拒绝但**未标注 `blocked_by`** 的路径（历史 25 条）。
#: 没有这个兜底，`submit_limit` 的 BUY 聚合一旦以 reasons 为空为放行条件，就会 fail-open。
RISK_ENGINE_REJECT = "risk_engine_reject"

#: 聚合顺序参考（**实际顺序** = `risk_decision.blocked_by` 原序 → 熔断原因）。
#: 既有闸门的 code 由 `RiskEngine` 给出，聚合层不再重复持有它们的字段。
AGGREGATION_ORDER = (
    RECONCILIATION_LOCK,
    ACCOUNT_HALT,
    STRATEGY_REDUCE_ONLY,
    DRAWDOWN_ABSOLUTE,
    DRAWDOWN_STOP,
    DRAWDOWN_WARN,
    DAILY_LOSS,
    RISK_BUDGET,
)


@dataclass(frozen=True)
class EntrySignals:
    """调用方（`submit_limit` / `RiskEngine` 侧）**已经算好**的熔断维度信号。

    单一来源（Chief 2026-10-25 裁定）：既有 4 套闸门的原因**只来自**
    `risk_decision.blocked_by`，这里**不再持有**对账锁/急停/只减仓/日亏损/预算字段
    —— 同一原因两个来源必然漂移（重复原因，或一个说有、一个说没有）。
    """

    breaker_reasons: tuple[BlockReason, ...] = field(default_factory=tuple)
    #: 非空 = 该交易环境熔断维度未启用（显式记录，不产生 block 原因、不停单）
    breaker_unsupported: str = ""


def resolve_breaker_inputs(
    settings: object,
    trading_env: str,
    breaker_reasons: tuple[BlockReason, ...] = (),
) -> tuple[tuple[BlockReason, ...], str]:
    """聚合层捕获 `BreakerUnsupportedEnv`：熔断维度未启用 → **不产生 block 原因**。

    返回 `(breaker_reasons, unsupported_env)`；`unsupported_env` 非空时由调用方
    写 audit + 页面提示（`page_hint`），**绝不允许异常冒到 `submit_limit`**
    —— 冒上去会让模拟盘全面停单。
    """
    try:
        resolve_equity_env(settings, trading_env)
    except BreakerUnsupportedEnv as exc:
        return (), str(getattr(exc, "trading_env", trading_env) or trading_env)
    return tuple(breaker_reasons), ""


def resolve_entry_block_reasons(
    signals: EntrySignals,
    risk_decision: object | None = None,
) -> list[BlockReason]:
    """返回**全部**阻止开新仓的原因（空列表 = 放行）。

    唯一出口：`risk_decision.blocked_by`（RiskEngine 既有闸门）作为**第一条原因**，
    再接熔断原因，按 code 去重；调用方一次写 audit（V9 走真实链路）。
    本函数仍**不判定**：只读取 RiskEngine 已给出的结论与数值。

    **shadow 泄漏缺陷修复（2026-11-07，用户实盘误拦定性）**：
    `RiskEngine` 的熔断结果里，**shadow 模式下 `blocked=False`、只有
    `would_block=True`**（`risk_breaker.py:441` 守住了），但 `risk.py:802` 把
    `reasons` 无条件透传，本函数原来也无条件并进输出 —— shadow 的
    `dd_vs_peak_absolute` 由此变成 `blocked_by`，实盘被硬拦（944/933 事故）。

    修复：熔断原因**只在 RiskEngine 真拒绝**（`allowed=False`，即 enforce 拦下）
    时才并入输出；shadow 观察（`allowed=True` 且仅 `would_block`）只留给
    `ENTRY_GATE_SHADOW` audit，不进 `blocked_by`。
    """
    reasons: list[BlockReason] = []
    seen: set[str] = set()
    decision_allowed = (
        bool(getattr(risk_decision, "allowed", True)) if risk_decision is not None else True
    )
    if risk_decision is not None:
        decision_reason = str(getattr(risk_decision, "reason", "") or "风控拒绝")
        for code in tuple(getattr(risk_decision, "blocked_by", ()) or ()):
            if code in seen:
                continue
            seen.add(code)
            reasons.append(BlockReason(str(code), decision_reason))
        # 熔断原因只属于「RiskEngine 真拒绝」的决策：shadow 观察不并入，
        # 否则配置的 shadow 语义在聚合层泄漏成硬拦（本缺陷根因）。
        if not decision_allowed:
            for reason in signals.breaker_reasons:
                if reason.code in seen:
                    continue
                seen.add(reason.code)
                reasons.append(reason)
    else:
        for reason in signals.breaker_reasons:
            if reason.code in seen:
                continue
            seen.add(reason.code)
            reasons.append(reason)
    # 兜底（P0 fail-open 防线）：RiskEngine 已拒绝但没有任何结构化原因时，
    # 必须补一条通用原因，保证拒绝不会被聚合层“吃掉”。
    if not reasons and risk_decision is not None and getattr(risk_decision, "allowed", True) is False:
        reasons.append(
            BlockReason(
                RISK_ENGINE_REJECT,
                str(getattr(risk_decision, "reason", "") or "风控拒绝（未标注具体闸门）"),
            )
        )
    return reasons


def audit_payload(reasons: list[BlockReason], *, breaker_unsupported: str = "") -> dict:
    """落 audit 的统一结构：每条原因一个 `blocked_by`，shadow 期同样记录。

    `breaker_unsupported` 非空时写明「熔断维度未启用」（`breaker_unsupported_env`），
    这是**显式记录而非静默关闭**：BUY 照常放行，但 audit/页面必须能看到原因。
    """
    payload: dict = {
        "blocked_by": [reason.code for reason in reasons],
        "block_details": [reason.as_audit() for reason in reasons],
        "blocked": bool(reasons),
    }
    if breaker_unsupported:
        payload["breaker_unsupported_env"] = breaker_unsupported
        payload["breaker_status"] = "not_enabled"
        payload["breaker_code"] = BreakerUnsupportedEnv.code
    return payload
