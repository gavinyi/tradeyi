import pandas as pd
import pytest

from futu_quant.option_signal import configured_volatility_direction_allowed
from futu_quant.volatility_optimizer import (
    decide_volatility_action, rank_volatility_contracts, volatility_exit_reason,
    sync_volatility_position_cycle, volatility_regime,
    tiered_premium_budget, volatility_partial_exit_plan,
    volatility_direction_size_factor, volatility_entry_confirmation,
    volatility_entry_quality_allowed, volatility_min_expected_return,
    volatility_profit_protection_profile, loss_adaptive_premium_budget,
    volatility_combo_entry_allowed, volatility_hard_loss_exit_reason,
    volatility_profit_capture_ratio, volatility_profit_lock_floor,
    same_signal_epoch_reentry_allowed, volatility_cooldown_reentry_allowed,
    select_partial_exit_batch, batch_r_multiple, volatility_batch_metrics,
    volatility_time_decay_stop,
)
from futu_quant.volatility_roll import should_replace_campaign
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from futu_quant.volatility_optimizer import volatility_signal_state


def test_same_signal_epoch_blocks_reentry_until_new_signal():
    assert same_signal_epoch_reentry_allowed(last_exit_epoch=3, current_epoch=3)[0] is False
    assert same_signal_epoch_reentry_allowed(last_exit_epoch=3, current_epoch=4)[0] is True


def test_strong_signal_quality_gate_matches_candidate_threshold():
    allowed, reason, checks = volatility_entry_quality_allowed({
        "adaptive_risk_tier": "强波动精选",
        "minimum_entry_quality_score": 54,
        "volatility_quality_score": 62,
        "spread_pct": 3,
        "option_open_interest": 1000,
        "volume": 100,
        "model_expected_return": .20,
    })
    assert allowed
    assert checks["minimum_quality_score"] == 60
    assert "通过" in reason


def test_expired_cooldown_allows_reentry_without_legacy_exit_epoch():
    allowed, reason = volatility_cooldown_reentry_allowed(
        remaining_hours=-0.1, last_exit_epoch=None, current_epoch=1,
    )
    assert allowed
    assert "冷却已结束" in reason


def test_active_cooldown_reports_time_reason_not_signal_epoch():
    """时间冷却期内，理由必须是**时间**，不能是「缺少上次退出信号周期」。

    2026-10-30：实盘 COIN 硬止损后页面持续提示「同一信号周期禁止重入：缺少上次
    退出信号周期」，用户以为等信号周期切换就能重入，实际要等满 24h。
    判定结果（拒绝）没错，错的是理由 —— 真实闸门是 `remaining_hours`，
    而 `remaining_hours > 0` 时 epoch 分支恒返回 False，对决策没有任何作用。
    """
    allowed, reason = volatility_cooldown_reentry_allowed(
        remaining_hours=2, last_exit_epoch=None, current_epoch=1,
    )
    assert not allowed
    assert "退出冷却中" in reason
    assert "剩余约 2.0 小时" in reason
    assert "缺少上次退出信号周期" not in reason, (
        "冷却期内的理由又退回信号周期口径 —— 会让人误以为等周期切换即可重入"
    )


def test_volatility_signal_state_survives_expression_roll():
    state = {
        "volatility_long_gamma:US.MSFT261002C505000": {
            "signal_started_at": "2026-09-25T10:00:00-04:00",
            "signal_start_volume_ratio": 4.0,
            "signal_active": True,
            "regime_was_active": True,
            "signal_epoch": 7,
        }
    }
    first = volatility_signal_state(
        state, state["volatility_long_gamma:US.MSFT261002C505000"],
        "volatility_long_gamma", "US.MSFT", "US.MSFT261002C505000",
        # 2026-10-27：信号键带租户段（无租户 fail-closed，不再共用共享键）
        tenant_id="futu_real_master",
    )
    second = volatility_signal_state(
        state, {}, "volatility_long_gamma", "US.MSFT", "US.MSFT261002C520000",
        tenant_id="futu_real_master",
    )
    assert second is first
    assert second["signal_started_at"] == "2026-09-25T10:00:00-04:00"
    assert second["signal_start_volume_ratio"] == 4.0
    assert second["signal_epoch"] == 7
    assert second["active_contract_code"] == "US.MSFT261002C520000"


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


