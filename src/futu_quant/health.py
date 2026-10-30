from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from .market_calendar import USMarketCalendar


def system_health(store, broker) -> dict:
    """执行轻量运行态检查；禁止在五分钟热路径中做整库维护。"""
    result = {"opend": "ERROR", "quote": "UNKNOWN"}
    accounts = broker.accounts()
    result["opend"] = "OK" if not accounts.empty else "EMPTY"
    symbols = list(broker.settings.watchlist[:1])
    if symbols:
        quote = broker.snapshots(symbols)
        result["quote"] = (
            "OK"
            if not quote.empty and float(quote.iloc[0].get("last_price", 0) or 0) > 0
            else "STALE"
        )
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        session = USMarketCalendar().session(now_ny)
        if not quote.empty and "update_time" in quote and quote.iloc[0].get("update_time"):
            updated = pd.Timestamp(quote.iloc[0]["update_time"])
            updated = (
                updated.tz_localize(now_ny.tzinfo)
                if updated.tzinfo is None
                else updated.tz_convert(now_ny.tzinfo)
            )
            latency = max(0, (now_ny - updated.to_pydatetime()).total_seconds())
            result["quote_latency_seconds"] = latency
            if session.regular_open(now_ny) and latency > 300:
                result["quote"] = "STALE"
        candles = broker.candles(symbols[0], count=20)
        required = {"time_key", "open", "high", "low", "close"}
        result["market_data"] = (
            "OK"
            if required.issubset(candles.columns)
            and not candles[list(required)].isna().any().any()
            and candles["time_key"].is_monotonic_increasing
            else "GAP_OR_INVALID"
        )
        if not candles.empty and session.is_session:
            expected = USMarketCalendar().calendar.previous_session(pd.Timestamp(now_ny.date()))
            latest = pd.Timestamp(candles.iloc[-1]["time_key"]).normalize().tz_localize(None)
            if latest < expected.tz_localize(None):
                result["market_data"] = "SESSION_GAP"
    store.audit("HEALTH_CHECK", result, actor="service:health")
    return result


def database_integrity_check(store) -> dict:
    result = {"database": store.integrity_check()}
    store.audit("DATABASE_INTEGRITY_CHECK", result, actor="service:maintenance")
    return result


def verified_backup(store) -> dict:
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"trading-{datetime.now():%Y%m%d}.db"
    required_tables = {"order_intents", "system_settings", "runtime_config", "campaign_state"}
    backup_valid = False
    if backup.exists():
        try:
            with sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            backup_valid = integrity == "ok" and required_tables.issubset(tables)
        except sqlite3.Error:
            backup_valid = False
    if not backup_valid:
        backup.unlink(missing_ok=True)
        Path(f"{backup}-wal").unlink(missing_ok=True)
        Path(f"{backup}-shm").unlink(missing_ok=True)
        store.backup(backup)
        backup.chmod(0o600)
    with sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"备份数据库完整性检查失败：{integrity}")
    config_backup = backup_dir / f"config-{datetime.now():%Y%m%d}.env"
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists() and not config_backup.exists():
        shutil.copy2(env_file, config_backup)
        config_backup.chmod(0o600)
    manifest = backup_dir / f"manifest-{datetime.now():%Y%m%d}.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": backup.name,
                "database_integrity": integrity,
                "configuration": config_backup.name if config_backup.exists() else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    result = {"backup": "VERIFIED", "database": backup.name}
    store.audit("DATABASE_BACKUP", result, actor="service:maintenance")
    return result


def retention_cleanup(store) -> dict:
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups"))
    backup_retention = max(7, int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
    backup_cutoff = datetime.now().timestamp() - backup_retention * 86400
    if backup_dir.exists():
        for candidate in backup_dir.glob("*"):
            if candidate.is_file() and candidate.stat().st_mtime < backup_cutoff:
                candidate.unlink()
    retention = max(30, int(os.getenv("DATA_RETENTION_DAYS", "365")))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    result = {"cleanup": store.cleanup(cutoff.isoformat())}
    # 大 JSON 表放独立短保留：全局 365 天对它们形同虚设（见各自 docstring 的实测体量）。
    # - config_versions：每次保存存整份快照，VOLATILITY_CAMPAIGNS 高频回写，实测 223 MB
    # - decision_logs：每行带完整 payload，雷达每轮每候选一条，实测 318 MB
    result["config_versions_prune"] = store.prune_config_versions(
        int(os.getenv("CONFIG_VERSION_RETAIN_DAYS", "30")),
    )
    result["decision_logs_prune"] = store.prune_decision_logs(
        int(os.getenv("DECISION_LOG_RETAIN_DAYS", "60")),
    )
    log = Path("data/service.log")
    max_log_bytes = int(os.getenv("MAX_LOG_BYTES", str(10 * 1024 * 1024)))
    if log.exists() and log.stat().st_size > max_log_bytes:
        result["log_rotation"] = "DEFERRED_LIVE_LOG"
    store.audit("RETENTION_CLEANUP", result, actor="service:maintenance")
    return result


# 兼容外部只读调用；调度器必须分别调度上述四个任务。
def maintenance(store, broker) -> dict:
    result = system_health(store, broker)
    result.update(database_integrity_check(store))
    result.update(verified_backup(store))
    result.update(retention_cleanup(store))
    return result
