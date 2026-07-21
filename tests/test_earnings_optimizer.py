import pandas as pd

from futu_quant.earnings_optimizer import build_structure_candidates, opportunity_score, recommend_timing


def test_opportunity_rejects_when_implied_move_exceeds_history():
    result = opportunity_score({"p75_gap": .04, "history_samples": 252}, .08, 3, 1000, 3, True)
    assert result["expected_move_edge"] < 0
    assert result["trade_allowed"] is False


def test_structure_builder_creates_defined_risk_choices():
    rows = pd.DataFrame([
        {"code": "C100", "option_type": "CALL", "strike_price": 100, "ask_price": 3,
         "bid_price": 2.9, "spread_pct": 3, "option_open_interest": 1000},
        {"code": "C105", "option_type": "CALL", "strike_price": 105, "ask_price": 1.5,
         "bid_price": 1.4, "spread_pct": 5, "option_open_interest": 800},
        {"code": "P100", "option_type": "PUT", "strike_price": 100, "ask_price": 3,
         "bid_price": 2.9, "spread_pct": 3, "option_open_interest": 1000},
        {"code": "P95", "option_type": "PUT", "strike_price": 95, "ask_price": 1.5,
         "bid_price": 1.4, "spread_pct": 5, "option_open_interest": 800},
    ])
    result = build_structure_candidates(rows, 100, {"p75_gap": .10, "history_samples": 252}, "CALL", True)
    assert {"LONG_STRADDLE", "LONG_STRANGLE", "CALL_DEBIT_SPREAD"}.issubset(set(result.structure))
    assert all(result.max_loss > 0)


def test_timing_shortens_theta_exposure_for_ordinary_edge():
    timing = recommend_timing(65, .01, "盘后")
    assert timing["entry_hours"] == 6
    assert timing["max_hold_hours"] == 18
