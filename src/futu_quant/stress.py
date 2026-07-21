from __future__ import annotations

import re

import pandas as pd

from .option_lifecycle import parse_option_code


OPTION = re.compile(r"\d{6}[CP]\d+$")


def stress_test(positions: pd.DataFrame, quotes: pd.DataFrame, equity: float) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame()
    merged = positions.merge(quotes, on="code", how="left", suffixes=("", "_quote"))
    merged["market_value"] = merged.get("market_val", pd.Series(0, index=merged.index)).astype(float)
    scenarios = [
        ("正股下跌 10%", -0.10, 0.0, 0.0),
        ("正股下跌 20%", -0.20, 0.0, 0.0),
        ("正股下跌 30%", -0.30, 0.0, 0.0),
        ("IV Crush 30%", 0.0, -0.30, 0.0),
        ("IV Crush 70%", 0.0, -0.70, 0.0),
        ("期权到期归零", 0.0, 0.0, 1.0),
        ("价差扩大且无法成交", -0.05, -0.20, 0.0),
    ]
    rows = []
    for name, price_shock, iv_shock, expire in scenarios:
        loss = 0.0
        for _, row in merged.iterrows():
            is_option = bool(OPTION.search(str(row["code"])))
            if not is_option:
                loss += float(row["market_value"]) * price_shock
                continue
            if expire:
                loss -= abs(float(row["market_value"]))
                continue
            qty = float(row.get("qty", 0) or 0)
            multiplier = float(row.get("option_contract_multiplier", 100) or 100)
            spot = float(row.get("stock_owner_price", row.get("last_price", 0)) or 0)
            delta = float(row.get("option_delta", 0) or 0)
            gamma = float(row.get("option_gamma", 0) or 0)
            vega = float(row.get("option_vega", 0) or 0)
            price_move = spot * price_shock
            loss += qty * multiplier * (
                delta * price_move + 0.5 * gamma * price_move**2 + vega * iv_shock
            )
        stressed_equity = equity + loss
        rows.append({
            "场景": name, "估算损益": loss, "压力后权益": stressed_equity,
            "权益损失比例": -loss / equity if equity > 0 else 0,
            "触发急停": bool(equity > 0 and -loss / equity >= 0.05),
        })
    return pd.DataFrame(rows)


JOINT_SCENARIOS = [
    ("温和回撤", -0.05, 15.0, 1, 0.02),
    ("市场急跌与 IV 飙升", -0.10, 40.0, 1, 0.05),
    ("极端跳空", -0.20, 50.0, 3, 0.10),
    ("快速反弹与 IV 回落", 0.10, -20.0, 1, 0.03),
    ("财报后 IV Crush", 0.00, -40.0, 1, 0.03),
    ("横盘一周时间衰减", 0.00, 0.0, 7, 0.02),
    ("流动性冻结", 0.00, 0.0, 0, 0.15),
]


def portfolio_market_snapshot(broker, positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame()
    codes = positions["code"].astype(str).tolist()
    underlyings = {
        contract.underlying for code in codes
        if (contract := parse_option_code(code)) is not None
    }
    snapshots = broker.snapshots(list(dict.fromkeys([*codes, *sorted(underlyings)])))
    if snapshots.empty:
        return snapshots
    price_map = {
        str(row["code"]): float(row.get("last_price", 0) or 0)
        for _, row in snapshots.iterrows()
    }
    result = snapshots[snapshots["code"].isin(codes)].copy()
    result["underlying_price"] = result["code"].map(
        lambda code: price_map.get(parse_option_code(str(code)).underlying, 0)
        if parse_option_code(str(code)) else price_map.get(str(code), 0)
    )
    return result


def joint_stress_test(positions: pd.DataFrame, quotes: pd.DataFrame, equity: float) -> pd.DataFrame:
    """Apply simultaneous spot, IV, time and exit-liquidity shocks."""
    if positions.empty:
        return pd.DataFrame()
    merged = positions.merge(quotes, on="code", how="left", suffixes=("", "_quote"))
    rows = []
    for name, price_shock, iv_points, days, liquidity_haircut in JOINT_SCENARIOS:
        pnl = 0.0
        stock_pnl = option_pnl = liquidity_cost = 0.0
        for _, row in merged.iterrows():
            quantity = float(row.get("qty", 0) or 0)
            contract = parse_option_code(str(row["code"]))
            market_value = abs(float(row.get("market_val", 0) or 0))
            if contract is None:
                contribution = float(row.get("market_val", 0) or 0) * price_shock
                stock_pnl += contribution
                pnl += contribution
                liquidity_cost += market_value * liquidity_haircut
                continue
            multiplier = float(row.get("option_contract_multiplier", 100) or 100)
            spot = float(row.get("underlying_price", 0) or 0)
            delta = float(row.get("option_delta", 0) or 0)
            gamma = float(row.get("option_gamma", 0) or 0)
            vega = float(row.get("option_vega", 0) or 0)
            theta = float(row.get("option_theta", 0) or 0)
            move = spot * price_shock
            contribution = quantity * multiplier * (
                delta * move + 0.5 * gamma * move**2 + vega * iv_points + theta * days
            )
            option_pnl += contribution
            pnl += contribution
            liquidity_cost += market_value * liquidity_haircut
        pnl -= liquidity_cost
        loss_ratio = max(0.0, -pnl / equity) if equity > 0 else 0.0
        rows.append({
            "scenario": name, "price_shock": price_shock, "iv_change_points": iv_points,
            "days_elapsed": days, "liquidity_haircut": liquidity_haircut,
            "stock_pnl": stock_pnl, "option_pnl": option_pnl,
            "liquidity_cost": liquidity_cost, "estimated_pnl": pnl,
            "stressed_equity": equity + pnl, "equity_loss_ratio": loss_ratio,
            "breach": loss_ratio >= 0.05,
        })
    return pd.DataFrame(rows).sort_values("estimated_pnl")


def monitor_portfolio_stress(broker, store) -> pd.DataFrame:
    account = broker.account_info()
    equity = float(account.iloc[0].get("total_assets", 0) or 0) if not account.empty else 0.0
    positions = broker.positions()
    quotes = portfolio_market_snapshot(broker, positions)
    result = joint_stress_test(positions, quotes, equity)
    if not result.empty:
        store.record_portfolio_stress(equity, result.to_dict("records"))
        worst = result.iloc[0]
        store.heartbeat(
            "portfolio_stress", "WARNING" if worst["breach"] else "HEALTHY",
            f"最差场景 {worst['scenario']}，权益损失 {float(worst['equity_loss_ratio']):.1%}",
        )
    else:
        store.heartbeat("portfolio_stress", "HEALTHY", "当前没有持仓")
    return result
