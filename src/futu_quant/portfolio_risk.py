from __future__ import annotations

import os
import re
import json

import pandas as pd


OPTION = re.compile(r"^US\.([A-Z.]+)\d{6}[CP]\d+$")


def underlying_of(symbol: str) -> str:
    match = OPTION.match(symbol)
    return f"US.{match.group(1)}" if match else symbol


def portfolio_exposure(positions: pd.DataFrame, quotes: pd.DataFrame) -> dict:
    if positions.empty:
        return {"delta_dollars": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                "families": {}, "gross": 0.0}
    merged = positions.merge(quotes, on="code", how="left", suffixes=("", "_quote"))
    totals = {"delta_dollars": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
              "families": {}, "gross": 0.0}
    def number(value, default=0.0):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return default if pd.isna(result) else result

    for _, row in merged.iterrows():
        symbol, qty = str(row["code"]), number(row.get("qty", 0))
        price = number(row.get("last_price", row.get("nominal_price", 0)))
        is_option = bool(OPTION.match(symbol))
        multiplier = number(row.get("option_contract_multiplier", 1), 100 if is_option else 1)
        delta = number(row.get("option_delta", 1 if not is_option else 0), 1 if not is_option else 0)
        delta_dollars = qty * multiplier * delta * price
        family = underlying_of(symbol)
        totals["delta_dollars"] += delta_dollars
        totals["families"][family] = totals["families"].get(family, 0.0) + abs(delta_dollars)
        totals["gross"] += abs(qty * multiplier * price)
        for greek in ("gamma", "vega", "theta"):
            totals[greek] += qty * multiplier * number(row.get(f"option_{greek}", 0))
    return totals


def projected_portfolio_allowed(
    broker, symbol: str, side: str, quantity: int, price: float, multiplier: float | None,
    underlying: str | None, store=None, execution_mode: str = "PAPER",
    rebalance_reduction_delta: float = 0.0,
) -> tuple[bool, str, dict]:
    positions = broker.positions()
    if execution_mode == "SHADOW" and store is not None:
        shadow = store.shadow_open_positions()
        if shadow:
            positions = pd.DataFrame([{
                "code": item["symbol"], "qty": item["quantity"],
                "cost_price": item["average_cost"],
            } for item in shadow])
    symbols = positions["code"].tolist() if not positions.empty else []
    quotes = broker.snapshots(symbols) if symbols else pd.DataFrame()
    exposure = portfolio_exposure(positions, quotes)
    info = broker.account_info()
    assets = float(info.iloc[0].get("total_assets", 0) or 0) if not info.empty else 0
    contract_multiplier = multiplier or (100.0 if OPTION.match(symbol) else 1.0)
    signed = 1 if side == "BUY" else -1
    candidate_delta = signed * quantity * contract_multiplier * price
    candidate_greeks = {"gamma": 0.0, "vega": 0.0, "theta": 0.0}
    if OPTION.match(symbol):
        try:
            option = broker.snapshots([symbol]).iloc[0]
            candidate_delta *= float(option.get("option_delta", 0) or 0)
            for greek in candidate_greeks:
                candidate_greeks[greek] = (
                    signed * quantity * contract_multiplier
                    * float(option.get(f"option_{greek}", 0) or 0)
                )
        except Exception:
            return False, "期权 Greeks 行情不可用，禁止增加未知组合风险", exposure
    projected_delta = exposure["delta_dollars"] + candidate_delta
    max_delta = float(os.getenv("RISK_MAX_NET_DELTA_PCT", "0.70"))
    if assets > 0 and abs(projected_delta) > assets * max_delta:
        return False, f"组合净 Delta 金额将超过权益的 {max_delta:.0%}", exposure
    family = underlying or underlying_of(symbol)
    family_value = exposure["families"].get(family, 0) + abs(candidate_delta)
    max_family = float(os.getenv("RISK_MAX_FAMILY_DELTA_PCT", "0.30"))
    if assets > 0 and family_value > assets * max_family:
        return False, f"{family} 正股与期权 Delta 集中度将超过 {max_family:.0%}", exposure
    try:
        clusters = json.loads(os.getenv("RISK_CORRELATION_CLUSTERS", "{}"))
    except json.JSONDecodeError:
        clusters = {}
    correlated = set(clusters.get(family, [family]))
    correlated_value = sum(value for key, value in exposure["families"].items() if key in correlated)
    # Core Satellite sends planned sells first.  During the same rebalance batch,
    # credit that released Delta before judging the replacement BUY.
    correlated_value = max(0.0, correlated_value - max(0.0, float(rebalance_reduction_delta)))
    correlated_value += abs(candidate_delta)
    max_correlated = float(os.getenv("RISK_MAX_CORRELATED_DELTA_PCT", "0.50"))
    if assets > 0 and correlated_value > assets * max_correlated:
        return False, f"高相关标的组合 Delta 将超过权益的 {max_correlated:.0%}", exposure
    for greek, env_key, default in (
        ("gamma", "RISK_MAX_ABS_GAMMA", "500"), ("vega", "RISK_MAX_ABS_VEGA", "50000"),
        ("theta", "RISK_MAX_ABS_THETA", "10000"),
    ):
        projected = exposure[greek] + candidate_greeks[greek]
        exposure[f"projected_{greek}"] = projected
        if abs(projected) > float(os.getenv(env_key, default)):
            return False, f"交易后组合 {greek.title()} 将超过上限", exposure
    return True, "组合 Delta/Greeks 风控通过", exposure
