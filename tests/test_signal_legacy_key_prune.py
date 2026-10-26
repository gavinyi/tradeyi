"""A16（2026-10-27）：历史无租户信号键不得被「整份 state 回写」复活。

缺陷形态（生产实测）：A15 把迁移写成「只搬不复制」（读到旧键搬到规范键并 `pop`），
但另一 Worker 周期开始时 load 到的是**含旧键的内存副本**，`save_state()` 整份回写
又把它写回 DB → `volatility_long_gamma:signal:US.AMD` 的 `updated_at` 每轮刷新
（18:27:30），规范键 `...:signal:futu_real_master:US.AMD` 同时在写，两份并存。

本文件锁死三件事：
1. `save_state()` 落盘前过滤历史无租户信号键（模拟「另一进程内存里有旧键」也写不回来）；
2. 存量旧键行由一次性清理删除（`drop_legacy_signal_state_rows`），规范键与 campaign 键不动；
3. 内存 state 不被清理改动 —— 迁移仍能从内存读到旧键（不白丢一次确认计时）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_gamma_strategy  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.volatility_orphans import (  # noqa: E402
    is_legacy_signal_state_key,
    legacy_volatility_signal_state_key,
    volatility_signal_state_key,
    without_legacy_signal_state_keys,
)

REAL = "futu_real_master"
UNDERLYING = "US.AMD"
LEGACY_KEY = legacy_volatility_signal_state_key(UNDERLYING)
CANONICAL_KEY = volatility_signal_state_key(UNDERLYING, REAL)
CAMPAIGN_KEY = "volatility_long_gamma:US.AMD261030P520000"
FEED = {"signal_started_at": "2026-10-27T18:27:30+00:00", "signal_epoch": 1}


def test_legacy_key_detection_is_same_source_as_strict_parse():
    """判定必须与 `parse_signal_state_key` 同源：只有「无租户段的信号键」算历史形态。"""
    assert is_legacy_signal_state_key(LEGACY_KEY) is True
    assert is_legacy_signal_state_key(CANONICAL_KEY) is False, "规范键带租户段，不是历史形态"
    assert is_legacy_signal_state_key(CAMPAIGN_KEY) is False
    assert is_legacy_signal_state_key("volatility_cooldown:US.AMD") is False
    assert is_legacy_signal_state_key("volatility_long_gamma:signal:") is False


def test_without_legacy_signal_state_keys_pure_copy():
    """过滤返回副本：只剔历史信号键，规范键/campaign 键保留，入参不被改。"""
    state = {LEGACY_KEY: FEED, CANONICAL_KEY: FEED, CAMPAIGN_KEY: {"peak": 1.0}}
    filtered = without_legacy_signal_state_keys(state)
    assert LEGACY_KEY not in filtered
    assert CANONICAL_KEY in filtered and CAMPAIGN_KEY in filtered
    assert LEGACY_KEY in state, "过滤不得改动内存 state —— 迁移还要从内存读旧键"


def test_save_state_never_persists_legacy_signal_key(tmp_path, monkeypatch):
    """核心守卫：另一进程内存里含旧键 → `save_state()` 之后 DB 里也没有旧键。"""
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr(run_gamma_strategy, "TradingStore", lambda *a, **k: store)

    # 模拟：本进程周期开始时 load 到的内存副本仍含旧键（另一进程尚未重启）
    state = {LEGACY_KEY: FEED, CANONICAL_KEY: FEED, CAMPAIGN_KEY: {"peak": 1.0}}
    run_gamma_strategy.save_state(state)

    persisted = store.campaign_states()
    assert LEGACY_KEY not in persisted, "历史无租户信号键被整份 state 回写复活了"
    assert persisted[CANONICAL_KEY]["signal_started_at"] == FEED["signal_started_at"]
    assert CAMPAIGN_KEY in persisted


def test_save_state_is_idempotent_after_legacy_cleanup(tmp_path, monkeypatch):
    """清理一次之后，再整份回写也不会把旧键带回来。"""
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr(run_gamma_strategy, "TradingStore", lambda *a, **k: store)

    state = {LEGACY_KEY: FEED, CANONICAL_KEY: FEED}
    run_gamma_strategy.save_state(state)
    assert LEGACY_KEY not in store.campaign_states()

    # 旧键在内存里仍存在 → 第二轮回写仍不得落库
    run_gamma_strategy.save_state(state)
    assert LEGACY_KEY not in store.campaign_states()


class _RecordingStore:
    def __init__(self, states: dict) -> None:
        self._states = dict(states)
        self.deleted: list[str] = []

    def campaign_states(self) -> dict:
        return dict(self._states)

    def delete_campaign_states(self, keys) -> int:
        keys = list(keys)
        self.deleted.extend(keys)
        for key in keys:
            self._states.pop(key, None)
        return len(keys)


def test_drop_legacy_signal_state_rows_removes_stock_and_keeps_migration_input():
    """存量旧键行删除；内存 state 不动（迁移仍可进行）；规范键与 campaign 键不碰。"""
    from run_volatility_strategy import drop_legacy_signal_state_rows

    store = _RecordingStore({LEGACY_KEY: FEED, CANONICAL_KEY: FEED, CAMPAIGN_KEY: {"peak": 1.0}})
    state = {LEGACY_KEY: FEED, CANONICAL_KEY: FEED, CAMPAIGN_KEY: {"peak": 1.0}}

    dropped = drop_legacy_signal_state_rows(store, state)

    assert dropped == [LEGACY_KEY]
    assert store.deleted == [LEGACY_KEY]
    assert LEGACY_KEY not in store.campaign_states(), "DB 里旧键行没删掉 → 下一轮还会被复活"
    assert CANONICAL_KEY in store.campaign_states()
    assert CAMPAIGN_KEY in store.campaign_states()
    assert LEGACY_KEY in state, "清理不得改内存 state（迁移路径要读它）"


def test_drop_legacy_signal_state_rows_noop_without_legacy_keys():
    """没有旧键时不做任何写入（避免每轮空扫写）。"""
    from run_volatility_strategy import drop_legacy_signal_state_rows

    store = _RecordingStore({CANONICAL_KEY: FEED})
    assert drop_legacy_signal_state_rows(store, {CANONICAL_KEY: FEED}) == []
    assert store.deleted == []
