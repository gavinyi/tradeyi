import pandas as pd

from futu_quant.volatility_structures import build_volatility_structure_candidates


def test_router_builds_single_debit_spread_and_backspread():
    rows = pd.DataFrame([
        {
            "code": "US.X260821C100000", "option_type": "CALL", "strike_time": "2026-08-21",
            "strike_price": 100, "option_delta": .55, "option_gamma": .03,
            "ask_price": 5.0, "bid_price": 4.9, "expected_exit_bid": 5.5,
            "expected_exit_ask": 5.6, "spread_pct": 2, "option_open_interest": 2000,
            "volume": 100, "model_expected_return": .08, "estimated_profit_probability": .55,
            "volatility_eligible": True, "volatility_quality_score": 70,
        },
        {
            "code": "US.X260821C105000", "option_type": "CALL", "strike_time": "2026-08-21",
            "strike_price": 105, "option_delta": .32, "option_gamma": .025,
            "ask_price": 2.1, "bid_price": 2.0, "expected_exit_bid": 2.5,
            "expected_exit_ask": 2.6, "spread_pct": 3, "option_open_interest": 1800,
            "volume": 80, "model_expected_return": .10, "estimated_profit_probability": .52,
            "volatility_eligible": True, "volatility_quality_score": 68,
        },
    ])

    result = build_volatility_structure_candidates(
        rows, "CALL", minimum_expected_return=0, premium_budget=1000,
    )

    assert {"LONG_OPTION", "CALL_DEBIT_SPREAD", "CALL_BACKSPREAD"}.issubset(
        set(result["structure"])
    )
    backspread = result[result["structure"] == "CALL_BACKSPREAD"].iloc[0]
    assert sum(leg["ratio"] for leg in backspread["legs"] if leg["side"] == "BUY") == 2
    assert backspread["net_gamma"] > 0
