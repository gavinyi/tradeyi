import pandas as pd

from futu_quant.option_signal import configured_volatility_direction_allowed
from futu_quant.volatility_optimizer import (
    decide_volatility_action, rank_volatility_contracts, volatility_exit_reason,
    sync_volatility_position_cycle, volatility_regime,
)
from futu_quant.volatility_roll import should_replace_campaign
from datetime import datetime, timedelta, timezone


def test_contract_rank_rejects_expensive_deep_itm_option():
    rows = pd.DataFrame([{
        "code": "X", "strike_time": "2026-07-23", "option_delta": .95,
        "spread_pct": 3, "option_open_interest": 1000, "volume": 20,
        "option_implied_volatility": 60, "option_gamma": .01, "option_theta": -.1,
        "model_expected_return": .1,
    }])
    ranked = rank_volatility_contracts(rows, 100, .30, as_of=pd.Timestamp("2026-07-16").date())
    assert not bool(ranked.iloc[0].volatility_eligible)
    assert "Delta" in ranked.iloc[0].volatility_filter_reason


def test_empty_contract_rank_keeps_downstream_schema():
    ranked = rank_volatility_contracts(pd.DataFrame(), 100, .30)

    assert ranked.empty
    assert "volatility_eligible" in ranked
    assert "volatility_filter_reason" in ranked


def _adaptive_candidate(**overrides):
    row = {
        "code": "US.TEST260724C100000", "strike_time": "2026-07-24",
        "option_delta": .50, "spread_pct": 3,
        "option_open_interest": 2000, "volume": 100,
        "option_implied_volatility": .57, "option_gamma": .02,
        "option_theta": -.05, "model_expected_return": .08,
        "ask_price": 5, "option_contract_multiplier": 100,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_iv_rv_gate_is_only_relaxed_for_confirmed_strong_signal():
    rows = _adaptive_candidate()

    normal = rank_volatility_contracts(
        rows, 100, .30, as_of=pd.Timestamp("2026-07-20").date(),
        max_premium_per_stage=1000,
    )
    strong = rank_volatility_contracts(
        rows, 100, .30, as_of=pd.Timestamp("2026-07-20").date(),
        signal_active=True, strong_signal=True, max_premium_per_stage=1000,
    )

    assert normal.iloc[0].iv_rv_limit == 1.5
    assert not bool(normal.iloc[0].volatility_eligible)
    assert strong.iloc[0].iv_rv_limit == 2.0
    assert bool(strong.iloc[0].volatility_eligible)


def test_relaxed_iv_gate_still_requires_positive_return_and_premium_budget():
    negative = rank_volatility_contracts(
        _adaptive_candidate(model_expected_return=-.01), 100, .30,
        signal_active=True, strong_signal=True, max_premium_per_stage=1000,
        as_of=pd.Timestamp("2026-07-20").date(),
    )
    expensive = rank_volatility_contracts(
        _adaptive_candidate(ask_price=12), 100, .30,
        signal_active=True, strong_signal=True, max_premium_per_stage=1000,
        as_of=pd.Timestamp("2026-07-20").date(),
    )

    assert not bool(negative.iloc[0].volatility_eligible)
    assert "预期收益" in negative.iloc[0].volatility_filter_reason
    assert not bool(expensive.iloc[0].volatility_eligible)
    assert "权利金" in expensive.iloc[0].volatility_filter_reason


def test_best_expression_replaces_stale_lower_quality_contract():
    replace, reason = should_replace_campaign(
        {
            "code": "US.AAPL260724P335000",
            "option_type": "PUT",
            "volatility_quality_score": 40,
            "volatility_filter_reason": "全部条件通过",
        },
        {
            "code": "US.AAPL260724P330000",
            "option_type": "PUT",
            "_roll_diagnostics": {
                "top_candidates": [{
                    "code": "US.AAPL260724P330000",
                    "eligible": True,
                    "score": 55,
                }],
            },
        },
    )

    assert replace is True
    assert "明显高于" in reason


def test_best_expression_keeps_current_contract_when_unchanged():
    replace, reason = should_replace_campaign(
        {"code": "US.AAPL260724P335000", "option_type": "PUT"},
        {"code": "US.AAPL260724P335000", "option_type": "PUT"},
    )

    assert replace is False
    assert "仍是" in reason


def test_volatility_exit_on_iv_crush():
    assert volatility_exit_reason({}, .39, .50, 1, 1) is not None


def test_volatility_profit_exit_is_active_immediately_after_fill():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)

    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(minutes=5),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.55,
        peak_pnl_ratio=.55,
        drawdown_from_peak=0,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
    )

    assert action == "SELL"
    assert reason == "达到波动率策略止盈目标"


