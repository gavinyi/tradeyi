from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


STRATEGY_NAME = "Core Satellite"
CORE_MAX_WEIGHTS = {"US.SPY": 0.25, "US.VOO": 0.25, "US.QQQ": 0.25}
PROFILES = {
    "conservative": {
        "label": "保守", "max_stock_weight": 0.70, "base_factor": 0.60,
        "core_budget": 0.55, "satellite_budget": 0.15,
        "core_weights": {"US.SPY": 0.35, "US.VOO": 0.35, "US.QQQ": 0.20},
        "satellite_weight": 0.05,
    },
    "balanced": {
        "label": "均衡", "max_stock_weight": 0.80, "base_factor": 0.80,
        "core_budget": 0.65, "satellite_budget": 0.15,
        "core_weights": {"US.SPY": 0.40, "US.VOO": 0.40, "US.QQQ": 0.25},
        "satellite_weight": 0.10,
    },
    "aggressive": {
        "label": "进取", "max_stock_weight": 0.90, "base_factor": 0.90,
        "core_budget": 0.70, "satellite_budget": 0.20,
        "core_weights": {"US.SPY": 0.40, "US.VOO": 0.40, "US.QQQ": 0.30},
        "satellite_weight": 0.15,
    },
}


@dataclass(frozen=True)
class RebalanceOrder:
    symbol: str
    side: str
    quantity: int
    price: float
    current_weight: float
    target_weight: float
    reason: str
    desired_quantity: int = 0
    desired_notional: float = 0.0
    order_notional: float = 0.0
    execution_slice: int = 1
    total_slices: int = 1
    throttle_reason: str = ""


def rebalance_due(frame: pd.DataFrame, frequency: str) -> tuple[bool, str]:
    """Decide whether live planning is due; risk-state changes bypass the calendar."""
    if frequency == "daily":
        return True, "daily schedule"
    close = frame["close"].astype(float)
    if len(close) < 201:
        raise ValueError("Core Satellite schedule requires at least 201 daily bars")
    current_risk = close.iloc[-1] >= close.iloc[-200:].mean()
    previous_risk = close.iloc[-2] >= close.iloc[-201:-1].mean()
    if current_risk != previous_risk:
        return True, "SPY risk state changed"
    day = pd.Timestamp(frame["time_key"].iloc[-1])
    if frequency == "weekly":
        return day.weekday() == 4, "weekly Friday schedule"
    if frequency == "monthly":
        return (day + pd.Timedelta(days=7)).month != day.month, "month-end week schedule"
    raise ValueError(f"Unknown rebalance frequency: {frequency}")


def indicators(frame: pd.DataFrame) -> dict[str, float]:
    close = frame["close"].astype(float)
    if len(close) < 200:
        raise ValueError("Core Satellite requires at least 200 daily bars")
    price = float(close.iloc[-1])
    sma20 = float(close.iloc[-20:].mean())
    sma200 = float(close.iloc[-200:].mean())
    high60 = float(close.iloc[-60:].max())
    returns = close.pct_change().dropna()
    realized_vol20 = float(returns.iloc[-20:].std()) if len(returns) >= 20 else 0.0
    return {
        "price": price,
        "sma20": sma20,
        "sma200": sma200,
        "drawdown60": price / high60 - 1,
        "extension200": price / sma200 - 1,
        "realized_vol20": realized_vol20,
    }


def allocation_factor(
    stats: dict[str, float], kind: str, risk_on: bool, base_factor: float = 0.60
) -> tuple[float, str]:
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    if not risk_on:
        factor = 0.20 if kind == "core" else (0.20 if above_long and above_short else 0.0)
        return factor, "SPY below SMA200; defensive allocation"
    if not above_long:
        factor = 0.25 if above_short else (0.20 if kind == "core" else 0.0)
        return factor, "below SMA200; waiting for right-side confirmation"

    factor, reason = base_factor, "above SMA200; base allocation"
    drawdown = stats["drawdown60"]
    if drawdown <= -0.25:
        factor, reason = 1.00, "60-day drawdown >= 25%; fourth tranche"
    elif drawdown <= -0.16:
        factor, reason = 0.90, "60-day drawdown >= 16%; third tranche"
    elif drawdown <= -0.08:
        factor, reason = 0.75, "60-day drawdown >= 8%; second tranche"

    extension = stats["extension200"]
    if extension >= 0.40:
        factor, reason = min(factor, 0.55), "price >= 40% above SMA200; trim"
    elif extension >= 0.25:
        factor, reason = min(factor, 0.75), "price >= 25% above SMA200; trim"
    return factor, reason


def max_weights(
    symbols: tuple[str, ...], max_stock_weight: float, profile: str = "conservative",
    core_budget: float | None = None, satellite_budget: float | None = None,
) -> dict[str, float]:
    parameters = PROFILES[profile]
    core = [symbol for symbol in symbols if symbol in CORE_MAX_WEIGHTS]
    satellites = [symbol for symbol in symbols if symbol not in CORE_MAX_WEIGHTS]
    core_budget = float(parameters["core_budget"]) if core_budget is None else float(core_budget)
    satellite_budget = (
        float(parameters["satellite_budget"])
        if satellite_budget is None else float(satellite_budget)
    )
    core_cap = min(max_stock_weight, max(0.0, core_budget))
    satellite_cap = max(0.0, min(
        max_stock_weight - core_cap, max(0.0, satellite_budget),
    ))
    weights = {symbol: parameters["core_weights"][symbol] for symbol in core}
    core_total = sum(weights.values())
    if core_total > core_cap and core_total > 0:
        scale = core_cap / core_total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    remaining = max(0.0, min(satellite_cap, max_stock_weight - sum(weights.values())))
    satellite_weight = min(parameters["satellite_weight"], remaining / len(satellites)) if satellites else 0.0
    weights.update({symbol: satellite_weight for symbol in satellites})
    return weights


