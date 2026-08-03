from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from futu_quant.crypto_backtest import (
    backtest_quality_metrics,
    closed_trade_frame,
    run_crypto_volatility_backtest,
    run_crypto_volatility_parameter_grid,
    trade_quality_summary,
)
from futu_quant.crypto_dashboard import (
    format_crypto_shadow_holdings,
    format_okx_balance,
    format_okx_orders,
)
from futu_quant.crypto_execution import execute_crypto_cycle
from futu_quant.crypto_protections import (
    CryptoProtectionConfig,
    entry_protection_reason,
    reconcile_okx_demo_fills,
)
from futu_quant.crypto_research import (
    crypto_lookahead_audit,
    crypto_monte_carlo,
    crypto_walk_forward,
)
from futu_quant.crypto_review import build_crypto_strategy_review
from futu_quant.crypto_service_control import code_restart_required
from futu_quant.crypto_radar import (
    managed_exit_decision,
    rank_spot_candidate,
    scan_crypto_radar,
)
from futu_quant.crypto_watchlist import normalize_crypto_symbols
from futu_quant.crypto_models import CryptoInstrument, CryptoOrderRequest
from futu_quant.crypto_volatility import (
    CryptoVolatilityConfig,
    CryptoVolatilityDecision,
    crypto_volatility_config_from_env,
    decide_crypto_volatility,
)
from futu_quant.okx_broker import OkxApiError, OkxDemoBroker
from futu_quant.okx_config import OkxSettings, normalize_okx_base_url


class FakeTransport:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout}
        )
        return self.responses.pop(0)


class FailingOnceTransport:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, headers, body, timeout):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("The handshake operation timed out")
        return {"code": "0", "msg": "", "data": [{"last": "100"}]}


def test_okx_settings_are_demo_only(monkeypatch):
    monkeypatch.setenv("OKX_TRADING_MODE", "LIVE")
    with pytest.raises(ValueError, match="只支持欧易模拟盘"):
        OkxSettings.from_env()


def test_okx_global_default_uses_official_openapi_domain():
    assert OkxSettings().base_url == "https://openapi.okx.com"


def test_legacy_okx_website_domain_is_migrated():
    assert normalize_okx_base_url("https://www.okx.com") == "https://openapi.okx.com"
    assert normalize_okx_base_url("https://openapi.okx.com/") == "https://openapi.okx.com"


def test_okx_balance_is_flattened_with_chinese_columns():
    summary, frame = format_okx_balance(
        [{
            "totalEq": "10000.5",
            "availEq": "9000",
            "upl": "12.3",
            "details": [{
                "ccy": "USDT", "eq": "8000", "availBal": "7500",
                "frozenBal": "500", "eqUsd": "8000", "upl": "2.3",
            }],
        }]
    )
    assert summary["账户总权益"] == 10000.5
    assert frame.iloc[0]["币种"] == "USDT"
    assert frame.iloc[0]["可用余额"] == 7500.0


def test_okx_orders_are_translated_and_time_is_china_timezone():
    frame = format_okx_orders([{
        "instId": "BTC-USDT", "side": "buy", "ordType": "limit",
        "state": "live", "px": "100", "sz": "2", "accFillSz": "0.5",
        "avgPx": "99", "ordId": "1", "cTime": "1785000000000",
    }])
    assert frame.iloc[0]["方向"] == "买入"
    assert frame.iloc[0]["状态"] == "待成交"
    assert frame.iloc[0]["订单类型"] == "限价单"
    assert frame.iloc[0]["创建时间"].endswith("+08:00")


def test_crypto_shadow_holdings_include_live_price_and_unrealized_pnl():
    frame = format_crypto_shadow_holdings(
        [{
            "symbol": "DOGE-USDT", "quantity": 1000,
            "average_cost": 0.07, "updated_at": "2026-07-26T00:00:00+00:00",
        }],
        {"DOGE-USDT": {"last": "0.077", "ts": "1785000000000"}},
        {"DOGE-USDT": {
            "peak_price": "0.08", "opened_at": "2026-07-25T00:00:00+00:00",
        }},
        now=pd.Timestamp("2026-07-26T00:00:00Z").to_pydatetime(),
    )
    assert frame.iloc[0]["当前市值"] == pytest.approx(77)
    assert frame.iloc[0]["未实现盈亏"] == pytest.approx(7)
    assert frame.iloc[0]["收益率"] == pytest.approx(0.10)
    assert frame.iloc[0]["持有小时"] == pytest.approx(24)


