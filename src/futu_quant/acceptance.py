from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os


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
    errors = [row for row in store.alerts(limit=10_000)
              if row["created_at"] >= observation_start and row["severity"] == "ERROR"]
    latest_reconciliation = {}
    for row in store.reconciliation_reports(limit=10_000):
        latest_reconciliation.setdefault(row["symbol"], row)
    mismatches = [row for row in latest_reconciliation.values() if row["status"] == "MISMATCH"]
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
        "service_errors": {"passed": not errors, "value": len(errors), "required": 0},
        "reconciliation": {"passed": not mismatches, "value": len(mismatches), "required": 0},
        "backtest": {"passed": latest.get("BACKTEST", False), "value": None, "required": True},
        "drill": {"passed": latest.get("DRILL", False), "value": None, "required": True},
        "risk_review": {"passed": latest.get("RISK_REVIEW", False), "value": None, "required": True},
    }
    return {"strategy": strategy_id, "passed": all(g["passed"] for g in gates.values()),
            "gates": gates}
