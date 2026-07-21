from __future__ import annotations

from datetime import date
from pathlib import Path

from .config import normalize_us_symbol
from .market_data_store import CanonicalMarketDataStore


def backfill_research_history(
    provider,
    symbols,
    start: date | str,
    end: date | str,
    *,
    warehouse: Path | str = "data/market-data",
) -> dict:
    """Incrementally backfill configured symbols while isolating per-symbol failures."""
    normalized = list(dict.fromkeys(normalize_us_symbol(symbol) for symbol in symbols))
    store = CanonicalMarketDataStore(warehouse)
    details = []
    rows = succeeded = 0
    for symbol in normalized:
        try:
            frame = provider.get_bars(symbol, start, end)
            if frame.empty:
                details.append({"symbol": symbol, "status": "EMPTY", "rows": 0, "message": "没有数据"})
                continue
            manifest = store.write_bars(frame, provider.name)
            succeeded += 1
            rows += len(frame)
            details.append(
                {
                    "symbol": symbol, "status": "SUCCESS", "rows": len(frame),
                    "message": f"版本 {manifest.dataset_version}",
                }
            )
        except Exception as exc:  # one unsupported symbol must not cancel the batch
            details.append(
                {"symbol": symbol, "status": "ERROR", "rows": 0, "message": str(exc)}
            )
    return {
        "requested": len(normalized),
        "succeeded": succeeded,
        "failed": sum(row["status"] == "ERROR" for row in details),
        "empty": sum(row["status"] == "EMPTY" for row in details),
        "rows": rows,
        "details": details,
    }