def trade_participation(
    spy_stats: dict[str, float],
    max_single_rebalance_pct: float = 1.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
) -> tuple[float, str]:
    base = min(1.0, max(0.0, float(max_single_rebalance_pct)))
    realized_vol = float(spy_stats.get("realized_vol20", 0.0))
    if realized_vol >= high_volatility_threshold:
        factor = min(base, max(0.0, float(high_volatility_trade_factor)))
        return factor, (
            f"市场20日波动率 {realized_vol:.2%} 高于阈值 "
            f"{high_volatility_threshold:.2%}，单次调仓降速到 {factor:.0%}"
        )
    return base, f"单次调仓比例 {base:.0%}"


def build_rebalance_plan(
    frames: dict[str, pd.DataFrame],
    positions: pd.DataFrame,
    account_value: float,
    symbols: tuple[str, ...],
    max_stock_weight: float = 0.70,
    min_trade_usd: float = 500.0,
    profile: str = "conservative",
    weight_tolerance: float = 0.0,
    cash_buffer: float = 0.0,
    max_single_rebalance_pct: float = 1.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
    max_order_notional: float = 0.0,
    core_budget: float | None = None,
    satellite_budget: float | None = None,
) -> tuple[list[RebalanceOrder], bool]:
    if account_value <= 0:
        raise ValueError("account value must be positive")
    spy = indicators(frames["US.SPY"])
    risk_on = spy["price"] >= spy["sma200"]
    parameters = PROFILES[profile]
    max_stock_weight = min(max_stock_weight, parameters["max_stock_weight"])
    cash_buffer = min(0.50, max(0.0, float(cash_buffer)))
    max_stock_weight = max(0.0, max_stock_weight - cash_buffer)
    maximums = max_weights(symbols, max_stock_weight, profile, core_budget, satellite_budget)
    participation, throttle_reason = trade_participation(
        spy, max_single_rebalance_pct, high_volatility_threshold,
        high_volatility_trade_factor,
    )
    position_map = {}
    total_market_value = 0.0
    if not positions.empty:
        for _, row in positions.iterrows():
            market_value = float(row.get("market_val", row.get("val", 0.0)) or 0.0)
            total_market_value += max(0.0, market_value)
            position_map[str(row["code"])] = {
                "qty": float(row.get("qty", 0.0)),
                "can_sell_qty": float(row.get("can_sell_qty", row.get("qty", 0.0))),
            }

    candidates = []
    for symbol in symbols:
        stats = indicators(frames[symbol])
        kind = "core" if symbol in CORE_MAX_WEIGHTS else "satellite"
        factor, reason = allocation_factor(stats, kind, risk_on, parameters["base_factor"])
        target_weight = maximums[symbol] * factor
        held = position_map.get(symbol, {"qty": 0.0, "can_sell_qty": 0.0})
        current_value = held["qty"] * stats["price"]
        current_weight = current_value / account_value
        delta_value = account_value * target_weight - current_value
        candidates.append((symbol, stats, held, current_weight, target_weight, delta_value, reason))

    # Existing managed or unmanaged holdings count toward the hard 70% stock cap.
    buy_budget = max(0.0, account_value * max_stock_weight - total_market_value)
    requested_buys = sum(max(0.0, item[5]) for item in candidates)
    buy_scale = min(1.0, buy_budget / requested_buys) if requested_buys else 1.0

    orders = []
    for symbol, stats, held, current_weight, target_weight, delta_value, reason in candidates:
        if abs(target_weight - current_weight) < weight_tolerance:
            continue
        if delta_value > 0:
            delta_value *= buy_scale
        if abs(delta_value) < min_trade_usd:
            continue
        desired_quantity = int(abs(delta_value) // stats["price"])
        if desired_quantity < 1:
            continue
        quantity = max(1, int(desired_quantity * participation))
        side = "BUY" if delta_value > 0 else "SELL"
        if side == "SELL":
            quantity = min(quantity, int(held["can_sell_qty"]))
        desired_notional = desired_quantity * stats["price"]
        if max_order_notional > 0 and quantity * stats["price"] > max_order_notional:
            quantity = max(1, int(max_order_notional // stats["price"]))
        if quantity < 1:
            continue
        total_slices = max(1, int((desired_notional + max_order_notional - 0.01) // max_order_notional)) if max_order_notional > 0 else 1
        orders.append(
            RebalanceOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=stats["price"],
                current_weight=current_weight,
                target_weight=target_weight,
                reason=reason,
                desired_quantity=desired_quantity,
                desired_notional=desired_notional,
                order_notional=quantity * stats["price"],
                execution_slice=1,
                total_slices=total_slices,
                throttle_reason=throttle_reason,
            )
        )
    return orders, risk_on
