from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.combo_execution import (
    ComboExecutionManager, create_combo, find_open_combo_for_campaign, validate_combo,
)
from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import TradingStore


def vertical_legs():
    return [
        {"role": "PROTECTIVE", "symbol": "US.NVDA260717C220000", "side": "BUY", "quantity": 1, "limit_price": 1.0},
        {"role": "RISK", "symbol": "US.NVDA260717C210000", "side": "SELL", "quantity": 1, "limit_price": 0.6},
    ]


def combo_config():
    return {
        "strategy_id": "volatility_long_gamma", "name": "测试看涨价差",
        "underlying": "US.NVDA", "structure": "VERTICAL",
        "max_net_debit": 0.5, "min_net_credit": 0,
        "assembly_timeout_seconds": 300, "naked_timeout_seconds": 15,
        "auto_rollback": True,
    }


def test_combo_rejects_unprotected_short():
    legs = vertical_legs()
    legs[0]["symbol"] = "US.NVDA260716C200000"

    with pytest.raises(ValueError, match="没有足量"):
        validate_combo(combo_config(), legs)


def test_combo_rejects_net_debit_above_limit():
    config = combo_config()
    config["max_net_debit"] = 0.3

    with pytest.raises(ValueError, match="净借记"):
        validate_combo(config, vertical_legs())


class FakeBroker:
    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol, "bid_price": 0.95, "ask_price": 1.05,
            "option_volume": 1000, "option_open_interest": 5000,
        } for symbol in symbols])

    def cancel_order(self, order_id):
        return pd.DataFrame([{"order_id": order_id}])


class FakeExecutor:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def submit_limit(self, strategy, key, symbol, side, quantity, price, metadata):
        intent, _ = self.store.create_intent(strategy, key, symbol, side, quantity, price, metadata)
        self.store.update_intent(intent["id"], "SUBMITTED", broker_order_id=f"paper-{intent['id']}")
        self.calls.append((symbol, side, quantity, price, metadata, intent["id"]))
        return SimpleNamespace(intent_id=intent["id"], status="SUBMITTED", message="模拟订单已提交")


class ApprovalBlockedExecutor:
    @staticmethod
    def submit_limit(*_args, **_kwargs):
        return SimpleNamespace(
            intent_id=0, status="APPROVAL_REQUIRED", message="策略运行版本未批准",
        )


def fill(store, intent_id):
    intent = store.intent(intent_id)
    store.update_shadow_intent(intent_id, "FILLED", intent["quantity"], intent["limit_price"], 0)