def test_crypto_volatility_config_reads_page_settings(monkeypatch):
    monkeypatch.setenv("CRYPTO_VOL_FAST_WINDOW", "18")
    monkeypatch.setenv("CRYPTO_VOL_EXPANSION", "1.6")
    monkeypatch.setenv("CRYPTO_VOL_HARD_STOP", "0.06")
    config = crypto_volatility_config_from_env()
    assert config.fast_window == 18
    assert config.volatility_expansion == 1.6
    assert config.hard_stop_loss == 0.06


def test_private_request_always_has_demo_header_and_signature():
    transport = FakeTransport([{"code": "0", "msg": "", "data": [{"totalEq": "10000"}]}])
    settings = OkxSettings(
        api_key="key", secret_key="secret", passphrase="pass", enable_demo_orders=False
    )
    broker = OkxDemoBroker(settings, transport=transport, clock=lambda: "2026-07-26T00:00:00.000Z")
    assert broker.account_balance()[0]["totalEq"] == "10000"
    headers = transport.calls[0]["headers"]
    assert headers["x-simulated-trading"] == "1"
    assert headers["OK-ACCESS-KEY"] == "key"
    assert headers["OK-ACCESS-SIGN"]


def test_okx_request_retries_transient_handshake_timeout():
    transport = FailingOnceTransport()
    broker = OkxDemoBroker(
        OkxSettings(retry_attempts=1, retry_backoff_seconds=0),
        transport=transport,
    )
    assert broker.ticker("BTC-USDT")["last"] == "100"
    assert transport.calls == 2


def test_order_is_blocked_until_explicit_demo_switch():
    settings = OkxSettings(
        api_key="key", secret_key="secret", passphrase="pass", enable_demo_orders=False
    )
    broker = OkxDemoBroker(settings, transport=FakeTransport([]))
    request = CryptoOrderRequest(
        inst_id="BTC-USDT", side="buy", order_type="limit",
        size=Decimal("0.001"), price=Decimal("50000"), client_order_id="test-1",
    )
    with pytest.raises(OkxApiError, match="模拟下单开关未启用"):
        broker.place_spot_order(request)


def test_candles_use_only_confirmed_rows_and_are_chronological():
    response = {
        "code": "0",
        "msg": "",
        "data": [
            ["3000", "3", "4", "2", "3.5", "30", "0", "0", "0"],
            ["2000", "2", "3", "1", "2.5", "20", "0", "0", "1"],
            ["1000", "1", "2", "0.5", "1.5", "10", "0", "0", "1"],
        ],
    }
    broker = OkxDemoBroker(OkxSettings(), transport=FakeTransport([response]))
    frame = broker.candles("BTC-USDT", limit=100)
    assert frame["timestamp"].tolist() == [1000, 2000]
    assert frame["close"].tolist() == [1.5, 2.5]


def test_historical_candles_paginates_and_deduplicates():
    first = {
        "code": "0",
        "msg": "",
        "data": [
            ["3000", "3", "4", "2", "3.5", "30", "0", "0", "1"],
            ["2000", "2", "3", "1", "2.5", "20", "0", "0", "1"],
        ],
    }
    second = {
        "code": "0",
        "msg": "",
        "data": [
            ["2000", "2", "3", "1", "2.5", "20", "0", "0", "1"],
            ["1000", "1", "2", "0.5", "1.5", "10", "0", "0", "1"],
        ],
    }
    broker = OkxDemoBroker(OkxSettings(), transport=FakeTransport([first, second]))
    frame = broker.historical_candles("BTC-USDT", limit=3)
    assert frame["timestamp"].tolist() == [1000, 2000, 3000]
    assert len(frame) == 3


def test_instrument_normalizes_price_and_size_down():
    instrument = CryptoInstrument(
        inst_id="BTC-USDT", base_currency="BTC", quote_currency="USDT",
        tick_size=Decimal("0.1"), lot_size=Decimal("0.00001"), min_size=Decimal("0.0001"),
    )
    assert instrument.normalize_price(Decimal("123.456")) == Decimal("123.4")
    assert instrument.normalize_size(Decimal("0.001239")) == Decimal("0.00123")


