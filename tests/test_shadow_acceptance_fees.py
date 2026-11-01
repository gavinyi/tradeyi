from __future__ import annotations
import pytest

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from futu_quant.acceptance import strategy_acceptance
from futu_quant.fee_model import estimate_fees
from futu_quant.shadow_matching import match_shadow_orders
from futu_quant.store import TradingStore


class QuoteBroker:
    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbols[0], "bid_price": 99.0, "ask_price": 100.0,
            "bid_vol": 10, "ask_vol": 4,
        }])

    def order_book(self, symbol, depth=10):
        return {"Bid": [(99.0, 10)], "Ask": [(100.0, 4)]}


def test_shadow_matcher_records_partial_fill_and_fee(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_PARTICIPATION_RATE", "0.5")
    store = TradingStore(tmp_path / "shadow.db")
    intent, _ = store.create_intent("sma", "x:SHADOW", "US.NVDA", "BUY", 5, 101)
    store.update_intent(intent["id"], "SHADOW")

    rth = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    assert match_shadow_orders(store, QuoteBroker(), now=rth) == 1
    updated = store.intents()[0]

    assert updated["status"] == "SHADOW_PART"
    assert updated["filled_quantity"] == 2
    assert updated["average_fill_price"] == 100
    assert len(store.shadow_fills()) == 1
    quantity, average_cost = store.shadow_position("sma", "US.NVDA")
    assert quantity == 2
    assert average_cost == 100


def test_urgent_shadow_exit_fills_at_current_bid_even_below_limit(tmp_path):
    store = TradingStore(tmp_path / "shadow-exit.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "exit:SHADOW", "US.NVDA", "SELL", 1, 105,
        {"urgent": True},
    )
    store.update_intent(intent["id"], "SHADOW")

    assert match_shadow_orders(store, QuoteBroker()) == 1
    updated = store.intents()[0]
    assert updated["status"] == "SHADOW_FILLED"
    assert updated["average_fill_price"] == 99
    assert store.strategy_has_completed_exit("volatility_long_gamma", "US.NVDA")


def test_option_shadow_does_not_fill_outside_regular_session(tmp_path):
    class OptionBroker(QuoteBroker):
        def snapshots(self, symbols):
            return pd.DataFrame([{
                "code": symbols[0], "bid_price": 1.9, "ask_price": 2.0,
                "bid_vol": 10, "ask_vol": 10, "update_time": "2026-07-15 17:00:00",
            }])

    store = TradingStore(tmp_path / "closed-option.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "x:SHADOW", "US.NVDA260717C200000", "BUY", 1, 3,
    )
    store.update_intent(intent["id"], "SHADOW")
    after_close = datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc)

    assert match_shadow_orders(store, OptionBroker(), now=after_close) == 0
    assert store.intents()[0]["status"] == "SHADOW"


def test_shadow_timeout_can_reprice_and_continue_chasing(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_ORDER_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("SHADOW_MAX_REPRICE_ATTEMPTS", "2")
    monkeypatch.setenv("SHADOW_REPRICE_STEP_PCT", "0.01")
    monkeypatch.setenv("SHADOW_MAX_SLIPPAGE_PCT", "0.20")
    monkeypatch.setenv("SHADOW_PARTICIPATION_RATE", "1.0")

    class ChaseBroker(QuoteBroker):
        def snapshots(self, symbols):
            ny_now = datetime(2026, 7, 17, 11, 0, tzinfo=ZoneInfo("America/New_York"))
            update_time = (ny_now - timedelta(seconds=30)).isoformat()
            return pd.DataFrame([{
                "code": symbols[0], "bid_price": 8.00, "ask_price": 8.10,
                "bid_vol": 10, "ask_vol": 10, "update_time": update_time,
                "option_open_interest": 1000,
            }])

        def order_book(self, symbol, depth=10):
            return {"Bid": [(8.00, 10)], "Ask": [(8.10, 10)]}

    store = TradingStore(tmp_path / "chase.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "chase", "US.SPCX260724P130000", "BUY", 1, 7.25,
        {"reference_price": 7.25},
    )
    store.update_intent(intent["id"], "SHADOW")
    fixed_now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    created_at = (fixed_now - timedelta(minutes=16)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE order_intents SET created_at=?,updated_at=? WHERE id=?",
            (created_at, created_at, intent["id"]),
        )

    matched = match_shadow_orders(store, ChaseBroker(), now=fixed_now)

    updated = store.intents()[0]
    assert matched == 1
    assert updated["status"] == "SHADOW_FILLED"
    assert updated["reprice_count"] == 1
    assert updated["limit_price"] > 7.25
    assert store.shadow_position("volatility_long_gamma", "US.SPCX260724P130000")[0] == 1


def test_acceptance_is_automatic_and_requires_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_DAYS", "1")
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_ORDERS", "1")
    store = TradingStore(tmp_path / "acceptance.db")
    intent, _ = store.create_intent("sma", "x", "US.NVDA", "BUY", 1, 100)
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 1, 100, 1)
    for evidence in ("BACKTEST", "DRILL", "RISK_REVIEW"):
        store.record_acceptance_evidence("sma", evidence, True)

    assert strategy_acceptance(store, "sma")["passed"]


