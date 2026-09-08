from __future__ import annotations

import argparse
import sqlite3

from futu_quant.postgres_migration import export_sqlite_to_postgres, postgres_readiness
from futu_quant.store import DEFAULT_DB


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the simulation database to PostgreSQL")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    readiness = postgres_readiness()
    with sqlite3.connect(DEFAULT_DB) as connection:
        tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    print({**readiness, "sqlite_path": str(DEFAULT_DB), "table_count": tables})
    if args.execute:
        if not readiness["safe_to_migrate"]:
            raise RuntimeError(readiness["message"])
        print(export_sqlite_to_postgres(DEFAULT_DB))
    elif not args.dry_run:
        raise RuntimeError("必须显式传入 --dry-run 或 --execute")


if __name__ == "__main__":
    main()
