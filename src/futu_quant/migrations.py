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
