from datetime import datetime, timedelta, timezone

from futu_quant.gamma_automation import decide_gamma_action
from futu_quant.volatility_optimizer import (
    volatility_cooldown_bypass_allowed,
    volatility_cooldown_hours,
)
from pathlib import Path


def decide(**overrides):
    event = datetime(2026, 8, 1, 20, tzinfo=timezone.utc)
    values = dict(
        now=event - timedelta(hours=2), event_time=event, quantity=0, stages_filled=0,
        max_stages=3, entry_hours=6, pnl_ratio=0.0, drawdown_from_peak=0.0,
        directional_confirmation=True, profit_target=0.5, stop_loss=0.4, trailing_stop=0.25,
    )
    values.update(overrides)
    return decide_gamma_action(**values)[0]


def test_initial_entry_inside_window():
    assert decide() == "BUY"


def test_initial_entry_waits_for_automatic_direction_confirmation():
    assert decide(directional_confirmation=False) == "HOLD"


def test_never_adds_to_loser():
    assert decide(quantity=1, stages_filled=1, pnl_ratio=-0.1, directional_confirmation=True) == "HOLD"


def test_post_event_profit_exit():
    event = datetime(2026, 8, 1, 20, tzinfo=timezone.utc)
    assert decide(now=event + timedelta(hours=1), event_time=event, quantity=1, pnl_ratio=0.6) == "SELL"


def test_volatility_cooldown_depends_on_exit_reason():
    assert volatility_cooldown_hours("达到止盈目标", 24) == 4
    assert volatility_cooldown_hours("盈利保护触发", 24) == 8
    assert volatility_cooldown_hours("实现波动开始收缩", 24) == 6
    assert volatility_cooldown_hours("触发硬止损", 24) == 24
    assert volatility_cooldown_hours("合约到期日临近收盘，强制退出以避免自动行权", 24) == 0


def test_volatility_cooldown_bypass_allows_strong_non_stop_signal():
    allowed, reason = volatility_cooldown_bypass_allowed(
        "盈利保护触发",
        {
            "active": True,
            "zscore_20d": 3.2,
            "intraday_return": 0.02,
            "atr_ratio": 1.4,
            "volume_ratio": 1.8,
        },
    )

    assert allowed
    assert "提前解除冷却" in reason


def test_volatility_cooldown_bypass_keeps_stop_loss_hard_by_default():
    allowed, reason = volatility_cooldown_bypass_allowed(
        "触发硬止损",
        {
            "active": True,
            "zscore_20d": 5.0,
            "intraday_return": 0.08,
            "atr_ratio": 4.0,
            "volume_ratio": 3.0,
        },
    )

    assert not allowed
    assert "硬止损" in reason


def test_volatility_cooldown_bypass_requires_volume_and_strong_trigger():
    allowed, reason = volatility_cooldown_bypass_allowed(
        "达到止盈目标",
        {
            "active": True,
            "zscore_20d": 2.1,
            "intraday_return": 0.02,
            "atr_ratio": 1.4,
            "volume_ratio": 1.2,
        },
    )

    assert not allowed
    assert "量比" in reason


def test_shared_campaign_runner_dispatches_to_independent_volatility_decider():
    source = Path("scripts/run_gamma_strategy.py").read_text()

    assert "decide_volatility_action(" in source
    assert "if trigger_mode == \"VOLATILITY\":" in source


def test_option_market_data_failures_are_tracked_instead_of_silent_prints():
    source = Path("scripts/run_gamma_strategy.py").read_text()

    assert "refreshed_trade_quotes(" in source
    assert "OPTION_MARKET_DATA_WAITING" in source
    assert "WAITING_EXIT_LIQUIDITY" in source
    assert "OPTION_EXIT_WAITING_FOR_BID" in source
