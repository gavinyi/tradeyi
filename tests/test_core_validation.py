from datetime import datetime, timedelta, timezone

from futu_quant.core_validation import (
    disable_core_daily_validation,
    enable_core_daily_validation,
    load_core_daily_validation,
    record_core_validation_session,
    resolve_core_rebalance_settings,
)
from futu_quant.store import TradingStore


class FormalSettings:
    core_satellite_rebalance_frequency = "weekly"
    core_satellite_weight_tolerance = 0.02
    core_satellite_min_trade_usd = 1_000.0


def test_daily_validation_overrides_execution_without_changing_formal_settings(tmp_path):
    store = TradingStore(tmp_path / "validation.db")
    now = datetime(2026, 7, 20, 4, tzinfo=timezone.utc)
    mode = enable_core_daily_validation(
        store, target_sessions=5, weight_tolerance=0.0075,
        min_trade_usd=300, now=now,
    )

    effective = resolve_core_rebalance_settings(FormalSettings(), store, now=now)

    assert mode.active is True
    assert effective.frequency == "daily"
    assert effective.weight_tolerance == 0.0075
    assert effective.min_trade_usd == 300
    assert effective.validation_mode is True
    assert FormalSettings.core_satellite_rebalance_frequency == "weekly"
    assert FormalSettings.core_satellite_weight_tolerance == 0.02
    assert FormalSettings.core_satellite_min_trade_usd == 1_000.0


def test_validation_counts_distinct_sessions_and_stops_automatically(tmp_path):
    store = TradingStore(tmp_path / "validation.db")
    now = datetime(2026, 7, 20, 4, tzinfo=timezone.utc)
    enable_core_daily_validation(store, target_sessions=2, now=now)

    record_core_validation_session(store, "2026-07-20", order_count=0, now=now)
    record_core_validation_session(store, "2026-07-20", order_count=3, now=now)
    halfway = load_core_daily_validation(store, now=now)
    assert halfway.completed_sessions == ("2026-07-20",)
    assert halfway.total_orders == 3
    assert halfway.remaining_sessions == 1

    record_core_validation_session(store, "2026-07-21", order_count=1, now=now)
    complete = load_core_daily_validation(store, now=now)
    assert complete.active is False
    assert complete.enabled is True
    assert complete.remaining_sessions == 0
    assert "已完成" in complete.reason
    assert resolve_core_rebalance_settings(FormalSettings(), store, now=now).frequency == "weekly"


def test_validation_expires_and_can_be_disabled(tmp_path):
    store = TradingStore(tmp_path / "validation.db")
    now = datetime(2026, 7, 20, 4, tzinfo=timezone.utc)
    enable_core_daily_validation(store, target_sessions=10, now=now, max_calendar_days=14)

    expired = load_core_daily_validation(store, now=now + timedelta(days=15))
    assert expired.active is False
    assert "到期" in expired.reason

    disabled = disable_core_daily_validation(store, reason="测试停止", now=now)
    assert disabled.enabled is False
    assert disabled.active is False
    assert disabled.reason == "测试停止"


def test_validation_inputs_are_bounded(tmp_path):
    store = TradingStore(tmp_path / "validation.db")
    for kwargs in (
        {"target_sessions": 0},
        {"weight_tolerance": 0.21},
        {"min_trade_usd": -1},
    ):
        try:
            enable_core_daily_validation(store, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid validation settings: {kwargs}")
