"""补齐 runtime_config 存量键的历史快照（P1-②，只读迁移：只 INSERT config_versions）。

问题（P1-② 只读审计，见 data/qa/inspect_version_coverage.py 输出）：
`CORE_SATELLITE_DAILY_VALIDATION`（v12）与 `GAMMA_CAMPAIGNS`（v14）在
`config_versions` 里 **0 行历史** —— 它们是「版本号一直在涨、历史账本从未记录」
的历史缺口键（写入方走了不走 `record_config_version` 的旧渠道）。

修复方式（**纯只读迁移**）：
- 对每个缺口键，把**当前生效值**补一行 `config_versions`
  （actor=ops:history_backfill，reason 注明补写口径与当时 version）。
- 不改 runtime_config（值与 version 都不动，**不算一次版本变更**，
  只补历史留痕，让「每一条生效配置都能在 config_versions 追溯到至少一行」）。
- 幂等：同键已存在 `history_backfill` 行（value_json 与当前值一致）则跳过。
"""

from __future__ import annotations

import argparse
import json
import sqlite3

BACKFILL_ACTOR = "ops:history_backfill"


def _is_backfilled(conn: sqlite3.Connection, key: str, value_json: str) -> bool:
    row = conn.execute(
        "SELECT value_json FROM config_versions "
        "WHERE config_key=? AND actor=? ORDER BY id DESC LIMIT 1",
        (key, BACKFILL_ACTOR),
    ).fetchone()
    return row is not None and row[0] == value_json


def backfill(db_path: str, apply: bool) -> int:
    if apply:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    keys = [r[0] for r in conn.execute("SELECT config_key FROM runtime_config").fetchall()]
    missing: list[tuple[str, str, int, str]] = []
    for key in keys:
        row = conn.execute(
            "SELECT value_json, version FROM runtime_config WHERE config_key=?", (key,)
        ).fetchone()
        if row is None:
            continue
        history = conn.execute(
            "SELECT COUNT(*) FROM config_versions WHERE config_key=?", (key,)
        ).fetchone()[0]
        if history == 0:
            missing.append((key, row["value_json"], int(row["version"]), row["value_json"]))
    conn.close()
    if not missing:
        print("[补写] 无缺口：所有 runtime_config 键在 config_versions 均有历史行")
        return 0
    print(f"[补写] 缺口键 {len(missing)} 个：{[m[0] for m in missing]}")
    if not apply:
        print("[dry-run] 仅展示，不写库")
        return len(missing)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key, value_json, version, _snapshot in missing:
            if _is_backfilled(conn, key, value_json):
                print(f"  - 跳过（已补写且值一致）：{key}")
                continue
            conn.execute(
                "INSERT INTO config_versions(config_key, value_json, actor, reason, created_at) "
                "VALUES(?,?,?,?,datetime('now'))",
                (
                    key,
                    value_json,
                    BACKFILL_ACTOR,
                    f"P1-② 历史缺口补写：runtime_config v{version} 生效值入账，"
                    "写入方为不走 record_config_version 的旧渠道（只补留痕，不改版本）",
                ),
            )
            print(f"  - 已补写：{key}（v{version} 生效值）")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(missing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="目标 SQLite 库路径")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省只 dry-run")
    args = parser.parse_args()
    n = backfill(args.db, args.apply)
    print(f"[完成] {'已写' if args.apply else 'dry-run'}：{n} 个键")


if __name__ == "__main__":
    main()
