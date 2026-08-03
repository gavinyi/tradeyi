from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from futu_quant.crypto_swap import CryptoSwapConfig, decide_crypto_swap
from futu_quant.crypto_swap_managed_futures import (
    CryptoSwapManagedFuturesConfig,
    decide_crypto_swap_managed_futures,
)
from futu_quant.crypto_swap_execution import execute_crypto_swap_cycle
from futu_quant.crypto_backtest import (
    backtest_quality_metrics,
    run_crypto_swap_managed_futures_backtest,
    run_crypto_swap_backtest,
    run_crypto_swap_parameter_grid,
)
from futu_quant.crypto_swap_models import (
    CryptoSwapInstrument,
    CryptoSwapOrderRequest,
    normalize_swap_symbol,
)
from futu_quant.crypto_swap_risk import CryptoSwapRiskConfig, swap_entry_block_reason
from futu_quant.okx_broker import OkxDemoBroker
from futu_quant.okx_config import OkxSettings


def swap_bars(direction: int = 1, count: int = 140) -> pd.DataFrame:
    price = 100.0
    rows = []
    for index in range(count):
        change = 0.001 if index < count - 12 else 0.018 * direction
        price *= 1 + change
        rows.append({
            "timestamp": index * 3_600_000,
            "open": price / (1 + change), "high": price * 1.01,
            "low": price * .99, "close": price,
            "volume": 100 if index < count - 1 else 400, "confirm": True,
        })
    return pd.DataFrame(rows)


def managed_short_bars(count: int = 180, drift: float = -0.004) -> pd.DataFrame:
    price = 100.0
    rows = []
    for index in range(count):
        change = drift if index > count - 130 else -0.001
        price *= 1 + change
        rows.append({
            "timestamp": index * 3_600_000,
            "open": price / (1 + change),
            "high": price * 1.004,
            "low": price * .996,
            "close": price,
            "volume": 100 if index < count - 1 else 130,
            "confirm": True,
        })
    return pd.DataFrame(rows)


def test_swap_symbol_and_contract_precision():
    assert normalize_swap_symbol("btc-usdt-swap") == "BTC-USDT-SWAP"
    assert normalize_swap_symbol("A-USDT-SWAP") == "A-USDT-SWAP"
    with pytest.raises(ValueError, match="USDT 永续"):
        normalize_swap_symbol("BTC-USDT")
    instrument = CryptoSwapInstrument(
        "BTC-USDT-SWAP", Decimal("0.1"), Decimal("1"), Decimal("1"),
        Decimal("0.01"), "BTC", "USDT",
    )
    assert instrument.normalize_contracts(Decimal("3.8")) == Decimal("3")


def test_swap_close_order_requires_reduce_only():
    with pytest.raises(ValueError, match="平仓订单必须"):
        CryptoSwapOrderRequest(
            "BTC-USDT-SWAP", "sell", Decimal("1"), Decimal("100"),
            reduce_only=False, intent="CLOSE_LONG",
        )


def test_swap_strategy_can_open_long_and_short():
    config = CryptoSwapConfig(
        entry_enabled=True,
        higher_timeframe_enabled=False,
        long_min_volume_ratio=1.0,
        long_min_trend_strength=0,
        short_min_volume_ratio=1.0,
        short_min_trend_strength=0,
        continuation_min_return=0,
        short_continuation_min_return=0,
    )
    long_decision = decide_crypto_swap(swap_bars(1), 0, 0, config)
    short_decision = decide_crypto_swap(swap_bars(-1), 0, 0, config)
    assert long_decision.action == "OPEN_LONG"
    assert short_decision.action == "OPEN_SHORT"
    assert long_decision.metrics["regime"] == "TREND_UP"
    assert short_decision.metrics["regime"] == "TREND_DOWN"


def test_swap_higher_timeframe_blocks_trend_breakout_mismatch():
    decision = decide_crypto_swap(
        swap_bars(1), 0, 0,
        CryptoSwapConfig(
            entry_enabled=True,
            higher_timeframe_enabled=True,
            higher_timeframe_min_bars=80,
            long_min_volume_ratio=1.0,
            long_min_trend_strength=0,
            continuation_min_return=0,
        ),
        higher_timeframe_bars=swap_bars(-1, 100),
    )
    assert decision.action == "HOLD"
    assert "高周期确认失败" in decision.reason


