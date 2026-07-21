from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WAREHOUSE = ROOT / "data" / "warehouse"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_partition(frame: pd.DataFrame, path: Path, keys: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        frame = pd.concat([pd.read_parquet(path), frame], ignore_index=True)
    frame = frame.drop_duplicates(keys, keep="last").sort_values(keys)
    frame.to_parquet(path, index=False, compression="zstd")
    return len(frame)


def archive_market_data(store, warehouse: Path | str = DEFAULT_WAREHOUSE) -> list[dict]:
    warehouse = Path(warehouse)
    records = []
    with sqlite3.connect(store.path) as connection:
        options = pd.read_sql_query("SELECT * FROM option_history", connection)
        combos = pd.read_sql_query("SELECT * FROM combo_market_snapshots", connection)
    datasets = [
        ("option_history", options, "underlying", "time_key", ["code", "time_key"]),
        ("combo_snapshots", combos, "underlying", "created_at", ["combo_id", "created_at"]),
    ]
    for dataset, frame, symbol_col, time_col, keys in datasets:
        if frame.empty:
            continue
        frame["partition_date"] = pd.to_datetime(frame[time_col], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
        frame[symbol_col] = frame[symbol_col].fillna("UNKNOWN").astype(str)
        for (symbol, day), partition in frame.dropna(subset=["partition_date"]).groupby([symbol_col, "partition_date"]):
            safe_symbol = str(symbol).replace(".", "_").replace("/", "_")
            path = warehouse / dataset / f"date={day}" / f"symbol={safe_symbol}" / "part.parquet"
            row_count = _write_partition(partition.drop(columns=["partition_date"]), path, keys)
            quality = "A" if dataset == "combo_snapshots" and float(partition.get("quality_score", pd.Series([0])).mean()) >= 90 else "B"
            record = {"dataset_type": dataset, "dataset_key": f"{symbol}:{day}",
                      "path": _portable_path(path), "source": "SQLite incremental archive",
                      "row_count": row_count, "quality_grade": quality, "content_hash": _hash(path),
                      "metadata": {"partition_date": day, "symbol": symbol, "format": "parquet-zstd"}}
            store.record_lineage(record)
            records.append(record)
    return records


def warehouse_files(warehouse: Path | str = DEFAULT_WAREHOUSE) -> list[dict]:
    warehouse = Path(warehouse)
    return [{"path": str(path), "bytes": path.stat().st_size, "modified_at": path.stat().st_mtime}
            for path in warehouse.rglob("*.parquet")] if warehouse.exists() else []
