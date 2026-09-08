from __future__ import annotations

import pandas as pd


def screen_gamma_contracts(
    chain: pd.DataFrame,
    quotes: pd.DataFrame,
    spot: float,
    option_type: str,
    min_open_interest: int = 100,
    max_spread_pct: float = 20.0,
    max_moneyness_pct: float = 10.0,
) -> pd.DataFrame:
    """Screen short-dated earnings options without using future information."""
    contracts = chain[chain["option_type"] == option_type].merge(
        quotes, on="code", suffixes=("", "_quote")
    )
    middle = (contracts["bid_price"] + contracts["ask_price"]) / 2
    contracts["mid_price"] = middle
    contracts["spread_pct"] = (
        (contracts["ask_price"] - contracts["bid_price"]) / middle * 100
    ).where(middle > 0)
    contracts["moneyness_pct"] = (contracts["strike_price"] / spot - 1) * 100
    contracts = contracts[
        (contracts["bid_price"] > 0)
        & (contracts["ask_price"] > 0)
        & (contracts["option_open_interest"] >= min_open_interest)
        & (contracts["spread_pct"] <= max_spread_pct)
        & (contracts["moneyness_pct"].abs() <= max_moneyness_pct)
        & (~contracts["suspension"])
    ].copy()
    contracts["atm_distance"] = (contracts["strike_price"] - spot).abs()
    return contracts.sort_values(["atm_distance", "spread_pct"]).reset_index(drop=True)
