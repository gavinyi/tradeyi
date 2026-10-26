"""A17（2026-10-27）：Reviewer B-1 两处窄口收口。

窄口一：`volatility_signal_state()` 在**无租户**上下文虽然返回游离 dict（不落库），
但随后那段 `campaign_state` 回填会把 campaign 里残留的 `signal_started_at` 抄进
游离 dict —— 一次调用就可能带着旧时间戳直接判过确认，fail-closed 不彻底。
修法：无租户分支**提前 return**，跳过回填。

窄口二：旧无租户信号键迁移是「只搬不复制」，接管过程完全静默（谁拿走了那段确认
计时无迹可查）。修法：接管时写一条 `VOLATILITY_LEGACY_SIGNAL_STATE_TAKEOVER` 审计；
旧键只在 state 里存在一次，所以该事件天然对每个 (标的, 旧键) 只发一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from futu_quant.volatility_optimizer import volatility_signal_state  # noqa: E402
from futu_quant.volatility_orphans import (  # noqa: E402
    legacy_volatility_signal_state_key,
    volatility_signal_state_key,
)
from run_gamma_strategy import audit_legacy_signal_state_takeover  # noqa: E402

REAL = "futu_real_master"
UNDERLYING = "US.AMD"
CODE = "US.AMD261030P520000"
STARTED_AT = "2026-10-27T18:27:30+00:00"
LEGACY_KEY = legacy_volatility_signal_state_key(UNDERLYING)
CANONICAL_KEY = volatility_signal_state_key(UNDERLYING, REAL)


def _feed() -> dict:
    return {
        "signal_started_at": STARTED_AT,
        "signal_start_volume_ratio": 1.35,
        "signal_active": True,
        "regime_was_active": True,
        "signal_epoch": 3,
        "exit_signal_epoch": None,
    }


def test_missing_tenant_skips_campaign_state_backfill():
    """无租户：不得把 campaign 残留计时抄进游离 dict —— 否则一次调用就能判过确认。"""
    state: dict = {}
    campaign_state = _feed()

    signal_state = volatility_signal_state(
        state, campaign_state, "volatility_long_gamma", UNDERLYING, CODE,
    )

    assert "signal_started_at" not in signal_state, (
        "无租户时回填了 campaign 的 signal_started_at → 确认可能直接被判过（fail-closed 不彻底）"
    )
    assert "signal_active" not in signal_state
    assert "regime_was_active" not in signal_state
    # 与确认计时无关的表达/epoch 默认值仍要给（调用方读它们）
    assert signal_state["active_contract_code"] == CODE
    assert signal_state["signal_epoch"] == 0
    assert signal_state["exit_signal_epoch"] is None
    # 不挂到 state 上 → 不落库
    assert state == {}


def test_tenant_path_still_backfills_campaign_timer():
    """回归守卫：有租户时回填照旧（窄口只收无租户分支，别误伤正常路径）。"""
    state: dict = {}
    signal_state = volatility_signal_state(
        state, _feed(), "volatility_long_gamma", UNDERLYING, CODE, tenant_id=REAL,
    )
    assert signal_state is state[CANONICAL_KEY]
    assert signal_state["signal_started_at"] == STARTED_AT
    assert signal_state["signal_epoch"] == 3


class _RecordingStore:
    def __init__(self) -> None:
        self.audits: list[tuple[str, dict, str]] = []

    def audit(self, event: str, payload: dict, strategy_id: str, **_kwargs) -> None:
        self.audits.append((event, payload, strategy_id))


def test_legacy_takeover_emits_single_audit_event():
    """旧键被接管：留一条审计（含接管租户/标的/旧计时），迁移后不再重复。"""
    store = _RecordingStore()
    state = {LEGACY_KEY: _feed()}

    assert audit_legacy_signal_state_takeover(
        store, state, "volatility_long_gamma", UNDERLYING, REAL,
    ) is True
    assert len(store.audits) == 1
    event, payload, strategy_id = store.audits[0]
    assert event == "VOLATILITY_LEGACY_SIGNAL_STATE_TAKEOVER"
    assert strategy_id == "volatility_long_gamma"
    assert payload["tenant_id"] == REAL
    assert payload["underlying"] == UNDERLYING
    assert payload["legacy_key"] == LEGACY_KEY
    assert payload["signal_started_at"] == STARTED_AT

    # 迁移执行（pop 旧键）→ 再次审计必须静默，不能每轮刷同一条
    volatility_signal_state(
        state, {}, "volatility_long_gamma", UNDERLYING, CODE, tenant_id=REAL,
    )
    assert LEGACY_KEY not in state
    assert audit_legacy_signal_state_takeover(
        store, state, "volatility_long_gamma", UNDERLYING, REAL,
    ) is False
    assert len(store.audits) == 1, "旧键已在内存里消失，不得重复发审计"


def test_legacy_takeover_audit_noop_without_legacy_key():
    """没有旧键时零写入（正常租户路径不该被这条审计污染）。"""
    store = _RecordingStore()
    assert audit_legacy_signal_state_takeover(
        store, {CANONICAL_KEY: _feed()}, "volatility_long_gamma", UNDERLYING, REAL,
    ) is False
    assert store.audits == []


def test_legacy_takeover_audit_ignores_non_signal_legacy_values():
    """旧键存在但值不是 dict（历史脏数据）→ 不误报接管审计。"""
    store = _RecordingStore()
    assert audit_legacy_signal_state_takeover(
        store, {LEGACY_KEY: "stale"}, "volatility_long_gamma", UNDERLYING, REAL,
    ) is False
    assert store.audits == []
