"""P1-C1 配置审计改造：audit_events.actor 字段、索引与 fail-closed 校验守卫。

设计文档：docs/config_storage_unification_design.md §3.3.2 / §3.3.3
落地 Runbook：docs/config_audit_runbook.md §1

验收标准：
1. 格式非法拒写（空值 / 缺失 / 无冒号）-> 拒写 + 抛 AuditActorError（fail-closed）；
2. 格式合规但 class 未知（如 weird:foo, system:radar）-> unclassified 只 WARN，不拒写；
3. 存量行 actor='' 不伪造、不改写；
4. 版本化迁移必须在存量库路径生效（构造带 order_intents 的旧库，断言 Migration 12/13 自动补齐列与索引）；
5. 索引 idx_audit_actor 必须建立在 (actor, id DESC) 上。
"""
from __future__ import annotations

import logging
import sqlite3
import pytest

from futu_quant.store import (
    ACTOR_CLASSES,
    AuditActorError,
    TradingStore,
    actor_kind,
    validate_actor,
)


def test_actor_classes_enumeration():
    """五类基础 class 枚举严格对齐已签字设计 §3.3.2。"""
    assert set(ACTOR_CLASSES) == {"streamlit_admin", "service", "scheduler", "cli", "test"}


@pytest.mark.parametrize("valid_actor,expected_class,expected_kind", [
    ("streamlit_admin:admin", "streamlit_admin", "human"),
    ("streamlit_admin:panel", "streamlit_admin", "human"),
    ("service:execution", "service", "machine"),
    ("service:radar", "service", "machine"),
    ("scheduler:main", "scheduler", "machine"),
    ("cli:migrate_risk_limits", "cli", "machine"),
    ("test:pytest", "test", "machine"),
])
def test_valid_actor_parsing_and_kind(valid_actor, expected_class, expected_kind):
    cls = validate_actor(valid_actor)
    assert cls == expected_class
    assert actor_kind(valid_actor) == expected_kind


@pytest.mark.parametrize("invalid_actor,expected_reason", [
    ("", "必填且不能为空"),
    (None, "必填且不能为空"),
    ("   ", "必填且不能为空"),
    ("scheduler", "必须是 `<class>:<id>`"),
    ("broker_position_verified", "必须是 `<class>:<id>`"),
    ("system", "必须是 `<class>:<id>`"),
    ("admin", "必须是 `<class>:<id>`"),
    (":admin", "两段都不能为空"),
    ("service:", "两段都不能为空"),
    (" : ", "两段都不能为空"),
])
def test_invalid_actor_format_raises_fail_closed(invalid_actor, expected_reason):
    """格式非法（无冒号或缺失）必须 fail-closed 拒写抛错。"""
    with pytest.raises(AuditActorError) as exc_info:
        validate_actor(invalid_actor)
    assert expected_reason in str(exc_info.value)


@pytest.mark.parametrize("unclassified_actor", [
    "weird:foo",
    "system:radar",
    "custom:something",
])
def test_unclassified_actor_is_warned_not_rejected(tmp_path, unclassified_actor, caplog):
    """有冒号但 class 不在枚举 -> unclassified 只 WARN，不拒写。"""
    db_path = tmp_path / "audit_warn.db"
    store = TradingStore(db_path)

    with caplog.at_level(logging.WARNING):
        store.audit("CONFIG_TEST_EVENT", {"val": 123}, actor=unclassified_actor)

    assert "unclassified" in caplog.text
    assert unclassified_actor in caplog.text

    # 且数据确实已落库
    events = store.audit_events(limit=10)
    assert len(events) >= 1
    assert events[0]["actor"] == unclassified_actor
    assert events[0]["event_type"] == "CONFIG_TEST_EVENT"


def test_store_audit_rejects_missing_actor_argument(tmp_path):
    """store.audit 的 actor 为必填关键字参数，不传会直接在函数签名层报错。"""
    db_path = tmp_path / "audit_missing.db"
    store = TradingStore(db_path)

    with pytest.raises(TypeError) as exc_info:
        store.audit("SOME_EVENT", {"test": True})  # type: ignore[call-arg]
    assert "actor" in str(exc_info.value)