def test_volatility_timeout_uses_position_open_time_and_not_earnings_copy():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)

    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(hours=25),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.05,
        peak_pnl_ratio=.10,
        drawdown_from_peak=.05,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
    )

    assert action == "SELL"
    assert reason == "波动率仓位已持有24小时，触发最长持有退出"
    assert "财报" not in reason


def test_volatility_trailing_exit_requires_position_to_have_been_profitable():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    common = dict(
        now=now,
        opened_at=now - timedelta(hours=1),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=-.10,
        drawdown_from_peak=.30,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
    )

    assert decide_volatility_action(peak_pnl_ratio=0, **common)[0] == "HOLD"
    assert decide_volatility_action(peak_pnl_ratio=.20, **common)[0] == "SELL"


def test_volatility_decay_thresholds_are_strategy_configurable():
    assert volatility_exit_reason(
        {}, current_iv=.44, entry_iv=.50, current_efficiency=1, entry_efficiency=1,
        iv_decline_ratio=.15,
    ) is None
    assert "隐含波动率" in volatility_exit_reason(
        {}, current_iv=.44, entry_iv=.50, current_efficiency=1, entry_efficiency=1,
        iv_decline_ratio=.10,
    )


def test_new_volatility_position_cycle_resets_peak_and_entry_baselines():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    state = {
        "previous_position_quantity": 0,
        "peak": 10.0,
        "entry_iv_actual": .90,
        "entry_gamma_theta_efficiency_actual": 5.0,
        "contraction_confirmations": 2,
    }

    sync_volatility_position_cycle(
        state, quantity=1, cost=2.0, last=2.1, current_iv=.45,
        current_efficiency=1.25, now=now,
    )

    assert state["position_opened_at"] == now.isoformat()
    assert state["peak"] == 2.1
    assert state["entry_iv_actual"] == .45
    assert state["entry_gamma_theta_efficiency_actual"] == 1.25
    assert state["contraction_confirmations"] == 0
    assert state["previous_position_quantity"] == 1


def test_flat_volatility_position_clears_cycle_specific_state():
    state = {
        "previous_position_quantity": 1,
        "position_opened_at": "2026-07-19T14:00:00+00:00",
        "peak": 3.0,
        "entry_iv_actual": .45,
        "entry_gamma_theta_efficiency_actual": 1.25,
        "contraction_confirmations": 2,
    }

    sync_volatility_position_cycle(
        state, quantity=0, cost=0, last=2.0, current_iv=.40,
        current_efficiency=1.0,
        now=datetime(2026, 7, 19, 15, tzinfo=timezone.utc),
    )

    assert state == {"previous_position_quantity": 0}


def test_active_shock_does_not_exit_only_because_long_term_volatility_contracts():
    regime = {
        "active": True, "contraction": True, "contraction_confirmations": 10,
    }

    assert volatility_exit_reason(regime, .50, .50, 1, 1) is None


def test_inactive_contraction_requires_three_confirmations():
    regime = {"active": False, "contraction": True, "contraction_confirmations": 2}
    assert volatility_exit_reason(regime, .50, .50, 1, 1) is None

    regime["contraction_confirmations"] = 3
    assert "实现波动" in volatility_exit_reason(regime, .50, .50, 1, 1)


def test_strong_intraday_shock_overrides_lagging_trend_direction():
    closes = pd.Series([90 + index * .2 for index in range(90)])
    bars = pd.DataFrame({
        "close": closes, "high": closes + .5, "low": closes - .5,
        "volume": [1_000_000] * 90,
    })
    owner = {"last_price": 96, "prev_close_price": 100}
    regime = volatility_regime(owner, bars, entry_z=2.0, minimum_move=.02)
    allowed, reason, _ = configured_volatility_direction_allowed(
        owner, bars, "PUT", regime,
    )
    assert regime["active"]
    assert regime["strong_signal_override"]
    assert allowed
    assert "强波动方向覆盖" in reason
