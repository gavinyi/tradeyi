from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.risk import RiskEngine, RiskLimits, market_number
from futu_quant.store import TradingStore


@pytest.fixture(autouse=True)
def disable_approval_gate(monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")


class FakeBroker:
    def __init__(self):
        self.calls = 0
        self.settings = SimpleNamespace(enable_paper_orders=True)
        self.order_frame = pd.DataFrame(columns=["order_id", "order_status"])

    def orders(self, historical: bool = False) -> pd.DataFrame:
        return self.order_frame

    @staticmethod
    def is_cancellable_status(status) -> bool:
        return False

    def place_limit_order(self, symbol, side, quantity, price) -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame([{"order_id": f"paper-{self.calls}"}])


def test_duplicate_intent_is_submitted_only_once(tmp_path):
    broker = FakeBroker()
    executor = ExecutionManager(broker, TradingStore(tmp_path / "test.db"))

    first = executor.submit_limit("sma", "2026-07-15:US.NVDA:BUY", "US.NVDA", "BUY", 1, 170)
    duplicate = executor.submit_limit("sma", "2026-07-15:US.NVDA:BUY", "US.NVDA", "BUY", 1, 170)

    assert first.status == "SUBMITTED"
    assert duplicate.message == "重复交易意图已拦截"
    assert broker.calls == 1


def test_failed_intent_without_broker_order_can_retry(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    intent, created = store.create_intent("sma", "retry", "US.NVDA", "BUY", 1, 100)
    assert created
    store.update_intent(intent["id"], "FAILED", error="temporary quote error")

    retried, created = store.create_intent("sma", "retry", "US.NVDA", "BUY", 2, 101)

    assert created
    assert retried["id"] == intent["id"]
    assert retried["status"] == "CREATED"
    assert retried["quantity"] == 2


def test_transient_risk_rejection_can_retry_after_recovery(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    intent, _ = store.create_intent("gamma", "retry", "US.TEST", "BUY", 1, 100)
    store.update_intent(intent["id"], "REJECTED", error="启动恢复发现三账差异，请先完成持仓归属与对账")

    retried, created = store.create_intent("gamma", "retry", "US.TEST", "BUY", 1, 101)

    assert created
    assert retried["id"] == intent["id"]
    assert retried["status"] == "CREATED"
    assert retried["limit_price"] == 101


def test_recovery_block_does_not_block_shadow_buy(tmp_path):
    broker = FakeBroker()
    broker.positions = lambda: pd.DataFrame(columns=["code", "qty", "market_val"])
    broker.account_info = lambda: pd.DataFrame([{"total_assets": 1000, "power": 800, "unrealized_pl": 0}])
    broker.snapshots = lambda symbols: pd.DataFrame([
        {
            "code": symbol, "last_price": 2.0, "option_delta": -0.10,
            "option_gamma": 0.01, "option_vega": 0.05, "option_theta": -0.02,
            "option_contract_multiplier": 100,
        }
        for symbol in symbols
    ])
    store = TradingStore(tmp_path / "test.db")
    store.set_setting("recovery_blocked", "true")
    store.set_setting("strategy_execution_mode:volatility_long_gamma", "SHADOW")
    executor = ExecutionManager(broker, store)

    result = executor.submit_limit(
        "volatility_long_gamma",
        "cycle0:OPEN:stage1",
        "US.NVDA260729P205000",
        "BUY",
        1,
        2,
        {"multiplier": 100, "underlying": "US.NVDA"},
    )

    assert result.status == "SHADOW"
    assert broker.calls == 0


def test_recovery_block_still_blocks_paper_buy(tmp_path):
    broker = FakeBroker()
    store = TradingStore(tmp_path / "test.db")
    store.set_setting("recovery_blocked", "true")
    executor = ExecutionManager(broker, store)

    result = executor.submit_limit("sma", "buy", "US.NVDA", "BUY", 1, 100)

    assert result.status == "REJECTED"
    assert "三账差异" in result.message
    assert broker.calls == 0


def test_submit_limit_can_override_execution_mode_for_manual_close(tmp_path):
    broker = FakeBroker()
    broker.positions = lambda: pd.DataFrame([{"code": "US.NVDA260729P205000", "qty": 1}])
    broker.account_info = lambda: pd.DataFrame()
    store = TradingStore(tmp_path / "test.db")
    store.set_setting("strategy_execution_mode:volatility_long_gamma", "SHADOW")
    store.set_virtual_allocation("volatility_long_gamma", "US.NVDA260729P205000", 1, 7.7)
    executor = ExecutionManager(broker, store)

    result = executor.submit_limit(
        "volatility_long_gamma", "manual-close",
        "US.NVDA260729P205000", "SELL", 1, 7.7,
        {"multiplier": 100, "underlying": "US.NVDA"},
        execution_mode_override="PAPER",
    )

    assert result.status == "SUBMITTED"
    assert broker.calls == 1


def test_daily_equity_kill_switch_rejection_can_retry_after_reset(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    intent, _ = store.create_intent("gamma", "daily-loss", "US.TEST", "BUY", 1, 100)
    store.update_intent(
        intent["id"], "REJECTED", error="账户日内权益损失达到急停线，已自动急停",
    )

    retried, created = store.create_intent("gamma", "daily-loss", "US.TEST", "BUY", 1, 101)

    assert created
    assert retried["id"] == intent["id"]
    assert retried["status"] == "CREATED"
    assert retried["error"] is None


def test_campaign_state_can_be_explicitly_deleted(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    store.save_campaign_states({"volatility_cooldown:US.TEST": "now", "keep": 1})

    assert store.delete_campaign_states(["volatility_cooldown:US.TEST"]) == 1
    assert store.campaign_states() == {"keep": 1}


def test_clear_symbol_activity_removes_all_shadow_artifacts(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    intent, _ = store.create_intent("volatility_long_gamma", "cycle0", "US.TEST260717P100000", "BUY", 1, 2)
    store.update_intent(intent["id"], "SHADOW")
    store.record_shadow_fill(intent["id"], 1, 2, 0, {})
    store.save_campaign_states({
        "volatility_long_gamma:US.TEST260717P100000": {"peak": 2},
        "volatility_cooldown:US.TEST": "now",
    })

    cleared = store.clear_symbol_activity(
        "volatility_long_gamma",
        "US.TEST260717P100000",
        [
            "volatility_long_gamma:US.TEST260717P100000",
            "volatility_cooldown:US.TEST",
        ],
    )

    assert cleared["order_intents"] == 1
    assert cleared["shadow_fills"] == 1
    assert cleared["order_state_events"] >= 1
    assert cleared["campaign_state"] == 2
    assert not any(row["symbol"] == "US.TEST260717P100000" for row in store.intents())
    assert store.campaign_states() == {}


def test_permanent_risk_rejection_stays_idempotent(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    intent, _ = store.create_intent("gamma", "blocked", "US.TEST", "BUY", 1, 100)
    store.update_intent(intent["id"], "REJECTED", error="超过单笔风险上限")

    duplicate, created = store.create_intent("gamma", "blocked", "US.TEST", "BUY", 1, 101)

    assert not created
    assert duplicate["status"] == "REJECTED"


def test_futu_na_account_value_is_normalized():
    assert market_number("N/A") == 0.0


def test_shadow_sell_uses_shadow_ledger_instead_of_broker_position(tmp_path):
    broker = FakeBroker()
    broker.positions = lambda: pd.DataFrame(columns=["code", "qty"])
    broker.account_info = lambda: pd.DataFrame()
    store = TradingStore(tmp_path / "test.db")
    buy, _ = store.create_intent("volatility_long_gamma", "buy", "US.TEST260717C100000", "BUY", 1, 2)
    store.update_shadow_intent(buy["id"], "SHADOW_FILLED", 1, 2, 0)
    store.record_shadow_fill(buy["id"], 1, 2, 0, {})

    decision = RiskEngine(store).check(
        broker, "US.TEST260717C100000", "SELL", 1, 1, 100,
        "US.TEST", "volatility_long_gamma", "SHADOW",
    )

    assert decision.allowed


def test_shadow_positions_for_strategy_scans_ledger_once(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "shadow-positions.db")
    rows = [
        {"strategy_id": "volatility_long_gamma", "symbol": "US.A", "side": "BUY", "quantity": 2, "price": 3},
        {"strategy_id": "volatility_long_gamma", "symbol": "US.A", "side": "SELL", "quantity": 1, "price": 4},
        {"strategy_id": "volatility_long_gamma", "symbol": "US.B", "side": "BUY", "quantity": 1, "price": 5},
        {"strategy_id": "other", "symbol": "US.C", "side": "BUY", "quantity": 9, "price": 9},
    ]
    calls = 0

    def fake_shadow_fills(limit=10_000):
        nonlocal calls
        calls += 1
        return list(reversed(rows))

    monkeypatch.setattr(store, "shadow_fills", fake_shadow_fills)

    positions = store.shadow_positions_for_strategy("volatility_long_gamma")

    assert calls == 1
    assert positions == {"US.A": (1.0, 3.0), "US.B": (1.0, 5.0)}


def test_kill_switch_blocks_buy_but_allows_exit(tmp_path):
    broker = FakeBroker()
    store = TradingStore(tmp_path / "test.db")
    store.set_setting("kill_switch", "true")
    executor = ExecutionManager(broker, store)

    buy = executor.submit_limit("gamma", "buy", "US.NVDA260717C200000", "BUY", 1, 2)
    sell = executor.submit_limit("gamma", "sell", "US.NVDA260717C200000", "SELL", 1, 2)

    assert buy.status == "REJECTED"
    assert sell.status == "SUBMITTED"
    assert broker.calls == 1


def test_option_risk_uses_contract_multiplier(tmp_path):
    broker = FakeBroker()
    store = TradingStore(tmp_path / "test.db")
    risk = RiskEngine(store, RiskLimits(max_order_notional=1_000, max_daily_notional=10_000))

    decision = risk.check(broker, "US.NVDA260717C200000", "BUY", 1, 11)

    assert decision.notional == 1_100
    assert not decision.allowed


def test_reconcile_records_partial_fill(tmp_path):
    broker = FakeBroker()
    store = TradingStore(tmp_path / "test.db")
    executor = ExecutionManager(broker, store)
    executor.submit_limit("sma", "one", "US.NVDA", "BUY", 2, 100)
    broker.order_frame = pd.DataFrame([{
        "order_id": "paper-1", "order_status": "FILLED_PART",
        "dealt_qty": 1, "dealt_avg_price": 99.5,
    }])

    executor.reconcile()

    saved = store.intents()[0]
    assert saved["status"] == "FILLED_PART"
    assert saved["filled_quantity"] == 1
    assert saved["average_fill_price"] == 99.5


class PortfolioBroker(FakeBroker):
    def positions(self):
        return pd.DataFrame([{"code": "US.NVDA", "qty": 2, "market_val": 200}])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 1000, "power": 800, "unrealized_pl": 0}])

    @staticmethod
    def snapshots(symbols):
        return pd.DataFrame([{"code": symbol, "last_price": 100} for symbol in symbols])


def test_portfolio_symbol_weight_is_enforced(tmp_path):
    broker = PortfolioBroker()
    risk = RiskEngine(
        TradingStore(tmp_path / "test.db"),
        RiskLimits(max_order_notional=10_000, max_daily_notional=50_000,
                   max_symbol_weight=0.25),
    )

    decision = risk.check(broker, "US.NVDA", "BUY", 1, 100)

    assert not decision.allowed
    assert "单标的仓位" in decision.reason


class SmartBroker(FakeBroker):
    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbols[0], "bid_price": 99, "ask_price": 101}])

    def place_limit_order(self, symbol, side, quantity, price):
        self.submitted_price = price
        return super().place_limit_order(symbol, side, quantity, price)


def test_smart_execution_starts_at_mid_price(tmp_path):
    broker = SmartBroker()
    executor = ExecutionManager(broker, TradingStore(tmp_path / "test.db"))

    executor.submit_limit("sma", "smart", "US.NVDA", "BUY", 1, 101)

    assert broker.submitted_price == 100


class RepriceBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.modified_price = None

    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbols[0], "bid_price": 9.50, "ask_price": 10.50}])

    def modify_limit_order(self, broker_order_id, quantity, price):
        self.modified_price = price