def test_okx_instrument_catalog_skips_rows_with_empty_precision_fields():
    response = {
        "code": "0", "msg": "", "data": [
            {
                "instId": "BROKEN-USDT", "baseCcy": "BROKEN", "quoteCcy": "USDT",
                "tickSz": "", "lotSz": "", "minSz": "", "state": "preopen",
            },
            {
                "instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT",
                "tickSz": "0.00001", "lotSz": "0.000001",
                "minSz": "1", "state": "live",
            },
        ],
    }
    broker = OkxDemoBroker(OkxSettings(), transport=FakeTransport([response]))
    instruments = broker.instruments()
    assert [item.inst_id for item in instruments] == ["DOGE-USDT"]


def _bars(count: int = 140, breakout: bool = True) -> pd.DataFrame:
    rows = []
    price = 100.0
    for index in range(count):
        change = 0.001 if index < count - 12 else (0.018 if breakout else -0.001)
        price *= 1 + change
        rows.append(
            {
                "timestamp": index * 3_600_000,
                "open": price / (1 + change),
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100 if index < count - 1 else 300,
                "confirm": True,
            }
        )
    return pd.DataFrame(rows)


def _pullback_bars(count: int = 180) -> pd.DataFrame:
    rows = []
    price = 100.0
    for index in range(count):
        if index < count - 6:
            change = 0.002
        elif index < count - 1:
            change = -0.003
        else:
            change = 0.006
        price *= 1 + change
        rows.append({
            "timestamp": index * 3_600_000,
            "open": price / (1 + change),
            "high": price * 1.01,
            "low": price * .99,
            "close": price,
            "volume": 120 if index < count - 1 else 220,
            "confirm": True,
        })
    return pd.DataFrame(rows)


def test_crypto_volatility_generates_long_entry_with_explainable_checks():
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0"), average_price=None,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
        ),
    )
    assert decision.action == "BUY"
    assert decision.target_notional > 0
    assert decision.checks
    assert all({"name", "current", "target", "passed"} <= set(item) for item in decision.checks)
    assert "regime" in decision.metrics


def test_crypto_volatility_can_use_trend_pullback_entry():
    decision = decide_crypto_volatility(
        _pullback_bars(), position_size=Decimal("0"), average_price=None,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            pullback_enabled=True,
            pullback_depth=.08,
            confirmation_bars=2,
            higher_timeframe_enabled=False,
        ),
    )
    assert decision.action == "BUY"
    assert decision.metrics["pullback_ok"] is True


def test_crypto_pullback_requires_strong_trend_and_bounce_confirmation():
    decision = decide_crypto_volatility(
        _pullback_bars(), position_size=Decimal("0"), average_price=None,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            pullback_enabled=True,
            trend_participation_enabled=False,
            pullback_depth=.08,
            pullback_min_trend_return=.50,
            pullback_min_last_return=.02,
        ),
    )
    assert decision.action == "HOLD"
    assert decision.metrics["pullback_ok"] is False


def test_crypto_trend_participation_enters_with_small_size():
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0"), average_price=None,
        config=CryptoVolatilityConfig(
            min_volume_ratio=99.0,
            pullback_enabled=False,
            trend_participation_enabled=True,
            trend_participation_min_24h_return=.01,
            trend_participation_min_72h_return=.02,
            trend_participation_max_rsi=95,
            trend_participation_position_scale=.25,
            higher_timeframe_enabled=False,
        ),
    )
    assert decision.action == "BUY"
    assert decision.metrics["trend_participation_ok"] is True
    assert decision.metrics["entry_source"] == "趋势参与"
    assert 0 < decision.target_notional < 500


def test_crypto_backtest_labels_trend_participation_entries():
    result = run_crypto_volatility_backtest(
        _bars(180), initial_cash=10_000,
        config=CryptoVolatilityConfig(
            min_volume_ratio=99.0,
            pullback_enabled=False,
            trend_participation_enabled=True,
            trend_participation_min_24h_return=.01,
            trend_participation_min_72h_return=.02,
            trend_participation_max_rsi=95,
            trend_participation_position_scale=.25,
            max_hold_hours=8,
            higher_timeframe_enabled=False,
        ),
    )
    assert "趋势参与" in set(result.trades.get("entry_type", []))


def test_crypto_higher_timeframe_confirmation_blocks_weak_large_trend():
    weak_higher = _bars(100).copy()
    weak_higher["close"] = list(reversed([100 + index for index in range(len(weak_higher))]))
    weak_higher["open"] = weak_higher["close"]
    weak_higher["high"] = weak_higher["close"] * 1.01
    weak_higher["low"] = weak_higher["close"] * .99
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0"), average_price=None,
        higher_timeframe_bars=weak_higher,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=True,
            higher_timeframe_min_bars=80,
        ),
    )
    assert decision.action == "HOLD"
    assert "高周期确认失败" in decision.reason
    assert decision.metrics["higher_timeframe_confirmed"] is False


