"""方案 R（统一棘轮止盈）+ 止损按建仓权利金缩放 的回归测试。

背景（`docs/2026-10-29_volatility_exit_stoploss_review.md`）：

1. **P2-3 阶梯锁盈不生效**：七档表的 `floor/peak` 最高 73.3%，而「回吐 20%」
   对应的出场线恒为峰值的 80%，价格必然先穿过 80% 那条线 —— 七档表与开关常年
   只改变日志文案。方案 R 用**一条**单调收紧的棘轮曲线取代四条并行规则。
2. **$300 绝对止损与仓位规模无关**：1 张 ~$450 权利金的合约上 $300 等于 -67%，
   绝对额那层形同虚设。改为 `min($300, 8% × 建仓权利金)` 并在建仓时锁定。

这些是资金侧规则，改错会直接改变盈亏分布，所以每条都单独钉住。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from futu_quant.volatility_optimizer import (
    decide_volatility_action,
    volatility_cooldown_hours,
    volatility_hard_loss_exit_reason,
    volatility_locked_max_loss_usd,
    volatility_profit_ratchet_floor,
    volatility_profit_ratchet_giveback,
    sync_volatility_position_cycle,
)
from scripts.run_gamma_strategy import _hard_stop_limits, _locked_hard_stop_limits

NOW = datetime(2026, 7, 19, 14, tzinfo=timezone.utc)


# --------------------------------------------------------------------- 棘轮曲线

def test_ratchet_giveback_decreases_as_peak_grows():
    """峰值越高，允许的回吐越小 —— 棘轮的核心语义。"""
    samples = [.10, .20, .50, 1.00, 1.50, 2.00]
    givebacks = [volatility_profit_ratchet_giveback(p) for p in samples]
    assert givebacks[0] == pytest.approx(.20)
    assert givebacks[-1] == pytest.approx(.10)
    for earlier, later in zip(givebacks, givebacks[1:]):
        assert later <= earlier + 1e-12, f"回吐未随峰值单调递减：{givebacks}"


def test_ratchet_floor_is_monotonic_in_peak():
    """止盈线必须随峰值单调不减，否则会出现「赚得更多反而更早被卖」。"""
    floors = [volatility_profit_ratchet_floor(p / 100)[0] for p in range(10, 201, 5)]
    for earlier, later in zip(floors, floors[1:]):
        assert later >= earlier - 1e-12, f"棘轮线非单调：{floors}"
    assert floors[-1] > floors[0] * 10, "峰值 200% 的止盈线应远高于峰值 10%"


def test_ratchet_not_started_below_start():
    assert volatility_profit_ratchet_giveback(.099) is None
    assert volatility_profit_ratchet_floor(.05) is None


def test_ratchet_tightens_after_hold_window():
    """持有满 60 分钟后回吐 ×0.6 —— 把原先独立的时间收紧闸门并入同一条曲线。"""
    before = volatility_profit_ratchet_giveback(.30, held_minutes=59)
    after = volatility_profit_ratchet_giveback(.30, held_minutes=60)
    assert after == pytest.approx(before * .60)
    # 再久也不会继续收紧（避免时间维度变成第二套隐藏曲线）
    assert volatility_profit_ratchet_giveback(.30, held_minutes=240) == pytest.approx(after)


def _decide(**overrides):
    kwargs = dict(
        now=NOW, opened_at=NOW - timedelta(minutes=30), quantity=1, stages_filled=1,
        max_stages=3, pnl_ratio=.0, peak_pnl_ratio=.0, drawdown_from_peak=.0,
        directional_confirmation=True, signal_active=True, profit_target=.50,
        stop_loss=.40, trailing_stop=.25, max_hold_hours=24,
        profit_protection_start=.10, profit_protection_drawdown=.20,
        single_contract_take_profit=0,
    )
    kwargs.update(overrides)
    return decide_volatility_action(**kwargs)


def test_ratchet_locks_in_more_on_big_winners_than_legacy_giveback():
    """峰值 100%、回落到 82%：旧规则 HOLD，棘轮 SELL。

    旧逻辑：固定回吐 20% 未触发（18%），阶梯表 70% 未触发 → 继续持有，
    等于把 18 个点的浮盈全让回去才动手。棘轮在 86.4% 就落袋。
    """
    action, reason = _decide(pnl_ratio=.82, peak_pnl_ratio=1.00)
    assert action == "SELL"
    assert "棘轮止盈触发" in reason

    action, reason = _decide(
        pnl_ratio=.82, peak_pnl_ratio=1.00, profit_ratchet_enabled=False,
    )
    assert action != "SELL", "回退路径应保留旧行为，用于线上应急"


def test_ratchet_reason_routes_to_take_profit_cooldown():
    """新文案必须落到止盈档 4h，不能被冷却映射漏判成 24h（P1-1 的坑）。"""
    _, reason = _decide(pnl_ratio=.10, peak_pnl_ratio=1.00)
    assert "棘轮止盈" in reason
    assert volatility_cooldown_hours(reason, 24) == pytest.approx(4.0)


def test_absolute_lock_reason_also_routes_to_take_profit():
    _, reason = _decide(pnl_ratio=.10, peak_pnl_ratio=.30, minimum_locked_profit=.25)
    assert "绝对锁盈线" in reason
    assert volatility_cooldown_hours(reason, 24) == pytest.approx(4.0)


# --------------------------------------------------------------------- 止损缩放

def test_locked_max_loss_usd_scales_with_premium():
    """小仓位按 8% 走，大仓位被 $300 封顶。"""
    assert volatility_locked_max_loss_usd(450.0) == pytest.approx(36.0)
    assert volatility_locked_max_loss_usd(1200.0) == pytest.approx(96.0)
    assert volatility_locked_max_loss_usd(10_000.0) == pytest.approx(300.0)
    assert volatility_locked_max_loss_usd(1_000_000.0) == pytest.approx(300.0)


def test_locked_max_loss_usd_never_returns_zero():
    """0 / 负数 / 不可解析 → None（调用方回落上限），**不得**返回 0。

    `volatility_hard_loss_exit_reason` 里是 `usd_limit > 0 and ...`，
    返回 0 会让止损永不影响判定（静默失效）。
    """
    for bad in (0, -1, None, "abc"):
        assert volatility_locked_max_loss_usd(bad) is None


def test_hard_stop_limits_applies_premium_ratio():
    limits = _hard_stop_limits(
        {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25},
        None, "US.TEST", entry_premium_usd=450.0,
    )
    assert limits["max_loss_usd"] == pytest.approx(36.0)
    assert limits["premium_basis"] == pytest.approx(450.0)


def test_hard_stop_limits_without_premium_falls_back_to_cap():
    limits = _hard_stop_limits(
        {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25},
        None, "US.TEST",
    )
    assert limits["max_loss_usd"] == pytest.approx(300.0)
    assert limits["premium_basis"] is None


def test_hard_stop_limits_respects_custom_premium_ratio():
    limits = _hard_stop_limits(
        {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25,
         "max_loss_premium_ratio": .05},
        None, "US.TEST", entry_premium_usd=1000.0,
    )
    assert limits["max_loss_usd"] == pytest.approx(50.0)


def test_small_position_stop_is_tighter_than_three_hundred():
    """回归 P0：$450 权利金的仓位，旧 $300 等于 -67%，现在应约 -8%。"""
    limits = _hard_stop_limits(
        {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25},
        None, "US.TEST", entry_premium_usd=450.0,
    )
    reason = volatility_hard_loss_exit_reason(
        pnl_ratio=-.09, pnl_amount=-40.5,
        max_loss_usd=limits["max_loss_usd"], max_loss_ratio=limits["max_loss_ratio"],
    )
    assert reason is not None, "亏损 9% 在 8% 绝对线之下应已触发"
    assert "40.50" in reason or "9.0%" in reason


# --------------------------------------------------------------------- 锁定生命周期

def test_hard_stop_lock_is_written_once_and_reused():
    campaign = {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25}
    state: dict = {"position_opened_at": NOW.isoformat()}

    first = _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=450.0, quantity=1,
    )
    assert first["max_loss_usd"] == pytest.approx(36.0)
    assert state["hard_stop_lock"]["max_loss_usd"] == pytest.approx(36.0)

    # 权利金基数变化（券商成本微调）不得让止损额漂移
    second = _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=900.0, quantity=1,
    )
    assert second["max_loss_usd"] == pytest.approx(36.0), "锁定额被重算了 —— 止损会随浮盈亏漂移"
    assert second["locked"] is True


def test_hard_stop_lock_recomputes_on_add():
    campaign = {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25}
    state: dict = {"position_opened_at": NOW.isoformat()}
    _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=450.0, quantity=1,
    )
    added = _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=900.0, quantity=2,
    )
    assert added["max_loss_usd"] == pytest.approx(72.0), "加仓后风险预算应跟着仓位放大"
    assert state["hard_stop_lock"]["quantity"] == 2


def test_hard_stop_lock_scales_down_after_partial_exit():
    campaign = {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25}
    state: dict = {"position_opened_at": NOW.isoformat()}
    _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=1200.0, quantity=2,
    )  # 锁定 96
    after = _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=600.0, quantity=1,
    )
    assert after["max_loss_usd"] == pytest.approx(48.0), "减半后风险预算应等比缩减"


def test_hard_stop_lock_cleared_when_position_closes():
    """清仓必须清掉锁 —— 否则下一个仓位继承上一笔的止损额（跨仓位泄漏）。"""
    state: dict = {
        "position_opened_at": NOW.isoformat(), "previous_position_quantity": 1,
        "hard_stop_lock": {"max_loss_usd": 36.0, "quantity": 1},
    }
    transition = sync_volatility_position_cycle(
        state, quantity=0, cost=0.0, last=0.0, current_iv=.3,
        current_efficiency=1.0, now=NOW,
    )
    assert transition == "CLOSED"
    assert "hard_stop_lock" not in state


def test_hard_stop_lock_absent_before_first_entry():
    """没有持仓时不得凭空写锁（否则首次建仓会用空基数的锁）。"""
    campaign = {"max_position_loss_usd": 300.0, "max_position_loss_ratio": .25}
    state: dict = {}
    _locked_hard_stop_limits(
        campaign, state, None, "US.TEST", entry_premium_usd=0.0, quantity=0,
    )
    assert "hard_stop_lock" not in state