def test_combo_opens_protection_first_and_closes_short_first(tmp_path):
    store = TradingStore(tmp_path / "combo.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    executor = FakeExecutor(store)
    manager = ComboExecutionManager(FakeBroker(), store, executor)

    manager.start(combo_id)
    assert [(call[0], call[1]) for call in executor.calls] == [
        ("US.NVDA260717C220000", "BUY")
    ]

    fill(store, executor.calls[-1][5])
    manager.advance(combo_id)
    assert [(call[0], call[1]) for call in executor.calls] == [
        ("US.NVDA260717C220000", "BUY"),
        ("US.NVDA260717C210000", "SELL"),
    ]
    assert executor.calls[-1][4]["protected_short"] is True

    fill(store, executor.calls[-1][5])
    manager.advance(combo_id)
    assert store.option_combo(combo_id)["status"] == "OPEN"

    manager.request_close(combo_id)
    assert executor.calls[-1][0:2] == ("US.NVDA260717C210000", "BUY")
    fill(store, executor.calls[-1][5])
    manager.advance(combo_id)
    assert executor.calls[-1][0:2] == ("US.NVDA260717C220000", "SELL")


def test_combo_close_skips_leg_already_flat_in_broker_positions(tmp_path):
    store = TradingStore(tmp_path / "combo-flat.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    for leg in store.option_combo_legs(combo_id):
        store.update_option_combo_leg(leg["id"], status="FILLED", filled_quantity=1)
    store.update_option_combo(combo_id, "OPEN")

    class FlatBroker(FakeBroker):
        def positions(self, refresh=False):
            return pd.DataFrame([
                {"code": "US.NVDA260717C220000", "qty": 1, "can_sell_qty": 1},
            ])

    executor = FakeExecutor(store)
    manager = ComboExecutionManager(FlatBroker(), store, executor)

    manager.request_close(combo_id)

    legs = store.option_combo_legs(combo_id)
    short_leg = next(leg for leg in legs if leg["side"] == "SELL")
    assert short_leg["status"] == "CLOSED"
    assert executor.calls == []


def test_combo_close_caps_quantity_to_broker_visible_position(tmp_path):
    store = TradingStore(tmp_path / "combo-cap.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    short_leg = next(leg for leg in store.option_combo_legs(combo_id) if leg["side"] == "SELL")
    store.update_option_combo_leg(short_leg["id"], status="FILLED", filled_quantity=2)
    store.update_option_combo(combo_id, "OPEN")

    class PartialShortBroker(FakeBroker):
        def positions(self, refresh=False):
            return pd.DataFrame([
                {"code": "US.NVDA260717C210000", "qty": -1},
            ])

    executor = FakeExecutor(store)
    manager = ComboExecutionManager(PartialShortBroker(), store, executor)

    manager.request_close(combo_id)

    assert executor.calls[-1][0:3] == ("US.NVDA260717C210000", "BUY", 1)


def test_monitor_closes_orphan_open_combo_with_broker_exposure(tmp_path):
    store = TradingStore(tmp_path / "orphan-combo.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    for leg in store.option_combo_legs(combo_id):
        store.update_option_combo_leg(leg["id"], status="FILLED", filled_quantity=1)
    store.update_option_combo(combo_id, "OPEN")

    class ComboPositionBroker(FakeBroker):
        def positions(self, refresh=False):
            return pd.DataFrame([
                {"code": "US.NVDA260717C220000", "qty": 1, "can_sell_qty": 1},
                {"code": "US.NVDA260717C210000", "qty": -1, "can_sell_qty": -1},
            ])

    executor = FakeExecutor(store)
    manager = ComboExecutionManager(ComboPositionBroker(), store, executor)

    manager.monitor_all(active_combo_ids=set(), close_orphan_open=True)

    assert store.option_combo(combo_id)["status"] == "CLOSING"
    assert executor.calls[-1][0:3] == ("US.NVDA260717C210000", "BUY", 1)


def test_monitor_keeps_active_open_combo(tmp_path):
    store = TradingStore(tmp_path / "active-combo.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    for leg in store.option_combo_legs(combo_id):
        store.update_option_combo_leg(leg["id"], status="FILLED", filled_quantity=1)
    store.update_option_combo(combo_id, "OPEN")

    class ComboPositionBroker(FakeBroker):
        def positions(self, refresh=False):
            return pd.DataFrame([
                {"code": "US.NVDA260717C220000", "qty": 1, "can_sell_qty": 1},
                {"code": "US.NVDA260717C210000", "qty": -1, "can_sell_qty": -1},
            ])

    executor = FakeExecutor(store)
    manager = ComboExecutionManager(ComboPositionBroker(), store, executor)

    manager.monitor_all(active_combo_ids={combo_id}, close_orphan_open=True)

    assert store.option_combo(combo_id)["status"] == "OPEN"
    assert executor.calls == []


def test_find_open_combo_recovers_drifted_campaign_identity(tmp_path):
    store = TradingStore(tmp_path / "combo-recovery.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    store.update_option_combo(combo_id, "OPEN")

    recovered = find_open_combo_for_campaign(
        store,
        strategy_id="volatility_long_gamma",
        underlying="US.NVDA",
        structure="VERTICAL",
        leg_symbols={"US.NVDA260717C210000", "US.NVDA260717C999000"},
    )

    assert recovered is not None
    assert recovered["combo_id"] == combo_id


def test_combo_approval_rejection_does_not_write_invalid_foreign_key(tmp_path):
    store = TradingStore(tmp_path / "combo-approval.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    manager = ComboExecutionManager(
        FakeBroker(), store, ApprovalBlockedExecutor(),
    )

    result = manager.start(combo_id)
    first_leg = store.option_combo_legs(combo_id)[0]

    assert result["status"] == "EXECUTING"
    assert first_leg["status"] == "FAILED"
    assert first_leg["intent_id"] is None


def test_protected_option_short_has_explicit_risk_path(tmp_path):
    store = TradingStore(tmp_path / "risk.db")

    class Broker:
        @staticmethod
        def orders():
            return pd.DataFrame(columns=["order_status"])

        @staticmethod
        def is_cancellable_status(_status):
            return False

        @staticmethod
        def positions():
            return pd.DataFrame(columns=["code", "qty"])

        @staticmethod
        def account_info():
            return pd.DataFrame()

    decision = RiskEngine(
        store, RiskLimits(max_order_notional=10_000, max_daily_notional=50_000)
    ).check(
        Broker(), "US.NVDA260717C210000", "SELL", 1, 0.6,
        multiplier=100, underlying="US.NVDA", strategy_id="volatility_long_gamma",
        protected_short=True,
    )

    assert decision.allowed
    assert any(row["event_type"] == "PROTECTED_COMBO_SHORT_ALLOWED" for row in store.audit_events())


def test_roll_draft_cannot_start_before_parent_is_closed(tmp_path):
    store = TradingStore(tmp_path / "roll.db")
    parent = create_combo(store, combo_config(), vertical_legs())
    child_config = {**combo_config(), "parent_combo_id": parent, "requires_parent_closed": True}
    child = create_combo(store, child_config, vertical_legs())
    manager = ComboExecutionManager(FakeBroker(), store, FakeExecutor(store))

    with pytest.raises(ValueError, match="旧组合尚未完全平仓"):
        manager.start(child)


def test_failed_unfilled_combo_retries_with_new_idempotency_generation(tmp_path):
    store = TradingStore(tmp_path / "retry.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())

    class RejectThenSubmit(FakeExecutor):
        def submit_limit(self, strategy, key, symbol, side, quantity, price, metadata):
            intent, _ = self.store.create_intent(
                strategy, key, symbol, side, quantity, price, metadata,
            )
            status = "REJECTED" if not self.calls else "SUBMITTED"
            self.store.update_intent(intent["id"], status, error="临时风控" if status == "REJECTED" else None)
            self.calls.append((key, intent["id"]))
            return SimpleNamespace(intent_id=intent["id"], status=status, message=status)

    executor = RejectThenSubmit(store)
    manager = ComboExecutionManager(FakeBroker(), store, executor)
    manager.start(combo_id)
    manager.advance(combo_id)
    assert store.option_combo(combo_id)["status"] == "FAILED"

    manager.start(combo_id)
    assert len(executor.calls) == 2
    assert executor.calls[0][0] != executor.calls[1][0]
    assert "attempt:2" in executor.calls[1][0]
    assert store.option_combo_legs(combo_id)[0]["intent_id"] == executor.calls[1][1]


def test_failed_combo_with_filled_leg_cannot_be_blindly_retried(tmp_path):
    store = TradingStore(tmp_path / "filled-retry.db")
    combo_id = create_combo(store, combo_config(), vertical_legs())
    first_leg = store.option_combo_legs(combo_id)[0]
    store.update_option_combo_leg(first_leg["id"], status="FILLED", filled_quantity=1)
    store.update_option_combo(combo_id, "FAILED", "需要人工处置")
    manager = ComboExecutionManager(FakeBroker(), store, FakeExecutor(store))

    with pytest.raises(ValueError, match="已有成交腿"):
        manager.start(combo_id)