def test_tiered_budget_scales_only_by_confirmed_signal_strength():
    assert tiered_premium_budget(2000, {"active": False}) == (2000.0, "常规")
    assert tiered_premium_budget(2000, {"active": True}) == (3000.0, "动态波动")
    assert tiered_premium_budget(2000, {"active": True, "strong_signal_override": True,
                                       "trigger_confirmed": True}) == (4000.0, "强波动")


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
        # 2026-10-29：IV 百分位闸门改为 fail-closed（缺失=拦），fixture 必须显式给值。
        # 这些用例测的是 iv_rv 分档 / 预期收益分档 / 预算替代，不该被 IV 闸门干扰。
        "iv_history_percentile": .50, "iv_percentile_status": "HISTORICAL",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_iv_rv_gate_is_only_relaxed_for_confirmed_strong_signal():
    rows = _adaptive_candidate(option_implied_volatility=.52)

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
    assert strong.iloc[0].iv_rv_limit == 1.8
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
    # 2026-11-03 起预算是「按预算反推张数」：$12/张 = $1,200 > $1,000 → 可装 0 张。
    # 文案随之改为带数字的拒绝原因（仍属于同一道预算闸门）。
    assert "预算" in expensive.iloc[0].volatility_filter_reason
    assert "可装 0 张" in expensive.iloc[0].volatility_filter_reason


def test_expected_return_gate_is_tiered_for_paper_validation():
    assert volatility_min_expected_return(signal_active=False, strong_signal=False) == .02
    assert volatility_min_expected_return(signal_active=True, strong_signal=False) == .005
    assert volatility_min_expected_return(signal_active=True, strong_signal=True) == 0

    strong = rank_volatility_contracts(
        _adaptive_candidate(model_expected_return=.007, option_implied_volatility=.52), 100, .30,
        signal_active=True, strong_signal=True, max_premium_per_stage=1000,
        as_of=pd.Timestamp("2026-07-20").date(),
    )
    normal = rank_volatility_contracts(
        _adaptive_candidate(model_expected_return=.007, option_implied_volatility=.52), 100, .30,
        signal_active=False, strong_signal=False, max_premium_per_stage=1000,
        as_of=pd.Timestamp("2026-07-20").date(),
    )

    assert bool(strong.iloc[0].volatility_eligible)
    assert not bool(normal.iloc[0].volatility_eligible)
    assert "低于常规门槛" in normal.iloc[0].volatility_filter_reason


def test_budget_alternative_allows_slightly_lower_quality_when_signal_is_active():
    low_quality = _adaptive_candidate(
        option_implied_volatility=.52,
        option_delta=.34,
        option_gamma=.002,
        option_theta=-.20,
        model_expected_return=.01,
    )
    ranked = rank_volatility_contracts(
        low_quality, 100, .30,
        signal_active=True, strong_signal=True, max_premium_per_stage=1000,
        as_of=pd.Timestamp("2026-07-20").date(),
    )

    assert bool(ranked.iloc[0].volatility_eligible)
    assert bool(ranked.iloc[0].budget_alternative)
    assert "预算替代表达放行" in ranked.iloc[0].volatility_filter_reason


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
        tiered_profit_lock_enabled=False,
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


def test_soft_profit_protection_handles_near_threshold_reversal():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now, opened_at=now - timedelta(minutes=20), quantity=1,
        stages_filled=1, max_stages=1, pnl_ratio=.03, peak_pnl_ratio=.0986,
        drawdown_from_peak=.063, directional_confirmation=False, signal_active=False,
        profit_target=.50, stop_loss=.40, trailing_stop=.25, max_hold_hours=24,
        profit_protection_start=.10, soft_profit_protection_start=.08,
        soft_profit_lock_floor=.03, soft_profit_giveback=.35,
        profit_giveback_from_peak=(.0986 - .03) / .0986,
        peak_reached_at=now - timedelta(minutes=5),
    )
    assert action == "SELL"
    assert "软启动锁盈" in reason


def test_soft_profit_protection_does_not_exit_small_pullback():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, _ = decide_volatility_action(
        now=now, opened_at=now - timedelta(minutes=20), quantity=1,
        stages_filled=1, max_stages=1, pnl_ratio=.07, peak_pnl_ratio=.09,
        drawdown_from_peak=.018, directional_confirmation=True, signal_active=True,
        profit_target=.50, stop_loss=.40, trailing_stop=.25, max_hold_hours=24,
        profit_protection_start=.10, soft_profit_protection_start=.08,
        soft_profit_lock_floor=.03, profit_giveback_from_peak=(.09 - .07) / .09,
        peak_reached_at=now - timedelta(minutes=10),
    )
    assert action == "HOLD"


def test_profit_protection_does_not_sell_before_configured_start():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(hours=1),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.12,
        peak_pnl_ratio=.14,
        drawdown_from_peak=.30,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
        profit_protection_start=.15,
        profit_protection_drawdown=.25,
    )

    assert action != "SELL"


