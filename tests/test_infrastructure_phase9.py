from __future__ import annotations

import json
import time
from pathlib import Path

from futu_quant.disaster_recovery import create_recovery_point, prune_recovery_points, verify_recovery_point
from futu_quant.migrations import migration_status
from futu_quant.parquet_store import archive_market_data
from futu_quant.postgres_migration import postgres_readiness
from futu_quant.rbac import allowed
from futu_quant.storage_monitor import storage_report
from futu_quant.store import TradingStore


def test_versioned_migrations_and_config_release_lifecycle(tmp_path):
    store = TradingStore(tmp_path / "infra.db")
    assert migration_status(store)["healthy"]
    # 采用已在配置中心登记的合法 runtime_config 键测试生命周期
    store.set_runtime_config("CORE_SATELLITE_DAILY_VALIDATION", {"value": 1}, "test:seed")
    release = store.create_config_release("CORE_SATELLITE_DAILY_VALIDATION", {"value": 2}, "test:maker", "test")

    version = store.publish_config_release(release, "test:approver")
    assert version == 2
    assert store.get_runtime_config("CORE_SATELLITE_DAILY_VALIDATION")[0] == {"value": 2}

    rollback_version = store.rollback_config_release(release, "test:approver")
    assert rollback_version == 3
    assert store.get_runtime_config("CORE_SATELLITE_DAILY_VALIDATION")[0] == {"value": 1}
    assert store.config_releases()[0]["status"] == "ROLLED_BACK"


def test_config_release_detects_stale_baseline(tmp_path):
    store = TradingStore(tmp_path / "config.db")
    store.set_runtime_config("CORE_SATELLITE_DAILY_VALIDATION", {"a": 1})
    release = store.create_config_release("CORE_SATELLITE_DAILY_VALIDATION", {"a": 2}, "test:maker")
    store.set_runtime_config("CORE_SATELLITE_DAILY_VALIDATION", {"a": 3})

    try:
        store.publish_config_release(release, "test:approver")
    except ValueError as exc:
        assert "基线已变化" in str(exc)
    else:
        raise AssertionError("stale config release must be rejected")


def test_parquet_archive_is_partitioned_and_records_lineage(tmp_path):
    store = TradingStore(tmp_path / "archive.db")
    store.import_option_history([{
        "code": "US.NVDA260724C220000", "time_key": "2026-07-17T14:30:00+00:00",
        "underlying": "US.NVDA", "underlying_price": 220, "bid": 1.0, "ask": 1.1,
        "quality_status": "GOOD", "quality_reason": "", "source": "TEST",
    }])

    records = archive_market_data(store, tmp_path / "warehouse")

    assert len(records) == 1
    path = Path(records[0]["path"])
    # Paths outside the project are absolute; project archives are relative for portability.
    assert (path if path.is_absolute() else Path.cwd() / path).exists()
    assert store.lineage()[0]["content_hash"] == records[0]["content_hash"]


def test_content_addressed_recovery_point_and_isolated_drill(tmp_path):
    store = TradingStore(tmp_path / "live.db")
    root = tmp_path / "recovery"
    point = create_recovery_point(store, root)
    result = verify_recovery_point(store, point["recovery_id"], root)

    assert result["passed"]
    manifest = json.loads((root / "manifests" / f"{point['recovery_id']}.json").read_text())
    assert (root / "blobs" / manifest["database_blob"]).exists()
    assert store.recovery_points()[0]["status"] == "VERIFIED"


def test_recovery_point_retention_removes_old_unreferenced_blobs(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "recovery-retention.db")
    root = tmp_path / "recovery"
    monkeypatch.setenv("RECOVERY_POINT_RETENTION_COUNT", "1")
    monkeypatch.setenv("RECOVERY_POINT_RETENTION_DAYS", "30")

    first = create_recovery_point(store, root)
    time.sleep(1.1)
    second = create_recovery_point(store, root)
    result = prune_recovery_points(store, root)

    remaining_ids = {item["recovery_id"] for item in store.recovery_points(limit=10)}
    assert second["recovery_id"] in remaining_ids
    assert first["recovery_id"] not in remaining_ids
    assert result["retention_count"] == 1


def test_rbac_storage_and_postgres_are_safe_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_BACKEND", raising=False)

    assert allowed("VIEWER", "read")
    assert not allowed("VIEWER", "trade")
    assert allowed("ADMIN", "publish_config")
    assert postgres_readiness()["runtime_backend"] == "sqlite"
    assert not postgres_readiness()["safe_to_migrate"]
    assert storage_report(tmp_path)["status"] in {"HEALTHY", "WARNING"}


def test_cleanup_uses_task_run_started_at(tmp_path):
    store = TradingStore(tmp_path / "cleanup.db")
    store.start_task_run("old-task", 10, pid=1)
    result = store.cleanup("2999-01-01T00:00:00+00:00")
    assert result["task_runs"] == 1
