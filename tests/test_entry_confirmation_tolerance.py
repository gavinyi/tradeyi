"""A13（2026-10-27）：入场二次确认计时不再被单轮 inactive 清零。

缺陷形态：判定是瞬时的，涨跌幅/Z/ATR 在阈值边缘抖动时，任一轮 `regime.active=False`
就会清掉 `signal_started_at`，下一轮从 0 重新累计 —— 临界波动永远达不到
`entry_confirmation_minutes`（实测 AMD 累计到 2.2/2.5 分钟被一次 blip 打回 0 并在
0.0→0.7→1.5→2.2→0 之间循环）。

口径：新增 campaign 参数 `entry_confirmation_inactive_tolerance_cycles`（默认 1）——
连续 inactive ≤ 容忍值时保留计时（当轮仍不进场），超过才结束 epoch；blip 后恢复不得
当成新 epoch 重置计时。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.run_gamma_strategy import (
    entry_confirmation_applicable,
    update_signal_epoch_state,
)

T0 = "2026-10-26T17:00:00+00:00"
NOW = datetime(2026, 10, 26, 17, 1, tzinfo=timezone.utc)


def _state(epoch: int = 3) -> dict:
    return {
        "signal_epoch": epoch,
        "signal_started_at": T0,
        "signal_start_volume_ratio": 0.9,
        "signal_active": True,
        "regime_was_active": True,
        "inactive_cycles": 0,
    }


def test_single_inactive_blip_keeps_timer_and_epoch():
    """① active → inactive 1 轮 → active：计时连续、epoch 不变。"""
    state = _state()

    update_signal_epoch_state(
        state, regime_active=False, now=NOW, inactive_tolerance_cycles=1,
    )
    assert state["signal_started_at"] == T0, "单轮 inactive 不得清掉确认计时"
    assert state["signal_epoch"] == 3, "单轮 inactive 不得结束 epoch"
    assert state["signal_active"] is False, "容忍期内当轮仍按未激活处理（不进场）"
    assert state["inactive_cycles"] == 1

    update_signal_epoch_state(
        state, regime_active=True, now=NOW + timedelta(minutes=1),
        inactive_tolerance_cycles=1,
    )
    assert state["signal_started_at"] == T0, "blip 后恢复被当成新 epoch 会重置计时"
    assert state["signal_epoch"] == 3
    assert state["signal_active"] is True
    assert state["inactive_cycles"] == 0


def test_two_consecutive_inactive_reset_timer_and_bump_epoch_on_reactivation():
    """② active → inactive 连续 2 轮：计时清零、重新激活时 epoch+1。"""
    state = _state()
    update_signal_epoch_state(
        state, regime_active=False, now=NOW, inactive_tolerance_cycles=1,
    )
    assert state["signal_started_at"] == T0  # 第 1 轮容忍

    update_signal_epoch_state(
        state, regime_active=False, now=NOW + timedelta(minutes=1),
        inactive_tolerance_cycles=1,
    )
    assert state["signal_started_at"] is None, "连续 2 轮 inactive 必须结束 epoch"
    assert state["exit_signal_epoch"] == 3
    assert state["signal_epoch"] == 3, "epoch 在重新激活时才自增"

    reactivate_at = NOW + timedelta(minutes=2)
    update_signal_epoch_state(
        state, regime_active=True, now=reactivate_at, inactive_tolerance_cycles=1,
    )
    assert state["signal_epoch"] == 4, "新信号必须开新 epoch"
    assert state["signal_started_at"] == reactivate_at.isoformat(), "新 epoch 计时从零起"


def test_sustained_inactive_stays_fail_closed():
    """③ 持续 inactive 长周期：计时保持清空、signal_active=False（不进场）。"""
    state = _state()
    for index in range(5):
        update_signal_epoch_state(
            state, regime_active=False, now=NOW + timedelta(minutes=index),
            inactive_tolerance_cycles=1,
        )
        assert state["signal_started_at"] is None
        assert state["signal_active"] is False
    assert state["signal_epoch"] == 3, "持续 inactive 不应凭空增加 epoch"


def test_zero_tolerance_keeps_old_strict_behaviour():
    """容忍值 0 = 旧口径：任一轮 inactive 立即结束 epoch。"""
    state = _state()
    update_signal_epoch_state(
        state, regime_active=False, now=NOW, inactive_tolerance_cycles=0,
    )
    assert state["signal_started_at"] is None
    assert state["exit_signal_epoch"] == 3


def test_invalid_tolerance_falls_back_to_zero_and_warns(monkeypatch, capsys):
    """非法容忍值（非整数/负数）→ 按 0 处理并 WARN，不许静默放大容忍度。"""
    import scripts.run_gamma_strategy as rgs

    monkeypatch.setattr(rgs, "_INVALID_TOLERANCE_WARNED", False)
    assert rgs.inactive_tolerance_cycles(
        {"entry_confirmation_inactive_tolerance_cycles": "abc"}
    ) == 0
    assert rgs.inactive_tolerance_cycles(
        {"entry_confirmation_inactive_tolerance_cycles": -3}
    ) == 0
    captured = capsys.readouterr().out
    assert "[WARN]" in captured, captured
    # 缺省 = 1；合法值原样返回
    assert rgs.inactive_tolerance_cycles({}) == 1
    assert rgs.inactive_tolerance_cycles(
        {"entry_confirmation_inactive_tolerance_cycles": 2}
    ) == 2


def test_entry_confirmation_only_applies_to_flat_position():
    """Reviewer 第 4 条：入场确认只对空仓生效 → 已入场/已持仓不会重复触发开仓。"""
    assert entry_confirmation_applicable(0) is True
    assert entry_confirmation_applicable(1) is False, "已持仓仍走确认会重复开仓"
    assert entry_confirmation_applicable(3) is False
    assert entry_confirmation_applicable(None) is True


def test_positioned_round_skips_entry_confirmation(tmp_path, monkeypatch):
    """Reviewer 第 5 条（行为级）：**确认通过并入场后**（已有持仓），下一轮即使
    `regime.active=True` 也不得再次进入入场确认 → 不会重复开仓。

    用既有的端到端脚手架（持仓 1 张、亏损超硬止损线）跑一轮 run_campaign，
    断言 `volatility_entry_confirmation` 根本未被调用。
    """
    import sys
    from pathlib import Path

    from futu_quant.store import TradingStore

    tests_dir = Path(__file__).resolve().parent
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    from test_hard_stop_end_to_end import (
        BID,
        _LossBroker,
        _RecordingExecutor,
        _campaign,
        _load_gamma,
    )

    rg = _load_gamma()
    store = TradingStore(tmp_path / "no_dup_entry.db")
    executor = _RecordingExecutor(store)
    calls: list[int] = []

    def _spy(*args, **kwargs):
        calls.append(1)
        return True, "确认通过（桩）"

    monkeypatch.setattr(rg, "volatility_entry_confirmation", _spy)
    monkeypatch.setattr(rg, "volatility_signal_state", lambda *a, **k: {})
    monkeypatch.setattr(rg, "sync_volatility_position_cycle", lambda *a, **k: None)
    monkeypatch.setattr(
        rg.MARKET_CALENDAR, "session",
        lambda _now: type("S", (), {"regular_open": staticmethod(lambda _n: True)})(),
    )

    campaign = _campaign()  # 持仓 1 张（已入场）
    rg.run_campaign(
        _LossBroker(bid=BID), executor, campaign, {},
        strategy_id="volatility_long_gamma",
    )
    assert not calls, "已持仓（已入场）仍走了入场确认 → 存在重复开仓风险"
