"""P1-11 批 1 守卫：按策略净值快照（只写不读）。

覆盖设计 §2.7 的批 1 相关项：迁移/隔离/同桶幂等/NULL 语义/失败隔离/FIFO 口径。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from futu_quant.store import TradingStore
from futu_quant import strategy_equity as se


@pytest.fixture()
def store():
    """自建 DB 路径（不依赖 pytest tmp_path：受限环境下 tempfile 会挂）。"""
    base = Path(__file__).resolve().parents[1] / ".pytest-tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"equity_{uuid4().hex}.db"
    return TradingStore(path, allow_migrations=True, migration_actor="pytest")


def _row(tenant="futu_real_master", strategy="core_satellite", as_of="2026-10-27T14:00:00+00:00",
         realized=10.0, bucket="5m"):
    return {
        "tenant_id": tenant, "strategy_id": strategy, "bucket": bucket,
        "as_of": as_of, "realized_pnl": realized, "unrealized_pnl": None,
        "attribution_status": "NO_MARK", "net_exposure": 0.0, "market_value": 0.0,
        "position_count": 1, "account_total_assets": 100000.0,
        "attribution_diff": None, "source": "cycle",
        "created_at": "2026-10-27T14:00:05+00:00",
    }


def test_table_created_by_migration_21(store):
    versions = [row["version"] for row in store.schema_migrations()] \
        if hasattr(store, "schema_migrations") else []
    rows = store.strategy_equity_snapshots("futu_real_master", limit=5)
    assert rows == []
    if versions:
        assert 21 in versions


def test_two_tenants_same_strategy_are_isolated(store):
    store.save_strategy_equity_snapshot(_row(tenant="futu_real_master", realized=1.0))
    store.save_strategy_equity_snapshot(_row(tenant="futu_paper_sandbox", realized=2.0))
    real = store.strategy_equity_snapshots("futu_real_master")
    paper = store.strategy_equity_snapshots("futu_paper_sandbox")
    assert len(real) == 1 and len(paper) == 1
    assert real[0]["realized_pnl"] == pytest.approx(1.0)
    assert paper[0]["realized_pnl"] == pytest.approx(2.0)


def test_strategy_equity_read_requires_tenant(store):
    with pytest.raises(ValueError, match="tenant_id"):
        store.strategy_equity_snapshots()


def test_latest_strategy_equity_returns_each_strategy_latest_bucket(store):
    store.save_strategy_equity_snapshot(_row(strategy="s1", realized=1.0))
    store.save_strategy_equity_snapshot(_row(
        strategy="s1", realized=2.0, as_of="2026-10-27T14:05:00+00:00",
    ))
    store.save_strategy_equity_snapshot(_row(strategy="s2", realized=3.0))
    store.save_strategy_equity_snapshot(_row(
        strategy="s1", realized=99.0, bucket="close",
        as_of="2026-10-27T20:00:00+00:00",
    ))
    rows = store.latest_strategy_equity("futu_real_master")
    assert [(row["strategy_id"], row["realized_pnl"]) for row in rows] == [
        ("s1", pytest.approx(2.0)), ("s2", pytest.approx(3.0)),
    ]


def test_same_bucket_retry_upserts_single_row(store):
    store.save_strategy_equity_snapshot(_row(realized=5.0))
    store.save_strategy_equity_snapshot(_row(realized=7.5))
    rows = store.strategy_equity_snapshots("futu_real_master")
    assert len(rows) == 1, "同桶重写必须 UPSERT 覆盖，不产生第二行"
    assert rows[0]["realized_pnl"] == pytest.approx(7.5)


def test_close_and_5m_buckets_coexist(store):
    store.save_strategy_equity_snapshot(_row(bucket="5m"))
    store.save_strategy_equity_snapshot(_row(bucket="close"))
    rows = store.strategy_equity_snapshots("futu_real_master")
    assert {row["bucket"] for row in rows} == {"5m", "close"}


def test_missing_mark_is_null_not_zero():
    now = datetime(2026, 10, 27, 14, 2, tzinfo=timezone.utc)
    rows = se.build_rows(
        tenant_id="futu_real_master", bucket="5m", as_of=se.bucket_start(now, 300),
        realized={"core_satellite": 12.5},
        positions={"core_satellite": [{"symbol": "US.SPY", "quantity": 10}]},
        total_assets=100000.0, now=now,
    )
    row = rows[0]
    assert row["unrealized_pnl"] is None
    assert row["attribution_status"] == "NO_MARK"
    assert row["position_count"] == 1
    assert row["realized_pnl"] == pytest.approx(12.5)


def test_flat_strategy_reports_complete_zero():
    now = datetime(2026, 10, 27, 14, 2, tzinfo=timezone.utc)
    rows = se.build_rows(
        tenant_id="futu_real_master", bucket="5m", as_of=se.bucket_start(now, 300),
        realized={"core_satellite": 0.0}, positions={},
        total_assets=None, now=now,
    )
    assert rows[0]["unrealized_pnl"] == 0.0
    assert rows[0]["attribution_status"] == "COMPLETE"
    assert rows[0]["account_total_assets"] is None


def test_fifo_realized_pnl_matches_hand_calc():
    fills = [
        {"strategy_id": "s1", "symbol": "US.A", "side": "BUY", "quantity": 2,
         "price": 10.0, "multiplier": 1, "fee_total": 1.0, "filled_at": "1"},
        {"strategy_id": "s1", "symbol": "US.A", "side": "SELL", "quantity": 1,
         "price": 15.0, "multiplier": 1, "fee_total": 1.0, "filled_at": "2"},
    ]
    realized = se.fifo_realized_pnl(fills)
    assert realized["s1"] == pytest.approx(15.0 - 10.0 - 1.0)


def test_tenantless_snapshot_is_skipped_with_event(store):
    written = se.snapshot_strategy_equity(
        store, None, tenant_id="", total_assets=1.0,
        now=datetime(2026, 10, 27, 14, 2, tzinfo=timezone.utc),
    )
    assert written == 0
    events = [e for e in store.operational_events()
              if e["event_type"] == "STRATEGY_EQUITY_SNAPSHOT_FAILED"]
    assert events, "空租户必须留 DEGRADED 事件，不得静默"


def test_write_failure_is_isolated_and_logged(store, monkeypatch):
    def boom(_tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "fill_ledger_rows", boom)
    written = se.snapshot_strategy_equity(
        store, None, tenant_id="futu_real_master", total_assets=1.0,
        now=datetime(2026, 10, 27, 14, 2, tzinfo=timezone.utc),
    )
    assert written == 0, "失败必须返回 0 且不抛异常（观测不打断主流程）"
    events = [e for e in store.operational_events()
              if e["event_type"] == "STRATEGY_EQUITY_SNAPSHOT_FAILED"]
    assert events and events[0]["status"] in {"DEGRADED", "ERROR"}


def test_snapshot_written_for_tenant(store):
    now = datetime(2026, 10, 27, 14, 2, tzinfo=timezone.utc)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO order_intents(id, strategy_id, intent_key, symbol, side, "
            "quantity, limit_price, status, created_at, updated_at, tenant_id) "
            "VALUES(9001,'core_satellite','k1','US.SPY','SELL',1,10.0,'FILLED',?,?,?)",
            (now.isoformat(), now.isoformat(), "futu_real_master"),
        )
        connection.execute(
            "INSERT INTO fill_ledger(intent_id, strategy_id, symbol, side, quantity, "
            "price, multiplier, fee_total, filled_at, source) "
            "VALUES(9001,'core_satellite','US.SPY','BUY',2,10.0,1,0,?,'BROKER_DEAL')",
            ("2026-10-27T13:00:00+00:00",),
        )
        connection.execute(
            "INSERT INTO fill_ledger(intent_id, strategy_id, symbol, side, quantity, "
            "price, multiplier, fee_total, filled_at, source) "
            "VALUES(9001,'core_satellite','US.SPY','SELL',1,15.0,1,0,?,'BROKER_DEAL')",
            ("2026-10-27T13:30:00+00:00",),
        )
    written = se.snapshot_strategy_equity(
        store, None, tenant_id="futu_real_master", total_assets=100000.0, now=now,
    )
    assert written >= 1
    rows = store.strategy_equity_snapshots("futu_real_master")
    assert rows and rows[0]["strategy_id"] == "core_satellite"
    assert rows[0]["realized_pnl"] == pytest.approx(5.0)  # FIFO: 15-10
    assert rows[0]["bucket"] in {"5m", "close"}