def test_crypto_tactical_profit_protection_exits_small_mfe_giveback():
    bars = _bars(140, breakout=False).copy()
    bars.loc[bars.index[-1], "close"] = 100.8
    bars.loc[bars.index[-1], "open"] = 100.8
    bars.loc[bars.index[-1], "high"] = 100.9
    bars.loc[bars.index[-1], "low"] = 100.7
    decision = decide_crypto_volatility(
        bars, position_size=Decimal("1"), average_price=Decimal("100"),
        peak_price=Decimal("102"),
        config=CryptoVolatilityConfig(
            profit_protection_start=.10,
            tactical_profit_protection_start=.015,
            tactical_profit_giveback=.50,
            tactical_minimum_locked_profit=.002,
        ),
    )
    assert decision.action == "SELL_ALL"
    assert "早期浮盈保护" in decision.reason


def test_crypto_weak_momentum_time_exit_closes_stale_loser():
    bars = _bars(140, breakout=False).copy()
    bars.loc[bars.index[-1], "close"] = 99.5
    bars.loc[bars.index[-1], "open"] = 99.5
    bars.loc[bars.index[-1], "high"] = 99.8
    bars.loc[bars.index[-1], "low"] = 99.2
    decision = decide_crypto_volatility(
        bars,
        position_size=Decimal("1"),
        average_price=Decimal("100"),
        peak_price=Decimal("100.4"),
        held_hours=13,
        config=CryptoVolatilityConfig(
            weak_momentum_exit_enabled=True,
            weak_momentum_exit_hours=12,
            weak_momentum_exit_max_pnl=0,
            weak_momentum_exit_min_peak_pnl=.008,
        ),
    )
    assert decision.action == "SELL_ALL"
    assert "弱动量时间退出" in decision.reason


def test_crypto_watchlist_normalizes_and_deduplicates():
    assert normalize_crypto_symbols(
        ["btc/usdt", "BTC-USDT", " eth-usdt "]
    ) == ("BTC-USDT", "ETH-USDT")


def test_crypto_radar_adds_only_new_active_symbols():
    added: list[str] = []
    rows = scan_crypto_radar(
        FakeCryptoBroker(),
        candidates=["BTC-USDT", "ETH-USDT"],
        watchlist=["BTC-USDT"],
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
        ),
        auto_add=True,
        add_symbol=added.append,
    )
    assert [row["symbol"] for row in rows] == ["ETH-USDT"]
    assert rows[0]["action"] == "BUY"
    assert rows[0]["added"] is True
    assert added == ["ETH-USDT"]


def test_crypto_radar_full_scan_scores_pool_before_auto_add(monkeypatch):
    monkeypatch.setattr(
        "futu_quant.crypto_radar.decide_crypto_volatility",
        lambda *args, **kwargs: CryptoVolatilityDecision(
            "BUY", "信号通过", signal_strength="强",
            metrics={
                "volatility_expansion": 2,
                "zscore": 3,
                "volume_ratio": 2,
                "breakout": 1,
            },
        ),
    )
    added: list[str] = []
    rows = scan_crypto_radar(
        FakeCryptoBroker(),
        candidates=["ETH-USDT", "SOL-USDT"],
        watchlist=["BTC-USDT"],
        config=CryptoVolatilityConfig(higher_timeframe_enabled=False),
        max_symbols=1,
        full_scan=True,
        auto_add=True,
        add_symbol=added.append,
        min_auto_add_score=60,
    )
    assert [row["symbol"] for row in rows] == ["ETH-USDT", "SOL-USDT"]
    assert len(added) == 1


def test_spot_radar_candidate_ranking_includes_score_and_spread_gate():
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0"), average_price=None,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
        ),
    )
    row = rank_spot_candidate(
        decision,
        {"bidPx": "119.9", "askPx": "120.0", "last": "120"},
        max_spread_ratio=0.003,
    )
    assert row["eligible"] is True
    assert row["score"] > 0
    assert row["spread_ratio"] < 0.003


def test_radar_managed_symbol_exits_only_after_inactive_threshold():
    count, remove, _ = managed_exit_decision(
        has_position=False, has_pending_order=False, action="HOLD",
        inactive_cycles=2, exit_after_cycles=4,
    )
    assert count == 3
    assert remove is False
    count, remove, reason = managed_exit_decision(
        has_position=False, has_pending_order=False, action="HOLD",
        inactive_cycles=count, exit_after_cycles=4,
    )
    assert remove is True
    assert "自动退出" in reason


