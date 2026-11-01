"""迁移脚本：为 task_runs 表增加 tenant_id 列并创建联合索引。

支持三阶段解耦平滑演进与幂等回滚：
1. 阶段 1：幂等添加可空列 tenant_id (毫秒级 DDL 元数据操作，持锁几乎为 0)；
2. 阶段 2：后台分批回填历史行的 tenant_id (每次 batch_size 行，避免长事务锁表)；
3. 阶段 3：一次性创建 idx_task_runs_tenant_status 联合索引 (在回填完成后执行)；
4. 回滚能力：支持 --rollback 参数安全清除索引。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time

logger = logging.getLogger("migration.v25_task_runs_tenant_id")


def add_tenant_id_column(connection: sqlite3.Connection) -> bool:
    """阶段 1：幂等添加可空列 tenant_id。"""
    cursor = connection.execute("PRAGMA table_info(task_runs)")
    columns = [row[1] for row in cursor.fetchall()]
    if "tenant_id" in columns:
        logger.info("task_runs.tenant_id 已经存在，跳过加列。")
        return False

    t0 = time.perf_counter()
    connection.execute("ALTER TABLE task_runs ADD COLUMN tenant_id TEXT")
    cost = time.perf_counter() - t0
    logger.info("成功添加 task_runs.tenant_id 可空列，耗时 %.4f 秒。", cost)
    return True


def backfill_tenant_id(
    connection: sqlite3.Connection,
    batch_size: int = 5000,
    default_tenant: str = "futu_paper_sandbox",
) -> int:
    """阶段 2：分批回填历史数据，避免单事务长时间锁库。"""
    cursor = connection.execute(
        "SELECT COUNT(*) FROM task_runs WHERE tenant_id IS NULL"
    )
    total_null = cursor.fetchone()[0]
    if total_null == 0:
        logger.info("所有 task_runs 均已具备 tenant_id，无需回填。")
        return 0

    logger.info("开始分批回填历史 task_runs.tenant_id，待回填总数：%d，批次大小：%d", total_null, batch_size)
    total_updated = 0
    while True:
        # 针对历史没有 tenant_id 的批次进行回填
        t0 = time.perf_counter()
        cursor = connection.execute(
            """
            UPDATE task_runs
            SET tenant_id = ?
            WHERE id IN (
                SELECT id FROM task_runs WHERE tenant_id IS NULL LIMIT ?
            )
            """,
            (default_tenant, batch_size),
        )
        updated = cursor.rowcount
        connection.commit()
        cost = time.perf_counter() - t0
        total_updated += updated
        if updated > 0:
            logger.info("已回填 %d / %d 行，本批耗时 %.4f 秒...", total_updated, total_null, cost)
        if updated < batch_size:
            break

    logger.info("历史回填全部完成，累计回填：%d 行。", total_updated)
    return total_updated


def create_tenant_indexes(connection: sqlite3.Connection) -> bool:
    """阶段 3：在数据回填完成后，一次性创建联合索引。"""
    t0 = time.perf_counter()
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_tenant_status ON task_runs(tenant_id, status, started_at)"
    )
    cost = time.perf_counter() - t0
    logger.info("成功创建联合索引 idx_task_runs_tenant_status，耗时 %.4f 秒。", cost)
    return True


def rollback_migration(connection: sqlite3.Connection) -> None:
    """回滚：删除联合索引 (SQLite 无法 DROP COLUMN，但 NULL 列不影响任何运行逻辑)。"""
    t0 = time.perf_counter()
    connection.execute("DROP INDEX IF EXISTS idx_task_runs_tenant_status")
    cost = time.perf_counter() - t0
    logger.info("成功回滚删除 idx_task_runs_tenant_status 索引，耗时 %.4f 秒。", cost)


def run_migration(
    db_path: str,
    stage: str = "all",
    batch_size: int = 5000,
    rollback: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("打开数据库进行迁移：%s", db_path)
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        if rollback:
            rollback_migration(connection)
            return

        if stage in ("1", "all"):
            add_tenant_id_column(connection)
        if stage in ("2", "all"):
            backfill_tenant_id(connection, batch_size=batch_size)
        if stage in ("3", "all"):
            create_tenant_indexes(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Task runs tenant_id migration")
    parser.add_argument("--db", required=True, help="Path to sqlite trading.db")
    parser.add_argument("--stage", choices=["1", "2", "3", "all"], default="all", help="Migration stage")
    parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for backfilling")
    parser.add_argument("--rollback", action="store_true", help="Rollback index")
    args = parser.parse_args()

    run_migration(args.db, stage=args.stage, batch_size=args.batch_size, rollback=args.rollback)