def test_urgent_sell_reprice_uses_exit_slippage_not_normal_one_percent(tmp_path, monkeypatch):
    monkeypatch.setenv("EXEC_REPRICE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("EXEC_MAX_REPRICE_ATTEMPTS", "3")
    monkeypatch.setenv("EXEC_REPRICE_STEP_PCT", "0.10")
    monkeypatch.setenv("EXEC_MAX_SLIPPAGE_PCT", "0.01")
    monkeypatch.setenv("EXEC_URGENT_EXIT_MAX_SLIPPAGE_PCT", "0.12")
    store = TradingStore(tmp_path / "test.db")
    broker = RepriceBroker()
    intent, _ = store.create_intent(
        "volatility_long_gamma",
        "urgent-exit",
        "US.SPCX260814P142000",
        "SELL",
        1,
        10.00,
        {"urgent": True, "reference_price": 10.00},
    )
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id="paper-1")
    with store.connection() as connection:
        connection.execute(
            "UPDATE order_intents SET created_at=?, last_reprice_at=? WHERE id=?",
            ("2026-08-13T14:00:00+00:00", "2026-08-13T14:00:00+00:00", intent["id"]),
        )

    repriced = ExecutionManager(broker, store).reprice_open_orders()

    assert repriced == 1
    assert broker.modified_price <= 9.50  # 突破了 1% 正常滑点(9.90)，启用了紧急滑点下探吃单


