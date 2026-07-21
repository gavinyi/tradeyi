from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile


ROOT = Path(__file__).resolve().parents[2]
RECOVERY_ROOT = ROOT / "data" / "recovery"


def _portable_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_blob(source: Path, blob_dir: Path) -> str:
    checksum = file_hash(source)
    target = blob_dir / checksum
    if not target.exists():
        shutil.copy2(source, target)
        target.chmod(0o600)
    return checksum


def create_recovery_point(store, root: Path | str = RECOVERY_ROOT) -> dict:
    root = Path(root)
    blob_dir = root / "blobs"
    manifest_dir = root / "manifests"
    blob_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    recovery_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as directory:
        database_copy = Path(directory) / "trading.db"
        store.backup(database_copy)
        database_hash = _store_blob(database_copy, blob_dir)
    env_file = ROOT / ".env"
    config_hash = _store_blob(env_file, blob_dir) if env_file.exists() else None
    warehouse = ROOT / "data" / "warehouse"
    data_manifest = {
        str(path.relative_to(ROOT)): file_hash(path) for path in warehouse.rglob("*.parquet")
    } if warehouse.exists() else {}
    data_bytes = json.dumps(data_manifest, sort_keys=True, separators=(",", ":")).encode()
    data_hash = hashlib.sha256(data_bytes).hexdigest()
    manifest = {
        "recovery_id": recovery_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "database_blob": database_hash, "config_blob": config_hash,
        "data_files": data_manifest, "data_manifest_hash": data_hash,
        "execution_safety": "SIMULATE_ONLY",
    }
    manifest_path = manifest_dir / f"{recovery_id}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest_path.chmod(0o600)
    row = {"recovery_id": recovery_id, "manifest_path": _portable_path(manifest_path),
           "database_hash": database_hash, "config_hash": config_hash, "data_hash": data_hash,
           "status": "CREATED", "created_at": manifest["created_at"]}
    store.save_recovery_point(row)
    return row


def verify_recovery_point(store, recovery_id: str, root: Path | str = RECOVERY_ROOT) -> dict:
    root = Path(root)
    manifest_path = root / "manifests" / f"{recovery_id}.json"
    if not manifest_path.exists():
        raise ValueError("恢复点清单不存在")
    manifest = json.loads(manifest_path.read_text())
    database_blob = root / "blobs" / manifest["database_blob"]
    checks = {"database_hash": database_blob.exists() and file_hash(database_blob) == manifest["database_blob"]}
    with tempfile.TemporaryDirectory() as directory:
        restored = Path(directory) / "restored.db"
        shutil.copy2(database_blob, restored)
        with sqlite3.connect(restored) as connection:
            checks["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            checks["required_tables"] = {"order_intents", "runtime_config", "schema_migrations"}.issubset(tables)
    checks["data_manifest"] = hashlib.sha256(
        json.dumps(manifest["data_files"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == manifest["data_manifest_hash"]
    passed = all(checks.values())
    now = datetime.now(timezone.utc).isoformat()
    store.save_recovery_point({
        "recovery_id": recovery_id, "manifest_path": _portable_path(manifest_path),
        "database_hash": manifest["database_blob"], "config_hash": manifest.get("config_blob"),
        "data_hash": manifest["data_manifest_hash"], "status": "VERIFIED" if passed else "FAILED",
        "verified_at": now, "created_at": manifest["created_at"],
    })
    return {"passed": passed, **checks}


def latest_recovery_drill(store) -> dict:
    points = store.recovery_points(limit=1)
    if not points:
        point = create_recovery_point(store)
    else:
        point = points[0]
    return verify_recovery_point(store, point["recovery_id"])
