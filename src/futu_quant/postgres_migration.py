from __future__ import annotations

import os
import sqlite3


def postgres_readiness() -> dict:
    url = os.getenv("DATABASE_URL", "").strip()
    try:
        import psycopg  # noqa: F401
        driver = True
    except ImportError:
        driver = False
    return {
        "configured": url.startswith(("postgresql://", "postgres://")),
        "driver_installed": driver,
        "runtime_backend": os.getenv("DATABASE_BACKEND", "sqlite").lower(),
        "safe_to_migrate": bool(url and driver),
        "message": "已具备迁移条件" if url and driver else "SQLite 继续运行；配置 DATABASE_URL 并安装 postgres extra 后可迁移",
    }


def export_sqlite_to_postgres(sqlite_path, database_url: str | None = None) -> dict:
    """Explicit offline copy. It never changes DATABASE_BACKEND or the live service."""
    url = database_url or os.getenv("DATABASE_URL", "")
    if not url:
        raise ValueError("尚未配置 DATABASE_URL")
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("请先安装可选依赖：uv sync --extra postgres") from exc
    copied = {}
    with sqlite3.connect(sqlite_path) as source, psycopg.connect(url) as target:
        source.row_factory = sqlite3.Row
        tables = [row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            columns = list(source.execute(f'PRAGMA table_info("{table}")'))
            definitions = []
            for column in columns:
                kind = str(column[2]).upper()
                pg_type = "DOUBLE PRECISION" if "REAL" in kind else "BIGINT" if "INT" in kind else "TEXT"
                definitions.append(sql.SQL("{} {}").format(sql.Identifier(column[1]), sql.SQL(pg_type)))
            target.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                sql.Identifier(table), sql.SQL(",").join(definitions)
            ))
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                target.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table)))
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, rows[0].keys())),
                    sql.SQL(",").join(sql.Placeholder() for _ in rows[0].keys()),
                )
                target.executemany(statement, [tuple(row) for row in rows])
            copied[table] = len(rows)
    return copied
