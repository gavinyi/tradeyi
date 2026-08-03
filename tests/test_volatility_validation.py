import pandas as pd

from futu_quant.volatility_validation import volatility_walk_forward


def history(rising: bool) -> pd.DataFrame:
    base = pd.Series(range(40), dtype=float)
    bid = 1 + base * (.03 if rising else -.01)
    return pd.DataFrame({
        "time_key": pd.date_range("2026-07-01", periods=40, freq="h", tz="UTC"),
        "ask": bid + .02, "bid": bid.clip(lower=.10),
    })


def test_walk_forward_reports_multiple_chronological_windows():
    windows, summary = volatility_walk_forward(
        {"US.X260821C100000": history(True)}, holding_hours=2,
        windows=3, minimum_returns=20,
    )

    assert len(windows) == 3
    assert summary["sample_count"] >= 20
    assert summary["passed"] is True


def test_walk_forward_does_not_pass_insufficient_evidence():
    windows, summary = volatility_walk_forward(
        {"US.X": history(True).head(5)}, holding_hours=4,
        minimum_returns=20,
    )

    assert windows.empty
    assert summary["status"] == "INSUFFICIENT"
