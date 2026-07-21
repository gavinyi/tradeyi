from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from .reconciliation import reconcile_ledgers


def startup_recovery(store, broker, executor) -> dict:
    result = {"accounts": 0, "orders_reconciled": 0, "ledger_mismatches": 0}
    accounts = broker.accounts()
    result["accounts"] = len(accounts)
    result["orders_reconciled"] = executor.reconcile()
    report = reconcile_ledgers(store, broker)
    result["ledger_mismatches"] = int((report["status"] == "MISMATCH").sum()) if not report.empty else 0
    safe = result["accounts"] > 0 and result["ledger_mismatches"] == 0
    store.set_setting("connectivity_degraded", "false")
    store.set_setting("recovery_blocked", str(not safe).lower())
    store.operational_event("recovery", "STARTUP_RECOVERY", "HEALTHY" if safe else "BLOCKED", result)
    return result


def daily_operations_report(store) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    intents = [row for row in store.intents(limit=50_000) if row["created_at"] >= since]
    alerts = [row for row in store.alerts(limit=10_000) if row["created_at"] >= since]
    report = {
        "orders": len(intents), "filled": sum(float(row.get("filled_quantity", 0) or 0) > 0 for row in intents),
        "rejected": sum(row["status"] == "REJECTED" for row in intents),
        "errors": sum(row["severity"] == "ERROR" for row in alerts),
        "shadow_orders": sum(str(row["status"]).startswith("SHADOW") for row in intents),
        "service_pid": os.getpid(),
    }
    store.operational_event("daily_report", "DAILY_SUMMARY", "HEALTHY", report)
    return report
