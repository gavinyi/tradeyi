from __future__ import annotations

import os
import json

import pandas as pd


PARAMETERS = {
    "RISK_MAX_ORDER_NOTIONAL", "RISK_MAX_DAILY_NOTIONAL", "EXEC_MAX_SPREAD_PCT",
    "EXEC_MAX_SLIPPAGE_PCT",
}


def generate_recommendations(store) -> list[dict]:
    intents = pd.DataFrame(store.intents(limit=20_000))
    if intents.empty:
        return []
    multiplier = intents["symbol"].astype(str).str.contains(r"\d{6}[CP]\d+$", regex=True).map({True: 100, False: 1})
    intents["notional"] = intents["quantity"] * intents["limit_price"] * multiplier
    suggestions = []
    order_current = float(os.getenv("RISK_MAX_ORDER_NOTIONAL", "20000"))
    order_value = max(100.0, float(intents["notional"].quantile(0.95) * 1.5))
    suggestions.append(("RISK_MAX_ORDER_NOTIONAL", order_current, order_value,
                        "历史订单名义金额95分位数的1.5倍"))
    intents["day"] = pd.to_datetime(intents["created_at"], utc=True).dt.date
    daily_current = float(os.getenv("RISK_MAX_DAILY_NOTIONAL", "100000"))
    daily_value = max(order_value, float(intents.groupby("day")["notional"].sum().quantile(0.95) * 1.25))
    suggestions.append(("RISK_MAX_DAILY_NOTIONAL", daily_current, daily_value,
                        "历史每日名义金额95分位数的1.25倍"))
    filled = intents[intents["filled_quantity"] > 0].copy()
    if not filled.empty:
        reference = filled["limit_price"].replace(0, pd.NA)
        adverse = ((filled["average_fill_price"] / reference - 1) *
                   filled["side"].map({"BUY": 1, "SELL": -1})).abs().dropna()
        slip_current = float(os.getenv("EXEC_MAX_SLIPPAGE_PCT", "0.01"))
        slip_value = max(0.001, min(0.05, float(adverse.quantile(0.95) * 1.5)))
        suggestions.append(("EXEC_MAX_SLIPPAGE_PCT", slip_current, slip_value,
                            "历史不利滑点95分位数的1.5倍"))
    spreads = []
    for snapshot in store.decision_snapshots(limit=5000):
        try:
            quote = json.loads(snapshot["snapshot_json"]).get("quote", [{}])[0]
            bid, ask = float(quote.get("bid_price", 0) or 0), float(quote.get("ask_price", 0) or 0)
            if bid > 0 and ask >= bid:
                spreads.append((ask - bid) / ((ask + bid) / 2))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    if spreads:
        spread_current = float(os.getenv("EXEC_MAX_SPREAD_PCT", "0.20"))
        spread_value = max(0.001, min(0.50, float(pd.Series(spreads).quantile(0.95) * 1.25)))
        suggestions.append(("EXEC_MAX_SPREAD_PCT", spread_current, spread_value,
                            "决策快照买卖价差95分位数的1.25倍"))
    result = []
    for parameter, current, recommended, reason in suggestions:
        store.add_risk_recommendation(parameter, current, recommended, reason)
        result.append({"parameter": parameter, "current": current,
                       "recommended": recommended, "reason": reason})
    return result


def recommendation_changes(row: dict) -> dict[str, str]:
    parameter = str(row["parameter"])
    if parameter not in PARAMETERS:
        raise ValueError("不允许自动应用该参数")
    return {parameter: str(float(row["recommended_value"]))}
