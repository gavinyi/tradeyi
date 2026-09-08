from __future__ import annotations

import hashlib

import pandas as pd

from .gamma_risk import market_number


STRUCTURE_LABELS = {
    "LONG_OPTION": "单腿买方期权",
    "CALL_DEBIT_SPREAD": "认购借方价差",
    "PUT_DEBIT_SPREAD": "认沽借方价差",
    "CALL_BACKSPREAD": "认购比例反向价差",
    "PUT_BACKSPREAD": "认沽比例反向价差",
}


def automatic_combo_id(strategy_id: str, campaign: dict, cycle: int = 0) -> str:
    identity = (
        f"{strategy_id}|{campaign.get('campaign_id', campaign.get('code', ''))}|"
        f"{campaign.get('event_date', '')}|{campaign.get('structure', '')}|{cycle}"
    )
    return "auto-" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _leg(row: pd.Series, side: str, ratio: int, role: str) -> dict:
    return {
        "code": str(row["code"]), "side": side, "ratio": int(ratio),
        "role": role,
    }


def _package(
    structure: str,
    legs: list[tuple[pd.Series, str, int, str]],
    *,
    minimum_expected_return: float,
    premium_budget: float,
) -> dict[str, object]:
    entry_debit = 0.0
    exit_value = 0.0
    net_gamma = 0.0
    worst_spread = 0.0
    minimum_oi = float("inf")
    minimum_volume = float("inf")
    maximum_iv_percentile = 0.0
    maximum_iv_rv = 0.0
    iv_rv_limit = float("inf")
    strikes: list[float] = []
    normalized_legs = []
    for row, side, ratio, role in legs:
        sign = 1 if side == "BUY" else -1
        entry_price = market_number(row.get("ask_price")) if sign > 0 else market_number(row.get("bid_price"))
        exit_price = market_number(row.get("expected_exit_bid")) if sign > 0 else market_number(row.get("expected_exit_ask"))
        entry_debit += sign * ratio * entry_price
        exit_value += sign * ratio * exit_price
        net_gamma += sign * ratio * abs(market_number(row.get("option_gamma")))
        worst_spread = max(worst_spread, market_number(row.get("spread_pct"), 100))
        minimum_oi = min(minimum_oi, market_number(row.get("option_open_interest")))
        minimum_volume = min(minimum_volume, market_number(row.get("volume")))
        maximum_iv_percentile = max(
            maximum_iv_percentile, market_number(row.get("iv_history_percentile"), 1)
        )
        maximum_iv_rv = max(maximum_iv_rv, market_number(row.get("iv_rv"), 99))
        iv_rv_limit = min(iv_rv_limit, market_number(row.get("iv_rv_limit"), 1.5))
        strikes.append(market_number(row.get("strike_price")))
        normalized_legs.append(_leg(row, side, ratio, role))
    width = max(strikes) - min(strikes) if strikes else 0.0
    if "BACKSPREAD" in structure:
        risk_per_share = max(0.01, width + entry_debit)
    else:
        risk_per_share = max(0.01, entry_debit)
    expected_return = (exit_value - entry_debit) / risk_per_share
    max_loss = risk_per_share * 100
    eligible = bool(
        entry_debit > 0
        and max_loss <= premium_budget
        and expected_return >= minimum_expected_return
        and net_gamma > 0
        and worst_spread <= 6.0
        and minimum_oi >= 800
        and minimum_volume >= 10
        and maximum_iv_percentile <= .75
        and maximum_iv_rv <= iv_rv_limit
    )
    reasons = []
    if entry_debit <= 0:
        reasons.append("当前仅自动执行净借记结构")
    if max_loss > premium_budget:
        reasons.append(f"最大风险 ${max_loss:,.0f} 超过预算 ${premium_budget:,.0f}")
    if expected_return < minimum_expected_return:
        reasons.append(
            f"短周期净期望收益 {expected_return:.2%} 低于 {minimum_expected_return:.2%}"
        )
    if net_gamma <= 0:
        reasons.append("组合净 Gamma 不为正")
    if worst_spread > 6:
        reasons.append(f"最差腿价差 {worst_spread:.1f}% 超过 6%")
    if minimum_oi < 800:
        reasons.append(f"最小未平仓量 {minimum_oi:.0f} 低于 800")
    if minimum_volume < 10:
        reasons.append(f"最小成交量 {minimum_volume:.0f} 低于 10")
    if maximum_iv_percentile > .75:
        reasons.append(f"最高腿 IV 百分位 {maximum_iv_percentile:.0%} 超过 75%")
    if maximum_iv_rv > iv_rv_limit:
        reasons.append(f"最高腿 IV/RV {maximum_iv_rv:.2f} 超过 {iv_rv_limit:.2f}")
    primary = next(row for row, side, _, _ in legs if side == "BUY")
    return {
        "code": str(primary["code"]),
        "option_type": str(primary["option_type"]),
        "structure": structure,
        "structure_label": STRUCTURE_LABELS[structure],
        "legs": normalized_legs,
        "structure_net_debit": float(entry_debit),
        "structure_max_loss": float(max_loss),
        "structure_expected_return": float(expected_return),
        "model_expected_return": float(expected_return),
        "estimated_profit_probability": float(primary.get("estimated_profit_probability", 0)),
        "net_gamma": float(net_gamma),
        "spread_pct": float(worst_spread),
        "option_open_interest": float(minimum_oi),
        "volume": float(minimum_volume),
        "iv_history_percentile": float(maximum_iv_percentile),
        "iv_rv": float(maximum_iv_rv),
        "iv_rv_limit": float(iv_rv_limit),
        "volatility_eligible": eligible,
        "volatility_filter_reason": "；".join(reasons) or "全部结构条件通过",
        "volatility_quality_score": float(primary.get("volatility_quality_score", 0)),
        "primary_row": primary.to_dict(),
    }


