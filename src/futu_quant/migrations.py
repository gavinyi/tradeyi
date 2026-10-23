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
    Migration(4, "combo_lifecycle_safety", """
        ALTER TABLE option_combo_legs ADD COLUMN close_filled_quantity REAL NOT NULL DEFAULT 0;
        ALTER TABLE option_combo_legs ADD COLUMN broker_status TEXT NOT NULL DEFAULT '';
        ALTER TABLE option_combo_legs ADD COLUMN remaining_quantity REAL NOT NULL DEFAULT 0;
        ALTER TABLE option_combo_legs ADD COLUMN last_broker_update TEXT;
        CREATE INDEX IF NOT EXISTS idx_combo_legs_symbol_status ON option_combo_legs(symbol,status);
    """),
    Migration(5, "shadow_incubator_track_mode", """
        ALTER TABLE shadow_incubator ADD COLUMN track_mode TEXT NOT NULL DEFAULT 'STANDARD';
    """),
    Migration(6, "equity_snapshots_env_isolation", """
        ALTER TABLE equity_snapshots ADD COLUMN env TEXT NOT NULL DEFAULT 'SIMULATE';
        CREATE INDEX IF NOT EXISTS idx_equity_snapshots_env_time ON equity_snapshots(env, created_at);
    """),
    Migration(7, "reconciliation_adjustments_tenant_account", """
        CREATE TABLE IF NOT EXISTS reconciliation_adjustments_v2 (
            tenant_id TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id, account_id, strategy_id, symbol)
        );
        INSERT OR REPLACE INTO reconciliation_adjustments_v2
            (tenant_id, account_id, strategy_id, symbol, quantity, reason, updated_at)
        SELECT '', '', strategy_id, symbol, quantity, reason, updated_at
        FROM reconciliation_adjustments;
        DROP TABLE reconciliation_adjustments;
        ALTER TABLE reconciliation_adjustments_v2 RENAME TO reconciliation_adjustments;
        CREATE INDEX IF NOT EXISTS idx_recon_adj_tenant
            ON reconciliation_adjustments(tenant_id, account_id, strategy_id, symbol);
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
        try:
            connection.executescript(migration.sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                pass
            else:
                raise
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
