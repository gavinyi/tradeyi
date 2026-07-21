from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from futu_quant.market_data import (
    CANONICAL_BAR_COLUMNS,
    AlpacaMarketDataProvider,
    FutuMarketDataProvider,
    MarketDataNotConfigured,
    YahooMarketDataProvider,
    normalize_bars,
)


def test_normalize_bars_adds_provenance_and_utc():
    raw = pd.DataFrame(
        {
            "time_key": ["2026-07-17"],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [10],
        }
    )

    result = normalize_bars(raw, "AAPL", "FUTU", "FUTU", "QFQ")

    assert list(result.columns) == CANONICAL_BAR_COLUMNS
    assert str(result.loc[0, "timestamp"].tz) == "UTC"
    assert result.loc[0, "symbol"] == "US.AAPL"
    assert result.loc[0, "source"] == "FUTU"
    assert result.loc[0, "dataset_version"]


def test_normalize_bars_rejects_missing_ohlcv_columns():
    with pytest.raises(ValueError, match="缺少行情字段"):
        normalize_bars(pd.DataFrame({"time_key": ["2026-07-17"], "close": [1]}), "AAPL", "X", "X")


def test_unconfigured_alpaca_is_nonfatal(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    provider = AlpacaMarketDataProvider.from_env()

    assert not provider.health().configured
    with pytest.raises(MarketDataNotConfigured, match="ALPACA_API_KEY"):
        provider.get_bars("US.AAPL", date(2026, 1, 1), date(2026, 1, 31))


def test_futu_provider_wraps_existing_broker_range_method():
    class FakeBroker:
        def candles_range(self, symbol, start, end):
            assert (symbol, start, end) == ("US.AAPL", "2026-01-01", "2026-01-31")
            return pd.DataFrame(
                {
                    "time_key": ["2026-01-02"],
                    "open": [10], "high": [11], "low": [9], "close": [10.5], "volume": [100],
                }
            )

    result = FutuMarketDataProvider(FakeBroker()).get_bars(
        "AAPL", date(2026, 1, 1), date(2026, 1, 31)
    )

    assert result.loc[0, "source"] == "FUTU"
    assert result.loc[0, "adjustment"] == "QFQ"


def test_yahoo_provider_reports_optional_dependency_without_importing_at_startup():
    provider = YahooMarketDataProvider(importer=lambda _: (_ for _ in ()).throw(ImportError("missing")))
    status = provider.health()

    assert not status.available
    assert "yfinance" in status.message
