import pandas as pd

from futu_quant.option_signal import automatic_option_direction, configured_direction_allowed


def bars(values):
    return pd.DataFrame({"close": values})


def test_volatility_signal_requires_meaningful_move(monkeypatch):
    monkeypatch.setenv("VOLATILITY_MIN_UNDERLYING_MOVE", "0.015")
    direction, reason, _ = automatic_option_direction(
        {"last_price": 101, "prev_close_price": 100}, bars([95] * 20), "VOLATILITY"
    )
    assert direction is None
    assert "1.50%" in reason


def test_composite_signal_confirms_call(monkeypatch):
    monkeypatch.setenv("VOLATILITY_MIN_UNDERLYING_MOVE", "0.015")
    allowed, reason, metrics = configured_direction_allowed(
        {"last_price": 103, "prev_close_price": 100},
        bars([90 + index * 0.5 for index in range(20)]), "CALL", "VOLATILITY",
    )
    assert allowed
    assert "看涨确认" in reason
    assert metrics["direction_score"] >= 2
