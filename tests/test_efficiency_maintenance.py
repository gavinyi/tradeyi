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

from datetime import datetime, timedelta, timezone

import pytest

from futu_quant.store import TradingStore


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


def test_vacuum_refuses_when_service_lock_present(tmp_path):
    from scripts.vacuum_db import _service_running

    root = tmp_path
    (root / "data").mkdir()
    (root / "data" / "trading-service-futu_paper_sandbox.lock").write_text("1", encoding="utf-8")

    assert _service_running(root) == ["trading-service-futu_paper_sandbox.lock"]


def test_vacuum_dry_run_stats(tmp_path):
    from scripts.vacuum_db import _stats

    db = tmp_path / "t.db"
    store = TradingStore(db)
    _insert_recon(store, status="MATCHED", age_days=1)
    store.prune_reconciliation_reports(retain_days=0)

    stats = _stats(db)
    assert stats["size_mb"] > 0
    assert "reclaimable_mb" in stats
