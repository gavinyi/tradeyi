from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


MIGRATIONS = (
    Migration(1, "baseline_sqlite_schema", "SELECT 1"),
    Migration(2, "infrastructure_indexes", """
        CREATE INDEX IF NOT EXISTS idx_lineage_type_key ON data_lineage(dataset_type,dataset_key);
        CREATE INDEX IF NOT EXISTS idx_config_releases_status ON config_releases(status,created_at);
        CREATE INDEX IF NOT EXISTS idx_recovery_points_created ON recovery_points(created_at);
    """),
    Migration(3, "volatility_tranche_protection", """
        CREATE TABLE IF NOT EXISTS position_batches (
            batch_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, campaign_id TEXT,
            symbol TEXT NOT NULL, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL DEFAULT 'OPEN',
            allocation_policy TEXT NOT NULL DEFAULT 'FIFO', entry_qty REAL NOT NULL DEFAULT 0,
            remaining_qty REAL NOT NULL DEFAULT 0, entry_cost REAL NOT NULL DEFAULT 0,
            initial_risk REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0,
            mfe_pnl_ratio REAL NOT NULL DEFAULT 0, mae_pnl_ratio REAL NOT NULL DEFAULT 0,
            r_multiple REAL, metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_position_batches_open ON position_batches(strategy_id,symbol,status);
        CREATE TABLE IF NOT EXISTS batch_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            fill_id INTEGER, intent_id INTEGER, opened_at TEXT NOT NULL,
            open_price REAL NOT NULL, open_qty REAL NOT NULL, remaining_qty REAL NOT NULL,
            initial_risk REAL NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(batch_id) REFERENCES position_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS exit_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL NOT NULL,
            open_price REAL NOT NULL, close_price REAL NOT NULL, realized_pnl REAL NOT NULL,
            r_multiple REAL, allocation_rank INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES position_batches(batch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_exit_allocations_batch ON exit_allocations(batch_id,created_at);
        CREATE TABLE IF NOT EXISTS oco_groups (
            oco_id TEXT PRIMARY KEY, batch_id TEXT, strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL, quantity REAL NOT NULL, take_profit_price REAL,
            stop_loss_price REAL, status TEXT NOT NULL DEFAULT 'ARMED',
            trigger_reason TEXT, tp_intent_id INTEGER, sl_intent_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """),
)


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL
    )""")
    applied = {int(row[0]): str(row[1]) for row in connection.execute(
        "SELECT version,checksum FROM schema_migrations"
    )}
    completed = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            if migration.version != 1 and applied[migration.version] != migration.checksum:
                raise RuntimeError(f"迁移 {migration.version} 校验和不一致，拒绝继续")
            continue
        connection.executescript(migration.sql)
        connection.execute(
            "INSERT INTO schema_migrations VALUES(?,?,?,datetime('now'))",
            (migration.version, migration.name, migration.checksum),
        )
        completed.append(migration.version)
    return completed


def migration_status(store) -> dict:
    applied = {row["version"] for row in store.schema_migrations()}
    expected = {item.version for item in MIGRATIONS}
    return {"current": max(applied or {0}), "latest": max(expected),
            "pending": sorted(expected - applied), "healthy": expected.issubset(applied)}