def test_profit_protection_holds_winner_until_drawdown_threshold():
    """棘轮止盈：回吐未到阈值前必须 HOLD，到了才 SELL。

    `opened_at` 用 30 分钟而不是 60 分钟：棘轮在满 60 分钟后会再乘收紧系数
    （`profit_ratchet_tighten_factor`），那是另一条独立断言
    （见 `test_profit_ratchet_tightens_after_hold_window`）。这里只想验证
    「阈值前不动手」，所以刻意避开时间收紧点。
    """
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    common = dict(
        now=now,
        opened_at=now - timedelta(minutes=30),
        quantity=1,
        stages_filled=3,
        max_stages=3,
        peak_pnl_ratio=.26,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
        profit_protection_start=.15,
        profit_protection_drawdown=.25,
        single_contract_take_profit=0,
        tiered_profit_lock_enabled=False,
    )

    action, reason = decide_volatility_action(
        pnl_ratio=.206,
        drawdown_from_peak=.04,
        profit_giveback_from_peak=(.26 - .206) / .26,
        **common,
    )
    assert action == "HOLD"
    assert "浮盈20.6%" in reason

    action, reason = decide_volatility_action(
        pnl_ratio=.183,
        drawdown_from_peak=.07,
        profit_giveback_from_peak=(.26 - .183) / .26,
        **common,
    )
    assert action == "SELL"
    assert "棘轮止盈触发" in reason
    assert "止盈线" in reason


def test_profit_protection_uses_profit_giveback_not_option_price_drawdown():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(hours=1),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.071,
        peak_pnl_ratio=.260,
        drawdown_from_peak=.150,
        profit_giveback_from_peak=(.260 - .071) / .260,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
        profit_protection_start=.15,
        profit_protection_drawdown=.25,
    )

    assert action == "SELL"
    assert "当前盈利7.1%" in reason


def test_profit_protection_minimum_locked_profit_exits_before_winner_turns_loss():
    """峰值 22% 的仓位回落到 4% 必须出场。

    `minimum_locked_profit=.08` 低于棘轮线（16.6%），所以由棘轮接管 —— 这条
    钉住的是「赢单不许变成亏损单」。绝对锁盈线接管的情形另有一条测试
    （`test_absolute_locked_profit_takes_over_when_above_ratchet`）。
    """
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(minutes=30),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.04,
        peak_pnl_ratio=.22,
        drawdown_from_peak=.10,
        profit_giveback_from_peak=.10,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
        profit_protection_start=.15,
        profit_protection_drawdown=.25,
        minimum_locked_profit=.08,
    )

    assert action == "SELL"
    assert "棘轮止盈触发" in reason


def test_absolute_locked_profit_takes_over_when_above_ratchet():
    """`minimum_locked_profit` 高于棘轮线时由它接管，reason 必须说明是哪条线。

    否则运维看到 SELL 无法判断该调哪个参数（棘轮曲线 vs 绝对锁盈线）。
    """
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    common = dict(
        now=now, opened_at=now - timedelta(minutes=30), quantity=1,
        stages_filled=1, max_stages=3, peak_pnl_ratio=.22,
        drawdown_from_peak=.10, profit_giveback_from_peak=.10,
        directional_confirmation=True, signal_active=True,
        profit_target=.50, stop_loss=.40, trailing_stop=.25, max_hold_hours=24,
        profit_protection_start=.15, profit_protection_drawdown=.25,
    )
    # 棘轮线 ≈ 16.6%；把绝对锁盈线抬到 20% → 由它接管
    action, reason = decide_volatility_action(
        pnl_ratio=.19, minimum_locked_profit=.20, **common,
    )
    assert action == "SELL"
    assert "绝对锁盈线止盈触发" in reason
    assert "锁定线20.0%" in reason

    # 同价位若锁盈线低于棘轮线，则由棘轮接管
    action, reason = decide_volatility_action(
        pnl_ratio=.19, minimum_locked_profit=.05, **common,
    )
    assert action == "HOLD", "19% 仍高于棘轮线 16.6%，不该卖"


def test_profit_protection_profile_uses_signal_tier_overrides():
    campaign = {
        "profit_protection_start_strong": .21,
        "profit_protection_giveback_strong": .31,
        "minimum_locked_profit_strong": .11,
    }
    profile = volatility_profit_protection_profile(
        campaign, {"active": True, "strong_signal_override": True, "trigger_confirmed": True}
    )

    assert profile == {
        "tier": "强波动",
        "profit_protection_start": .21,
        "profit_protection_giveback": .31,
        "minimum_locked_profit": .11,
    }


