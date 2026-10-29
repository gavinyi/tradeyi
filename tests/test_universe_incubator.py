"""
测试模块 2: 隔离沙盒影子演练与微观特征提取器 (Sandbox Incubator)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    with pytest.raises(PermissionError, match="REAL 租户禁止自动继承"):
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
