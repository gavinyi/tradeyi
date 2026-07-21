from datetime import date

import pandas as pd

from futu_quant.option_ranking import expiry_profit_probability, rank_long_option_candidates


def test_expiry_profit_probability_uses_premium_in_breakeven() -> None:
    probability, breakeven = expiry_profit_probability(
        100, 100, 5, 0.30, 30 / 365, "CALL"
    )
    itm_probability, _ = expiry_profit_probability(
        100, 100, 0, 0.30, 30 / 365, "CALL"
    )
    assert breakeven == 105
    assert 0 < probability < itm_probability < 0.5


def test_rank_candidates_is_highest_probability_first() -> None:
    candidates = pd.DataFrame({
        "code": ["EXPENSIVE", "CHEAPER"],
        "option_type": ["CALL", "CALL"],
        "strike_price": [100.0, 100.0],
        "strike_time": ["2026-08-15", "2026-08-15"],
        "ask_price": [8.0, 3.0],
        "option_implied_volatility": [30.0, 30.0],
        "spread_pct": [3.0, 3.0],
        "option_open_interest": [1000, 1000],
    })
    ranked = rank_long_option_candidates(candidates, 100, date(2026, 7, 16))
    assert ranked["code"].tolist() == ["CHEAPER", "EXPENSIVE"]
    assert ranked["probability_confidence"].tolist() == ["高", "高"]
    assert ranked.iloc[0]["model_expected_return"] > ranked.iloc[1]["model_expected_return"]
