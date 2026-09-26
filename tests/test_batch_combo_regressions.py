from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.combo_execution import ComboExecutionManager, create_combo
from futu_quant.store import TradingStore


LONG = "US.NVDA260717C220000"
SHORT = "US.NVDA260717C210000"


def _combo(store):
    combo_id = create_combo(store, {
        "strategy_id": "test", "name": "测试价差", "underlying": "US.NVDA",
        "structure": "VERTICAL", "max_net_debit": 10, "auto_rollback": True,
    }, [
        {"role": "PROTECTIVE", "symbol": LONG, "side": "BUY", "quantity": 1, "limit_price": 1},
        {"role": "RISK", "symbol": SHORT, "side": "SELL", "quantity": 1, "limit_price": .6},
    ])
    legs = store.option_combo_legs(combo_id)
    for leg in legs:
        store.update_option_combo_leg(leg["id"], status="FILLED", filled_quantity=1)
    store.update_option_combo(combo_id, "OPEN")
    return combo_id, legs


class Broker:
    def __init__(self):
        self.positions_calls = 0
        self.quantities = {LONG: 1, SHORT: -1}
        self.modified = []
        self.cancelled = []
        self.ask = 1.5

    def positions(self, refresh=False):
        self.positions_calls += 1
        return pd.DataFrame([{"code": key, "qty": value, "can_sell_qty": max(value, 0)}
                             for key, value in self.quantities.items() if value])

    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbol, "ask_price": self.ask, "bid_price": .5}
                             for symbol in symbols])

    def modify_limit_order(self, order_id, quantity, price):
        self.modified.append((order_id, quantity, price))

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)


class Executor:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def submit_limit(self, strategy, key, symbol, side, quantity, price, metadata):
        intent, _ = self.store.create_intent(strategy, key, symbol, side, quantity, price, metadata)
        self.store.update_intent(intent["id"], "SUBMITTED", f"broker-{intent['id']}")
        self.calls.append((key, symbol, side, quantity, intent["id"]))
        return SimpleNamespace(intent_id=intent["id"], status="SUBMITTED", message="ok")


def test_batch_4_plus_6_entry_3_plus_7_exit_and_atomic_overallocation(tmp_path):
    store = TradingStore(tmp_path / "batch.db")
    store.create_position_batch("batch", strategy_id="s", symbol=LONG, quantity=4, entry_price=1)
    store.append_batch_lot("batch", quantity=4, price=1)
    store.create_position_batch("batch", strategy_id="s", symbol=LONG, quantity=6, entry_price=2)
    store.append_batch_lot("batch", quantity=6, price=2)
    batch = store.position_batches()[0]
    assert (batch["entry_qty"], batch["remaining_qty"], batch["status"]) == (10, 10, "OPEN")
    store.allocate_exit("batch", quantity=3, close_price=3)
    batch = store.position_batches()[0]
    assert (batch["entry_qty"], batch["remaining_qty"], batch["status"]) == (10, 7, "OPEN")
    with pytest.raises(ValueError, match="数量不足"):
        store.allocate_exit("batch", quantity=8, close_price=3)
    assert store.position_batches()[0]["remaining_qty"] == 7
    allocations = store.allocate_exit("batch", quantity=7, close_price=3)
    assert [row["quantity"] for row in allocations] == [1, 6]
    assert (store.position_batches()[0]["remaining_qty"], store.position_batches()[0]["status"]) == (0, "CLOSED")


def test_broker_status_updates_never_regress_fills_or_terminal_state(tmp_path):
    store = TradingStore(tmp_path / "out_of_order.db")
    intent, _ = store.create_intent("test", "one", LONG, "BUY", 10, 1.0)
    store.update_intent(intent["id"], "SUBMITTED", "broker-order")
    store.update_by_broker_order("broker-order", "FILLED_PART", 4, 1.0)
    store.update_by_broker_order("broker-order", "SUBMITTED", 0, 0)
    assert store.intent(intent["id"])["filled_quantity"] == 4
    assert store.intent(intent["id"])["status"] == "FILLED_PART"
    store.update_by_broker_order("broker-order", "FILLED", 10, 1.0)
    store.update_by_broker_order("broker-order", "FILLED_PART", 4, 1.0)
    assert store.intent(intent["id"])["filled_quantity"] == 10
    assert store.intent(intent["id"])["status"] == "FILLED"


