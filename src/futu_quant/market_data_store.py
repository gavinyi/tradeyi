from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from .config import normalize_us_symbol
from .market_data import CANONICAL_BAR_COLUMNS


@dataclass(frozen=True)
class DatasetManifest:
    symbol: str
    source: str
    adjustment: str
    start: str
    end: str
    row_count: int
    dataset_version: str
    content_hash: str
    path: str
    quality_grade: str
    updated_at: str


@dataclass(frozen=True)
class ReconciliationResult:
    quality_status: str
    reason: str
    conflict_count: int
    compared_count: int
    maximum_close_difference: float


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    message: str


@dataclass(frozen=True)
class RoutedBars:
    frame: pd.DataFrame
    source: str
    attempts: tuple[ProviderAttempt, ...]


def _safe_symbol(symbol: str) -> str:
    return normalize_us_symbol(symbol).replace(".", "_").replace("/", "_")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CanonicalMarketDataStore:
    """Versioned local Parquet store for research data, separate from trading state."""

    def __init__(self, root: Path | str = "data/market-data"):
        self.root = Path(root)

    def _partition(self, symbol: str, year: int) -> Path:
        return self.root / "bars" / f"symbol={_safe_symbol(symbol)}" / f"year={year}" / "part.parquet"

    def _manifest_path(self, symbol: str, year: int) -> Path:
        return self._partition(symbol, year).with_name("manifest.json")

    def write_bars(self, frame: pd.DataFrame, source: str | None = None) -> DatasetManifest:
        if frame.empty:
            raise ValueError("不能写入空行情数据")
        missing = sorted(set(CANONICAL_BAR_COLUMNS) - set(frame.columns))
        if missing:
            raise ValueError(f"规范行情缺少字段：{', '.join(missing)}")
        symbols = frame["symbol"].dropna().astype(str).unique().tolist()
        if len(symbols) != 1:
            raise ValueError("每次写入只能包含一个标的")
        symbol = normalize_us_symbol(symbols[0])
        data = frame.copy()
        data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
        years = data["timestamp"].dt.year.unique().tolist()
        manifests = []
        for year in years:
            partition = data[data["timestamp"].dt.year == year].copy()
            path = self._partition(symbol, int(year))
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                existing = pd.read_parquet(path)
                existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
                partition = pd.concat([existing, partition], ignore_index=True)
            adjustment_counts = partition.groupby("source")["adjustment"].nunique()
            if (adjustment_counts > 1).any():
                bad_sources = ", ".join(adjustment_counts[adjustment_counts > 1].index.astype(str))
                raise ValueError(f"同一数据源不能混合不同复权口径：{bad_sources}")
            partition = (
                partition.sort_values(["timestamp", "fetched_at"])
                .drop_duplicates(["symbol", "timestamp", "source", "adjustment"], keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            temporary = path.with_suffix(".parquet.tmp")
            partition.to_parquet(temporary, index=False, compression="zstd")
            temporary.replace(path)
            sources = sorted(set(partition["source"].astype(str)))
            adjustments = sorted(set(partition["adjustment"].astype(str)))
            versions = sorted(set(partition["dataset_version"].astype(str)))
            content_hash = _file_hash(path)
            manifest = DatasetManifest(
                symbol=symbol,
                source="+".join(sources),
                adjustment="+".join(adjustments),
                start=partition["session_date"].astype(str).min(),
                end=partition["session_date"].astype(str).max(),
                row_count=len(partition),
                dataset_version=hashlib.sha256("|".join(versions).encode()).hexdigest()[:20],
                content_hash=content_hash,
                path=str(path),
                quality_grade=(
                    "A" if partition["quality_status"].eq("GOOD").mean() >= 0.99 else "B"
                    if partition["quality_status"].eq("GOOD").mean() >= 0.90 else "C"
                ),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            manifest_path = self._manifest_path(symbol, int(year))
            manifest_tmp = manifest_path.with_suffix(".json.tmp")
            manifest_tmp.write_text(json.dumps(asdict(manifest), ensure_ascii=False, indent=2))
            manifest_tmp.replace(manifest_path)
            manifests.append(manifest)
        return manifests[-1]

    def read_bars(
        self, symbol: str, start: date | str | None = None, end: date | str | None = None,
        *, include_all_sources: bool = False,
    ) -> pd.DataFrame:
        base = self.root / "bars" / f"symbol={_safe_symbol(symbol)}"
        paths = sorted(base.glob("year=*/part.parquet")) if base.exists() else []
        if not paths:
            return pd.DataFrame(columns=CANONICAL_BAR_COLUMNS)
        result = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
        result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True)
        if start is not None:
            result = result[result["session_date"].astype(str) >= str(start)[:10]]
        if end is not None:
            result = result[result["session_date"].astype(str) <= str(end)[:10]]
        if not include_all_sources:
            priority = {"FUTU": 0, "ALPACA": 1, "YAHOO": 2}
            result["_source_priority"] = result["source"].map(priority).fillna(99)
            result = (
                result.sort_values(["timestamp", "_source_priority", "fetched_at"])
                .drop_duplicates("timestamp", keep="first")
                .drop(columns=["_source_priority"])
            )
        return result.sort_values("timestamp").reset_index(drop=True)

    def catalog(self) -> pd.DataFrame:
        rows = []
        if self.root.exists():
            for path in self.root.glob("bars/symbol=*/year=*/manifest.json"):
                try:
                    rows.append(json.loads(path.read_text()))
                except (OSError, json.JSONDecodeError):
                    continue
        if not rows:
            return pd.DataFrame(columns=[field.name for field in DatasetManifest.__dataclass_fields__.values()])
        return pd.DataFrame(rows).sort_values(["symbol", "start"]).reset_index(drop=True)


def reconcile_sources(
    primary: pd.DataFrame, secondary: pd.DataFrame, tolerance: float = 0.03
) -> ReconciliationResult:
    if primary.empty or secondary.empty:
        return ReconciliationResult("INSUFFICIENT", "没有共同数据可比较", 0, 0, 0.0)
    left = primary[["session_date", "close"]].rename(columns={"close": "primary_close"})
    right = secondary[["session_date", "close"]].rename(columns={"close": "secondary_close"})
    aligned = left.merge(right, on="session_date", how="inner")
    if aligned.empty:
        return ReconciliationResult("INSUFFICIENT", "两个来源没有共同交易日", 0, 0, 0.0)
    denominator = aligned["primary_close"].abs().replace(0, pd.NA)
    differences = ((aligned["secondary_close"] - aligned["primary_close"]).abs() / denominator).fillna(float("inf"))
    conflicts = differences > tolerance
    count = int(conflicts.sum())
    maximum = float(differences.max())
    if count:
        reason = f"{count} 个交易日收盘价差异超过 {tolerance:.1%}，最大 {maximum:.1%}"
        status = "QUARANTINED"
    else:
        reason = f"{len(aligned)} 个共同交易日价格差异均在 {tolerance:.1%} 内"
        status = "GOOD"
    return ReconciliationResult(status, reason, count, len(aligned), maximum)


class MarketDataRouter:
    def __init__(self, providers):
        self.providers = tuple(providers)

    def get_bars(self, symbol, start, end, interval: str = "1d") -> RoutedBars:
        attempts = []
        for provider in self.providers:
            status = provider.health()
            if not status.available:
                attempts.append(ProviderAttempt(provider.name, "SKIPPED", status.message))
                continue
            try:
                frame = provider.get_bars(symbol, start, end, interval)
            except Exception as exc:  # provider boundary deliberately isolates failures
                attempts.append(ProviderAttempt(provider.name, "ERROR", str(exc)))
                continue
            if frame.empty:
                attempts.append(ProviderAttempt(provider.name, "EMPTY", "该范围没有数据"))
                continue
            attempts.append(ProviderAttempt(provider.name, "SUCCESS", f"获得 {len(frame)} 行"))
            return RoutedBars(frame, provider.name, tuple(attempts))
        details = "；".join(f"{item.provider}:{item.status}:{item.message}" for item in attempts)
        raise RuntimeError(f"所有研究行情源均不可用：{details}")
