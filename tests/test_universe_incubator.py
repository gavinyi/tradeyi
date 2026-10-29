"""
测试模块 2: 隔离沙盒影子演练与微观特征提取器 (Sandbox Incubator)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

import pandas as pd
import pytest

from futu_quant.scheduled_tasks import run_daily_universe_ranking
from futu_quant.store import TradingStore
from futu_quant.universe.incubator_engine import IncubatorEngine


def test_incubator_signal_and_trade_aggregation():
    db_path = Path("data/test_incubator.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        engine = IncubatorEngine(store=store)

        # 1. 模拟记录 3 次突破信号
        engine.record_simulated_signal("US.PLTR", z_score=2.3, volume_ratio=1.5, spread_pct=0.02)
        engine.record_simulated_signal("US.PLTR", z_score=2.5, volume_ratio=1.8, spread_pct=0.018)
        engine.record_simulated_signal("US.PLTR", z_score=2.1, volume_ratio=1.3, spread_pct=0.022)

        data = store.incubator_symbol("US.PLTR")
        assert data is not None
        assert data["signal_triggers"] == 3

        # 2. 模拟 2 胜 1 负的虚拟交易
        engine.update_simulated_trade("US.PLTR", is_win=True, pnl_usd=150.0, return_ratio=0.15, slippage_pct=0.015, volume_retention=0.75)
        engine.update_simulated_trade("US.PLTR", is_win=True, pnl_usd=200.0, return_ratio=0.20, slippage_pct=0.018, volume_retention=0.80)
        engine.update_simulated_trade("US.PLTR", is_win=False, pnl_usd=-80.0, return_ratio=-0.08, slippage_pct=0.020, volume_retention=0.65)

        updated = store.incubator_symbol("US.PLTR")
        assert updated["simulated_wins"] == 2
        assert updated["simulated_losses"] == 1
        assert round(updated["simulated_pnl_usd"], 2) == 270.0
        assert updated["simulated_max_dd"] < 0.25

        # 3. 模拟满 10 日且高分后晋级门槛评估
        updated["latest_ml_score"] = 0.82
        store.upsert_incubator_metrics("US.PLTR", updated)

        eligible = engine.evaluate_daily_metrics("US.PLTR", current_days=10)
        assert eligible is True

        final_data = store.incubator_symbol("US.PLTR")
        assert final_data["promotion_eligible"] == 1

        # 4. 若回撤超过 25%，应一票否决
        final_data["simulated_max_dd"] = 0.28
        store.upsert_incubator_metrics("US.PLTR", final_data)
        eligible_fail = engine.evaluate_daily_metrics("US.PLTR", current_days=10)
        assert eligible_fail is False
    finally:
        db_path.unlink(missing_ok=True)


def test_virtual_position_open_and_settle_is_idempotent(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    engine = IncubatorEngine(store=store)
    opened_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    assert engine.open_virtual_position("US.PLTR", 10.0, "sig-1", opened_at, holding_hours=24)
    assert not engine.open_virtual_position("US.PLTR", 10.0, "sig-1", opened_at, holding_hours=24)
    engine.record_simulated_signal("US.PLTR", 2.0, 1.2, 0.01, opened_at)

    settled = engine.settle_due_positions({"US.PLTR": 11.0})
    assert len(settled) == 1
    assert engine.settle_due_positions({"US.PLTR": 12.0}) == []
    metrics = store.incubator_symbol("US.PLTR")
    assert metrics["simulated_wins"] == 1
    assert metrics["simulated_losses"] == 0
    assert metrics["simulated_pnl_usd"] == pytest.approx(1.0)


def test_daily_chain_settles_scores_and_promotes(monkeypatch, tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    now = datetime.now(timezone.utc)
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO candidate_universe
            (week_start,symbol,underlying_name,industry,option_daily_volume,open_interest,
             stock_turnover_usd,median_spread_pct,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("2026-10-26", "US.PLTR", "PLTR", "TECH", 100, 100, 100, .01, "ACTIVE", now.isoformat()),
        )
    engine = IncubatorEngine(store=store)
    for index in range(3):
        opened = (now - timedelta(days=10, hours=index)).isoformat()
        engine.record_simulated_signal("US.PLTR", 2.0, 1.2, .01, opened)
        engine.open_virtual_position("US.PLTR", 10.0, f"sig-{index}", opened, 24)

    class Broker:
        def snapshots_tolerant(self, symbols):
            return pd.DataFrame([{"code": symbols[0], "last_price": 12.0}])

    message = run_daily_universe_ranking(store, now=now, broker=Broker())

    assert "结算 3 笔" in message
    metrics = store.incubator_symbol("US.PLTR")
    assert metrics["simulated_wins"] == 3
    assert metrics["latest_ml_score"] > .75  # 模型先验 0.5 被真实赛马实绩拉开
    assert metrics["promotion_eligible"] == 1
    assert any(row["symbol"] == "US.PLTR" for row in store.active_promoted_symbols("futu_paper_sandbox"))


def test_zero_settlement_and_promotion_alerts_are_auditable_and_deduplicated(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    now = datetime.now(timezone.utc)
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO candidate_universe
            (week_start,symbol,underlying_name,industry,option_daily_volume,open_interest,
             stock_turnover_usd,median_spread_pct,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("2026-10-26", "US.PLTR", "PLTR", "TECH", 100, 100, 100, .01, "ACTIVE", now.isoformat()),
        )
    engine = IncubatorEngine(store=store)
    for index in range(20):
        engine.record_simulated_signal("US.PLTR", 2.0, 1.2, .01, (now - timedelta(days=index)).isoformat())

    run_daily_universe_ranking(store, now=now)
    run_daily_universe_ranking(store, now=now)
    events = [row["event_type"] for row in store.alerts()]
    assert events.count("INCUBATOR_ZERO_SETTLEMENT") == 1
    assert events.count("INCUBATOR_ZERO_PROMOTION") == 1


def test_loss_settlement_lowers_racing_score(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    engine = IncubatorEngine(store=store)
    now = datetime.now(timezone.utc)
    for index in range(3):
        opened = (now - timedelta(days=2, hours=index)).isoformat()
        engine.record_simulated_signal("US.LOSS", 2.0, 1.2, .01, opened)
        assert engine.open_virtual_position("US.LOSS", 10.0, f"loss-{index}", opened, 1)
    before = store.incubator_symbol("US.LOSS")["latest_ml_score"]
    assert len(engine.settle_due_positions({"US.LOSS": 8.0}, now.isoformat())) == 3
    from futu_quant.universe.ranking_pipeline import RankingPipeline
    after = RankingPipeline(store=store).score_symbol("US.LOSS")
    metrics = store.incubator_symbol("US.LOSS")
    assert metrics["simulated_losses"] == 3
    assert after < before


def test_missing_quote_keeps_open_and_blocks_promotion(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    engine = IncubatorEngine(store=store)
    opened = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    engine.record_simulated_signal("US.NOQUOTE", 2.0, 1.2, .01, opened)
    assert engine.open_virtual_position("US.NOQUOTE", 10.0, "no-quote", opened, 1)
    assert engine.settle_due_positions({}) == []
    assert store.open_incubator_virtual_positions()
    profile = store.incubator_symbol("US.NOQUOTE")
    profile.update(observation_days=10, signal_triggers=10, latest_ml_score=.9,
                   simulated_pnl_usd=10, promotion_eligible=0)
    store.upsert_incubator_metrics("US.NOQUOTE", profile)
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO candidate_universe
            (week_start,symbol,underlying_name,industry,option_daily_volume,open_interest,
             stock_turnover_usd,median_spread_pct,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("2026-10-26", "US.NOQUOTE", "NOQUOTE", "TECH", 100, 100, 100, .01,
             "ACTIVE", opened),
        )
    run_daily_universe_ranking(store, broker=None)
    assert store.incubator_symbol("US.NOQUOTE")["promotion_eligible"] == 0
    assert store.active_promoted_symbols("futu_paper_sandbox") == []
    assert any(e["event_type"] == "VIRTUAL_SETTLEMENT_DEFERRED_NO_QUOTE"
               for e in store.universe_lifecycle_events())


def test_delivery_pool_is_tenant_isolated_and_real_auto_promotion_is_denied(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    store.promote_universe_symbol("futu_paper_sandbox", "US.PLTR", .9, expires)
    assert [r["symbol"] for r in store.active_promoted_symbols("futu_paper_sandbox")] == ["US.PLTR"]
    assert store.active_promoted_symbols("futu_real_master") == []
    with pytest.raises(PermissionError, match="缺少独立批准"):
        store.promote_universe_symbol("futu_real_master", "US.PLTR", .9, expires)

    from futu_quant import config
    monkey_env = config.ENV_FILE
    try:
        # 生产动态读取只有默认环境文件才接数据库；此处直接验证同一存储的 Worker 合并语义。
        from futu_quant.universe.lifecycle_manager import LifecycleManager
        paper_symbols = LifecycleManager(store, "futu_paper_sandbox").sync_to_runtime_universe()
        real_symbols = LifecycleManager(store, "futu_real_master").sync_to_runtime_universe()
        assert "US.PLTR" in paper_symbols
        assert "US.PLTR" not in real_symbols
        assert store.get_setting("VOLATILITY_SYMBOLS", None) is None
    finally:
        config.ENV_FILE = monkey_env


def test_upgrade_keeps_global_pool_and_migrates_unproven_symbols_to_paper(tmp_path):
    """迁移 24 不删除全局键任何标的；无法证明来源的非核心项保守迁入 PAPER 投放层。

    覆盖 Reviewer 阻塞场景：同一标的（US.PLTR）既在全局键、又在旧投放表 ——
    既不能删全局键（可能同时是人工核心配置），也不能覆盖旧投放记录（保留原评分）。
    """
    from futu_quant.migrations import MIGRATIONS, apply_migrations

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT,applied_at TEXT)")
    connection.execute("CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,strategy_id TEXT,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,actor TEXT NOT NULL DEFAULT '')")
    connection.execute("CREATE TABLE runtime_config(config_key TEXT PRIMARY KEY,value_json TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL)")
    connection.execute("""CREATE TABLE promoted_universe(symbol TEXT PRIMARY KEY,promoted_at TEXT NOT NULL,expires_at TEXT NOT NULL,promotion_score REAL NOT NULL,industry TEXT NOT NULL DEFAULT 'OTHER',live_trades_count INTEGER NOT NULL DEFAULT 0,live_realized_pnl REAL NOT NULL DEFAULT 0,consecutive_hard_stops INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'ACTIVE',eviction_reason TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
    for migration in MIGRATIONS:
        if migration.version <= 22:
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,datetime('now'))",
                (migration.version, migration.name, migration.checksum),
            )
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT INTO promoted_universe VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("US.PLTR", now, now, .9, "TECH", 0, 0, 0, "ACTIVE", "", now, now),
    )
    connection.execute(
        "INSERT INTO runtime_config VALUES('VOLATILITY_SYMBOLS',?,1,?)",
        (json.dumps(["US.AAPL", "US.PLTR", "US.CUSTOM"]), now),
    )
    connection.commit()
    assert apply_migrations(connection, actor="service:test") == [23, 24]
    # 全局键一个符号都不能丢：同名双重来源（US.PLTR）也保留，迁移不猜测来源。
    symbols = json.loads(connection.execute(
        "SELECT value_json FROM runtime_config WHERE config_key='VOLATILITY_SYMBOLS'"
    ).fetchone()[0])
    assert symbols == ["US.AAPL", "US.PLTR", "US.CUSTOM"]
    paper = {
        row[0]: row[1] for row in connection.execute(
            "SELECT symbol, promotion_score FROM promoted_universe "
            "WHERE tenant_id='futu_paper_sandbox'"
        ).fetchall()
    }
    real = connection.execute(
        "SELECT symbol FROM promoted_universe WHERE tenant_id='futu_real_master'"
    ).fetchall()
    # 旧投放记录保留原评分（INSERT OR IGNORE 不覆盖）；非核心自定义项 CUSTOM 被承接。
    assert paper == {"US.PLTR": .9, "US.CUSTOM": 0.0}
    # REAL 不继承任何历史投放（含全局键来源）。
    assert real == []
    connection.close()


