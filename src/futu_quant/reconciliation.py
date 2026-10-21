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


def _disable_strategy_for_reconciliation(store, strategy_id: str, prefix: str = "") -> None:
    restore_key = f"strategy_reconciliation_restore:{prefix}{strategy_id}"
    if not store.get_setting(restore_key, ""):
        restore = {
            key: store.get_setting(key, "")
            for key in _strategy_enable_keys(strategy_id)
        }
        store.set_setting(restore_key, json.dumps(restore, ensure_ascii=False))
    store.set_setting(f"strategy_reconciliation_disabled:{prefix}{strategy_id}", "true")
    if prefix:
        store.set_setting(f"strategy_enabled:{prefix}{strategy_id}", "false")
    else:
        for key in _strategy_enable_keys(strategy_id):
            store.set_setting(key, "false")


def _recover_strategy_from_reconciliation(store, strategy_id: str, prefix: str = "") -> None:
    marker = f"strategy_reconciliation_disabled:{prefix}{strategy_id}"
    if store.get_setting(marker, "false").lower() != "true":
        return
    restore_raw = store.get_setting(f"strategy_reconciliation_restore:{prefix}{strategy_id}", "")
    try:
        restore = json.loads(restore_raw) if restore_raw else {}
    except json.JSONDecodeError:
        restore = {}
    if prefix:
        previous = str(restore.get(f"strategy_enabled:{prefix}{strategy_id}", "true") or "true")
        store.set_setting(f"strategy_enabled:{prefix}{strategy_id}", previous)
    else:
        for key in _strategy_enable_keys(strategy_id):
            previous = str(restore.get(key, "true") or "true")
            store.set_setting(key, previous)
    store.set_setting(marker, "false")
    store.set_setting(f"strategy_reconciliation_restore:{prefix}{strategy_id}", "")
    store.audit(
        "RECONCILIATION_STRATEGY_AUTO_RECOVERED",
        {"strategy": strategy_id, "restored_settings": restore, "prefix": prefix},
        strategy_id,
    )


def reconcile_ledgers(
    store,
    broker,
    tolerance: float = 1e-6,
    tenant_id: str | None = None,
) -> pd.DataFrame:
    broker_env = str(getattr(broker, "trading_env", "SIMULATE")).upper()
    broker_account_id = getattr(broker, "account_id", None)
    positions = broker.positions()
    if not positions.empty and "code" in positions.columns:
        positions = positions[positions["code"].astype(str).map(futu_reconciliation_symbol)].copy()
    broker_qty = (
        positions.groupby("code")["qty"].sum().astype(float).to_dict()
        if not positions.empty else {}
    )
    virtual_rows = [
        row for row in store.virtual_positions(env=broker_env, account_id=broker_account_id, tenant_id=tenant_id)
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
    acc_prefix = f"{broker_account_id}:" if broker_account_id else ""
    tenant_prefix = f"{tenant_id}:" if tenant_id else (f"{broker_account_id}:" if broker_account_id else "")
    for key in store.settings_with_prefix(f"symbol_blocked:{acc_prefix}"):
        symbol = key.removeprefix(f"symbol_blocked:{acc_prefix}")
        if symbol not in current_symbols or not futu_reconciliation_symbol(symbol):
            store.set_setting(key, "false")
    mismatched_strategies: set[str] = set()
    for symbol in sorted(current_symbols):
        actual = broker_qty.get(symbol, 0.0)
        ledger = ledger_qty.get(symbol, 0.0)
        difference = actual - ledger
        strategy_owners = owners.get(symbol, set())

        # 用户原生手工存量底仓豁免保护 (例如实盘存量 8 股 NVDA、4.43 股 GLD):
        # 未被任何量化策略认领的外部长线底仓，属于安全受控资产，绝对不判定为对账丢单差异！
        if not strategy_owners and abs(ledger) <= tolerance:
            store.record_reconciliation(symbol, actual, ledger, "MATCHED")
            store.set_setting(f"symbol_blocked:{acc_prefix}{symbol}", "false")
            reports.append({
                "symbol": symbol, "broker_quantity": actual, "ledger_quantity": ledger,
                "difference": 0.0, "status": "MATCHED",
                "affected_strategies": "USER_NATIVE_HOLDING",
            })
            continue

        status = "MATCHED" if abs(difference) <= tolerance else "MISMATCH"
        store.record_reconciliation(symbol, actual, ledger, status)
        store.set_setting(f"symbol_blocked:{acc_prefix}{symbol}", str(status == "MISMATCH").lower())
        if status == "MISMATCH":
            for strategy in strategy_owners:
                mismatched_strategies.add(strategy)
                _disable_strategy_for_reconciliation(store, strategy, prefix=tenant_prefix)
        reports.append({
            "symbol": symbol, "broker_quantity": actual, "ledger_quantity": ledger,
            "difference": difference, "status": status,
            "affected_strategies": ",".join(sorted(strategy_owners)),
        })
    scan_prefix = f"strategy_reconciliation_disabled:{tenant_prefix}"
    for key in store.settings_with_prefix(scan_prefix):
        strategy = key.removeprefix(scan_prefix)
        if strategy not in mismatched_strategies:
            _recover_strategy_from_reconciliation(store, strategy, prefix=tenant_prefix)
    store.set_setting(f"recovery_blocked:{broker_account_id}" if broker_account_id else "recovery_blocked", str(bool(mismatched_strategies)).lower())
    if mismatched_strategies:
        store.record_incident(
            "LEDGER_MISMATCH", "ERROR",
            f"三账对账发现 {len(mismatched_strategies)} 个策略存在持仓差异，相关策略已暂停",
        )
    else:
        store.recover_incident("LEDGER_MISMATCH")
    cols = ["symbol", "broker_quantity", "ledger_quantity", "difference", "status", "affected_strategies"]
    return pd.DataFrame(reports, columns=cols)