def test_swap_mean_reversion_uses_small_independent_model():
    bars = swap_bars(1, 140)
    bars.loc[bars.index[-1], "close"] = bars["close"].iloc[-2] * 1.18
    bars.loc[bars.index[-1], "open"] = bars["close"].iloc[-1]
    bars.loc[bars.index[-1], "high"] = bars["close"].iloc[-1] * 1.01
    bars.loc[bars.index[-1], "low"] = bars["close"].iloc[-1] * .99
    decision = decide_crypto_swap(
        bars, 0, 0,
        CryptoSwapConfig(
            entry_enabled=True,
            trend_breakout_enabled=False,
            mean_reversion_enabled=True,
            higher_timeframe_enabled=False,
            mean_reversion_zscore=1.0,
            mean_reversion_rsi_overbought=60,
            mean_reversion_bollinger_upper=.5,
            mean_reversion_min_volume_ratio=0,
            mean_reversion_position_scale=.25,
        ),
    )
    assert decision.action == "OPEN_SHORT"
    assert decision.metrics["entry_model"] == "均值回归"
    assert decision.target_notional == pytest.approx(250)


def test_managed_futures_opens_only_weak_alt_short():
    config = CryptoSwapConfig(entry_enabled=True, margin_budget=500, leverage=2)
    mf_config = CryptoSwapManagedFuturesConfig(score_threshold=55, max_position_loss=.5)
    decision = decide_crypto_swap_managed_futures(
        "DOGE-USDT-SWAP",
        managed_short_bars(),
        0,
        0,
        config,
        mf_config,
        higher_timeframe_bars=managed_short_bars(),
        daily_bars=managed_short_bars(),
        benchmark_bars=swap_bars(1, 180),
    )
    assert decision.action == "OPEN_SHORT"
    assert decision.target_notional == pytest.approx(350)
    assert decision.metrics["strategy_engine"] == "crypto_swap_managed_futures"


def test_managed_futures_blocks_major_short_by_default():
    decision = decide_crypto_swap_managed_futures(
        "BTC-USDT-SWAP",
        managed_short_bars(),
        0,
        0,
        CryptoSwapConfig(entry_enabled=True),
        CryptoSwapManagedFuturesConfig(score_threshold=55),
        higher_timeframe_bars=managed_short_bars(),
        daily_bars=managed_short_bars(),
        benchmark_bars=managed_short_bars(),
    )
    assert decision.action == "HOLD"
    assert "禁止开空" in decision.reason


def test_managed_futures_adds_only_when_weakness_still_valid():
    config = CryptoSwapConfig(entry_enabled=True, margin_budget=500, leverage=2)
    mf_config = CryptoSwapManagedFuturesConfig(score_threshold=55, max_position_loss=.5)
    decision = decide_crypto_swap_managed_futures(
        "DOGE-USDT-SWAP",
        managed_short_bars(),
        -2,
        45,
        config,
        mf_config,
        recovery_layer=0,
        higher_timeframe_bars=managed_short_bars(),
        daily_bars=managed_short_bars(),
        benchmark_bars=swap_bars(1, 180),
    )
    assert decision.action == "OPEN_SHORT"
    assert decision.metrics["next_recovery_layer"] == 1


def test_swap_strategy_default_blocks_new_entries_only():
    decision = decide_crypto_swap(swap_bars(1), 0, 0, CryptoSwapConfig(min_volume_ratio=1.0))
    assert decision.action == "HOLD"
    assert "新开仓已关闭" in decision.reason
    managed = decide_crypto_swap(
        swap_bars(1), 2, 100,
        CryptoSwapConfig(min_volume_ratio=1.0, min_hold_hours=0),
        peak_pnl=.2,
        partial_stage=0,
        held_hours=3,
    )
    assert managed.action == "CLOSE_LONG"


def test_swap_continuation_filter_blocks_stale_breakout():
    config = CryptoSwapConfig(
        entry_enabled=True,
        min_volume_ratio=1.0,
        higher_timeframe_enabled=False,
        continuation_bars=2,
        continuation_min_return=.20,
    )
    decision = decide_crypto_swap(swap_bars(1), 0, 0, config)
    assert decision.action == "HOLD"
    assert "趋势延续不足" in decision.reason
    assert decision.metrics["continuation_long_ok"] is False


def test_swap_short_uses_independent_stricter_filters():
    decision = decide_crypto_swap(
        swap_bars(-1), 0, 0,
        CryptoSwapConfig(
            entry_enabled=True,
            min_volume_ratio=0.7,
            higher_timeframe_enabled=False,
            short_min_volume_ratio=99.0,
            short_min_trend_strength=.50,
            short_continuation_min_return=.20,
        ),
    )
    assert decision.action == "HOLD"
    assert "做空" in decision.reason
    assert decision.metrics["raw_short_signal"] is True
    assert decision.metrics["short_volume_ok"] is False


