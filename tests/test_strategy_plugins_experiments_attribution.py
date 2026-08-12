import pandas as pd

from futu_quant.attribution import attribution_report
from futu_quant.store import TradingStore
from futu_quant.strategies import STRATEGIES
from futu_quant.strategy_plugins import strategy_plugins


def test_all_stock_plugin_runner_keys_exist():
    for plugin in strategy_plugins():
        if plugin.runner_key:
            assert plugin.runner_key in STRATEGIES
        assert plugin.execution_adapter == "unified_limit_execution"


def test_experiment_promotion_keeps_one_active_version(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    first = store.register_experiment("core", "hash-1", "v1", {}, ["US.AAPL"], "data-1")
    second = store.register_experiment("core", "hash-2", "v2", {}, ["US.NVDA"], "data-2")
    store.promote_experiment(first)
    assert store.active_experiment("core")["id"] == first
    store.promote_experiment(second)
    assert store.active_experiment("core")["id"] == second
    assert sum(row["is_active"] for row in store.strategy_experiments("core")) == 1


def test_attribution_combines_fifo_unrealized_fees_and_slippage(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.set_virtual_allocation("core", "US.AAPL", 10, 100)
    buy, _ = store.create_intent(
        "core", "buy", "US.AAPL", "BUY", 1, 101, {"reference_price": 100},
    )
    store.update_intent(buy["id"], "SUBMITTED", "buy-order")
    store.update_by_broker_order("buy-order", "FILLED", 1, 101, 1)
    sell, _ = store.create_intent(
        "core", "sell", "US.AAPL", "SELL", 1, 119, {"reference_price": 120},
    )
    store.update_intent(sell["id"], "SUBMITTED", "sell-order")
    store.update_by_broker_order("sell-order", "FILLED", 1, 119, 1)

    class Broker:
        @staticmethod
        def snapshots(symbols):
            return pd.DataFrame([{"code": symbol, "last_price": 110} for symbol in symbols])

    summary, detail = attribution_report(store, Broker())
    assert detail.iloc[0]["realized_pnl"] == 19
    assert detail.iloc[0]["unrealized_pnl"] == 99
    assert detail.iloc[0]["fees"] == 2
    assert detail.iloc[0]["slippage_cost"] == 2
    assert detail.iloc[0]["delta_exposure"] == 1100
    assert summary.iloc[0]["total_pnl"] == 116


def test_attribution_skips_non_us_symbols_for_futu_snapshots(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.set_virtual_allocation("crypto_volatility_swap", "AVAX-USDT-SWAP", 2, 25)
    store.set_virtual_allocation("core", "US.AAPL", 1, 100)

    class Broker:
        @staticmethod
        def snapshots(symbols):
            assert symbols == ["US.AAPL"]
            return pd.DataFrame([{"code": "US.AAPL", "last_price": 110}])

    summary, detail = attribution_report(store, Broker())
    by_symbol = detail.set_index("symbol")

    assert set(by_symbol.index) == {"AVAX-USDT-SWAP", "US.AAPL"}
    assert by_symbol.loc["AVAX-USDT-SWAP", "quote_status"] == "未请求行情：非美股代码"
    assert by_symbol.loc["AVAX-USDT-SWAP", "market_value"] == 0
    assert summary["strategy_id"].tolist() == ["core", "crypto_volatility_swap"]
