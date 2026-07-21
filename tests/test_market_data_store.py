from __future__ import annotations

from datetime import date

import pandas as pd

from futu_quant.market_data import ProviderStatus, normalize_bars
from futu_quant.market_data_store import (
    CanonicalMarketDataStore,
    MarketDataRouter,
    reconcile_sources,
)


def canonical_frame(close: float = 100.0, source: str = "FUTU") -> pd.DataFrame:
    return normalize_bars(
        pd.DataFrame(
            {
                "time_key": ["2026-07-17"],
                "open": [close], "high": [close + 1], "low": [close - 1],
                "close": [close], "volume": [100],
            }
        ),
        "US.AAPL", source, source, "QFQ",
    )


def test_store_deduplicates_and_records_manifest(tmp_path):
    store = CanonicalMarketDataStore(tmp_path)

    manifest = store.write_bars(canonical_frame(), "FUTU")
    store.write_bars(canonical_frame(), "FUTU")
    result = store.read_bars("US.AAPL")

    assert len(result) == 1
    assert manifest.dataset_version and manifest.content_hash
    catalog = store.catalog()
    assert catalog.loc[0, "symbol"] == "US.AAPL"
    assert catalog.loc[0, "row_count"] == 1


def test_store_refuses_to_mix_adjustment_modes(tmp_path):
    store = CanonicalMarketDataStore(tmp_path)
    store.write_bars(canonical_frame(), "FUTU")
    raw = canonical_frame().copy()
    raw["adjustment"] = "RAW"
    raw["source"] = "FUTU"

    try:
        store.write_bars(raw, "YAHOO")
    except ValueError as exc:
        assert "复权口径" in str(exc)
    else:
        raise AssertionError("mixed adjustment modes must be rejected")


def test_store_retains_sources_but_default_read_selects_primary(tmp_path):
    store = CanonicalMarketDataStore(tmp_path)
    store.write_bars(canonical_frame(100, "FUTU"), "FUTU")
    store.write_bars(canonical_frame(101, "ALPACA"), "ALPACA")

    canonical = store.read_bars("US.AAPL")
    all_sources = store.read_bars("US.AAPL", include_all_sources=True)

    assert len(all_sources) == 2
    assert len(canonical) == 1
    assert canonical.loc[0, "source"] == "FUTU"
    assert canonical.loc[0, "close"] == 100


def test_reconcile_quarantines_price_conflict():
    result = reconcile_sources(canonical_frame(100), canonical_frame(120, "ALPACA"), 0.03)

    assert result.quality_status == "QUARANTINED"
    assert "收盘价差异" in result.reason
    assert result.conflict_count == 1


def test_router_falls_back_and_records_attempts():
    class Provider:
        def __init__(self, name, result=None, error=None):
            self.name, self.result, self.error = name, result, error

        def health(self):
            return ProviderStatus(self.name, True, True, "ok")

        def get_bars(self, symbol, start, end, interval="1d"):
            if self.error:
                raise self.error
            return self.result

    router = MarketDataRouter(
        [Provider("FUTU", error=RuntimeError("quota")), Provider("ALPACA", canonical_frame(101, "ALPACA"))]
    )

    result = router.get_bars("AAPL", date(2026, 1, 1), date(2026, 2, 1))

    assert result.source == "ALPACA"
    assert len(result.frame) == 1
    assert [attempt.status for attempt in result.attempts] == ["ERROR", "SUCCESS"]
