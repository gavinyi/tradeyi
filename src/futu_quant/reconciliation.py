from __future__ import annotations

import json

import pandas as pd

from .strategy_plugins import BY_ID


def futu_reconciliation_symbol(symbol: str) -> bool:
    """Futu ledger reconciliation only covers US stocks/options."""
    return str(symbol).startswith("US.")


def _strategy_enable_keys(strategy_id: str) -> tuple[str, ...]:
    keys = [f"strategy_enabled:{strategy_id}"]
    plugin = BY_ID.get(strategy_id)
    if plugin:
        keys.append(f"strategy_enabled:{plugin.service_name}")
    return tuple(dict.fromkeys(keys))


def _disable_strategy_for_reconciliation(store, strategy_id: str) -> None:
    restore_key = f"strategy_reconciliation_restore:{strategy_id}"
    if not store.get_setting(restore_key, ""):
        restore = {
            key: store.get_setting(key, "")
            for key in _strategy_enable_keys(strategy_id)
        }
        store.set_setting(restore_key, json.dumps(restore, ensure_ascii=False))
    store.set_setting(f"strategy_reconciliation_disabled:{strategy_id}", "true")
    for key in _strategy_enable_keys(strategy_id):
        store.set_setting(key, "false")


def _recover_strategy_from_reconciliation(store, strategy_id: str) -> None:
    marker = f"strategy_reconciliation_disabled:{strategy_id}"
    if store.get_setting(marker, "false").lower() != "true":
        return
    restore_raw = store.get_setting(f"strategy_reconciliation_restore:{strategy_id}", "")
    try:
        restore = json.loads(restore_raw) if restore_raw else {}
    except json.JSONDecodeError:
        restore = {}
    for key in _strategy_enable_keys(strategy_id):
        previous = str(restore.get(key, "true") or "true")
        store.set_setting(key, previous)
    store.set_setting(marker, "false")
    store.set_setting(f"strategy_reconciliation_restore:{strategy_id}", "")
    store.audit(
        "RECONCILIATION_STRATEGY_AUTO_RECOVERED",
        {"strategy": strategy_id, "restored_settings": restore},
        strategy_id,
    )


def reconcile_ledgers(store, broker, tolerance: float = 1e-6) -> pd.DataFrame:
    positions = broker.positions()
    if not positions.empty and "code" in positions.columns:
        positions = positions[positions["code"].astype(str).map(futu_reconciliation_symbol)].copy()
    broker_qty = (
        positions.groupby("code")["qty"].sum().astype(float).to_dict()
        if not positions.empty else {}
    )
    virtual_rows = [
        row for row in store.virtual_positions()
        if futu_reconciliation_symbol(row["symbol"])
    ]
    ledger_qty: dict[str, float] = {}
    owners: dict[str, set[str]] = {}
    for row in virtual_rows:
        symbol = row["symbol"]
        ledger_qty[symbol] = ledger_qty.get(symbol, 0) + float(row["quantity"])
        if abs(float(row["quantity"])) > tolerance:
            owners.setdefault(symbol, set()).add(row["strategy_id"])
    reports = []
    current_symbols = set(broker_qty) | set(ledger_qty)
    for key in store.settings_with_prefix("symbol_blocked:"):
        symbol = key.removeprefix("symbol_blocked:")
        if symbol not in current_symbols or not futu_reconciliation_symbol(symbol):
            store.set_setting(key, "false")
    mismatched_strategies: set[str] = set()
    for symbol in sorted(current_symbols):
        actual = broker_qty.get(symbol, 0.0)
        ledger = ledger_qty.get(symbol, 0.0)
        difference = actual - ledger
        status = "MATCHED" if abs(difference) <= tolerance else "MISMATCH"
        store.record_reconciliation(symbol, actual, ledger, status)
        store.set_setting(f"symbol_blocked:{symbol}", str(status == "MISMATCH").lower())
        if status == "MISMATCH":
            for strategy in owners.get(symbol, set()):
                mismatched_strategies.add(strategy)
                _disable_strategy_for_reconciliation(store, strategy)
        reports.append({
            "symbol": symbol, "broker_quantity": actual, "ledger_quantity": ledger,
            "difference": difference, "status": status,
            "affected_strategies": ",".join(sorted(owners.get(symbol, set()))),
        })
    for key in store.settings_with_prefix("strategy_reconciliation_disabled:"):
        strategy = key.removeprefix("strategy_reconciliation_disabled:")
        if strategy not in mismatched_strategies:
            _recover_strategy_from_reconciliation(store, strategy)
    store.set_setting("recovery_blocked", str(bool(mismatched_strategies)).lower())
    if mismatched_strategies:
        store.record_incident(
            "LEDGER_MISMATCH", "ERROR",
            f"三账对账发现 {len(mismatched_strategies)} 个策略存在持仓差异，相关策略已暂停",
        )
    else:
        store.recover_incident("LEDGER_MISMATCH")
    return pd.DataFrame(reports)
