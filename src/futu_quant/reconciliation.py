from __future__ import annotations

import pandas as pd


def futu_reconciliation_symbol(symbol: str) -> bool:
    """Futu ledger reconciliation only covers US stocks/options."""
    return str(symbol).startswith("US.")


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
    for symbol in sorted(current_symbols):
        actual = broker_qty.get(symbol, 0.0)
        ledger = ledger_qty.get(symbol, 0.0)
        difference = actual - ledger
        status = "MATCHED" if abs(difference) <= tolerance else "MISMATCH"
        store.record_reconciliation(symbol, actual, ledger, status)
        store.set_setting(f"symbol_blocked:{symbol}", str(status == "MISMATCH").lower())
        if status == "MISMATCH":
            for strategy in owners.get(symbol, set()):
                store.set_setting(f"strategy_enabled:{strategy}", "false")
        reports.append({
            "symbol": symbol, "broker_quantity": actual, "ledger_quantity": ledger,
            "difference": difference, "status": status,
            "affected_strategies": ",".join(sorted(owners.get(symbol, set()))),
        })
    return pd.DataFrame(reports)
