"""P2-3 ML 打分开关（租户级）守卫。

要守的三件事：
1. **默认 `rule`**：不显式开启就不上 ML（用户 2026-10-24 确认"装依赖与开启放最后"）——
   不能因为生产 `.env` 里遗留的 `ML_ENABLED=true` 就静默变成 ML；
2. 租户键优先、全局键兜底、非法值失败安全（落 ERROR，不静默当 ML）；
3. 写侧**只写租户键**，拿不到租户就拒绝写（写全局会连带切换所有租户）。

变异：把 `resolve_scoring_mode` 的默认值从 `rule` 改成 `ml` → 第 1 条转红；
把非法值分支改成直接返回原值 → 第 2 条转红。
"""

from __future__ import annotations

import pytest

from futu_quant.ml_config import (
    DEFAULT_MODE,
    MODE_ML,
    MODE_RULE,
    resolve_ml_factor,
    resolve_scoring_mode,
    set_ml_factor,
    set_scoring_mode,
)
from futu_quant.store import TradingStore

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


def test_default_is_rule_not_ml(tmp_path):
    """不显式开启就是规则打分 —— 部署不会改变策略行为。"""
    store = TradingStore(tmp_path / "t.db")
    assert DEFAULT_MODE == MODE_RULE
    assert resolve_scoring_mode(store, REAL) == MODE_RULE


def test_tenant_switch_overrides_and_isolated(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    key = set_scoring_mode(store, PAPER, MODE_ML)

    assert key == f"scoring_mode:{PAPER}"
    assert resolve_scoring_mode(store, PAPER) == MODE_ML
    # 其他租户不受影响（实盘继续规则打分 = 用户要的"先在模拟盘试"）
    assert resolve_scoring_mode(store, REAL) == MODE_RULE


def test_global_fallback_used_when_tenant_missing(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting("scoring_mode", MODE_ML)
    assert resolve_scoring_mode(store, REAL) == MODE_ML


def test_invalid_mode_is_visibly_rejected(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting(f"scoring_mode:{REAL}", "yes-please")

    assert resolve_scoring_mode(store, REAL) == MODE_RULE, "非法值不得被当成 ML"
    events = [
        e for e in store.operational_events(limit=50)
        if e["event_type"] == "SCORING_MODE_INVALID"
    ]
    assert events, "非法配置必须落 ERROR 事件，否则'写错了'会表现成'ML 没生效'"
    assert events[0]["status"] == "ERROR"


def test_set_refuses_without_tenant_and_bad_value(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    with pytest.raises(ValueError):
        set_scoring_mode(store, "", MODE_ML)
    with pytest.raises(ValueError):
        set_scoring_mode(store, REAL, "maybe")
    assert store.get_setting("scoring_mode") == "", "拒绝时不得悄悄写全局键"
def test_factor_defaults_and_tenant_override(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    assert resolve_ml_factor(store, REAL, "ml_threshold") == 0.5
    assert resolve_ml_factor(store, REAL, "min_samples") == 200

    set_ml_factor(store, PAPER, "ml_threshold", "0.62")
    set_ml_factor(store, PAPER, "min_samples", "50")
    assert resolve_ml_factor(store, PAPER, "ml_threshold") == pytest.approx(0.62)
    assert resolve_ml_factor(store, PAPER, "min_samples") == 50
    # 租户隔离
    assert resolve_ml_factor(store, REAL, "ml_threshold") == 0.5


def test_invalid_factor_falls_back_visibly(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting(f"ml_threshold:{REAL}", "1.5")  # 越界

    assert resolve_ml_factor(store, REAL, "ml_threshold") == 0.5
    events = [
        e for e in store.operational_events(limit=50)
        if e["event_type"] == "ML_FACTOR_INVALID"
    ]
    assert events and events[0]["status"] == "ERROR"


def test_set_factor_refuses_bad_input(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    with pytest.raises(ValueError):
        set_ml_factor(store, "", "ml_threshold", "0.6")
    with pytest.raises(ValueError):
        set_ml_factor(store, REAL, "ml_threshold", "2.0")
    with pytest.raises(KeyError):
        set_ml_factor(store, REAL, "no_such_factor", "1")