def build_volatility_structure_candidates(
    candidates: pd.DataFrame,
    direction: str,
    *,
    minimum_expected_return: float,
    premium_budget: float,
) -> pd.DataFrame:
    """Route a directional signal across single-leg and defined-risk expressions."""
    if candidates.empty:
        return pd.DataFrame()
    direction = str(direction).upper()
    frame = candidates[
        candidates["option_type"].astype(str).str.upper() == direction
    ].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["abs_delta"] = pd.to_numeric(frame["option_delta"], errors="coerce").abs()
    frame["strike_numeric"] = pd.to_numeric(frame["strike_price"], errors="coerce")
    rows: list[dict[str, object]] = []

    for _, row in frame.iterrows():
        single = row.to_dict()
        single.update({
            "structure": "LONG_OPTION",
            "structure_label": STRUCTURE_LABELS["LONG_OPTION"],
            "legs": [_leg(row, "BUY", 1, "PROTECTIVE")],
            "structure_net_debit": market_number(row.get("ask_price")),
            "structure_max_loss": market_number(row.get("ask_price")) * 100,
            "structure_expected_return": market_number(row.get("model_expected_return"), -1),
            "primary_row": row.to_dict(),
        })
        rows.append(single)

    for expiry, expiry_frame in frame.groupby(frame["strike_time"].astype(str)):
        del expiry
        ordered = expiry_frame.sort_values("strike_numeric")
        near = ordered.iloc[(ordered["abs_delta"] - .55).abs().argmin()]
        if direction == "CALL":
            further = ordered[ordered["strike_numeric"] > float(near["strike_numeric"])]
        else:
            further = ordered[ordered["strike_numeric"] < float(near["strike_numeric"])]
        if further.empty:
            continue
        wing = further.iloc[(further["abs_delta"] - .32).abs().argmin()]
        rows.append(_package(
            f"{direction}_DEBIT_SPREAD",
            [(near, "BUY", 1, "PROTECTIVE"), (wing, "SELL", 1, "RISK")],
            minimum_expected_return=minimum_expected_return,
            premium_budget=premium_budget,
        ))
        rows.append(_package(
            f"{direction}_BACKSPREAD",
            [(near, "SELL", 1, "RISK"), (wing, "BUY", 2, "PROTECTIVE")],
            minimum_expected_return=minimum_expected_return,
            premium_budget=premium_budget,
        ))
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(
        ["volatility_eligible", "structure_expected_return", "volatility_quality_score"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