def test_swap_strategy_supports_partial_exit_and_min_hold():
    config = CryptoSwapConfig(
        min_volume_ratio=1.0, min_hold_hours=2,
        partial_profit_tier1=.05, partial_profit_tier2=.12,
    )
    decision = decide_crypto_swap(
        swap_bars(1), 2, 100, config, peak_pnl=.2,
        partial_stage=0, held_hours=.5,
    )
    assert decision.action == "HOLD"
    decision = decide_crypto_swap(
        swap_bars(1), 2, 100, config, peak_pnl=.2,
        partial_stage=0, held_hours=3,
    )
    assert decision.action == "CLOSE_LONG"
    assert decision.quantity_fraction == pytest.approx(.5)


def test_swap_hard_risk_can_exit_before_min_hold():
    config = CryptoSwapConfig(
        min_volume_ratio=1.0,
        min_hold_hours=2,
        emergency_reverse_loss=.02,
    )
    decision = decide_crypto_swap(
        swap_bars(-1), 2, 94, config, peak_pnl=0,
        partial_stage=0, held_hours=.2,
    )
    assert decision.action == "CLOSE_LONG"
    assert "紧急反向信号退出" in decision.reason


def test_swap_liquidation_buffer_can_exit_before_min_hold():
    config = CryptoSwapConfig(
        min_volume_ratio=1.0,
        min_hold_hours=2,
        liquidation_buffer_exit=.15,
    )
    decision = decide_crypto_swap(
        swap_bars(1), 2, 100, config, peak_pnl=0,
        partial_stage=0, held_hours=.2, liquidation_buffer=.10,
    )
    assert decision.action == "CLOSE_LONG"
    assert "强平缓冲过低" in decision.reason


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        return self.responses.pop(0)


def test_swap_order_payload_is_isolated_and_reduce_only():
    transport = Transport([{"code": "0", "msg": "", "data": [{"ordId": "1", "sCode": "0"}]}])
    broker = OkxDemoBroker(
        OkxSettings(
            api_key="k", secret_key="s", passphrase="p", enable_demo_orders=True,
        ),
        transport=transport,
        clock=lambda: "2026-07-26T00:00:00.000Z",
    )
    broker.place_swap_order(CryptoSwapOrderRequest(
        "BTC-USDT-SWAP", "sell", Decimal("2"), Decimal("100"),
        reduce_only=True, intent="CLOSE_LONG",
    ))
    body = transport.calls[0][3]
    assert '"tdMode":"isolated"' in body
    assert '"reduceOnly":true' in body


class Store:
    def __init__(self):
        self.rows = {}
        self.settings = {}

    def virtual_allocations(self):
        return list(self.rows.values())

    def set_virtual_allocation(self, strategy_id, symbol, quantity, average_cost=0):
        self.rows[(strategy_id, symbol)] = {
            "strategy_id": strategy_id, "symbol": symbol,
            "quantity": quantity, "average_cost": average_cost,
        }

    def delete_virtual_allocation(self, strategy_id, symbol):
        self.rows.pop((strategy_id, symbol), None)

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class Broker:
    settings = OkxSettings(max_order_notional=500)

    def candles(self, symbol, bar="1H", limit=300):
        return swap_bars(-1)

    def ticker(self, symbol):
        return {"askPx": "50.1", "bidPx": "50", "last": "50", "ts": "1"}

    def swap_instruments(self):
        return [CryptoSwapInstrument(
            "BTC-USDT-SWAP", Decimal("0.1"), Decimal("1"), Decimal("1"),
            Decimal("0.01"), "BTC", "USDT",
        )]


class DemoPositionBroker(Broker):
    def candles(self, symbol, bar="1H", limit=300):
        return swap_bars(1)

    def positions(self, inst_type="SWAP"):
        return [{
            "instId": "BTC-USDT-SWAP", "pos": "-3", "avgPx": "100",
            "posSide": "net",
        }]

    def pending_orders(self, inst_type="SWAP"):
        return [{"instId": "BTC-USDT-SWAP", "state": "live"}]


def test_shadow_swap_records_short_as_negative_contracts(monkeypatch):
    monkeypatch.setenv("CRYPTO_SWAP_ENGINE", "LEGACY_VOLATILITY")
    store = Store()
    result = execute_crypto_swap_cycle(
        Broker(), "BTC-USDT-SWAP",
        CryptoSwapConfig(
            entry_enabled=True,
            higher_timeframe_enabled=False,
            short_min_volume_ratio=1.0,
            short_min_trend_strength=0,
            short_continuation_min_return=0,
        ), "SHADOW", store,
    )
    assert result.order["status"] == "SHADOW_FILLED"
    assert store.virtual_allocations()[0]["quantity"] < 0