def test_legacy_core_symbol_list_matches_config():
    """迁移 24 的核心清单必须与 config.DEFAULT_VOLATILITY_SYMBOLS 完全一致。"""
    from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS
    from futu_quant.migrations import _LEGACY_CORE_SYMBOLS

    assert _LEGACY_CORE_SYMBOLS == DEFAULT_VOLATILITY_SYMBOLS


def test_worker_pool_is_default_core_plus_tenant_deliveries_only(tmp_path, monkeypatch):
    """运行时人工配置可进入 PAPER，但不得进入 REAL；租户投放仍保持隔离。"""
    from futu_quant import config
    import futu_quant.store as store_module

    store = TradingStore(str(tmp_path / "pool.db"))
    store.set_runtime_config(
        "VOLATILITY_SYMBOLS", ["US.AAPL", "US.LEGACY", "US.CUSTOM"], "test:seed",
    )
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    store.promote_universe_symbol("futu_paper_sandbox", "US.CUSTOM", 0.0, expires)

    monkeypatch.setattr(config, "ENV_FILE", config.DEFAULT_ENV_FILE)
    monkeypatch.setattr(store_module, "TradingStore", lambda: store)

    real_pool = config.load_volatility_symbols("futu_real_master")
    paper_pool = config.load_volatility_symbols("futu_paper_sandbox")
    shared_pool = config.load_volatility_symbols()

    assert real_pool == config.DEFAULT_VOLATILITY_SYMBOLS
    assert "US.LEGACY" not in real_pool and "US.CUSTOM" not in real_pool
    assert "US.CUSTOM" in paper_pool
    assert "US.LEGACY" in paper_pool  # 既有 UI 人工配置持续逐轮生效
    assert "US.LEGACY" not in real_pool  # 未经独立批准不得穿透 REAL
    assert "US.CUSTOM" in shared_pool  # 无租户上下文只做展示并集