def test_profit_protection_profile_uses_near_expiry_override():
    tomorrow = (
        datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)
    ).date().isoformat()
    campaign = {
        "strike_time": tomorrow,
        "near_expiry_protection_days": 1,
        "profit_protection_start_near_expiry": .10,
        "profit_protection_giveback_near_expiry": .20,
        "minimum_locked_profit_near_expiry": .05,
        "profit_protection_start_strong": .20,
        "profit_protection_giveback_strong": .30,
        "minimum_locked_profit_strong": .10,
    }
    profile = volatility_profit_protection_profile(
        campaign, {"active": True, "strong_signal_override": True, "trigger_confirmed": True}
    )

    assert profile == {
        "tier": "临近到期",
        "profit_protection_start": .10,
        "profit_protection_giveback": .20,
        "minimum_locked_profit": .05,
    }


def test_batch_exit_policy_and_r_multiple():
    batches = [
        {"tranche_id": "a", "quantity": 1, "pnl_ratio": .30, "r_multiple": 1.0},
        {"tranche_id": "b", "quantity": 1, "pnl_ratio": -.10, "r_multiple": -.5},
    ]
    assert select_partial_exit_batch(batches, mode="FIFO")["tranche_id"] == "a"
    assert select_partial_exit_batch(batches, mode="LIFO")["tranche_id"] == "b"
    assert select_partial_exit_batch(batches, mode="RISK_PRIORITY")["tranche_id"] == "b"
    assert batch_r_multiple(mark=1.2, entry_price=1.0, stop_price=.9) == 2.0


def test_batch_metrics_capture_mfe_mae_and_r():
    metrics = volatility_batch_metrics(
        mark=1.1,
        batch={"entry_price": 1.0, "stop_price": .9, "mae_pnl_ratio": -.2},
        peak_mark=1.3,
    )
    assert metrics["mfe_pnl_ratio"] == .3
    assert metrics["mae_pnl_ratio"] == -.2
    assert metrics["r_multiple"] == 1.0


def test_volatility_partial_profit_plan_skips_single_contract():
    qty, tier, reason = volatility_partial_exit_plan(
        quantity=1, pnl_ratio=.40, campaign_state={},
    )

    assert qty == 0
    assert tier is None
    assert reason is None


def test_volatility_partial_profit_plan_takes_first_tier_and_keeps_runner():
    qty, tier, reason = volatility_partial_exit_plan(
        quantity=3, pnl_ratio=.20, campaign_state={},
        tier1_profit=.18, tier1_fraction=.50,
    )

    assert qty == 2
    assert tier == "tier1"
    assert "分批止盈第一档" in str(reason)


def test_volatility_partial_profit_plan_does_not_repeat_filled_tier():
    qty, tier, reason = volatility_partial_exit_plan(
        quantity=2, pnl_ratio=.65, campaign_state={"partial_exit_tiers": ["tier1"]},
        tier1_profit=.30, tier1_fraction=.50,
        tier2_profit=.60, tier2_fraction=.25,
    )

    assert qty == 1
    assert tier == "tier2"
    assert "第二档" in str(reason)


def test_volatility_decay_thresholds_are_strategy_configurable():
    assert volatility_exit_reason(
        {}, current_iv=.44, entry_iv=.50, current_efficiency=1, entry_efficiency=1,
        iv_decline_ratio=.15,
    ) is None
    assert "隐含波动率" in volatility_exit_reason(
        {}, current_iv=.44, entry_iv=.50, current_efficiency=1, entry_efficiency=1,
        iv_decline_ratio=.10, pnl_ratio=-.01,
    )


def test_iv_decline_alone_does_not_exit_profitable_confirmed_position():
    assert volatility_exit_reason(
        {"active": True}, current_iv=.30, entry_iv=.50,
        current_efficiency=1, entry_efficiency=1,
        iv_decline_ratio=.20, pnl_ratio=.05, directional_confirmation=True,
    ) is None


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
    assert len(state["tranches"]) == 1
    assert state["mae_pnl_ratio"] <= 0


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


