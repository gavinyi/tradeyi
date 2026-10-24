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


def _prefixed_enable_keys(strategy_id: str, prefix: str) -> tuple[str, ...]:
    """带前缀变体实际会写的所有 strategy_enabled 键。

    写侧用小写策略 ID（``core_satellite``），而有些调用方写显示名
    （``Core Satellite``）；快照必须把两种拼法都记下来，否则恢复时读不到原值。
    """
    names = [strategy_id]
    plugin = BY_ID.get(strategy_id)
    if plugin and plugin.service_name and plugin.service_name != strategy_id:
        names.append(plugin.service_name)
    return tuple(dict.fromkeys(f"strategy_enabled:{prefix}{name}" for name in names))


def _disable_strategy_for_reconciliation(store, strategy_id: str, prefix: str = "") -> None:
    restore_key = f"strategy_reconciliation_restore:{prefix}{strategy_id}"
    if not store.get_setting(restore_key, ""):
        keys = _prefixed_enable_keys(strategy_id, prefix) if prefix else _strategy_enable_keys(strategy_id)
        # 快照必须覆盖本变体**实际会写的那些键**，否则恢复时读不到原值、
        # 只能回落 "true"，把用户主动停用的策略静默改回启用。
        restore = {key: store.get_setting(key, "") for key in keys}
        store.set_setting(restore_key, json.dumps(restore, ensure_ascii=False))
    store.set_setting(f"strategy_reconciliation_disabled:{prefix}{strategy_id}", "true")
    if prefix:
        # 写侧必须覆盖读侧认的**每一种拼法**：读侧显示名优先、蛇形兜底（先取到非空即止），
        # 若这里只写蛇形键，那么只要 {prefix}Core Satellite 存在且为 "true"，
        # 暂停写进蛇形键的 "false" 就永远读不到 —— 暂停静默失效。
        for key in _prefixed_enable_keys(strategy_id, prefix):
            store.set_setting(key, "false")
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
    keys = _prefixed_enable_keys(strategy_id, prefix) if prefix else _strategy_enable_keys(strategy_id)
    restored_keys: dict[str, str] = {}
    removed_keys: list[str] = []
    for key in keys:
        # 只还原「仍然是本次暂停所设值」的键。若暂停期间有人手工改过这个键，
        # 说明那是人的明确意图，对账恢复不该覆盖它。
        #
        # 已知限制（P2）：这个保护分辨不出「暂停期间有人手工**停用**」——因为暂停
        # 自己写的就是 "false"，两者取值相同。要区分只能比对键的 updated_at 与暂停
        # 时刻，本次不做，勿把这条当作完备保护。
        if store.get_setting(key, "") != "false":
            continue
        snapshot_value = restore.get(key, "")
        if snapshot_value:
            store.set_setting(key, snapshot_value)
            restored_keys[key] = snapshot_value
        else:
            # 快照记的是空串 = pause 前该键**不存在**。必须还原成「不存在」而不是
            # 写一个空串：`get_setting(key, default)` 只在键缺失时才回落默认值，
            # 写空串会把全局键的 configured_env 默认值短路掉，等于凭空改变启用状态。
            store.delete_setting(key)
            removed_keys.append(key)
    store.set_setting(marker, "false")
    store.set_setting(f"strategy_reconciliation_restore:{prefix}{strategy_id}", "")
    store.audit(
        "RECONCILIATION_STRATEGY_AUTO_RECOVERED",
        {
            "strategy": strategy_id,
            "prefix": prefix,
            "restored_settings": restore,
            "restored_keys": restored_keys,
            "removed_keys_absent_before_pause": removed_keys,
        },
        strategy_id,
     actor="service:reconciliation")


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
    # 恢复扫描必须覆盖全部历史前缀：租户前缀（现行）、账户前缀（旧版）、无前缀（最旧）。
    # 只扫现行前缀会让旧前缀键永久孤儿化 —— 账本已平，但 risk.py 仍会读到残留的
    # `strategy_reconciliation_disabled:{account}:{strategy}=true`，策略被永久阻断
    # （生产实测：偏移写入后账户前缀键仍残留为 true，core_satellite 开不出新仓）。
    recovery_prefixes: list[str] = []
    for candidate in (tenant_prefix, acc_prefix):
        if candidate and candidate not in recovery_prefixes:
            recovery_prefixes.append(candidate)

    recovered: set[str] = set()
    disabled_marker = "strategy_reconciliation_disabled:"
    for key in store.settings_with_prefix(disabled_marker):
        marker = key.removeprefix(disabled_marker)
        # 只认两种形态：无前缀变体（marker 不含冒号）与「已知前缀 + 策略名」。
        # 其余一律跳过 —— 那是**别家租户**的键（如 futu_paper_sandbox），
        # 本租户的对账无权清理，否则会把别人真实的对账锁一并清掉。
        strategy, matched_prefix = "", None
        if ":" not in marker:
            strategy, matched_prefix = marker, ""
        else:
            for prefix in recovery_prefixes:
                if marker.startswith(prefix):
                    strategy, matched_prefix = marker[len(prefix):], prefix
                    break
        if not strategy or matched_prefix is None:
            continue
        if strategy in recovered or strategy in mismatched_strategies:
            continue
        _recover_strategy_from_reconciliation(store, strategy, prefix=matched_prefix)
        recovered.add(strategy)
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
