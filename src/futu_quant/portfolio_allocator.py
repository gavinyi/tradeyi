from __future__ import annotations

import os

import pandas as pd

from .option_lifecycle import parse_option_code


DEFAULT_BUDGETS = [
    {"strategy_id": "core_satellite", "capital_ratio": 0.65, "max_loss_ratio": 0.15,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "earnings_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
     "option_premium_ratio": 0.10, "enabled": True},
    {"strategy_id": "volatility_long_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
     "option_premium_ratio": 0.10, "enabled": True},
    {"strategy_id": "earnings_gamma_manual", "capital_ratio": 0.05, "max_loss_ratio": 0.02,
     "option_premium_ratio": 0.05, "enabled": True},
    {"strategy_id": "SMA Cross", "capital_ratio": 0.02, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "RSI Mean Reversion", "capital_ratio": 0.015, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "20 Day Breakout", "capital_ratio": 0.015, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
]


def ensure_default_budgets(store) -> list[dict]:
    current = store.strategy_risk_budgets()
    if not current:
        store.save_strategy_risk_budgets(DEFAULT_BUDGETS)
        store.set_setting("strategy_budget_schema_version", "2")
        current = store.strategy_risk_budgets()
    elif int(store.get_setting("strategy_budget_schema_version", "1") or 1) < 2:
        migrated = []
        for item in current:
            row = dict(item)
            if row["strategy_id"] == "core_satellite" and abs(float(row["capital_ratio"]) - 0.55) < 1e-9:
                row["capital_ratio"] = 0.65
            migrated.append(row)
        store.save_strategy_risk_budgets(migrated)
        store.set_setting("strategy_budget_schema_version", "2")
        current = store.strategy_risk_budgets()
    return current


def strategy_budget_usage(store, broker) -> tuple[pd.DataFrame, float]:
    budgets = ensure_default_budgets(store)
    try:
        account = broker.account_info()
        positions = broker.positions()
    except Exception:
        cached_account = store.broker_snapshot("account_info") or {}
        cached_positions = store.broker_snapshot("positions") or {}
        account = pd.DataFrame(cached_account.get("payload", []))
        positions = pd.DataFrame(cached_positions.get("payload", []))
    equity = float(account.iloc[0].get("total_assets", 0) or 0) if not account.empty else 0.0
    symbols = positions["code"].astype(str).tolist() if not positions.empty else []
    quotes = broker.snapshots(symbols) if symbols else pd.DataFrame()
    quote_map = {
        str(row["code"]): float(row.get("last_price", 0) or 0)
        for _, row in quotes.iterrows()
    } if not quotes.empty else {}
    position_map = {
        str(row["code"]): row for _, row in positions.iterrows()
    } if not positions.empty else {}
    usage = {item["strategy_id"]: {"capital_used": 0.0, "loss_used": 0.0,
                                           "option_premium_used": 0.0} for item in budgets}
    # 租户隔离铁律：预算账本必须与当前 broker 同环境同账户，严禁把模拟沙盒历史账本计入实盘配额（反之亦然）
    broker_env = str(getattr(broker, "trading_env", "") or "").upper()
    broker_account = getattr(broker, "account_id", None)
    ledger_positions = store.virtual_positions(
        env=broker_env or None,
        account_id=broker_account,
    )
    for item in ledger_positions:
        strategy, symbol = item["strategy_id"], item["symbol"]
        if strategy not in usage:
            continue
        position = position_map.get(symbol)
        fallback = float(position.get("nominal_price", position.get("cost_price", 0)) or 0) \
            if position is not None else float(item.get("average_cost", 0) or 0)
        price = quote_map.get(symbol, fallback)
        quantity = abs(float(item["quantity"]))
        is_option = parse_option_code(symbol) is not None
        value = quantity * price * (100 if is_option else 1)
        usage[strategy]["capital_used"] += value
        usage[strategy]["loss_used"] += value if is_option else value * float(
            os.getenv("STOCK_STRESS_LOSS_RATIO", "0.10")
        )
        if is_option:
            usage[strategy]["option_premium_used"] += value
    rows = []
    for budget in budgets:
        values = usage[budget["strategy_id"]]
        rows.append({
            **budget, **values,
            "capital_limit": equity * float(budget["capital_ratio"]),
            "loss_limit": equity * float(budget["max_loss_ratio"]),
            "option_premium_limit": equity * float(budget["option_premium_ratio"]),
            "capital_utilization": values["capital_used"] / (equity * float(budget["capital_ratio"]))
            if equity > 0 and budget["capital_ratio"] else 0,
            "loss_utilization": values["loss_used"] / (equity * float(budget["max_loss_ratio"]))
            if equity > 0 and budget["max_loss_ratio"] else 0,
        })
    return pd.DataFrame(rows), equity


def strategy_budget_allowed(
    store, broker, strategy_id: str | None, symbol: str, side: str,
    quantity: int, price: float, multiplier: float,
) -> tuple[bool, str, dict]:
    if side == "SELL" or not strategy_id:
        return True, "减仓不占用新增策略预算", {}
    budget = store.strategy_risk_budget(strategy_id)
    if budget is None:
        return True, "未配置三维策略预算，沿用原有资金配额", {}
    if not budget["enabled"]:
        return False, f"{strategy_id} 已在组合资金分配器中停用", {}
    usage, equity = strategy_budget_usage(store, broker)
    current = usage[usage["strategy_id"] == strategy_id]
    if current.empty or equity <= 0:
        return False, "无法计算策略预算使用率", {}
    row = current.iloc[0]
    notional = quantity * price * multiplier
    is_option = parse_option_code(symbol) is not None
    projected = {
        "capital": float(row["capital_used"]) + notional,
        "loss": float(row["loss_used"]) + (
            notional if is_option else notional * float(os.getenv("STOCK_STRESS_LOSS_RATIO", "0.10"))
        ),
        "option_premium": float(row["option_premium_used"]) + (notional if is_option else 0),
        "capital_limit": float(row["capital_limit"]),
        "loss_limit": float(row["loss_limit"]),
        "option_premium_limit": float(row["option_premium_limit"]),
    }
    if projected["capital"] > projected["capital_limit"]:
        return False, f"{strategy_id} 资金占用将超过组合配额", projected
    if projected["loss"] > projected["loss_limit"]:
        return False, f"{strategy_id} 压力损失预算将超过上限", projected
    if is_option and projected["option_premium"] > projected["option_premium_limit"]:
        return False, f"{strategy_id} 期权权利金预算将超过上限", projected
    return True, "策略资金、损失与期权权利金预算通过", projected