def test_demo_swap_syncs_platform_position_and_blocks_duplicate_pending_order(monkeypatch):
    monkeypatch.setenv("CRYPTO_SWAP_ENGINE", "LEGACY_VOLATILITY")
    store = Store()
    result = execute_crypto_swap_cycle(
        DemoPositionBroker(), "BTC-USDT-SWAP",
        CryptoSwapConfig(min_volume_ratio=1.0), "OKX_DEMO", store,
    )
    assert store.virtual_allocations()[0]["quantity"] == -3
    assert result.decision.action == "HOLD"
    assert "待成交订单" in result.decision.reason


def test_swap_risk_allows_same_symbol_recovery_layer():
    store = Store()
    store.set_virtual_allocation("crypto_volatility_swap", "DOGE-USDT-SWAP", -2, 50)
    store.set_setting("crypto_swap_margin:DOGE-USDT-SWAP", "175")
    config = CryptoSwapRiskConfig(
        max_open_positions=1,
        max_total_margin=500,
        max_margin_per_position=500,
        max_leverage=2,
    )
    assert swap_entry_block_reason(store, "DOGE-USDT-SWAP", 175, 2, config) == ""
    assert "最大持仓数" in swap_entry_block_reason(
        store, "SOL-USDT-SWAP", 175, 2, config
    )


def test_swap_backtest_uses_contract_specific_semantics():
    result = run_crypto_swap_backtest(
        swap_bars(1, 180), 10_000,
        CryptoSwapConfig(
            entry_enabled=True,
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
            min_hold_hours=0,
        ),
    )
    assert not result.equity.empty
    assert {"total_return", "max_drawdown"} <= set(result.metrics)
    assert "pnl_ratio" in result.equity.columns


def test_managed_futures_backtest_records_recovery_layers():
    result = run_crypto_swap_managed_futures_backtest(
        "DOGE-USDT-SWAP",
        managed_short_bars(220),
        10_000,
        CryptoSwapConfig(entry_enabled=True, margin_budget=500, leverage=2, min_hold_hours=0),
        CryptoSwapManagedFuturesConfig(score_threshold=55),
        higher_timeframe_bars=managed_short_bars(220),
        daily_bars=managed_short_bars(220),
        benchmark_bars=swap_bars(1, 220),
    )
    assert not result.equity.empty
    assert "recovery_layer" in result.equity.columns
    assert {"total_return", "trade_count"} <= set(result.metrics)


def test_swap_backtest_records_realized_trade_quality():
    result = run_crypto_swap_backtest(
        swap_bars(1, 220), 10_000,
        CryptoSwapConfig(
            entry_enabled=True,
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
            min_hold_hours=0,
            max_hold_hours=8,
            continuation_min_return=0,
            long_min_volume_ratio=1.0,
            long_min_trend_strength=0,
        ),
    )
    closed = result.trades[pd.to_numeric(result.trades.get("pnl"), errors="coerce").notna()]
    assert not closed.empty
    assert {"pnl", "pnl_ratio", "mfe", "mae", "entry_type"} <= set(result.trades.columns)
    assert result.metrics["closed_trades"] == len(closed)
    quality_metrics = backtest_quality_metrics(result)
    assert quality_metrics["可验收"] is True
    assert quality_metrics["质量评分"] >= 0


def test_swap_parameter_grid_returns_ranked_rows():
    grid = run_crypto_swap_parameter_grid(
        swap_bars(1, 220), 10_000,
        CryptoSwapConfig(
            entry_enabled=True,
            min_volume_ratio=1.0, min_hold_hours=0, max_hold_hours=8,
            long_min_volume_ratio=1.0, long_min_trend_strength=0,
        ),
        long_volume_ratios=(0.7,),
        short_volume_ratios=(1.0,),
        confirmation_bars=(1,),
        continuation_returns=(0.0, .002),
    )
    assert len(grid) == 2
    assert {
        "相对持有", "盈利因子", "单笔期望", "验收状态", "质量评分", "主要问题",
    } <= set(grid.columns)
    assert grid["可验收"].dtype == bool


def test_swap_parameter_grid_pushes_no_trade_rows_below_valid_rows():
    grid = run_crypto_swap_parameter_grid(
        swap_bars(1, 220), 10_000,
        CryptoSwapConfig(
            entry_enabled=True,
            min_volume_ratio=1.0, min_hold_hours=0, max_hold_hours=8,
            long_min_volume_ratio=1.0, long_min_trend_strength=0,
        ),
        long_volume_ratios=(0.7,),
        short_volume_ratios=(1.0,),
        confirmation_bars=(1,),
        continuation_returns=(0.0, .20),
    )
    assert bool(grid.iloc[0]["可验收"]) is True
    assert bool(grid.iloc[-1]["可验收"]) is False
