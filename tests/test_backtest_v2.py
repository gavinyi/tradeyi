from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.backtest import run_backtest
from futu_quant.backtest_metrics import calculate_backtest_metrics


def test_metrics_include_risk_trade_and_benchmark_values():
    equity = pd.DataFrame(
        {
            "date": pd.bdate_range("2026-01-01", periods=4),
            "equity": [100.0, 110.0, 99.0, 120.0],
            "position": [0, 1, 1, 0],
        }
    )
    trades = pd.DataFrame([{"side": "SELL", "pnl": 20.0, "notional": 100.0}])
    benchmark = pd.Series([100.0, 102.0, 101.0, 105.0], index=equity["date"])

    metrics = calculate_backtest_metrics(equity, trades, 100.0, benchmark)

    assert metrics["total_return"] == pytest.approx(0.20)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)
    assert metrics["benchmark_return"] == pytest.approx(0.05)
    assert metrics["excess_return"] == pytest.approx(0.15)
    assert {
        "cagr", "annual_volatility", "sharpe", "sortino", "calmar",
        "max_drawdown_duration", "profit_factor", "expectancy", "turnover",
        "average_exposure", "maximum_exposure", "beta", "alpha",
        "up_capture", "down_capture",
    } <= metrics.keys()


def test_metrics_are_safe_for_flat_curve_and_no_closed_trades():
    equity = pd.DataFrame(
        {"date": pd.bdate_range("2026-01-01", periods=3), "equity": [100, 100, 100]}
    )
    metrics = calculate_backtest_metrics(equity, pd.DataFrame(), 100)

    assert metrics["sharpe"] == 0
    assert metrics["sortino"] == 0
    assert metrics["profit_factor"] == 0
    assert metrics["max_drawdown_duration"] == 0


def test_existing_backtest_exposes_v2_metrics_without_breaking_old_keys():
    closes = [100.0] * 35 + [90, 91, 92, 93, 94, 110, 111, 112]
    bars = pd.DataFrame(
        {
            "time_key": pd.date_range("2025-01-01", periods=len(closes)).astype(str),
            "open": closes,
            "close": closes,
        }
    )

    result = run_backtest(bars, "SMA Cross", 10_000, 10, commission=1, slippage_bps=2)

    metric_names = set(result.metrics)
    assert {"initial_cash", "final_equity", "total_return", "max_drawdown", "win_rate"} <= metric_names
    assert {"cagr", "sharpe", "sortino", "calmar", "turnover"} <= metric_names


def test_volume_participation_caps_fill_and_records_unfilled_quantity():
    closes = [100.0] * 35 + [101, 102, 103, 104, 105, 106, 107]
    bars = pd.DataFrame(
        {
            "time_key": pd.date_range("2025-01-01", periods=len(closes)).astype(str),
            "open": closes,
            "close": closes,
            "volume": [5] * len(closes),
        }
    )

    result = run_backtest(
        bars, "SMA Cross", 100_000, 100, max_volume_participation=0.20
    )

    assert not result.trades.empty
    assert result.trades["quantity"].max() == 1
    assert result.trades.iloc[0]["requested_quantity"] == 100
    assert result.trades.iloc[0]["unfilled_quantity"] == 99
