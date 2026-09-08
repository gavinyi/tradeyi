from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from .market_calendar import USMarketCalendar


def maintenance(store, broker) -> dict:
    result = {"database": store.integrity_check(), "opend": "ERROR", "quote": "UNKNOWN"}
    accounts = broker.accounts()
    result["opend"] = "OK" if not accounts.empty else "EMPTY"
    symbols = list(broker.settings.watchlist[:1])
    if symbols:
        quote = broker.snapshots(symbols)
        result["quote"] = "OK" if not quote.empty and float(quote.iloc[0].get("last_price", 0) or 0) > 0 else "STALE"
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        session = USMarketCalendar().session(now_ny)
        if not quote.empty and "update_time" in quote and quote.iloc[0].get("update_time"):
            updated = pd.Timestamp(quote.iloc[0]["update_time"])
            updated = updated.tz_localize(now_ny.tzinfo) if updated.tzinfo is None else updated.tz_convert(now_ny.tzinfo)
            latency = max(0, (now_ny - updated.to_pydatetime()).total_seconds())
            result["quote_latency_seconds"] = latency
            if session.regular_open(now_ny) and latency > 300:
                result["quote"] = "STALE"
        candles = broker.candles(symbols[0], count=20)
        required = {"time_key", "open", "high", "low", "close"}
        result["market_data"] = (
            "OK" if required.issubset(candles.columns)
            and not candles[list(required)].isna().any().any()
            and candles["time_key"].is_monotonic_increasing else "GAP_OR_INVALID"
        )
        if not candles.empty and session.is_session:
            expected = USMarketCalendar().calendar.previous_session(pd.Timestamp(now_ny.date()))
            latest = pd.Timestamp(candles.iloc[-1]["time_key"]).normalize().tz_localize(None)
            if latest < expected.tz_localize(None):
                result["market_data"] = "SESSION_GAP"
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups"))
    backup = backup_dir / f"trading-{datetime.now():%Y%m%d}.db"
    required_backup_tables = {"order_intents", "system_settings", "runtime_config", "campaign_state"}
    backup_valid = False
    if backup.exists():
        try:
            with sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True) as connection:
                backup_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            backup_valid = backup_integrity == "ok" and required_backup_tables.issubset(tables)
        except sqlite3.Error:
            backup_valid = False
    if not backup_valid:
        backup.unlink(missing_ok=True)
        Path(f"{backup}-wal").unlink(missing_ok=True)
        Path(f"{backup}-shm").unlink(missing_ok=True)
        store.backup(backup)
        backup.chmod(0o600)
    with sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True) as connection:
        backup_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if backup_integrity != "ok":
        raise RuntimeError(f"备份数据库完整性检查失败：{backup_integrity}")
    config_backup = backup_dir / f"config-{datetime.now():%Y%m%d}.env"
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists() and not config_backup.exists():
        shutil.copy2(env_file, config_backup)
        config_backup.chmod(0o600)
    manifest = backup_dir / f"manifest-{datetime.now():%Y%m%d}.json"
    manifest.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": backup.name, "database_integrity": backup_integrity,
        "configuration": config_backup.name if config_backup.exists() else None,
    }, ensure_ascii=False, separators=(",", ":")))
    manifest.chmod(0o600)
    result["backup"] = "VERIFIED"
    backup_retention = max(7, int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
    backup_cutoff = datetime.now().timestamp() - backup_retention * 86400
    for candidate in backup_dir.glob("*"):
        if candidate.is_file() and candidate.stat().st_mtime < backup_cutoff:
            candidate.unlink()
    retention = max(30, int(os.getenv("DATA_RETENTION_DAYS", "365")))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    result["cleanup"] = store.cleanup(cutoff.isoformat())
    log = Path("data/service.log")
    max_log_bytes = int(os.getenv("MAX_LOG_BYTES", str(10 * 1024 * 1024)))
    if log.exists() and log.stat().st_size > max_log_bytes:
        rotated = log.with_suffix(".log.1")
        rotated.unlink(missing_ok=True)
        shutil.move(log, rotated)
        result["log_rotation"] = "ROTATED"
    store.audit("HEALTH_CHECK", result)
    return result
