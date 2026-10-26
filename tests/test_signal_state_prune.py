"""A14（2026-10-27）：共享信号状态键不得被 state 过滤/剪枝丢弃。

缺陷形态：`run_volatility_strategy` 每轮把 `volatility_long_gamma:` 开头、且不属于
active campaign / 合约的键丢掉。共享信号键 `volatility_long_gamma:signal:<UNDERLYING>`
既不是 campaign 键、也不对应合约代码 → **每轮被丢** → 下一轮
`volatility_signal_state()` 又 setdefault 重建 → `signal_started_at` 丢失，
入场二次确认计时恒 0（AMD 实测 `signal_epoch` 恒 1、各轮 started_at 都等于当轮时间）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_volatility_strategy import (  # noqa: E402
    filter_volatility_state,
    is_signal_state_key,
    prunable_state_keys,
)

SIGNAL_KEY = "volatility_long_gamma:signal:US.AMD"
STALE_KEY = "volatility_long_gamma:US.AMD261030P520000"
STARTED_AT = "2026-10-27T18:08:41+00:00"


class _FakeStore:
    def __init__(self, keys: list[str]) -> None:
        self._states = {key: {"peak": 0.0} for key in keys}

    def campaign_states(self) -> dict:
        return dict(self._states)


def test_signal_key_survives_two_rounds_with_same_started_at():
    """连续两轮过滤后 signal 键仍在、signal_started_at 不变（计时可以累积）。"""
    state = {
        SIGNAL_KEY: {"signal_started_at": STARTED_AT, "signal_epoch": 1},
        STALE_KEY: {"peak": 1.0},
    }

    first = filter_volatility_state(state, active_keys=set(), active_codes=set())
    assert SIGNAL_KEY in first, "共享信号键被当 stale 丢掉 → 计时必然归零"
    assert first[SIGNAL_KEY]["signal_started_at"] == STARTED_AT
    assert STALE_KEY not in first, "非 active campaign 的 stale 键仍应被剪掉"

    second = filter_volatility_state(first, active_keys=set(), active_codes=set())
    assert second[SIGNAL_KEY]["signal_started_at"] == STARTED_AT, (
        "第二轮计时被重置 —— 入场二次确认永远累积不到目标分钟"
    )
    assert second[SIGNAL_KEY]["signal_epoch"] == 1


def test_signal_key_is_not_prunable():
    """落库剪枝（delete_campaign_states 的候选集合）也不得包含共享信号键。"""
    store = _FakeStore([SIGNAL_KEY, STALE_KEY])
    stale = prunable_state_keys(
        store, tenant_id="futu_real_master", active_keys=set(), active_codes=set(),
    )
    assert SIGNAL_KEY not in stale, "signal 键被 prune 删除 → 计时又会归零"
    assert STALE_KEY in stale, "真正 stale 的 campaign 键仍应被清理"


def test_signal_key_detection():
    assert is_signal_state_key(SIGNAL_KEY) is True
    assert is_signal_state_key("volatility_long_gamma:2817:US.AMD261030P520000") is False
    assert is_signal_state_key("volatility_cooldown:US.AMD") is False