def test_post_migration_runtime_config_changes_reach_paper_but_never_real(tmp_path, monkeypatch):
    """升级后的既有 UI 配置入口仍逐轮生效，但未批准标的绝不进入 REAL。"""
    from futu_quant import config
    import futu_quant.store as store_module

    store = TradingStore(str(tmp_path / "dynamic-pool.db"))
    monkeypatch.setattr(config, "ENV_FILE", config.DEFAULT_ENV_FILE)
    monkeypatch.setattr(config, "_mirror_env", lambda *_a, **_k: None)
    monkeypatch.setattr(store_module, "TradingStore", lambda: store)

    config.save_volatility_symbols(("US.AAPL", "US.PLTR"))
    assert "US.PLTR" in config.load_volatility_symbols("futu_paper_sandbox")
    assert "US.PLTR" not in config.load_volatility_symbols("futu_real_master")

    # 下一次 UI 保存删除 PLTR 后，下一轮 Worker 读取必须立即去除；REAL 始终不继承。
    config.save_volatility_symbols(("US.AAPL", "US.CUSTOM"))
    paper_pool = config.load_volatility_symbols("futu_paper_sandbox")
    real_pool = config.load_volatility_symbols("futu_real_master")
    assert "US.PLTR" not in paper_pool
    assert "US.CUSTOM" in paper_pool
    assert "US.CUSTOM" not in real_pool


