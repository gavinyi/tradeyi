import pandas as pd

from futu_quant.option_expectancy import (
    add_iv_history_percentiles, empirical_forward_returns, iv_history_percentile,
    short_horizon_option_expectancy,
)


def option_row(**overrides):
    row = {
        "option_type": "CALL", "ask_price": 5.1, "bid_price": 4.9,
        "option_delta": .55, "option_gamma": .03, "option_theta": -.08,
        "option_vega": .12, "option_implied_volatility": .45,
    }
    row.update(overrides)
    return row


def test_short_horizon_model_rewards_aligned_strong_signal():
    aligned = short_horizon_option_expectancy(
        option_row(), 100, .35,
        {"active": True, "intraday_return": .04}, holding_hours=4,
    )
    opposed = short_horizon_option_expectancy(
        option_row(), 100, .35,
        {"active": True, "intraday_return": -.04}, holding_hours=4,
    )

    assert aligned["expected_return"] > opposed["expected_return"]
    assert aligned["model_version"] == "SHORT_HORIZON_GREEKS_V1"


def test_iv_percentile_uses_history_and_reports_insufficient_samples():
    enough = pd.DataFrame({"iv": [.20 + index / 1000 for index in range(40)]})
    result = iv_history_percentile(.23, enough)
    short = iv_history_percentile(.23, enough.head(5))

    assert result["status"] == "GOOD"
    assert 0 < result["percentile"] < 1
    assert short["status"] == "INSUFFICIENT"
    assert short["percentile"] is None


def test_empirical_returns_use_ask_entry_and_bid_exit():
    frame = pd.DataFrame({
        "time_key": pd.date_range("2026-07-31 14:00", periods=4, freq="h", tz="UTC"),
        "ask": [1.10, 1.20, 1.30, 1.40],
        "bid": [1.00, 1.10, 1.40, 1.50],
    })
    values = empirical_forward_returns(frame, 2, tolerance_minutes=1)

    assert values
    assert values[0] == 1.40 / 1.10 - 1


def test_candidate_iv_percentile_uses_chain_proxy_until_history_is_ready():
    candidates = pd.DataFrame({
        "option_implied_volatility": [.30, .40, .50, .60],
    })
    enriched = add_iv_history_percentiles(candidates, pd.DataFrame({"iv": [.35] * 5}))

    assert set(enriched["iv_percentile_status"]) == {"CHAIN_PROXY"}
    assert enriched.iloc[-1]["iv_history_percentile"] == 1.0
