"""迁移「duplicate column name」被吞掉时必须校验既有列定义。

背景：生产事故时手工 ALTER 过 `alerts` 三列，迁移再跑会报 duplicate column。
吞掉是对的（不重复加列），但**同名列类型不同 = 静默不一致**：查询/写入拿到的类型
与代码假设不同，比缺列更难查。这里钉住「存在即校验定义」。

变异：去掉 `_verify_existing_column` 里的类型比较 → 类型不一致用例转红。
"""

from __future__ import annotations

import sqlite3

import pytest

from futu_quant.migrations import _verify_existing_column


def _conn(tmp_path, ddl: str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(tmp_path / "m.db"))
    connection.executescript(ddl)
    return connection


def test_matching_definition_is_accepted(tmp_path):
    connection = _conn(tmp_path, "CREATE TABLE t (id INTEGER PRIMARY KEY, actor TEXT);")
    # 类型一致 → 不抛（这就是生产手工补列后再跑迁移的形态）
    _verify_existing_column(
        connection, "ALTER TABLE t ADD COLUMN actor TEXT NOT NULL DEFAULT '';",
        sqlite3.OperationalError("duplicate column name: actor"),
    )
    connection.close()


def test_mismatched_definition_raises(tmp_path):
    connection = _conn(tmp_path, "CREATE TABLE t (id INTEGER PRIMARY KEY, actor INTEGER);")
    with pytest.raises(RuntimeError, match="类型不一致"):
        _verify_existing_column(
            connection, "ALTER TABLE t ADD COLUMN actor TEXT NOT NULL DEFAULT '';",
            sqlite3.OperationalError("duplicate column name: actor"),
        )
    connection.close()


def test_missing_column_raises(tmp_path):
    connection = _conn(tmp_path, "CREATE TABLE t (id INTEGER PRIMARY KEY);")
    with pytest.raises(RuntimeError, match="并不存在"):
        _verify_existing_column(
            connection, "ALTER TABLE t ADD COLUMN actor TEXT;",
            sqlite3.OperationalError("duplicate column name: actor"),
        )
    connection.close()
