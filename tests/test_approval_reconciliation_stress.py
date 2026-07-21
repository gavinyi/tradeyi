from __future__ import annotations

import pandas as pd

from futu_quant.approval import strategy_fingerprint
from futu_quant.execution import ExecutionManager
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.stress import stress_test


class Broker:
    def __init__(self):
        self.calls = 0

    def orders(self, historical=False):
        return pd.DataFrame(columns=["order_id", "order_status"])

    def positions(self):
        return pd.DataFrame([{
            "code": "US.NVDA", "qty": 2, "market_val": 200,
            "nominal_price": 100, "cost_price": 100,
        }])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 1000, "power": 800}])

    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol, "bid_price": 99, "ask_price": 101, "last_price": 100,
        } for symbol in symbols])

    def candles(self, symbol, count=5):
        return pd.DataFrame({"time_key": ["2026-07-15"], "close": [100]})

    @staticmethod
    def is_cancellable_status(status):
        return False

    def place_limit_order(self, symbol, side, quantity, price):
        self.calls += 1
        return pd.DataFrame([{"order_id": "approved-1"}])


def test_paper_requires_current_strategy_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "true")
    broker = Broker()
    store = TradingStore(tmp_path / "test.db")
    executor = ExecutionManager(broker, store)

    blocked = executor.submit_limit("sma", "one", "US.QQQ", "BUY", 1, 100)
    fingerprint = strategy_fingerprint("sma")
    store.approve_strategy("sma", fingerprint, "test", True, True, True, True)
    approved = executor.submit_limit("sma", "one", "US.QQQ", "BUY", 1, 100)

    assert blocked.status == "APPROVAL_REQUIRED"
    assert approved.status == "SUBMITTED"
    assert broker.calls == 1


def test_core_satellite_is_exempt_from_version_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "true")
    monkeypatch.delenv("APPROVAL_EXEMPT_STRATEGIES", raising=False)
    broker = Broker()
    store = TradingStore(tmp_path / "test.db")

    result = ExecutionManager(broker, store).submit_limit(
        "core_satellite", "rebalance", "US.QQQ", "BUY", 1, 100,
    )

    assert result.status == "SUBMITTED"
    assert broker.calls == 1


def test_reconciliation_blocks_mismatched_symbol(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    store.set_virtual_allocation("core", "US.NVDA", 1, 100)

    report = reconcile_ledgers(store, Broker())

    assert report.iloc[0]["status"] == "MISMATCH"
    assert store.get_setting("symbol_blocked:US.NVDA") == "true"
    assert store.get_setting("strategy_enabled:core") == "false"


def test_virtual_allocation_can_be_updated_from_zero_and_deleted(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    store.set_virtual_allocation("core", "US.QQQ", 0, 100)
    store.set_virtual_allocation("core", "US.QQQ", 5, 101)

    assert store.virtual_allocations()[0]["quantity"] == 5

    store.delete_virtual_allocation("core", "US.QQQ")
    assert store.virtual_allocations() == []


def test_stress_test_reports_equity_loss():
    positions = pd.DataFrame([{"code": "US.NVDA", "qty": 2, "market_val": 200}])
    quotes = pd.DataFrame([{"code": "US.NVDA", "last_price": 100}])

    result = stress_test(positions, quotes, 1000)

    down_30 = result[result["场景"] == "正股下跌 30%"].iloc[0]
    assert down_30["估算损益"] == -60
    assert down_30["压力后权益"] == 940
