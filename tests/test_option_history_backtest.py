from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from futu_quant.option_event_backtest import run_option_event_backtest
from futu_quant.option_history import collect_option_snapshots
from futu_quant.store import TradingStore


def test_option_history_import_and_coverage(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    store.import_option_history([{
        "code": "US.AAPL260720C300000", "time_key": "2026-07-17 10:00:00",
        "underlying": "US.AAPL", "underlying_price": 330, "bid": 30,
        "ask": 30.5, "iv": 0.4, "delta": 0.8, "gamma": 0.01,
        "theta": -0.1, "vega": 0.2, "rho": 0.1, "volume": 10,
        "open_interest": 100, "source": "FUTU_SNAPSHOT", "quality_status": "GOOD",
    }])
    coverage = store.option_history_coverage()
    assert coverage[0]["sample_count"] == 1
    assert coverage[0]["good_samples"] == 1
    assert store.option_history("US.AAPL260720C300000")[0]["vega"] == 0.2


def test_event_backtest_uses_ask_entry_and_bid_exit():
    snapshots = pd.DataFrame({
        "time_key": pd.date_range("2026-07-17 10:00", periods=5, freq="min"),
        "bid": [1.0, 1.1, 1.2, 1.4, 1.7],
        "ask": [1.1, 1.2, 1.3, 1.5, 1.8],
        "underlying_price": [300, 301, 302, 303, 304],
        "quality_status": ["GOOD"] * 5,
    })
    result = run_option_event_backtest(
        snapshots, "US.AAPL260720C300000", 10_000, 1,
        commission_per_contract=0, take_profit=0.5, stop_loss=0.5,
    )
    assert result.trades.iloc[0]["price"] == 1.1
    assert result.trades.iloc[-1]["price"] == 1.7
    assert result.metrics["spread_cost"] == 10
    assert result.metrics["total_return"] > 0


def test_collector_records_point_in_time_snapshot(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    fresh_market_time = datetime.now(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    monkeypatch.setattr(
        "futu_quant.option_history.load_gamma_campaigns",
        lambda: [{"code": "US.AAPL260720C300000"}],
    )
    # #1 键级分区：option_history 是「显式读全局」的三处之一，读取入口已改为
    # `load_all_volatility_campaigns()`（跨租户只读），这里按新名字打桩。
    monkeypatch.setattr(
        "futu_quant.option_history.load_all_volatility_campaigns", lambda: []
    )

    class Broker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            assert set(symbols) == {"US.AAPL260720C300000", "US.AAPL"}
            return pd.DataFrame([
                {
                    "code": "US.AAPL260720C300000", "bid_price": 30,
                    "ask_price": 30.5, "option_implied_volatility": 0.4,
                    "option_delta": 0.8, "option_gamma": 0.01,
                    "option_theta": -0.1, "option_vega": 0.2, "option_rho": 0.1,
                    "option_open_interest": 100, "volume": 10,
                    "update_time": fresh_market_time,
                },
                {"code": "US.AAPL", "last_price": 330},
            ])

    result = collect_option_snapshots(Broker(), store)
    # usable 是 2026-10-27 新增的「心跳口径」计数（盘口活性基准）；good/degraded 仍是 ML 标签口径
    assert result == {
        "contracts": 1, "stored": 1, "good": 1, "degraded": 0,
        "usable": 1, "missing": 0,
    }
    row = store.option_history("US.AAPL260720C300000")[0]
    assert row["source"] == "FUTU_SNAPSHOT_V1"
    assert row["underlying_price"] == 330