def test_shadow_mode_never_calls_broker(tmp_path):
    broker = FakeBroker()
    store = TradingStore(tmp_path / "test.db")
    store.set_setting("execution_mode", "SHADOW")
    executor = ExecutionManager(broker, store)

    result = executor.submit_limit("sma", "shadow", "US.NVDA", "BUY", 1, 100)

    assert result.status == "SHADOW"
    assert broker.calls == 0


def test_strategy_budget_blocks_new_risk(tmp_path, monkeypatch):
    broker = PortfolioBroker()
    store = TradingStore(tmp_path / "test.db")
    store.save_strategy_risk_budgets([{
        "strategy_id": "sma", "capital_ratio": 0.1,
        "max_loss_ratio": 0.1, "option_premium_ratio": 0.0, "enabled": True,
    }])
    risk = RiskEngine(
        store,
        RiskLimits(max_order_notional=10_000, max_daily_notional=50_000,
                   max_symbol_weight=1.0, max_gross_exposure=1.0),
    )

    decision = risk.check(broker, "US.QQQ", "BUY", 1, 200, strategy_id="sma")

    assert not decision.allowed
    assert "资金占用" in decision.reason


class LargePositionBroker(FakeBroker):
    def positions(self):
        return pd.DataFrame([{
            "code": "US.SPY", "qty": 425, "can_sell_qty": 425,
            "market_val": 315_834.5,
        }])

    @staticmethod
    def account_info():
        return pd.DataFrame([{
            "total_assets": 987_529, "power": 400_000, "unrealized_pl": 0,
        }])


