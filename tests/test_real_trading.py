from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from futu_quant.config import Settings
from futu_quant.real_trading import (
    check_real_trade,
    confirmation_code,
    enable_session,
    session_status,
)
from futu_quant.store import TradingStore


def real_settings(monkeypatch) -> Settings:
    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456")
    monkeypatch.setenv("REAL_ALLOWED_STRATEGIES", "core_satellite,volatility_long_gamma")
    monkeypatch.setenv("REAL_MAX_ORDER_NOTIONAL", "500")
    monkeypatch.setenv("REAL_MAX_DAILY_NOTIONAL", "1000")
    monkeypatch.setenv("REAL_REQUIRE_MANUAL_CONFIRM", "true")
    return Settings.load(refresh=True)


def test_real_session_requires_correct_confirmation_code(tmp_path, monkeypatch):
    settings = real_settings(monkeypatch)
    store = TradingStore(tmp_path / "test.db")
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)

    ok, message = enable_session(store, settings, "wrong", now=now)

    assert not ok
    assert "确认码不正确" in message

    ok, message = enable_session(store, settings, confirmation_code(settings, now), now=now)
    active, reason, _ = session_status(store, settings, now=now)

    assert ok
    assert "已开启" in message
    assert active
    assert reason == "实盘确认会话有效"


def test_real_trade_gate_blocks_without_session(tmp_path, monkeypatch):
    settings = replace(
        real_settings(monkeypatch),
        real_require_manual_confirm=True,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
    )
    store = TradingStore(tmp_path / "test.db")

    decision = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 100, {}
    )

    assert not decision.allowed
    assert "确认" in decision.reason


def test_real_trade_gate_blocks_strategy_and_notional(tmp_path, monkeypatch):
    settings = replace(
        real_settings(monkeypatch),
        real_require_manual_confirm=True,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
        real_max_order_notional=500.0,
    )
    store = TradingStore(tmp_path / "test.db")
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    enable_session(store, settings, confirmation_code(settings, now), now=now)

    blocked_strategy = check_real_trade(
        store, settings, "SMA Cross", "US.SPY", "BUY", 1, 100, {}, now=now
    )
    blocked_notional = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 6, 100, {}, now=now
    )

    assert not blocked_strategy.allowed
    assert (
        "不在实盘白名单" in blocked_strategy.reason
        or "多账户隔离铁律" in blocked_strategy.reason
    )
    assert not blocked_notional.allowed
    assert "超过上限" in blocked_notional.reason


def test_real_trade_gate_allows_small_whitelisted_order(tmp_path, monkeypatch):
    settings = replace(
        real_settings(monkeypatch),
        real_require_manual_confirm=True,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
        real_max_order_notional=500.0,
    )
    store = TradingStore(tmp_path / "test.db")
    now = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    enable_session(store, settings, confirmation_code(settings, now), now=now)

    decision = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 100, {}, now=now
    )

    assert decision.allowed
    assert decision.details["notional"] == 100


def test_real_trade_gate_respects_manual_confirm_disabled(tmp_path, monkeypatch):
    settings = replace(
        real_settings(monkeypatch),
        real_require_manual_confirm=False,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
    )
    store = TradingStore(tmp_path / "test.db")

    decision = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 100, {}
    )

    assert decision.allowed