def test_service_errors_are_diagnostic_and_do_not_block_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_DAYS", "1")
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_ORDERS", "1")
    store = TradingStore(tmp_path / "service-errors.db")
    intent, _ = store.create_intent("sma", "x", "US.NVDA", "BUY", 1, 100)
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 1, 100, 1)
    store.add_alert("ERROR", "BROKER", "temporary diagnostic failure")
    for evidence in ("BACKTEST", "DRILL", "RISK_REVIEW"):
        store.record_acceptance_evidence("sma", evidence, True)

    acceptance = strategy_acceptance(store, "sma")

    assert acceptance["passed"]
    assert "service_errors" not in acceptance["gates"]
    assert acceptance["details"]["service_errors"] == 1


def test_acceptance_uses_latest_reconciliation_state(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_DAYS", "1")
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_ORDERS", "1")
    store = TradingStore(tmp_path / "latest.db")
    intent, _ = store.create_intent("sma", "x", "US.NVDA", "BUY", 1, 100)
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 1, 100, 1)
    store.record_reconciliation("US.NVDA", 1, 0, "MISMATCH")
    store.record_reconciliation("US.NVDA", 1, 1, "MATCHED")
    for evidence in ("BACKTEST", "DRILL", "RISK_REVIEW"):
        store.record_acceptance_evidence("sma", evidence, True)

    assert strategy_acceptance(store, "sma")["gates"]["reconciliation"]["passed"]


def test_acceptance_reconciliation_is_scoped_to_strategy_symbols(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_DAYS", "1")
    monkeypatch.setenv("APPROVAL_MIN_SHADOW_ORDERS", "1")
    store = TradingStore(tmp_path / "scoped-reconciliation.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "x", "US.NVDA260729P205000", "BUY", 1, 2,
    )
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 1, 2, 0)
    store.record_reconciliation("US.NVDA260729P205000", 1, 1, "MATCHED")
    store.record_reconciliation("DOGE-USDT-SWAP", 0, 13.68, "MISMATCH")
    store.record_reconciliation("AVAX-USDT-SWAP", 0, 149.5, "MISMATCH")
    for evidence in ("BACKTEST", "DRILL", "RISK_REVIEW"):
        store.record_acceptance_evidence("volatility_long_gamma", evidence, True)

    acceptance = strategy_acceptance(store, "volatility_long_gamma")

    assert acceptance["gates"]["reconciliation"]["passed"]
    assert acceptance["gates"]["reconciliation"]["value"] == 0
    assert "DOGE-USDT-SWAP" not in acceptance["details"]["reconciliation_symbols"]


def test_fee_model_is_conservative(monkeypatch):
    monkeypatch.setenv("FEE_STOCK_MINIMUM", "1")
    monkeypatch.setenv("FEE_OPTION_PER_CONTRACT", "0.65")

    assert estimate_fees("US.NVDA", "BUY", 1, 100) == pytest.approx(1.003, abs=0.01)
    assert estimate_fees("US.NVDA260717C150000", "BUY", 2, 5) == pytest.approx(3.025, abs=0.01)