def test_verified_reduction_uses_separate_order_allowance(tmp_path):
    broker = LargePositionBroker()
    store = TradingStore(tmp_path / "test.db")
    store.set_virtual_allocation("core_satellite", "US.SPY", 425, 751.82)
    limits = RiskLimits(
        max_order_notional=20_000, max_daily_notional=100_000,
        max_reduction_order_notional=150_000,
        max_daily_reduction_notional=500_000,
    )

    decision = RiskEngine(store, limits).check(
        broker, "US.SPY", "SELL", 145, 743.14, strategy_id="core_satellite",
    )

    assert decision.allowed
    assert decision.notional == pytest.approx(107_755.3)


def test_reduction_allowance_still_rejects_excess_and_short_sales(tmp_path):
    broker = LargePositionBroker()
    limits = RiskLimits(
        max_order_notional=20_000, max_daily_notional=100_000,
        max_reduction_order_notional=100_000,
        max_daily_reduction_notional=500_000,
    )
    risk = RiskEngine(TradingStore(tmp_path / "test.db"), limits)

    oversized = risk.check(broker, "US.SPY", "SELL", 145, 743.14)
    short_sale = risk.check(broker, "US.SPY", "SELL", 426, 743.14)

    assert not oversized.allowed
    assert "单笔减仓" in oversized.reason
    assert not short_sale.allowed
    assert "可用持仓" in short_sale.reason


def test_virtual_position_prevents_cross_strategy_sell(tmp_path, monkeypatch):
    monkeypatch.setenv("ENFORCE_VIRTUAL_POSITIONS", "true")
    broker = PortfolioBroker()
    store = TradingStore(tmp_path / "test.db")
    store.set_virtual_allocation("core", "US.NVDA", 1, 100)
    risk = RiskEngine(store, RiskLimits(max_order_notional=10_000, max_daily_notional=50_000))

    decision = risk.check(broker, "US.NVDA", "SELL", 2, 100, strategy_id="core")

    assert not decision.allowed
    assert "虚拟持仓" in decision.reason
