from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.option_pnl import build_option_pnl_ledger, option_pnl_summary
from futu_quant.store import TradingStore


class QuoteBroker:
    def __init__(self, price: float):
        self.price = price

    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbol, "last_price": self.price} for symbol in symbols])


def fill_order(store: TradingStore, intent: dict, status: str, price: float, fees: float = 1.0) -> None:
    broker_id = f"order-{intent['id']}"
    store.update_intent(intent["id"], "SUBMITTED", broker_id)
    store.update_by_broker_order(
        broker_id, status, float(intent["quantity"]), price, fees,
    )


def test_option_pnl_ledger_marks_open_lot_to_market(tmp_path):
    store = TradingStore(tmp_path / "pnl.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma",
        "open",
        "US.AAPL260724P335000",
        "BUY",
        1,
        8.8,
        {"reason": "动态波动条件通过", "entry_tier": "STRONG"},
    )
    fill_order(store, intent, "FILLED", 8.8, 1.0)

    ledger, orders, quote_status = build_option_pnl_ledger(store, QuoteBroker(9.4))

    assert quote_status == "行情已刷新"
    assert len(ledger) == 1
    assert len(orders) == 1
    row = ledger.iloc[0]
    assert row["underlying"] == "US.AAPL"
    assert row["remaining_quantity"] == 1
    assert row["unrealized_pnl"] == pytest.approx(59)
    assert row["total_pnl"] == pytest.approx(59)
    assert row["status"] == "持仓中"


def test_option_pnl_ledger_fifo_realized_pnl_after_close(tmp_path):
    store = TradingStore(tmp_path / "closed.db")
    buy, _ = store.create_intent(
        "volatility_long_gamma", "buy", "US.NVDA260724C200000", "BUY", 2, 10,
    )
    fill_order(store, buy, "FILLED", 10, 2.0)
    sell, _ = store.create_intent(
        "volatility_long_gamma", "sell", "US.NVDA260724C200000", "SELL", 1, 12,
    )
    fill_order(store, sell, "FILLED", 12, 1.0)

    ledger, _, _ = build_option_pnl_ledger(store, QuoteBroker(11))
    row = ledger.iloc[0]

    assert row["closed_quantity"] == 1
    assert row["remaining_quantity"] == 1
    assert row["realized_pnl"] == 198
    assert row["unrealized_pnl"] == 99
    assert row["total_pnl"] == 297
    assert option_pnl_summary(ledger)["open_contracts"] == 1


def test_option_pnl_ledger_uses_shadow_fill_time_as_open_time(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "shadow_time.db")
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:00:00+00:00")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "shadow-open", "US.TSLA260727P340000", "BUY", 1, 14,
    )
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:08:30+00:00")
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 1, 14, 1.0)
    store.record_shadow_fill(intent["id"], 1, 14, 1.0, {"code": intent["symbol"]})

    ledger, _, _ = build_option_pnl_ledger(store, QuoteBroker(15))

    assert ledger.iloc[0]["open_time"] == "2026-07-24T01:08:30+00:00"
    assert ledger.iloc[0]["open_time_source"] == "Shadow 成交时间"


def test_option_pnl_ledger_uses_first_broker_fill_event_for_paper_fill(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "paper_time.db")
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:00:00+00:00")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "paper-open", "US.GOOG260724P325000", "BUY", 1, 12,
    )
    broker_id = f"order-{intent['id']}"
    store.update_intent(intent["id"], "SUBMITTED", broker_id)
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:11:45+00:00")
    store.update_by_broker_order(broker_id, "FILLED", 1, 12, 1.0)

    ledger, _, _ = build_option_pnl_ledger(store, QuoteBroker(13))

    assert ledger.iloc[0]["open_time"] == "2026-07-24T01:11:45+00:00"
    assert ledger.iloc[0]["open_time_source"] == "富途首次成交时间"


def test_option_pnl_ledger_does_not_move_fill_time_after_later_reconcile(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "paper_reconcile_time.db")
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:00:00+00:00")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "paper-open", "US.GOOG260724P325000", "BUY", 1, 12,
    )
    broker_id = f"order-{intent['id']}"
    store.update_intent(intent["id"], "SUBMITTED", broker_id)
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T01:11:45+00:00")
    store.update_by_broker_order(broker_id, "FILLED", 1, 12, 1.0)
    monkeypatch.setattr(store, "now", lambda: "2026-07-24T04:30:00+00:00")
    store.update_by_broker_order(broker_id, "FILLED", 1, 12, 1.0)

    ledger, _, _ = build_option_pnl_ledger(store, QuoteBroker(13))

    assert ledger.iloc[0]["open_time"] == "2026-07-24T01:11:45+00:00"