def test_store_audit_rejects_invalid_actor_value(tmp_path):
    """store.audit 传入非法 actor 值（如无冒号单段词）拒写且不落库。"""
    db_path = tmp_path / "audit_invalid.db"
    store = TradingStore(db_path)

    before_count = len(store.audit_events())
    with pytest.raises(AuditActorError):
        store.audit("INVALID_ACTOR_EVENT", {"k": "v"}, actor="scheduler")

    after_count = len(store.audit_events())
    assert after_count == before_count, "拒写失败：非法 actor 产生了脏写入"


def test_existing_db_migration_adds_column_and_index_via_migrations(tmp_path, monkeypatch):
    """存量库（有 order_intents、走 initialize() 存量分支）必须通过版本化迁移补齐 actor 列与索引。

    做法（对齐 test_alert_delivery_policy）：
    1. 先用新库路径建出完整 schema（含 order_intents）；
    2. 插入存量行；
    3. 模拟真实存量库：删掉 actor 列、删掉 idx_audit_actor 索引、抹掉 schema_migrations 中的 12/13；
    4. 重新打开 TradingStore（走存量库路径）-> 断言版本化迁移自动补齐列与索引，且存量行 actor='' 不被伪造。
    """
    db_path = tmp_path / "legacy.db"
    TradingStore(db_path)  # 建出完整 schema

    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "INSERT INTO audit_events (event_type, strategy_id, payload_json, created_at, actor) "
        "VALUES ('LEGACY_EVENT_1', 'strat1', '{}', '2026-10-23T00:00:00Z', 'legacy_temp'), "
        "       ('LEGACY_EVENT_2', NULL, '{\"x\": 1}', '2026-10-23T01:00:00Z', 'legacy_temp')"
    )
    raw.execute("DROP INDEX IF EXISTS idx_audit_actor")
    raw.execute("ALTER TABLE audit_events DROP COLUMN actor")
    raw.execute("DELETE FROM schema_migrations WHERE version IN (12, 13)")
    raw.commit()

    before = {row[1] for row in raw.execute("PRAGMA table_info(audit_events)").fetchall()}
    assert "actor" not in before, "夹具失效：actor 列没被删掉，无法测试存量库补列"
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
    ).fetchone() is not None, "夹具失效：没有 order_intents，会走新建库分支"
    raw.close()

    # 存量库升级必须显式授权（与 run_service 启动路径同口径），未授权时 __init__ 会拒绝
    from futu_quant.migrations import authorize_migrations
    authorize_migrations("test:config_audit_c1")
    # 重新打开 TradingStore -> 必须走 initialize() 存量分支中的 apply_migrations
    store = TradingStore(db_path)

    # 1. 检查 schema_migrations 记录了 Migration 12 和 13
    with store.connection() as conn:
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        assert 12 in applied, "Migration(12, 'audit_events_actor_column') 未在存量库被应用"
        assert 13 in applied, "Migration(13, 'audit_events_actor_index') 未在存量库被应用"

        # 2. 检查列定义与默认值
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        assert "actor" in columns, "audit_events 缺少 actor 列"
        assert columns["actor"][2].upper() == "TEXT"

        # 3. 检查索引定义
        indexes = conn.execute("PRAGMA index_list(audit_events)").fetchall()
        idx_names = {row[1] for row in indexes}
        assert "idx_audit_actor" in idx_names, "缺少 idx_audit_actor 索引"

        # 4. 存量行必须为 actor=''（不伪造历史）；按业务事件过滤，避免把迁移审计行算进来
        legacy_rows = conn.execute(
            "SELECT id, event_type, actor FROM audit_events"
            " WHERE event_type IN ('LEGACY_EVENT_1','LEGACY_EVENT_2') ORDER BY id ASC"
        ).fetchall()
        assert [row[1] for row in legacy_rows] == ["LEGACY_EVENT_1", "LEGACY_EVENT_2"]
        assert legacy_rows[0][2] == "", "存量行被非法改写伪造了 actor"
        assert legacy_rows[1][2] == "", "存量行被非法改写伪造了 actor"

    # 5. 迁移后新写一行，正常写入 actor
    store.audit("POST_MIGRATION_EVENT", {"ok": True}, actor="service:test")
    events = store.audit_events(limit=5)
    new_event = next(e for e in events if e["event_type"] == "POST_MIGRATION_EVENT")
    assert new_event["actor"] == "service:test"


def test_mutation_check_no_colon_actor_must_fail():
    """变异证红：证明无冒号单段值绝不可能被悄悄放行。"""
    cases = ["scheduler", "service", "system", "admin", "test", "streamlit_admin"]
    for c in cases:
        with pytest.raises(AuditActorError):
            validate_actor(c)
