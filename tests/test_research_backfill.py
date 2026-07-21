from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from futu_quant.market_data import normalize_bars
from futu_quant.research_backfill import backfill_research_history


def test_backfill_deduplicates_symbols_and_isolates_failures(tmp_path):
    class Provider:
        name = "FUTU"

        def get_bars(self, symbol, start, end, interval="1d"):
            if symbol == "US.BAD":
                raise RuntimeError("no permission")
            return normalize_bars(
                pd.DataFrame(
                    {
                        "time_key": ["2026-07-17"], "open": [1], "high": [2],
                        "low": [0.5], "close": [1.5], "volume": [10],
                    }
                ),
                symbol, "FUTU", "FUTU", "QFQ",
            )

    result = backfill_research_history(
        Provider(), ["AAPL", "US.AAPL", "BAD"], date(2026, 7, 1), date(2026, 7, 18),
        warehouse=tmp_path,
    )

    assert result["requested"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["rows"] == 1
    assert result["details"][1]["status"] == "ERROR"


def test_unified_service_schedules_research_backfill():
    source = (Path(__file__).resolve().parents[1] / "scripts/run_service.py").read_text()

    assert 'last_run:research_data_backfill' in source
    assert '"research_data_backfill"' in source
