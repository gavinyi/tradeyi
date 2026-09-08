from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3


def verify_backup_bundle(manifest_path: Path | str, restore_dir: Path | str) -> dict:
    """Restore a backup into an isolated directory and verify it is usable."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    source_dir = manifest_path.parent
    restore_dir = Path(restore_dir)
    restore_dir.mkdir(parents=True, exist_ok=True)
    database = restore_dir / "trading.db"
    shutil.copy2(source_dir / manifest["database"], database)
    config_name = manifest.get("configuration")
    if config_name:
        shutil.copy2(source_dir / config_name, restore_dir / ".env")
    with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        required = {"order_intents", "system_settings", "runtime_config", "campaign_state"}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    missing = sorted(required - tables)
    if integrity != "ok" or missing:
        raise RuntimeError(f"恢复验证失败：integrity={integrity}, missing={missing}")
    return {"integrity": integrity, "missing_tables": missing, "database": str(database)}