def test_volatility_pyramid_addition_tranche_entry_price_tracks_incremental_cost():
    """Bug 6 守卫：
    加仓时 tranche 的 entry_price 必须记录该批增量的真实成交成本，
    不能直接使用混仓后的券商综合持仓均价 cost，否则批次 R 乘数与局部止盈失真。
    若第 1 笔 1 张买在 $1.00，涨到 $2.00 时加仓 1 张（成交 $2.00，混仓 cost 为 $1.50）：
    即使不显式传 incremental_entry_price，状态字典中持久化的上一轮 state['cost']=1.00
    也必须支持通过 (1.50*2 - 1.00*1)/1 倒推出新批次 entry_price == 2.00，而非 $1.50！
    """
    now = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
    state = {}
    # 1. 首次开仓 1 张，成本 1.00
    sync_volatility_position_cycle(
        state, quantity=1, cost=1.00, last=1.00, current_iv=0.45,
        current_efficiency=1.25, now=now,
    )
    assert len(state["tranches"]) == 1
    assert state["tranches"][0]["entry_price"] == 1.00
    assert state.get("cost") == 1.00

    # 2. 金字塔加仓 1 张，券商综合成本变为 1.50，不传 incremental_entry_price，完全依靠持久化 state['cost'] 倒推
    now_add = now + timedelta(minutes=30)
    sync_volatility_position_cycle(
        state, quantity=2, cost=1.50, last=2.00, current_iv=0.48,
        current_efficiency=1.30, now=now_add,
    )
    assert len(state["tranches"]) == 2
    add_tranche = state["tranches"][1]
    assert add_tranche["entry_price"] == 2.00, f"新加仓批次成本必须是 2.00，实际: {add_tranche['entry_price']}"
    assert add_tranche["entry_price"] != 1.50
    assert state.get("cost") == 1.50

    # 3. 清仓时 cost 必须被清除，防止泄漏到下一个新仓位
    now_close = now + timedelta(hours=1)
    sync_volatility_position_cycle(
        state, quantity=0, cost=0.0, last=2.50, current_iv=0.40,
        current_efficiency=1.0, now=now_close,
    )
    assert "cost" not in state


def test_volatility_pyramid_addition_heals_unmigrated_state_using_tranches():
    """Bug 6 存量兼容守卫：
    若当前持仓属于升级部署前已开立的仓位，state 中缺少 'cost' 字段，
    系统必须能自愈：优先从已有 tranches 列表加权反推上一轮真实均价，准确倒推加仓批次成本，绝不退化为综合成本！
    """
    now = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
    # 构造老版本存量仓位状态：有 position_opened_at 和 tranches，但没有 state['cost']
    state = {
        "position_opened_at": now.isoformat(),
        "previous_position_quantity": 1,
        "tranches": [{
            "tranche_id": "legacy-entry",
            "entry_time": now.isoformat(),
            "entry_price": 1.00,
            "quantity": 1,
        }],
    }
    assert "cost" not in state

    # 加仓 1 张，混仓均价变为 1.50（理论增量应为 2.00）
    now_add = now + timedelta(minutes=30)
    sync_volatility_position_cycle(
        state, quantity=2, cost=1.50, last=2.00, current_iv=0.48,
        current_efficiency=1.30, now=now_add,
    )
    assert len(state["tranches"]) == 2
    add_tranche = state["tranches"][1]
    assert add_tranche["entry_price"] == 2.00, f"存量状态加仓批次必须通过 tranches 倒推为 2.00，实际: {add_tranche['entry_price']}"
    assert state["cost"] == 1.50


def test_volatility_pyramid_addition_zero_cost_state_heals_via_tranches():
    """Bug 6 守卫（P2 补强）：
    若某轮券商异常返回 cost=0.0，导致 state['cost']=0.0，
    下一轮加仓时绝对不能把 0.0 当成有效成本（否则倒推算成 1.5*2 - 0 = 3.00，导致 stop_price 虚高至 1.95）；
    必须自动转入 else 分支通过已有 tranches 倒推为 2.00！
    """
    now = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
    state = {
        "position_opened_at": now.isoformat(),
        "previous_position_quantity": 1,
        "cost": 0.0,  # 异常 0.0 成本
        "tranches": [{
            "tranche_id": "entry-1",
            "entry_time": now.isoformat(),
            "entry_price": 1.00,
            "quantity": 1,
        }],
    }
    now_add = now + timedelta(minutes=30)
    sync_volatility_position_cycle(
        state, quantity=2, cost=1.50, last=2.00, current_iv=0.48,
        current_efficiency=1.30, now=now_add,
    )
    assert len(state["tranches"]) == 2
    add_tranche = state["tranches"][1]
    # 如果把 0.0 当成有效成本，implied 会变成 (1.50*2 - 0*1)/1 = 3.00；
    # 正确自愈通过 tranches 倒推必须为 2.00！
    assert add_tranche["entry_price"] == 2.00, f"加仓成本应为 2.00，实际为: {add_tranche['entry_price']}"
    assert add_tranche["entry_price"] != 3.00
    assert add_tranche["stop_price"] == pytest.approx(1.30)  # 2.00 * 0.65 = 1.30，而非 3.00 * 0.65 = 1.95





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
    closes = pd.Series([90.0] * 90)
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


