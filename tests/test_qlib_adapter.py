from __future__ import annotations

import pandas as pd

from futu_quant.market_data import normalize_bars
from futu_quant.qlib_adapter import QlibResearchAdapter


def canonical_data():
    return normalize_bars(
        pd.DataFrame(
            {
                "time_key": ["2026-07-16", "2026-07-17"],
                "open": [100, 101], "high": [102, 103], "low": [99, 100],
                "close": [101, 102], "volume": [1000, 1100],
            }
        ),
        "US.AAPL", "FUTU", "FUTU", "QFQ",
    )


def test_missing_qlib_is_reported_not_raised():
    adapter = QlibResearchAdapter(
        importer=lambda _: (_ for _ in ()).throw(ImportError("missing"))
    )

    status = adapter.status()

    assert not status.available
    assert "未安装" in status.message


def test_prepare_dataset_uses_qlib_instrument_and_datetime_index(tmp_path):
    adapter = QlibResearchAdapter(importer=lambda _: object())

    path = adapter.prepare_dataset(canonical_data(), tmp_path, "data-v1")
    result = pd.read_parquet(path)

    assert list(result.index.names) == ["instrument", "datetime"]
    assert set(result.columns) >= {"open", "high", "low", "close", "volume", "factor"}


def test_predictions_become_non_executable_research_signals():
    predictions = pd.Series(
        [0.8],
        index=pd.MultiIndex.from_tuples(
            [("AAPL", pd.Timestamp("2026-07-17"))], names=["instrument", "datetime"]
        ),
        name="score",
    )

    rows = QlibResearchAdapter(importer=lambda _: object()).normalize_predictions(
        predictions, "model-v1", "data-v1"
    )

    assert set(rows.columns) >= {
        "symbol", "score", "generated_at", "model_version", "dataset_version", "executable"
    }
    assert not rows["executable"].any()
    assert rows.loc[0, "symbol"] == "US.AAPL"
