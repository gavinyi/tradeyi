import pandas as pd

from futu_quant.portfolio_allocator import strategy_budget_allowed, strategy_budget_usage
from futu_quant.store import TradingStore
from futu_quant.stress import joint_stress_test


class BudgetBroker:
    @staticmethod
    def account_info():
        return pd.DataFrame([{"total_assets": 100_000}])

    @staticmethod
    def positions():
        return pd.DataFrame([{
            "code": "US.AAPL260720C300000", "qty": 1, "nominal_price": 10,
            "cost_price": 10, "market_val": 1000,
        }])

    @staticmethod
    def snapshots(symbols):
        return pd.DataFrame([{"code": symbol, "last_price": 10} for symbol in symbols])


def test_three_dimension_budget_usage_and_block(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.save_strategy_risk_budgets([{
        "strategy_id": "gamma", "capital_ratio": 0.02,
        "max_loss_ratio": 0.02, "option_premium_ratio": 0.02, "enabled": True,
    }])
    store.set_virtual_allocation("gamma", "US.AAPL260720C300000", 1, 10)
    usage, equity = strategy_budget_usage(store, BudgetBroker())
    assert equity == 100_000
    assert usage.iloc[0]["capital_used"] == 1000
    allowed, reason, details = strategy_budget_allowed(
        store, BudgetBroker(), "gamma", "US.AAPL260720C300000", "BUY", 2, 10, 100,
    )
    assert not allowed
    assert "资金占用" in reason
    assert details["capital"] == 3000


def test_disabled_strategy_blocks_buy_but_sell_is_allowed(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.save_strategy_risk_budgets([{
        "strategy_id": "gamma", "capital_ratio": 0.1,
        "max_loss_ratio": 0.1, "option_premium_ratio": 0.1, "enabled": False,
    }])
    buy = strategy_budget_allowed(store, BudgetBroker(), "gamma", "US.AAPL", "BUY", 1, 100, 1)
    sell = strategy_budget_allowed(store, BudgetBroker(), "gamma", "US.AAPL", "SELL", 1, 100, 1)
    assert not buy[0]
    assert sell[0]


def test_joint_stress_combines_spot_iv_theta_and_liquidity():
    positions = pd.DataFrame([
        {"code": "US.AAPL", "qty": 100, "market_val": 30_000},
        {"code": "US.AAPL260720C300000", "qty": 1, "market_val": 1_000},
    ])
    quotes = pd.DataFrame([
        {"code": "US.AAPL", "last_price": 300, "underlying_price": 300},
        {
            "code": "US.AAPL260720C300000", "last_price": 10,
            "underlying_price": 300, "option_delta": 0.5, "option_gamma": 0.01,
            "option_vega": 0.2, "option_theta": -0.1,
            "option_contract_multiplier": 100,
        },
    ])
    result = joint_stress_test(positions, quotes, 100_000)
    assert len(result) == 7
    assert result.iloc[0]["estimated_pnl"] < 0
    assert {"stock_pnl", "option_pnl", "liquidity_cost"}.issubset(result.columns)
