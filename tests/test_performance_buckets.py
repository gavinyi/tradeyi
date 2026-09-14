from __future__ import annotations

from futu_quant.performance import performance_buckets
from futu_quant.store import TradingStore


def _intent(store, key, symbol, side, qty, price, when):
    row, _ = store.create_intent(
        "volatility_long_gamma", key, symbol, side, qty, price,
        {"multiplier": 100, "reference_price": price, "execution_mode": "PAPER"},
    )
    store.update_intent(row["id"], "FILLED", filled_quantity=qty, average_fill_price=price, fees=1.25)
    store.record_fill(
        row["id"], qty, price, 1.25, source="TEST", filled_at=when,
        quote={"bid_price": price - .05, "ask_price": price + .05}, multiplier=100,
    )


def test_fill_ledger_is_idempotent_for_deal_ids(tmp_path):
    store = TradingStore(tmp_path / "fills.db")
    row, _ = store.create_intent("x", "k", "US.AAPL260918C200000", "BUY", 1, 2.0, {})
    first = store.record_fill(row["id"], 1, 2.0, 1.0, source="BROKER_DEAL", broker_deal_id="deal-1")
    second = store.record_fill(row["id"], 1, 2.0, 1.0, source="BROKER_DEAL", broker_deal_id="deal-1")
    assert first == second
    assert len(store.fills()) == 1


def test_performance_buckets_aggregates_costs_by_period(tmp_path):
    store = TradingStore(tmp_path / "pnl.db")
    _intent(store, "buy-1", "US.AAPL260918C200000", "BUY", 1, 2.00, "2026-09-01T14:00:00+00:00")
    _intent(store, "sell-1", "US.AAPL260918C200000", "SELL", 1, 2.50, "2026-09-02T14:00:00+00:00")

    result = performance_buckets(store, period="daily")

    assert len(result) == 2
    assert result["手续费"].sum() == 2.5
    assert result["价差成本"].sum() == 10.0
    assert result["行情覆盖率"].min() == 1.0
    assert result["成交金额"].sum() == 450.0
    assert result["已实现盈亏"].sum() == 50.0
    assert result["净收益"].sum() < 50.0