def test_close_reprices_and_cancels_close_intent_not_open_intent(tmp_path):
    store = TradingStore(tmp_path / "close.db")
    combo_id, legs = _combo(store)
    broker, executor = Broker(), Executor(store)
    manager = ComboExecutionManager(broker, store, executor)
    short = next(leg for leg in legs if leg["side"] == "SELL")
    open_intent, _ = store.create_intent("test", "old-open", SHORT, "SELL", 1, .6)
    store.update_intent(open_intent["id"], "SUBMITTED", "old-open-order")
    store.update_option_combo_leg(short["id"], intent_id=open_intent["id"])
    manager.request_close(combo_id)
    close_id = executor.calls[-1][-1]
    assert executor.calls[-1][2] == "BUY"
    broker.ask = 2.0
    initial_price = store.intent(close_id)["limit_price"]
    expected = min(broker.ask, initial_price + max(.01, round(abs(broker.ask - initial_price) / 4, 2)))
    with store.connection() as db:
        db.execute("UPDATE order_intents SET created_at=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), close_id))
    manager.advance(combo_id)
    assert broker.modified == [(f"broker-{close_id}", 1, expected)]
    assert store.intent(open_intent["id"])["reprice_count"] == 0
    manager._cancel_working(store.option_combo_legs(combo_id))
    assert broker.cancelled == [f"broker-{close_id}"]


def test_partial_close_new_intent_and_fresh_broker_position(tmp_path):
    store = TradingStore(tmp_path / "partial.db")
    combo_id, legs = _combo(store)
    short = next(leg for leg in legs if leg["side"] == "SELL")
    store.update_option_combo_leg(short["id"], filled_quantity=10)
    broker, executor = Broker(), Executor(store)
    broker.quantities[SHORT] = -10
    manager = ComboExecutionManager(broker, store, executor,
                                    positions_snapshot=pd.DataFrame([{"code": SHORT, "qty": -10}]))
    manager.request_close(combo_id)
    first_id = executor.calls[-1][-1]
    assert broker.positions_calls == 1
    with store.connection() as db:
        db.execute("UPDATE order_intents SET status='CANCELLED_PART',filled_quantity=3 WHERE id=?", (first_id,))
    broker.quantities[SHORT] = -7
    manager.advance(combo_id)
    assert store.option_combo_legs(combo_id)[1]["close_filled_quantity"] == 3
    manager.advance(combo_id)
    assert executor.calls[-1][3] == 7
    assert executor.calls[-1][0] != executor.calls[-2][0]
    assert broker.positions_calls == 2
    second_id = executor.calls[-1][-1]
    with store.connection() as db:
        db.execute("UPDATE order_intents SET status='FILLED',filled_quantity=7 WHERE id=?", (second_id,))
    broker.quantities[SHORT] = 0
    manager.advance(combo_id)
    assert store.option_combo_legs(combo_id)[1]["close_filled_quantity"] == 10


def test_reversed_and_unknown_broker_positions_fail_closed(tmp_path):
    store = TradingStore(tmp_path / "unknown.db")
    combo_id, _ = _combo(store)
    broker, executor = Broker(), Executor(store)
    broker.quantities[SHORT] = 1
    manager = ComboExecutionManager(broker, store, executor)
    manager.request_close(combo_id)
    assert not executor.calls
    broker.positions = lambda refresh=False: pd.DataFrame([{"code": SHORT, "qty": "unknown"}])
    with pytest.raises(RuntimeError, match="数量未知"):
        manager._closing_quantity(next(leg for leg in store.option_combo_legs(combo_id)
                                       if leg["side"] == "SELL"), "BUY", 1)
    assert not executor.calls


def test_update_intent_late_submission_does_not_reopen_filled(tmp_path):
    store = TradingStore(tmp_path / "intent.db")
    intent, _ = store.create_intent("s", "key", LONG, "BUY", 1, 1)
    store.update_intent(intent["id"], "SUBMITTED", "broker-1")
    store.update_intent(intent["id"], "FILLED")
    store.update_intent(intent["id"], "SUBMITTED")
    store.update_intent(intent["id"], "CANCEL_REQUESTED")
    assert store.intent(intent["id"])["status"] == "FILLED"