def test_real_delivery_requires_matching_unexpired_single_use_approval(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    delivery_until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    from futu_quant.admin_auth import hash_password

    password = "correct-admin-password"
    note = "申请人自称 Chief（非权威备注）"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password, iterations=1_000))
        approval_id = store.create_real_universe_delivery_approval(
            "futu_real_master", "US.PLTR", valid_until,
            "ticket:REAL-PLTR-001", "独立评审通过",
            admin_password=password, note=note,
        )
    assert [row["symbol"] for row in store.active_promoted_symbols("futu_real_master")] == []
    with store.connection() as connection:
        stored = connection.execute(
            "SELECT approved_by, note FROM real_universe_delivery_approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
    from futu_quant.store import SHARED_ADMIN_CREDENTIAL_ACTOR

    assert stored["approved_by"] == SHARED_ADMIN_CREDENTIAL_ACTOR
    assert stored["note"] == note
    with pytest.raises(PermissionError, match="不匹配"):
        store.promote_universe_symbol(
            "futu_real_master", "US.NVDA", .9, delivery_until, approval_id=approval_id,
        )
    store.promote_universe_symbol(
        "futu_real_master", "US.PLTR", .9, delivery_until, approval_id=approval_id,
    )
    assert [row["symbol"] for row in store.active_promoted_symbols("futu_real_master")] == [
        "US.PLTR"
    ]
    with pytest.raises(PermissionError, match="已消费"):
        store.promote_universe_symbol(
            "futu_real_master", "US.PLTR", .9, delivery_until, approval_id=approval_id,
        )
    events = {row["event_type"] for row in store.audit_events()}
    assert "REAL_UNIVERSE_DELIVERY_APPROVED" in events
    assert "REAL_UNIVERSE_DELIVERY_APPROVAL_CONSUMED" in events
    approval_event = next(
        row for row in store.audit_events()
        if row["event_type"] == "REAL_UNIVERSE_DELIVERY_APPROVED"
    )
    payload = json.loads(approval_event["payload_json"])
    assert approval_event["actor"] == SHARED_ADMIN_CREDENTIAL_ACTOR
    assert payload["actor_identity_verified"] is False
    assert payload["actor_authority"] == "shared_admin_credential"
    assert payload["approval_note"] == note


def test_real_approval_rejects_caller_supplied_identity(tmp_path, monkeypatch):
    """审计 actor 不可由调用方指定：关键字参数与数据库直写都必须被拒。"""
    from futu_quant.admin_auth import hash_password

    store = TradingStore(str(tmp_path / "incubator.db"))
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    password = "correct-admin-password"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password, iterations=1_000))
    with pytest.raises(TypeError):
        store.create_real_universe_delivery_approval(
            "futu_real_master", "US.PLTR", expires, "ticket:x", "评审通过",
            admin_password=password, approved_by="streamlit_admin:chief",
        )
    # 即便绕过 API 直接写库，CHECK 约束也不接受伪造的个人身份。
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as connection:
            connection.execute(
                """INSERT INTO real_universe_delivery_approvals
                (tenant_id,symbol,approved_by,approved_at,expires_at,evidence,reason,created_at)
                VALUES('futu_real_master','US.PLTR','streamlit_admin:chief',?,?,?,?,?)""",
                (store.now(), expires, "ticket:x", "评审通过", store.now()),
            )


