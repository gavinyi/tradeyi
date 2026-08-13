import pandas as pd

from futu_quant.store import TradingStore
from futu_quant.volatility_orphans import (
    detect_volatility_orphan_positions,
    manage_volatility_orphans,
    protected_volatility_state_keys,
)


class FakeBroker:
    @staticmethod
    def is_cancellable_status(status):
        return str(status).upper() in {"SUBMITTED", "SUBMITTING", "FILLED_PART"}

    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol,
            "bid_price": 3.4,
            "ask_price": 3.6,
            "last_price": 3.5,
        } for symbol in symbols])


class FakeExecutor:
    def __init__(self, store):
        self.store = store
        self.submitted = []

    def submit_limit(self, *args, **kwargs):
        self.submitted.append((args, kwargs))
        raise AssertionError("ADOPT policy must not submit an immediate close order")


def test_detects_strategy_broker_position_missing_from_campaigns(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma",
        "open-old-spcx",
        "US.SPCX260814C141000",
        "BUY",
        1,
        4.75,
        {},
    )
    store.update_intent(intent["id"], "FILLED", broker_order_id="1")
    store.update_by_broker_order("1", "FILLED", 1, 4.75)
    positions = pd.DataFrame([{
        "code": "US.SPCX260814C141000",
        "qty": 1,
        "can_sell_qty": 1,
        "cost_price": 4.75,
        "nominal_price": 3.5,
        "market_val": 350,
        "pl_val": -125,
        "pl_ratio": -26.3,
    }])
    campaigns = [{"code": "US.SPCX260828C160000", "underlying": "US.SPCX"}]

    rows = detect_volatility_orphan_positions(
        store, positions, pd.DataFrame(), FakeBroker(), campaigns,
    )

    assert len(rows) == 1
    assert rows[0].code == "US.SPCX260814C141000"
    assert rows[0].status == "SELLABLE_ORPHAN"


def test_does_not_treat_manual_option_as_strategy_orphan(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    positions = pd.DataFrame([{
        "code": "US.SPCX260814C141000",
        "qty": 1,
        "can_sell_qty": 1,
        "cost_price": 4.75,
        "nominal_price": 3.5,
    }])

    rows = detect_volatility_orphan_positions(
        store, positions, pd.DataFrame(), FakeBroker(), [],
    )

    assert rows == []


def test_broker_risk_protects_state_from_pruning(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.save_campaign_states({
        "volatility_long_gamma:US.SPCX260814C141000": {"previous_position_quantity": 1},
    })
    positions = pd.DataFrame([{
        "code": "US.SPCX260814C141000",
        "qty": 1,
        "can_sell_qty": 1,
    }])

    keys = protected_volatility_state_keys(
        store, positions, pd.DataFrame(), FakeBroker(),
    )

    assert "volatility_long_gamma:US.SPCX260814C141000" in keys


def test_orphan_policy_defaults_to_adopt_instead_of_close(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr("futu_quant.store.TradingStore", lambda: store)
    intent, _ = store.create_intent(
        "volatility_long_gamma",
        "open-old-spcx",
        "US.SPCX260814C141000",
        "BUY",
        1,
        4.75,
        {},
    )
    store.update_intent(intent["id"], "FILLED", broker_order_id="1")
    store.update_by_broker_order("1", "FILLED", 1, 4.75)
    positions = pd.DataFrame([{
        "code": "US.SPCX260814C141000",
        "qty": 1,
        "can_sell_qty": 1,
        "cost_price": 4.75,
        "nominal_price": 5.75,
        "market_val": 575,
        "pl_val": 100,
        "pl_ratio": 21.0,
    }])
    campaigns = [{
        "code": "US.SPCX260828C160000",
        "underlying": "US.SPCX",
        "contracts_per_stage": 1,
        "max_stages": 1,
        "max_premium_per_stage": 3000,
        "profit_target": .5,
        "stop_loss": .25,
        "trailing_stop": .25,
        "entry_hours": 6,
    }]
    executor = FakeExecutor(store)

    results = manage_volatility_orphans(
        broker=FakeBroker(),
        executor=executor,
        campaigns=campaigns,
        positions=positions,
        orders=pd.DataFrame(),
    )

    assert results[0]["action"] == "ADOPTED"
    assert executor.submitted == []
    states = store.campaign_states()
    assert states["volatility_long_gamma:US.SPCX260814C141000"]["previous_position_quantity"] == 1
    adopted, _ = store.get_runtime_config("VOLATILITY_CAMPAIGNS")
    assert adopted[0]["code"] == "US.SPCX260814C141000"
    assert adopted[0]["exit_only"] is True
