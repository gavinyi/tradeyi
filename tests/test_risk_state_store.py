"""`risk_state` 存储层守卫（G4，建表幂等 + 单行 upsert + backfill 峰值）。

对应设计 D1/D2.2：
- 新表独立，**不改 `equity_snapshots`**；
- 主键 `(tenant_id, scope)`，重复保存必须 upsert 而不是插重复行；
- 首次启用必须能取到 backfill 的历史峰值（禁止用当前权益冒充，见 test_risk_breaker）。
"""

from __future__ import annotations

import sqlite3

import pytest

from futu_quant.store import TradingStore

STATE = dict(
    tenant_id="281756481476169109",
    scope="equity",
    peak_hist=1_000.0,
    peak_hist_at="2026-10-25T14:00:00",
    peak_hist_source="backfill",
    baseline_equity=900.0,
    baseline_at="2026-10-25T14:00:00",
    baseline_source="live",
    peak_reset=900.0,
    breaker_level=0,
    breaker_reason="",
    de_risk_scale=1.0,
    reset_count_30d=0,
    updated_at="2026-10-25T14:00:00",
)


def test_risk_state_table_created_on_existing_db_via_migration(tmp_path, monkeypatch):
    from futu_quant.migrations import authorize_migrations
    authorize_migrations("test:risk_state_store")  # 迁移授权（P0 门禁，见 test_migration_gate）
    """存量库分支守卫：`initialize()` 对存量库是 `apply_migrations()` 后直接 return，

    只写「新建库」分支的建表语句在存量库上**永远不生效**（alerts 三列同源缺陷）。
    做法：先建一个完整库，再删掉 risk_state + 抹掉迁移 16 记录，模拟 G4 之前的生产库。
    """
    path = tmp_path / "existing.db"
    TradingStore(path)
    con = sqlite3.connect(path)
    con.execute("DROP TABLE risk_state")
    con.execute("DELETE FROM schema_migrations WHERE version=16")
    con.commit()
    con.close()

    TradingStore(path)  # 存量库分支：必须靠迁移把表补回来
    con = sqlite3.connect(path)
    has = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='risk_state'"
    ).fetchone()[0]
    migration = con.execute(
        "SELECT name FROM schema_migrations WHERE version=16"
    ).fetchone()
    con.close()
    assert has == 1, "存量库没有 risk_state：DDL 写错分支（必须走版本化迁移）"
    assert migration is not None and migration[0] == "risk_state_circuit_breaker"
    # 幂等：再打开一次不报错、不重复记迁移
    TradingStore(path)
    con = sqlite3.connect(path)
    count = con.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=16").fetchone()[0]
    con.close()
    assert count == 1


def test_risk_state_table_created_and_initially_empty(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    assert store.load_risk_state("281756481476169109") is None
    with store.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_state'"
        ).fetchone()
    assert row is not None, "risk_state 表未建（建表必须幂等）"


def test_risk_state_upsert_is_single_row_per_tenant(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    store.save_risk_state(STATE)
    store.save_risk_state({**STATE, "breaker_level": 2, "updated_at": "2026-10-25T15:00:00"})
    loaded = store.load_risk_state(STATE["tenant_id"])
    assert loaded["breaker_level"] == 2
    with store.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM risk_state WHERE tenant_id=?", (STATE["tenant_id"],)
        ).fetchone()[0]
    assert count == 1, "同一租户必须只有一行（upsert 而非重复插入）"


def test_risk_state_scope_isolated(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    store.save_risk_state(STATE)
    store.save_risk_state({**STATE, "scope": "margin", "peak_hist": 2_000.0})
    assert store.load_risk_state(STATE["tenant_id"])["peak_hist"] == pytest.approx(1_000.0)
    assert store.load_risk_state(STATE["tenant_id"], "margin")["peak_hist"] == pytest.approx(2_000.0)


def test_risk_state_requires_peak_fields(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    for missing in ("peak_hist", "baseline_equity", "peak_reset", "updated_at"):
        with pytest.raises(ValueError, match="缺必填字段"):
            store.save_risk_state({**STATE, missing: None})


def test_historical_peak_equity_full_history_and_account_env(tmp_path):
    """Product/Architect 验收第 19 条：全历史 + 账户专属 env + 两条 fail-closed。"""
    store = TradingStore(tmp_path / "risk.db")
    account_env = "REAL_281756481476169109"  # 注意末三位 109，写错一位就零行
    store.record_equity(58_156.47, 40_000.0, 18_156.47, env=account_env)
    store.record_equity(53_504.75, 40_000.0, 13_504.75, env=account_env)
    # legacy：峰值远低于当前权益，用它 backfill 会把回撤洗成 0（假绿）
    store.record_equity(30_007.59, 20_000.0, 10_000.0, env="REAL")
    # SIMULATE：峰值 100 万，跨环境取会误触发 95% 回撤
    store.record_equity(1_008_376.58, 500_000.0, 508_376.58, env="SIMULATE")

    peak = store.historical_peak_equity(account_env, current_equity=53_504.75)
    assert peak == pytest.approx(58_156.47), "必须取账户专属 env 的全历史峰值"

    # ① env 必填
    with pytest.raises(ValueError, match="账户专属 env"):
        store.historical_peak_equity("")
    # ② env 零行 → fail-closed（写错一位不能当「无历史」放过）
    with pytest.raises(ValueError, match="零行"):
        store.historical_peak_equity("REAL_281756481476169110", current_equity=53_504.75)
    # ③ peak < 当前权益 → 拒绝用作基准（legacy REAL 的典型形态）
    with pytest.raises(ValueError, match="低于当前权益"):
        store.historical_peak_equity("REAL", current_equity=53_504.75)
    # ④ 不传 current_equity 时只做零行校验（调用侧必须自己再核倒挂）
    assert store.historical_peak_equity("REAL") == pytest.approx(30_007.59)


def test_risk_state_survives_reopen(tmp_path):
    path = tmp_path / "risk.db"
    TradingStore(path).save_risk_state(STATE)
    reopened = TradingStore(path)
    assert reopened.load_risk_state(STATE["tenant_id"])["peak_hist"] == pytest.approx(1_000.0)


def test_risk_state_table_has_no_not_null_breach(tmp_path):
    """DDL 的 NOT NULL 列必须被 save 覆盖（防以后加列漏填导致写入报错）。"""
    store = TradingStore(tmp_path / "risk.db")
    with store.connection() as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(risk_state)").fetchall()
        }
    assert set(columns) >= {
        "peak_hist", "peak_reset", "baseline_equity", "ack_dd_vs_peak",
        "de_risk_scale", "reset_count_30d",
    }
    store.save_risk_state(STATE)  # 不应抛 NOT NULL constraint
    # 未在必填校验里列的 NOT NULL 列由数据库兜底（防以后加列漏填静默写入 NULL）
    with pytest.raises(sqlite3.IntegrityError):
        store.save_risk_state({**STATE, "peak_hist_source": None})