def test_worker_cannot_self_sign_or_use_expired_real_approval(tmp_path, monkeypatch):
    from futu_quant.admin_auth import hash_password

    store = TradingStore(str(tmp_path / "incubator.db"))
    now = datetime.now(timezone.utc)
    password = "correct-admin-password"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password, iterations=1_000))
    with pytest.raises(PermissionError, match="未经管理员认证"):
        store.create_real_universe_delivery_approval(
            "futu_real_master", "US.PLTR",
            (now + timedelta(hours=1)).isoformat(), "self", "self",
            admin_password="worker-self-signed",
        )

    expired_at = (now - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="有效期必须晚于"):
        store.create_real_universe_delivery_approval(
            "futu_real_master", "US.PLTR",
            expired_at, "ticket:expired", "超时测试", admin_password=password,
        )


def test_atomic_settlement_rolls_back_position_when_metric_write_fails(monkeypatch, tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    engine = IncubatorEngine(store=store)
    opened = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    engine.record_simulated_signal("US.PLTR", 2.0, 1.2, .01, opened)
    assert engine.open_virtual_position("US.PLTR", 10.0, "atomic", opened, 1)
    before = dict(store.incubator_symbol("US.PLTR"))

    def fail_write(*_args, **_kwargs):
        raise RuntimeError("metric write failed")

    monkeypatch.setattr(store, "_write_incubator_metrics", fail_write)
    with pytest.raises(RuntimeError, match="metric write failed"):
        engine.settle_due_positions({"US.PLTR": 11.0})
    assert store.open_incubator_virtual_positions()[0]["status"] == "OPEN"
    assert store.incubator_symbol("US.PLTR") == before


def test_virtual_incubator_never_calls_broker_order_api(tmp_path):
    store = TradingStore(str(tmp_path / "incubator.db"))
    engine = IncubatorEngine(store=store)
    broker_calls: list[str] = []

    class Broker:
        def place_limit_order(self, *_args, **_kwargs):
            broker_calls.append("order")
            raise AssertionError("孵化虚拟交易禁止触发券商订单")

    assert engine.open_virtual_position("US.PLTR", 10.0, "safe-signal")
    assert broker_calls == []