def test_entry_confirmation_waits_before_first_open():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    regime = {"active": True, "reason": "全部动态波动条件已通过"}

    allowed, reason = volatility_entry_confirmation(
        now=now,
        signal_started_at=(now - timedelta(minutes=2)).isoformat(),
        regime=regime,
        confirmation_minutes=5,
    )
    assert not allowed
    assert "等待入场二次确认" in reason

    allowed, reason = volatility_entry_confirmation(
        now=now,
        signal_started_at=(now - timedelta(minutes=6)).isoformat(),
        regime=regime,
        confirmation_minutes=5,
    )
    assert allowed
    assert "通过" in reason


def test_strong_signal_uses_one_minute_dynamic_confirmation():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    regime = {
        "active": True,
        "strong_signal_override": True,
        "trigger_confirmed": True,
        "volume_confirmed": True,
        "direction_confirmed": False,
    }

    allowed, reason = volatility_entry_confirmation(
        now=now,
        signal_started_at=(now - timedelta(minutes=1.1)).isoformat(),
        regime=regime,
        confirmation_minutes=5,
    )

    assert allowed
    assert "强信号" in reason
    assert "1 分钟" in reason


def test_entry_confirmation_blocks_when_volume_fades_after_signal_start():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    regime = {
        "active": True,
        "reason": "全部动态波动条件已通过",
        "strong_signal_override": True,
        "trigger_confirmed": True,
        "volume_confirmed": True,
        "direction_confirmed": True,
        "volume_ratio": 0.9,
    }

    allowed, reason = volatility_entry_confirmation(
        now=now,
        signal_started_at=(now - timedelta(minutes=2)).isoformat(),
        regime=regime,
        confirmation_minutes=5,
        signal_start_volume_ratio=2.0,
        minimum_volume_ratio=0.8,
        minimum_volume_retention=0.65,
    )

    assert not allowed
    assert "量能保持率" in reason


def test_confirmed_adverse_direction_exits_before_option_hard_stop():
    reason = volatility_exit_reason(
        {"active": True, "contraction": False},
        .50, .50, 1, 1,
        pnl_ratio=-.12,
        directional_confirmation=False,
        current_delta=.25,
        adverse_signal_confirmations=2,
        adverse_signal_confirmations_required=2,
    )

    assert reason is not None
    assert "方向连续2轮" in reason
    assert "硬止损" in reason


def test_conflicted_strong_signal_downgrades_size():
    factor, reason = volatility_direction_size_factor({
        "strong_signal_override": True,
        "trigger_confirmed": True,
        "direction_confirmed": False,
    })

    assert factor == .5
    assert "降级" in reason


def test_saved_campaign_entry_quality_gate_blocks_stale_low_score():
    allowed, reason, checks = volatility_entry_quality_allowed({
        "adaptive_risk_tier": "强波动精选",
        "volatility_quality_score": 42,
        "spread_pct": 3.0,
        "option_open_interest": 2000,
        "volume": 200,
        "model_expected_return": .05,
    })

    assert not allowed
    assert "质量分" in reason
    assert checks["quality_score"] == 42


def test_saved_campaign_entry_quality_gate_allows_liquid_positive_edge():
    allowed, reason, _ = volatility_entry_quality_allowed({
        "adaptive_risk_tier": "强波动精选",
        "volatility_quality_score": 71,
        "spread_pct": 3.0,
        "option_open_interest": 2000,
        "volume": 200,
        "model_expected_return": .05,
        "estimated_total_cost_pct": .01,
    })

    assert allowed
    assert "通过" in reason


def test_entry_quality_rejects_wide_spread_and_low_return_to_cost():
    allowed, reason, checks = volatility_entry_quality_allowed({
        "volatility_quality_score": 75,
        "spread_pct": 10.1,
        "option_open_interest": 2000,
        "volume": 200,
        "model_expected_return": .05,
        "estimated_total_cost_pct": .03,
    })
    assert not allowed
    assert "价差" in reason
    assert checks["estimated_total_cost_pct"] == .03


def test_entry_quality_requires_two_times_cost():
    allowed, reason, _ = volatility_entry_quality_allowed({
        "volatility_quality_score": 75,
        "spread_pct": 3.0,
        "option_open_interest": 2000,
        "volume": 200,
        "model_expected_return": .03,
        "estimated_total_cost_pct": .02,
    })
    assert not allowed
    assert "2倍" in reason


