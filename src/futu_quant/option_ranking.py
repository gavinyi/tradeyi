from __future__ import annotations

from datetime import date
from math import erf, log, sqrt
import os

import pandas as pd

from futu_quant.gamma_risk import market_number, normalize_iv


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def expiry_profit_probability(
    spot: float,
    strike: float,
    premium: float,
    volatility: float,
    years: float,
    option_type: str,
    risk_free_rate: float = 0.0,
) -> tuple[float, float]:
    """Risk-neutral probability of finishing beyond a long option's expiry breakeven."""
    kind = str(option_type).upper()
    breakeven = strike + premium if kind == "CALL" else strike - premium
    if spot <= 0 or strike <= 0 or premium < 0 or volatility <= 0 or years <= 0:
        return 0.0, breakeven
    if kind == "PUT" and breakeven <= 0:
        return 0.0, breakeven
    denominator = volatility * sqrt(years)
    d2 = (log(spot / breakeven) + (risk_free_rate - 0.5 * volatility**2) * years) / denominator
    probability = _normal_cdf(d2) if kind == "CALL" else _normal_cdf(-d2)
    return min(1.0, max(0.0, probability)), breakeven


def rank_long_option_candidates(
    candidates: pd.DataFrame,
    spot: float,
    as_of: date | None = None,
    profit_target: float = 0.50,
    stop_loss: float = 0.40,
) -> pd.DataFrame:
    """Add explainable long-option probability fields and rank highest first.

    The probability uses the ask-paid expiry breakeven under the option's implied
    lognormal distribution. It is a model estimate, not a forecast or backtested win rate.
    """
    if candidates.empty:
        return candidates.copy()
    today = as_of or date.today()
    rate = float(os.getenv("OPTION_RISK_FREE_RATE", "0"))
    ranked = candidates.copy()
    probabilities: list[float] = []
    breakevens: list[float] = []
    confidences: list[str] = []
    expected_returns: list[float] = []
    gamma_theta_efficiencies: list[float] = []
    for _, row in ranked.iterrows():
        expiry = pd.to_datetime(row.get("strike_time"), errors="coerce")
        dte = max(0.5, float((expiry.date() - today).days) + 0.5) if not pd.isna(expiry) else 0.5
        probability, breakeven = expiry_profit_probability(
            spot=market_number(spot),
            strike=market_number(row.get("strike_price")),
            premium=market_number(row.get("ask_price")),
            volatility=normalize_iv(row.get("option_implied_volatility", 0)),
            years=dte / 365.0,
            option_type=str(row.get("option_type", "CALL")),
            risk_free_rate=rate,
        )
        spread = market_number(row.get("spread_pct"), 100.0)
        open_interest = market_number(row.get("option_open_interest"))
        confidence = "高" if spread <= 5 and open_interest >= 500 else "中" if spread <= 10 and open_interest >= 100 else "低"
        half_spread_cost = max(0.0, spread) / 200.0
        expected_return = (
            probability * float(profit_target)
            - (1 - probability) * float(stop_loss)
            - half_spread_cost
        )
        gamma = abs(market_number(row.get("option_gamma")))
        theta = abs(market_number(row.get("option_theta")))
        gamma_theta_efficiency = gamma / theta if theta > 0 else 0.0
        probabilities.append(probability)
        breakevens.append(breakeven)
        confidences.append(confidence)
        expected_returns.append(expected_return)
        gamma_theta_efficiencies.append(gamma_theta_efficiency)
    ranked["estimated_profit_probability"] = probabilities
    ranked["breakeven_price"] = breakevens
    ranked["probability_confidence"] = confidences
    ranked["model_expected_return"] = expected_returns
    ranked["gamma_theta_efficiency"] = gamma_theta_efficiencies
    return ranked.sort_values(
        ["model_expected_return", "estimated_profit_probability", "gamma_theta_efficiency", "spread_pct"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
