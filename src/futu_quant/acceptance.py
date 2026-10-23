from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from .config import load_all_volatility_campaigns, load_gamma_campaigns


def strategy_reconciliation_symbols(store, strategy_id: str) -> set[str]:
    """Limit approval reconciliation to symbols owned by the selected strategy."""
    symbols = {str(row["symbol"]) for row in store.intents(limit=50_000) if row["strategy_id"] == strategy_id and row.get("symbol")}
    symbols.update(str(row["symbol"]) for row in store.shadow_fills(limit=50_000) if row["strategy_id"] == strategy_id and row.get("symbol"))
    symbols.update(str(row["symbol"]) for row in store.virtual_allocations() if row["strategy_id"] == strategy_id and row.get("symbol"))
    if strategy_id in {"earnings_gamma", "earnings_gamma_manual"}:
        symbols.update(str(item.get("code", "")) for item in load_gamma_campaigns())
    if strategy_id == "volatility_long_gamma":
        # #1 键级分区：**故意读全局**（含各租户键）—— 验收看的是"策略历史上用过哪些合约"，
        # 本就该跨租户取全量；**只读**，不参与清理、不写键。
        symbols.update(str(item.get("code", "")) for item in load_all_volatility_campaigns())
    return {symbol for symbol in symbols if symbol}


def strategy_acceptance(store, strategy_id: str) -> dict:
    now = datetime.now(timezone.utc)
    min_days = max(1, int(os.getenv("APPROVAL_MIN_SHADOW_DAYS", "5")))
    min_orders = max(1, int(os.getenv("APPROVAL_MIN_SHADOW_ORDERS", "5")))
    min_fill_rate = float(os.getenv("APPROVAL_MIN_SHADOW_FILL_RATE", "0.70"))
    max_slippage = float(os.getenv("APPROVAL_MAX_SHADOW_SLIPPAGE_PCT", "0.01"))
    intents = [row for row in store.intents(limit=50_000)
               if row["strategy_id"] == strategy_id and row["status"].startswith("SHADOW")]
    dates = {datetime.fromisoformat(row["created_at"]).date() for row in intents}
    filled = [row for row in intents if float(row.get("filled_quantity", 0) or 0) > 0]
    fill_rate = len(filled) / len(intents) if intents else 0.0
    slippages = []
    for row in filled:
        reference = float(row["limit_price"] or 0)
        if reference:
            direction = 1 if row["side"] == "BUY" else -1
            slippages.append((float(row["average_fill_price"]) / reference - 1) * direction)
    avg_slippage = sum(slippages) / len(slippages) if slippages else 0.0
    recent = (now - timedelta(days=max(min_days + 2, 7))).isoformat()
    observation_start = min((row["created_at"] for row in intents), default=now.isoformat())
    observation_start = max(recent, observation_start)
    # 只把当前仍未确认/未恢复的错误作为上线阻塞项；历史已处理日志
    # 继续保留用于审计，但不应永久卡住新版本审批。
    # 审批检查当前运行健康度，不把数日前已经停止产生的历史告警永久
    # 计入当前值；历史日志仍可在运行日志页查看。
    error_window_start = max(
        observation_start,
        (now - timedelta(hours=max(1, int(os.getenv("APPROVAL_ERROR_WINDOW_HOURS", "24"))))).isoformat(),
    )
    errors = [row for row in store.alerts(limit=10_000)
              if row["created_at"] >= error_window_start
              and row["severity"] == "ERROR"
              and not bool(row.get("acknowledged", 0))
              and str(row.get("action_status", "")).upper() not in {
                  "RESOLVED", "RECOVERED", "CLOSED", "ACKNOWLEDGED",
              }]
    latest_reconciliation = {}
    for row in store.reconciliation_reports(limit=10_000):
        latest_reconciliation.setdefault(row["symbol"], row)
    reconciliation_symbols = strategy_reconciliation_symbols(store, strategy_id)
    scoped_reconciliation = [row for symbol, row in latest_reconciliation.items() if not reconciliation_symbols or symbol in reconciliation_symbols]
    mismatches = [row for row in scoped_reconciliation if row["status"] == "MISMATCH"]
    evidence = store.acceptance_evidence(strategy_id)
    latest = {}
    for row in evidence:
        latest.setdefault(row["evidence_type"], bool(row["passed"]))
    gates = {
        "shadow_days": {"passed": len(dates) >= min_days, "value": len(dates), "required": min_days},
        "shadow_orders": {"passed": len(intents) >= min_orders, "value": len(intents), "required": min_orders},
        "fill_rate": {"passed": fill_rate >= min_fill_rate, "value": fill_rate, "required": min_fill_rate},
        "slippage": {"passed": avg_slippage <= max_slippage, "value": avg_slippage,
                     "required": max_slippage},
        "reconciliation": {"passed": not mismatches, "value": len(mismatches), "required": 0},
        "backtest": {"passed": latest.get("BACKTEST", False), "value": None, "required": True},
        "drill": {"passed": latest.get("DRILL", False), "value": None, "required": True},
        "risk_review": {"passed": latest.get("RISK_REVIEW", False), "value": None, "required": True},
    }
    return {"strategy": strategy_id, "passed": all(g["passed"] for g in gates.values()), "gates": gates,
            "details": {
                "reconciliation_symbols": sorted(reconciliation_symbols),
                "reconciliation_mismatches": mismatches,
                # 服务错误仅作为运行健康度诊断，不再阻塞策略上线审批。
                "service_errors": len(errors),
                "service_error_window_hours": max(
                    1, int(os.getenv("APPROVAL_ERROR_WINDOW_HOURS", "24"))
                ),
            }}
