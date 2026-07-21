import pandas as pd

from futu_quant.backtest import run_backtest, run_core_satellite_backtest


def test_backtest_returns_equity_and_metrics():
    closes = [100.0] * 35 + [90, 91, 92, 93, 94, 110, 111, 112]
    bars = pd.DataFrame(
        {
            "time_key": pd.date_range("2025-01-01", periods=len(closes)).astype(str),
            "open": closes,
            "close": closes,
        }
    )
    result = run_backtest(bars, "SMA Cross", 10_000, 10, commission=1, slippage_bps=2)
    assert len(result.equity) == len(bars)
    assert result.metrics["initial_cash"] == 10_000
    assert set(result.equity.columns) >= {"date", "equity", "signal"}


def test_backtest_rejects_short_history():
    bars = pd.DataFrame({"time_key": ["2025-01-01"], "open": [1], "close": [1]})
    try:
        run_backtest(bars, "SMA Cross", 1000, 1)
    except ValueError as exc:
        assert "35" in str(exc)
    else:
        raise AssertionError("short history should be rejected")


def test_core_satellite_portfolio_backtest():
    closes = [100 + index * 0.2 for index in range(220)]
    spy = pd.DataFrame(
        {
            "time_key": pd.bdate_range("2025-01-01", periods=220).astype(str),
            "open": closes,
            "close": closes,
        }
    )
    result = run_core_satellite_backtest(
        {"US.SPY": spy}, ("US.SPY",), 100_000, min_trade_usd=100
    )
    assert len(result.equity) == 220
    assert not result.trades.empty
    assert set(result.trades["code"]) == {"US.SPY"}