class _IntentStore:
    def __init__(self, intents, settings=None):
        self._intents = intents
        self._settings = settings or {}

    def intents(self, limit=500):
        return self._intents[:limit]

    def get_setting(self, key, default=""):
        return self._settings.get(key, default)


def test_loss_adaptive_budget_blocks_after_three_consecutive_losses():
    rows = [
        {
            "strategy_id": "volatility_long_gamma",
            "symbol": "US.SPCX260814P134000",
            "side": "BUY",
            "status": "FILLED",
            "filled_quantity": 1,
            "average_fill_price": 6.0,
            "created_at": "2099-01-01T00:00:00+00:00",
        },
        {
            "strategy_id": "volatility_long_gamma",
            "symbol": "US.SPCX260814P134000",
            "side": "SELL",
            "status": "FILLED",
            "filled_quantity": 1,
            "average_fill_price": 5.0,
            "created_at": "2099-01-01T00:01:00+00:00",
        },
    ] * 3

    budget, reason = loss_adaptive_premium_budget(1000, _IntentStore(rows), "US.SPCX")

    assert budget == 0
    assert "连续亏损3次" in reason
    assert "暂停新增风险" in reason


def test_loss_adaptive_budget_recovers_after_recent_win():
    rows = [
        {
            "strategy_id": "volatility_long_gamma",
            "symbol": "US.TSLA260814C340000",
            "side": "BUY",
            "status": "FILLED",
            "filled_quantity": 1,
            "average_fill_price": 6.0,
            "created_at": "2099-01-01T00:00:00+00:00",
        },
        {
            "strategy_id": "volatility_long_gamma",
            "symbol": "US.TSLA260814C340000",
            "side": "SELL",
            "status": "FILLED",
            "filled_quantity": 1,
            "average_fill_price": 7.0,
            "created_at": "2099-01-01T00:01:00+00:00",
        },
    ]

    budget, reason = loss_adaptive_premium_budget(1000, _IntentStore(rows), "US.TSLA")

    assert budget == 1000
    assert "连续亏损0次" in reason


def test_combo_entry_requires_higher_quality_than_single_leg():
    allowed, reason, checks = volatility_combo_entry_allowed({
        "volatility_quality_score": 61,
        "model_expected_return": .03,
        "spread_pct": 3.5,
        "option_open_interest": 2000,
        "volume": 100,
    })

    assert not allowed
    assert "回退单腿" in reason
    assert checks["minimum_quality_score"] == 68


def test_combo_entry_allows_high_quality_structure():
    allowed, reason, _ = volatility_combo_entry_allowed({
        "volatility_quality_score": 74,
        "model_expected_return": .08,
        "spread_pct": 2.5,
        "option_open_interest": 3000,
        "volume": 200,
    })

    assert allowed
    assert "组合结构质量通过" in reason


def test_volatility_hard_loss_reason_uses_usd_and_ratio_limits():
    reason = volatility_hard_loss_exit_reason(
        pnl_ratio=-.20,
        pnl_amount=-125,
        max_loss_usd=100,
        max_loss_ratio=.25,
    )

    assert reason is not None
    assert "$125.00" in reason
    assert "单笔硬止损" in reason


def test_volatility_dynamic_time_stop_tightens_before_hard_stop():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)

    threshold, held_minutes, reason = volatility_time_decay_stop(
        pnl_ratio=-.16,
        opened_at=now - timedelta(minutes=90),
        now=now,
        hard_stop_loss=.25,
        floor_stop_loss=.12,
        start_minutes=15,
        full_decay_minutes=90,
    )

    assert held_minutes == 90
    assert threshold == .12
    assert reason is not None
    assert "时间衰减动态止损" in reason


def test_decide_volatility_action_uses_dynamic_time_stop():
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(minutes=90),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=-.16,
        peak_pnl_ratio=0,
        drawdown_from_peak=0,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.25,
        trailing_stop=.25,
        max_hold_hours=24,
        dynamic_stop_floor=.12,
        dynamic_stop_start_minutes=15,
        dynamic_stop_full_decay_minutes=90,
    )

    assert action == "SELL"
    assert "时间衰减动态止损" in reason


