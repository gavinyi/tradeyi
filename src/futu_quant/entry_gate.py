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
]

#: 固定聚合顺序（对账锁 → 急停 → 只减仓 → 熔断 → 日亏损 → 预算）
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
    """调用方（`submit_limit` / `RiskEngine` 侧）**已经算好**的门禁信号。

    这里没有 broker、没有 store、没有权益查询入口——纯数据。
    """

    reconciliation_locked: bool = False
    reconciliation_detail: str = ""
    account_halt: str = ""
    strategy_reduce_only: str = ""
    breaker_reasons: tuple[BlockReason, ...] = field(default_factory=tuple)
    #: 非空 = 该交易环境熔断维度未启用（显式记录，不产生 block 原因、不停单）
    breaker_unsupported: str = ""
    daily_loss_halted: str = ""
    budget_exhausted: str = ""


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


def resolve_entry_block_reasons(signals: EntrySignals) -> list[BlockReason]:
    """返回**全部**阻止开新仓的原因（空列表 = 放行）。

    多条原因**全量返回**，由调用方全部落 audit（不是只报第一条）。
    """
    reasons: list[BlockReason] = []
    if signals.reconciliation_locked:
        reasons.append(
            BlockReason(
                RECONCILIATION_LOCK,
                signals.reconciliation_detail or "三账不一致，恢复前禁止开仓",
            )
        )
    if signals.account_halt:
        reasons.append(BlockReason(ACCOUNT_HALT, signals.account_halt))
    if signals.strategy_reduce_only:
        reasons.append(BlockReason(STRATEGY_REDUCE_ONLY, signals.strategy_reduce_only))
    reasons.extend(signals.breaker_reasons)
    if signals.daily_loss_halted:
        reasons.append(BlockReason(DAILY_LOSS, signals.daily_loss_halted))
    if signals.budget_exhausted:
        reasons.append(BlockReason(RISK_BUDGET, signals.budget_exhausted))
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
