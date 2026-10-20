from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.real_trading import check_real_trade
from futu_quant.store import TradingStore


class FakeBroker:
    def __init__(self, env="SIMULATE"):
        self.settings = SimpleNamespace(trading_env=env, real_trading_enabled=False)
        self.trading_env = env
        self.calls = []

    def orders(self, historical: bool = False) -> pd.DataFrame:
        return pd.DataFrame(columns=["order_id", "order_status"])

    @staticmethod
    def is_cancellable_status(status) -> bool:
        return False

    def place_limit_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return pd.DataFrame([{"order_id": "order-1"}])


@pytest.fixture(autouse=True)
def approval_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")


def test_paper_never_places_on_real_broker(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    store.set_setting("execution_mode", "PAPER")
    broker = FakeBroker("REAL")
    result = ExecutionManager(broker, store).submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert result.status == "REJECTED"
    assert broker.calls == []
    assert store.intents() == []


def test_real_cannot_fall_back_to_simulated_broker(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    store.set_setting("execution_mode", "REAL")
    broker = FakeBroker()
    result = ExecutionManager(broker, store).submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert result.status == "REJECTED"
    assert broker.calls == []


def test_real_gate_blocks_before_broker(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    store.set_setting("execution_mode", "REAL")
    broker = FakeBroker("REAL")
    result = ExecutionManager(broker, store).submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert result.status == "REJECTED"
    assert broker.calls == []


def test_broker_acceptance_followed_by_local_failure_never_retries(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "orders.db")
    broker = FakeBroker()
    manager = ExecutionManager(broker, store)
    manager.risk.check = lambda *a, **k: SimpleNamespace(allowed=True, reason="通过", notional=100)
    original = store.update_intent
    failed = False

    def fail_once(intent_id, status, *args, **kwargs):
        nonlocal failed
        if status == "SUBMITTED" and not failed:
            failed = True
            raise OSError("database write failed")
        return original(intent_id, status, *args, **kwargs)

    monkeypatch.setattr(store, "update_intent", fail_once)
    first = manager.submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    second = manager.submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert first.status == second.status == "UNKNOWN"
    assert len(broker.calls) == 1
    assert store.intents()[0]["broker_order_id"] == "order-1"


def test_broker_transport_error_is_unknown_not_retryable(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    broker = FakeBroker()

    def uncertain(*args, **kwargs):
        broker.calls.append((args, kwargs))
        raise TimeoutError("timed out after broker request")

    broker.place_limit_order = uncertain
    manager = ExecutionManager(broker, store)
    manager.risk.check = lambda *a, **k: SimpleNamespace(allowed=True, reason="通过", notional=100)
    with pytest.raises(TimeoutError):
        manager.submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    again = manager.submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert again.status == "UNKNOWN"
    assert len(broker.calls) == 1


def test_push_fill_is_not_overwritten_by_submission_response(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    broker = FakeBroker()
    manager = ExecutionManager(broker, store)
    manager.risk.check = lambda *a, **k: SimpleNamespace(allowed=True, reason="通过", notional=100)

    def push_before_return(*args, **kwargs):
        row = store.intents()[0]
        store.update_intent(row["id"], "FILLED", broker_order_id="order-1")
        return pd.DataFrame([{"order_id": "order-1"}])

    broker.place_limit_order = push_before_return
    result = manager.submit_limit("sma", "one", "US.SPY", "BUY", 1, 100)
    assert result.status == "FILLED"
    assert store.intents()[0]["status"] == "FILLED"


def test_reprice_and_cancel_refuse_mismatched_environment(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    intent, _ = store.create_intent("sma", "one:PAPER", "US.SPY", "BUY", 1, 100)
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id="order-1")
    with store.connection() as connection:
        connection.execute("UPDATE order_intents SET created_at=? WHERE id=?",
                           ("2020-01-01T00:00:00+00:00", intent["id"]))
    broker = FakeBroker("REAL")
    broker.snapshots = lambda symbols: pd.DataFrame([{"bid_price": 100, "ask_price": 101}])
    broker.modify_limit_order = lambda *a, **k: pytest.fail("unsafe modification")
    broker.cancel_order = lambda *a, **k: pytest.fail("unsafe cancellation")
    manager = ExecutionManager(broker, store)
    assert manager.reprice_open_orders() == 0
    assert manager.cancel_stale_orders() == 0


def test_real_gate_requires_account_even_without_confirmation(tmp_path):
    settings = SimpleNamespace(trading_env="REAL", real_trading_enabled=True,
                               real_account_id="", real_require_manual_confirm=False,
                               real_allowed_strategies=("sma",), real_max_order_notional=1000,
                               real_max_daily_notional=2000)
    result = check_real_trade(TradingStore(tmp_path / "orders.db"), settings,
                              "sma", "US.SPY", "BUY", 1, 100)
    assert not result.allowed
    assert "实盘账户 ID" in result.reason


def test_direct_real_broker_placement_requires_gate_context():
    pytest.importorskip("futu")
    from futu_quant.broker import BrokerError, FutuPaperBroker

    broker = object.__new__(FutuPaperBroker)
    broker.settings = SimpleNamespace(trading_env="REAL", enable_paper_orders=True)
    broker.trading_env = "REAL"
    with pytest.raises(BrokerError, match="实盘门禁"):
        broker.place_limit_order("US.SPY", "BUY", 1, 100)


def test_direct_modify_cancel_guard_environment():
    pytest.importorskip("futu")
    from futu_quant.broker import BrokerError, FutuPaperBroker

    broker = object.__new__(FutuPaperBroker)
    broker.settings = SimpleNamespace(trading_env="REAL")
    broker.trading_env = "REAL"
    with pytest.raises(BrokerError, match="环境"):
        broker.modify_limit_order("order-1", 1, 100, expected_env="SIMULATE")
    with pytest.raises(BrokerError, match="环境"):
        broker.cancel_order("order-1", expected_env="SIMULATE")


def test_urgent_sell_reprice_chases_bid_and_cancels_faster():
    from pathlib import Path
    db_path = Path("data/test_urgent_reprice.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        intent, _ = store.create_intent(
            "volatility_long_gamma", "urgent-exit-1:PAPER", "US.META", "SELL", 1, 20.0,
            {"urgent": True, "reference_price": 20.0},
        )
        store.update_intent(intent["id"], "SUBMITTED", broker_order_id="broker-meta-1")

        modified = []
        broker = FakeBroker("SIMULATE")
        broker.snapshots = lambda symbols: pd.DataFrame([{"code": "US.META", "bid_price": 19.00, "ask_price": 19.50}])
        broker.modify_limit_order = lambda oid, qty, price: modified.append((oid, qty, price))
        broker.cancel_order = lambda oid: None

        with store.connection() as conn:
            conn.execute("UPDATE order_intents SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (intent["id"],))

        manager = ExecutionManager(broker, store)
        assert manager.reprice_open_orders() == 1
        assert len(modified) == 1
        assert modified[0][2] == 18.81  # 19.00 * 0.99

        assert manager.cancel_stale_orders() == 1
        updated = [i for i in store.intents() if i["id"] == intent["id"]][0]
        assert updated["status"] == "CANCEL_REQUESTED"
    finally:
        db_path.unlink(missing_ok=True)