def test_single_contract_profit_target_activates_trailing_runner(): 
    now = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    action, reason = decide_volatility_action(
        now=now,
        opened_at=now - timedelta(minutes=20),
        quantity=1,
        stages_filled=1,
        max_stages=3,
        pnl_ratio=.19,
        peak_pnl_ratio=.19,
        drawdown_from_peak=0,
        directional_confirmation=True,
        signal_active=True,
        profit_target=.50,
        stop_loss=.40,
        trailing_stop=.25,
        max_hold_hours=24,
        single_contract_take_profit=.18,
    )

    assert action == "HOLD"
    assert "启动移动止盈" in reason


def test_tiered_profit_lock_floor_scales_with_mfe():
    assert volatility_profit_lock_floor(.11) == (.06, "轻盈利档")
    assert volatility_profit_lock_floor(.31) == (.18, "强盈利档")
    assert volatility_profit_lock_floor(.81) == (.50, "爆发档")
    assert volatility_profit_lock_floor(1.05) == (.70, "翻倍主升档")
    assert volatility_profit_lock_floor(1.55) == (1.10, "超级主升档")


def test_profit_capture_ratio_measures_mfe_retention():
    assert volatility_profit_capture_ratio(.10, .20) == .5
    assert volatility_profit_capture_ratio(.10, 0) is None


def test_weekend_carry_exit_triggers_on_friday_near_close():
    from zoneinfo import ZoneInfo
    ny_tz = ZoneInfo("America/New_York")
    # 2026-10-16 是星期五，美东 15:45 (收盘前 15 分钟)
    friday_preclose = datetime(2026, 10, 16, 15, 45, tzinfo=ny_tz)

    # 1. 浮盈只有 +5% (< 20% 安全垫)，周五收盘前应果断避险平仓
    action, reason = decide_volatility_action(
        now=friday_preclose,
        opened_at=friday_preclose - timedelta(hours=3),
        quantity=1,
        stages_filled=1,
        max_stages=1,
        pnl_ratio=0.05,
        peak_pnl_ratio=0.08,
        drawdown_from_peak=0.03,
        directional_confirmation=True,
        signal_active=True,
        profit_target=0.50,
        stop_loss=0.35,
        trailing_stop=0.25,
        max_hold_hours=24,
        weekend_protection_enabled=True,
        weekend_protection_min_profit=0.20,
    )
    assert action == "SELL"
    assert "周五收盘前避险退出" in reason

    # 2. 浮盈有 +25% (>= 20% 安全垫)，具有足够缓冲，不触发周五避险退出
    action2, _ = decide_volatility_action(
        now=friday_preclose,
        opened_at=friday_preclose - timedelta(hours=3),
        quantity=1,
        stages_filled=1,
        max_stages=1,
        pnl_ratio=0.25,
        peak_pnl_ratio=0.25,
        drawdown_from_peak=0.0,
        directional_confirmation=True,
        signal_active=True,
        profit_target=0.50,
        stop_loss=0.35,
        trailing_stop=0.25,
        max_hold_hours=24,
        weekend_protection_enabled=True,
        weekend_protection_min_profit=0.20,
    )
    assert action2 == "HOLD"


def test_weekend_carry_exit_respects_early_close_market_session():
    """Bug 2 守卫：
    美股感恩节翌日等提前休市日（13:00 收盘）：
    若 12:45（距真实收盘15分钟）浮盈不足 20%，必须触发周末避险退出；
    若硬编码 16:00，距收盘被算成 195 分钟，将无法触发避险退出而带虚值期权过周末。
    """
    from zoneinfo import ZoneInfo
    from futu_quant.market_calendar import MarketSession

    ny_tz = ZoneInfo("America/New_York")
    # 模拟感恩节翌日（星期五），13:00 收盘，当前时间 12:45
    early_friday = datetime(2026, 11, 27, 12, 45, tzinfo=ny_tz)
    session = MarketSession(
        is_session=True,
        market_open=datetime(2026, 11, 27, 9, 30, tzinfo=ny_tz),
        market_close=datetime(2026, 11, 27, 13, 0, tzinfo=ny_tz),
    )

    action, reason = decide_volatility_action(
        now=early_friday,
        opened_at=early_friday - timedelta(hours=2),
        quantity=1,
        stages_filled=1,
        max_stages=1,
        pnl_ratio=0.05,
        peak_pnl_ratio=0.08,
        drawdown_from_peak=0.03,
        directional_confirmation=True,
        signal_active=True,
        profit_target=0.50,
        stop_loss=0.35,
        trailing_stop=0.25,
        max_hold_hours=24,
        weekend_protection_enabled=True,
        weekend_protection_min_profit=0.20,
        market_session=session,
    )
    assert action == "SELL"
    assert "周五收盘前避险退出" in reason
    assert "距收盘15分钟" in reason
