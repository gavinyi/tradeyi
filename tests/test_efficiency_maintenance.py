"""P2-14 效率：对账明细降采样 + 停机 VACUUM 守卫。

背景（实测生产库）：`reconciliation_reports` 38.4 万行，其中 MATCHED（正常）37.9 万，
60 秒一轮 × 每标的 × 多租户的产物；库 1.6 GB、freelist 约 570 MB 可回收。

守三件事：
1. 只删「旧且正常」的行；**MISMATCH 无论多旧都保留**（事故证据）；
2. VACUUM 之前必须确认服务已停（锁文件即判据）——否则失败或长时间阻塞对账/下单；
3. VACUUM 不做 dry-run 之外的事，且缺磁盘余量要拒绝。

变异：把 `keep_statuses` 去掉（连 MISMATCH 一起删）→ 第 1 条转红；
把 `_service_running` 改成恒返回空 → 第 2 条转红。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from futu_quant.store import TradingStore

# 仓库根兜底：`pytest <file>` 直接跑单文件时不会把 CWD 加进 sys.path，
# `import scripts.vacuum_db` 会 ModuleNotFoundError（假红）。整仓运行不受影响。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _insert_recon(store, *, status: str, age_days: int) -> None:
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO reconciliation_reports"
            " (symbol, broker_quantity, ledger_quantity, difference, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("US.SPY", 1, 1, 0, status, created),
        )


def test_prune_deletes_old_matched_but_keeps_mismatch(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _insert_recon(store, status="MATCHED", age_days=30)
    _insert_recon(store, status="MATCHED", age_days=1)
    _insert_recon(store, status="MISMATCH", age_days=30)

    deleted = store.prune_reconciliation_reports(retain_days=7)

    assert deleted == 1, f"应只删 1 条旧的 MATCHED，实际删了 {deleted}"
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT status, count(1) FROM reconciliation_reports GROUP BY status"
        ).fetchall()
    counts = {status: count for status, count in rows}
    assert counts.get("MATCHED") == 1, "新的 MATCHED 必须保留"
    assert counts.get("MISMATCH") == 1, "MISMATCH 无论多旧都必须保留（事故证据）"


def test_vacuum_refuses_when_service_holds_lock(tmp_path):
    """锁被**真实持有**时必须拒绝 VACUUM。

    只造一个空的 .lock 文件不够：Windows 字节锁不阻止 open，
    用「能 open 就算在跑」会漏报；用「文件存在就算在跑」会把正常停机
    （stop.bat/进程退出都不删 .lock）误判成在跑。判据只能是**抢锁失败**。
    """
    from futu_quant.filelock import exclusive_lock
    from scripts.vacuum_db import _service_running

    (tmp_path / "data").mkdir()
    lock = tmp_path / "data" / "trading-service-futu_paper_sandbox.lock"
    lock.write_text("1", encoding="utf-8")

    with exclusive_lock(lock):
        assert _service_running(tmp_path) == [lock.name]


def test_vacuum_allows_stale_lock_file(tmp_path):
    """陈旧锁文件（无人持有）不得阻塞停机维护。"""
    from scripts.vacuum_db import _service_running

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trading-service.lock").write_text("123\n", encoding="utf-8")

    assert _service_running(tmp_path) == []


def test_vacuum_dry_run_stats(tmp_path):
    from scripts.vacuum_db import _stats

    db = tmp_path / "t.db"
    store = TradingStore(db)
    _insert_recon(store, status="MATCHED", age_days=1)
    store.prune_reconciliation_reports(retain_days=0)

    stats = _stats(db)
    assert stats["size_mb"] > 0
    assert "reclaimable_mb" in stats


def test_hot_path_indexes_reach_existing_databases(tmp_path):
    """热路径索引必须走**存量库必经之路**（版本化迁移）。

    与 alerts 三列同源的问题：只写在 `initialize()` 的「新建库」分支时，存量库拿不到。
    生产实测缺 `idx_decision_logs_lookup`，决策查询退化为全表扫。
    """
    store = TradingStore(tmp_path / "t.db")
    with store.connection() as connection:
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
    assert 15 in applied, "Migration(15) 未记录为已应用"
    for required in (
        "idx_decision_logs_lookup",
        "idx_decision_tenant",
        "idx_operational_events_id_desc",
    ):
        assert required in names, f"热路径索引缺失：{required}"