def test_radar_managed_symbol_never_exits_with_position_or_pending_order():
    assert managed_exit_decision(
        has_position=True, has_pending_order=False, action="HOLD",
        inactive_cycles=99, exit_after_cycles=4,
    )[1] is False
    assert managed_exit_decision(
        has_position=False, has_pending_order=True, action="HOLD",
        inactive_cycles=99, exit_after_cycles=4,
    )[1] is False


def test_crypto_review_separates_signals_executions_and_radar_additions():
    class ReviewStore:
        def decisions(self, limit=1000):
            return [
                {
                    "strategy_id": "crypto_volatility_spot",
                    "symbol": "BTC-USDT", "decision": "BUY", "reason": "突破成立",
                    "payload_json": (
                        '{"mode":"SHADOW","order":{"status":"SHADOW_FILLED",'
                        '"side":"buy","quantity":0.01,"price":100}}'
                    ),
                    "created_at": "2026-07-26T02:00:00+00:00",
                },
                {
                    "strategy_id": "crypto_volatility_radar",
                    "symbol": "ETH-USDT", "decision": "BUY", "reason": "雷达触发",
                    "payload_json": '{"added_to_watchlist":true}',
                    "created_at": "2026-07-26T03:00:00+00:00",
                },
            ]

        def virtual_allocations(self):
            return [{
                "strategy_id": "crypto_volatility_spot", "symbol": "BTC-USDT",
                "quantity": 0.01, "average_cost": 100,
                "updated_at": "2026-07-26T02:00:00+00:00",
            }]

    review = build_crypto_strategy_review(ReviewStore(), "2026-07-26")
    assert review.metrics["交易信号"] == 1
    assert review.metrics["执行记录"] == 1
    assert review.metrics["雷达自动加入"] == 1
    assert review.executions.iloc[0]["执行状态"] == "Shadow 已成交"


def test_crypto_service_detects_code_newer_than_running_process(tmp_path):
    code = tmp_path / "crypto_radar.py"
    code.write_text("old")
    started_at = code.stat().st_mtime + 10
    assert code_restart_required(started_at, (code,)) is False
    assert code_restart_required(code.stat().st_mtime - 1, (code,)) is True


def test_crypto_volatility_hard_stop_has_priority():
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0.01"), average_price=Decimal("200"),
        config=CryptoVolatilityConfig(hard_stop_loss=0.08),
    )
    assert decision.action == "SELL_ALL"
    assert "硬止损" in decision.reason


def test_crypto_volatility_protects_profit_after_peak():
    decision = decide_crypto_volatility(
        _bars(), position_size=Decimal("0.01"), average_price=Decimal("100"),
        peak_price=Decimal("180"),
        config=CryptoVolatilityConfig(
            profit_protection_start=0.10,
            profit_giveback=0.30,
            minimum_locked_profit=0.04,
        ),
    )
    assert decision.action == "SELL_ALL"
    assert "盈利保护" in decision.reason


class FakeAllocationStore:
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


class FakeCryptoBroker:
    settings = OkxSettings(max_order_notional=500)

    def candles(self, symbol, bar="1H", limit=300):
        return _bars()

    def ticker(self, symbol):
        return {"askPx": "120", "bidPx": "119.9", "last": "120", "ts": "1000"}

    def instruments(self):
        return [
            CryptoInstrument(
                "BTC-USDT", "BTC", "USDT", Decimal("0.1"),
                Decimal("0.00001"), Decimal("0.0001"),
            )
        ]

    def fills(self):
        return [
            {
                "clOrdId": "cqabc", "instId": "BTC-USDT", "side": "buy",
                "fillSz": "0.01", "fillPx": "100",
            },
            {
                "clOrdId": "cqdef", "instId": "BTC-USDT", "side": "sell",
                "fillSz": "0.004", "fillPx": "120",
            },
        ]


def test_shadow_cycle_records_position_and_prevents_duplicate_entry():
    store = FakeAllocationStore()
    first = execute_crypto_cycle(
        FakeCryptoBroker(), "BTC-USDT",
        CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
        ), "SHADOW", store,
    )
    second = execute_crypto_cycle(
        FakeCryptoBroker(), "BTC-USDT",
        CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            higher_timeframe_enabled=False,
        ), "SHADOW", store,
    )
    assert first.order["status"] == "SHADOW_FILLED"
    assert store.virtual_allocations()[0]["quantity"] > 0
    assert second.decision.action != "BUY"


