"""A15（2026-10-27）：信号状态键加租户段 + 判定严格化。

风险（Reviewer/Chief）：原信号键 `volatility_long_gamma:signal:{UNDERLYING}` 无租户段，
而 campaign_state 是两租户共享表 → 一旦实盘与模拟出现同一标的，两边会**互相推进**
对方的入场确认计时。本文件锁死三件事：

1. 两租户同标的 → 键不同、状态互不影响；
2. 历史无租户键**只搬不复制**（迁移到规范键后旧键不残留）；
3. `is_signal_state_key` 走严格解析（段数 + `signal` 段 + 标的形态），过滤/剪枝共用。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from futu_quant.volatility_optimizer import volatility_signal_state  # noqa: E402
from futu_quant.volatility_orphans import (  # noqa: E402
    is_signal_state_key,
    legacy_volatility_signal_state_key,
    parse_signal_state_key,
    volatility_signal_state_key,
    volatility_state_keys,
)

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"
UNDERLYING = "US.AMD"
CODE = "US.AMD261030P520000"
STARTED_AT = "2026-10-27T18:17:46+00:00"


def _feed(started_at: str = STARTED_AT) -> dict:
    return {
        "signal_started_at": started_at,
        "signal_epoch": 1,
        "signal_active": True,
        "regime_was_active": True,
        "signal_start_volume_ratio": 1.2,
    }


def test_two_tenants_same_underlying_are_isolated():
    """核心风险：两租户同标的不得共用同一条确认计时。"""
    real_key = volatility_signal_state_key(UNDERLYING, REAL)
    paper_key = volatility_signal_state_key(UNDERLYING, PAPER)
    assert real_key != paper_key, "两租户同标的仍生成同一个键 → 会互相推进计时"
    assert parse_signal_state_key(real_key) == (REAL, UNDERLYING)
    assert parse_signal_state_key(paper_key) == (PAPER, UNDERLYING)

    state: dict = {}
    real_state = volatility_signal_state(
        state, _feed(), "volatility_long_gamma", UNDERLYING, CODE, tenant_id=REAL,
    )
    real_state["signal_started_at"] = STARTED_AT
    # 模拟租户同标的：不得看到、也不得覆盖实盘计时
    paper_state = volatility_signal_state(
        state, {}, "volatility_long_gamma", UNDERLYING, CODE, tenant_id=PAPER,
    )
    paper_state["signal_started_at"] = "2026-10-27T18:20:00+00:00"

    assert state[real_key]["signal_started_at"] == STARTED_AT, "模拟租户覆盖了实盘计时"
    assert state[paper_key]["signal_started_at"] == "2026-10-27T18:20:00+00:00"
    assert real_key in state and paper_key in state


def test_legacy_tenantless_key_migrates_once_without_residue():
    """历史无租户键 → 只搬不复制，迁移后旧键不残留、不出现两份并存。"""
    legacy_key = legacy_volatility_signal_state_key(UNDERLYING)
    canonical_key = volatility_signal_state_key(UNDERLYING, REAL)
    state = {legacy_key: _feed()}

    volatility_signal_state(
        state, {}, "volatility_long_gamma", UNDERLYING, CODE, tenant_id=REAL,
    )
    assert canonical_key in state
    assert legacy_key not in state, "旧无租户键必须搬走（只搬不复制），否则会读旧值"
    assert state[canonical_key]["signal_started_at"] == STARTED_AT

    # 再调用一次：不重复迁移、不残留
    volatility_signal_state(
        state, {}, "volatility_long_gamma", UNDERLYING, CODE, tenant_id=REAL,
    )
    assert legacy_key not in state
    assert list(state.keys()).count(canonical_key) == 1


def test_missing_tenant_fails_closed_instead_of_shared_key():
    """无租户上下文：不写共享键（fail-closed），计时无法持久化 → 确认不会通过。"""
    state: dict = {}
    volatility_signal_state(
        state, _feed(), "volatility_long_gamma", UNDERLYING, CODE,
    )
    assert state == {}, "无租户时写了共享信号键 → 跨租户串状态"
    # 第二轮仍拿不到持久化计时
    again = volatility_signal_state(state, {}, "volatility_long_gamma", UNDERLYING, CODE)
    assert "signal_started_at" not in again


def test_signal_state_key_strict_parsing():
    canonical = "volatility_long_gamma:signal:futu_real_master:US.AMD"
    legacy = "volatility_long_gamma:signal:US.AMD"
    assert parse_signal_state_key(canonical) == (REAL, UNDERLYING)
    assert parse_signal_state_key(legacy) == ("", UNDERLYING)
    assert is_signal_state_key(canonical) is True
    assert is_signal_state_key(legacy) is True

    # 反例：缺标的、非 signal 段、非波动率前缀、多段、非法标的、campaign 键
    assert is_signal_state_key("volatility_long_gamma:signal:") is False
    assert is_signal_state_key("volatility_long_gamma:signal:futu_real_master") is False
    assert is_signal_state_key("earnings_gamma:signal:US.AMD") is False
    assert is_signal_state_key("volatility_long_gamma:signal:a:b:US.AMD") is False
    assert is_signal_state_key("volatility_long_gamma:signal:x:AMD") is False
    assert is_signal_state_key("volatility_long_gamma:futu_real_master:US.AMD") is False
    assert is_signal_state_key("volatility_long_gamma:US.AMD261030P520000") is False


def test_protected_key_sets_cover_both_signal_forms():
    """保护/活跃集合必须同时覆盖规范键与历史键（否则剪枝会误删其一）。"""
    keys = volatility_state_keys(CODE, REAL, UNDERLYING)
    assert volatility_signal_state_key(UNDERLYING, REAL) in keys
    assert legacy_volatility_signal_state_key(UNDERLYING) in keys
