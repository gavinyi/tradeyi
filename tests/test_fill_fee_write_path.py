"""P1-13 费用三态守卫：真值 / 估算 / 不可用。

契约（2026-10-25 定稿）：
- `fee_is_estimate=0` 且 `fee_unavailable_reason IS NULL` → 真值，唯一可当「实付」；
- `fee_is_estimate=1` → 估算，可做成本近似但必须标注；
- `fee_unavailable_reason` 非空 → 不可用，数值无意义，任何成本聚合默认排除。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from futu_quant.order_push import resolve_fill_fee
from futu_quant.store import TradingStore

PLACEHOLDER = "legacy_placeholder_1usd"


def _new_intent(store: TradingStore, fees: float = 2.5) -> int:
    intent, _ = store.create_intent("core_satellite", f"fee:{datetime.now().timestamp()}",
                                    "US.QQQ", "BUY", 1, 100.0)
    store.update_intent(intent["id"], "FILLED", broker_order_id="b1")
    with store.connection() as connection:
        connection.execute(
            "UPDATE order_intents SET fees=?, filled_quantity=1, average_fill_price=100 WHERE id=?",
            (fees, intent["id"]),
        )
    return int(intent["id"])


def test_real_commission_is_truth_state():
    fee, is_estimate, reason = resolve_fill_fee("US.QQQ", "BUY", 1, 100.0, 2.31)
    assert fee == pytest.approx(2.31)
    assert is_estimate is False and reason is None


def test_missing_commission_uses_estimate_never_placeholder(monkeypatch):
    monkeypatch.setattr("futu_quant.order_push.estimate_fees", lambda *a, **k: 2.44)
    for missing in (0, None, "", -1):
        fee, is_estimate, reason = resolve_fill_fee("US.QQQ", "BUY", 1, 100.0, missing)
        assert fee == pytest.approx(2.44), f"commission={missing!r} 时不得写 0/1.0 占位"
        assert is_estimate is True and reason is None


def test_record_fill_writes_three_states(tmp_path):
    store = TradingStore(tmp_path / "fee.db")
    with store.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(fill_ledger)").fetchall()}
    assert {"fee_is_estimate", "fee_unavailable_reason"} <= columns

    store.record_fill(_new_intent(store), 1, 100.0, 2.5, source="BROKER_DEAL",
                      broker_deal_id="d-truth")
    store.record_fill(_new_intent(store), 1, 100.0, 2.44, source="BROKER_DEAL",
                      broker_deal_id="d-est", fee_is_estimate=True)
    store.record_fill(_new_intent(store), 1, 100.0, 0.0, source="BROKER_DEAL",
                      broker_deal_id="d-unknown", fee_unavailable_reason=PLACEHOLDER)
    with store.connection() as connection:
        rows = {
            row["broker_deal_id"]: dict(row)
            for row in connection.execute("SELECT * FROM fill_ledger").fetchall()
        }
    assert rows["d-truth"]["fee_is_estimate"] == 0
    assert rows["d-truth"]["fee_unavailable_reason"] is None
    assert rows["d-est"]["fee_is_estimate"] == 1
    assert rows["d-unknown"]["fee_unavailable_reason"] == PLACEHOLDER


def test_legacy_placeholder_columns_default_for_new_rows(tmp_path):
    """新写入行默认就是「真值态」；历史占位值由迁移 19 标记（见下一条用例）。"""
    store = TradingStore(tmp_path / "legacy.db")
    intent_id = _new_intent(store, fees=1.0)
    store.record_fill(intent_id, 1, 100.0, 1.0, source="BROKER_DEAL",
                      broker_deal_id="d-legacy-new")
    with store.connection() as connection:
        row = dict(connection.execute(
            "SELECT fee_total, fee_is_estimate, fee_unavailable_reason "
            "FROM fill_ledger WHERE broker_deal_id='d-legacy-new'"
        ).fetchone())
    assert row["fee_total"] == pytest.approx(1.0)
    assert row["fee_unavailable_reason"] is None, "新写入不做历史标记"
    assert row["fee_is_estimate"] == 0


def test_migration_19_marks_existing_placeholder_rows(tmp_path):
    path = tmp_path / "marker.db"
    store = TradingStore(path)
    intent_id = _new_intent(store)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO fill_ledger(intent_id,strategy_id,symbol,side,quantity,price,"
            "multiplier,fee_total,source,filled_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (intent_id, "core_satellite", "US.QQQ", "BUY", 1, 100.0, 1, 1.0,
             "LEGACY_INTENT_AGGREGATE", "2026-08-01T00:00:00"),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version=19")
    TradingStore(path)  # 重开：迁移 19 重放，只打标记
    with sqlite3.connect(path) as connection:
        reason = connection.execute(
            "SELECT fee_unavailable_reason FROM fill_ledger WHERE fee_total=1.0"
        ).fetchone()[0]
    assert reason == PLACEHOLDER
def test_safe_resolve_fill_fee_never_breaks_fill_ledger(tmp_path, monkeypatch):
    """P0 防线：estimate_fees 抛错时不得中断落账，返回不可用态 + ERROR 审计。"""
    from futu_quant.order_push import safe_resolve_fill_fee

    store = TradingStore(tmp_path / "safe.db")

    def _boom(*args, **kwargs):
        raise RuntimeError("fee model exploded")

    monkeypatch.setattr("futu_quant.order_push.estimate_fees", _boom)
    fee, is_estimate, reason = safe_resolve_fill_fee(
        store, "US.QQQ", "BUY", 1, 100.0, 0,
    )
    assert (fee, is_estimate, reason) == (0.0, False, "estimate_failed")
    events = [e for e in store.operational_events(limit=50)
              if e["event_type"] == "FEE_ESTIMATE_FAILED"]
    assert events and events[0]["status"] == "ERROR"
    # 落账照常
    intent_id = _new_intent(store)
    assert store.record_fill(intent_id, 1, 100.0, fee, source="BROKER_DEAL",
                             broker_deal_id="d-safe", fee_unavailable_reason=reason) is not None


def test_performance_three_states_and_excludes_unavailable(tmp_path):
    """P1-13：PnL 聚合不得把不可用态当实付；估算态必须可区分。"""
    from futu_quant.performance import performance_buckets

    store = TradingStore(tmp_path / "perf.db")
    cases = ((2.5, False, None), (2.44, True, None), (1.0, False, PLACEHOLDER))
    for index, (fee, is_estimate, reason) in enumerate(cases):
        intent_id = _new_intent(store)
        store.record_fill(intent_id, 1, 100.0, fee, source="BROKER_DEAL",
                          broker_deal_id=f"d-perf-{index}",
                          fee_is_estimate=is_estimate, fee_unavailable_reason=reason)
    buckets = performance_buckets(store, period="daily")
    row = buckets.iloc[0]
    assert row["手续费"] == pytest.approx(2.5 + 2.44), "不可用态必须排除在成本聚合之外"
    assert int(row["估算费用笔数"]) == 1
    assert int(row["不可用费用笔数"]) == 1