def test_demo_fills_reconcile_partial_position():
    store = FakeAllocationStore()
    rows = reconcile_okx_demo_fills(FakeCryptoBroker(), store)
    assert rows[0]["quantity"] == pytest.approx(0.006)
    assert store.virtual_allocations()[0]["quantity"] == pytest.approx(0.006)


def test_portfolio_protection_limits_open_positions():
    store = FakeAllocationStore()
    store.set_virtual_allocation("crypto_volatility_spot", "BTC-USDT", 1, 100)
    reason = entry_protection_reason(
        store, "ETH-USDT", 100,
        CryptoProtectionConfig(max_open_positions=1),
    )
    assert "最大同时持仓数" in reason


def test_crypto_backtest_executes_on_next_open_without_lookahead():
    result = run_crypto_volatility_backtest(
        _bars(180), initial_cash=10_000, config=CryptoVolatilityConfig(min_volume_ratio=1.0),
        fee_rate=0.001, slippage_bps=5,
    )
    assert not result.equity.empty
    assert {"total_return", "max_drawdown"} <= set(result.metrics)
    if not result.trades.empty:
        first = result.trades.iloc[0]
        signal_row = result.bars[result.bars["timestamp"] == first["signal_timestamp"]].iloc[0]
        fill_row = result.bars[result.bars["timestamp"] == first["timestamp"]].iloc[0]
        assert fill_row["timestamp"] > signal_row["timestamp"]
    assert crypto_lookahead_audit(result)["passed"] is True
    assert crypto_monte_carlo(result.equity, simulations=20)["模拟次数"] == 20


def test_crypto_backtest_records_realized_trade_quality():
    result = run_crypto_volatility_backtest(
        _bars(220), initial_cash=10_000,
        config=CryptoVolatilityConfig(
            min_volume_ratio=1.0, max_hold_hours=8,
        ),
        fee_rate=0.001, slippage_bps=5,
    )
    closed = result.trades[pd.to_numeric(result.trades.get("pnl"), errors="coerce").notna()]
    assert not closed.empty
    assert {"pnl", "pnl_ratio", "mfe", "mae", "entry_type"} <= set(result.trades.columns)
    assert result.metrics["closed_trades"] == len(closed)
    assert result.metrics["profit_factor"] >= 0
    assert len(closed_trade_frame(result.trades)) == len(closed)
    quality_metrics = backtest_quality_metrics(result)
    assert quality_metrics["可验收"] is True
    assert quality_metrics["质量评分"] >= 0
    quality = trade_quality_summary(result.trades)
    assert not quality.empty
    assert "入场类型" in quality.columns


def test_crypto_parameter_grid_returns_ranked_rows():
    grid = run_crypto_volatility_parameter_grid(
        _pullback_bars(220), 10_000,
        CryptoVolatilityConfig(
            min_volume_ratio=1.0,
            pullback_enabled=True,
            confirmation_bars=2,
        ),
        pullback_depths=(0.04,),
        rsi_limits=(65.0,),
        bollinger_limits=(0.8, 0.9),
    )
    assert len(grid) == 2
    assert {
        "相对持有", "盈利因子", "单笔期望", "验收状态", "质量评分", "主要问题",
    } <= set(grid.columns)
    assert grid["可验收"].dtype == bool


def test_crypto_parameter_grid_marks_no_trade_rows_as_insufficient():
    grid = run_crypto_volatility_parameter_grid(
        _bars(220, breakout=False), 10_000,
        CryptoVolatilityConfig(
            min_volume_ratio=99.0,
            pullback_enabled=False,
            trend_participation_enabled=False,
        ),
        pullback_depths=(0.04,),
        rsi_limits=(65.0,),
        bollinger_limits=(0.8,),
    )
    assert bool(grid.iloc[0]["可验收"]) is False
    assert grid.iloc[0]["验收状态"] in {"无交易", "样本不足"}


def test_crypto_walk_forward_returns_out_of_sample_windows():
    validation = crypto_walk_forward(
        _bars(300), 10_000, CryptoVolatilityConfig(min_volume_ratio=1.0),
        train_bars=144, test_bars=72,
    )
    assert len(validation["windows"]) >= 1
    assert isinstance(validation["passed"], bool)
