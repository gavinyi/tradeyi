"""「预期收益 ≥ N× 预计成本」闸门的可配置化（`min_return_cost_ratio`）。

口径（2026-10-28 查证）：`estimated_total_cost_pct` 缺失时兜底为
`spread_pct/100 + fee_slippage_allowance_pct`，注释明确是 **round-trip**
（一个完整报价价差 + 往返费用/滑点）。因此默认 2.0 = 行业「edge ≥ 2× 往返成本」
口径，**默认值不动**，只把它变成可配。

同时把写死的 1% allowance 变成 `fee_slippage_allowance_pct`（默认 0.01）。
"""

from __future__ import annotations

import pytest

from futu_quant.volatility_optimizer import (
    _bounded_campaign_float,
    volatility_entry_quality_allowed,
)


def _campaign(**overrides):
    base = {
        "volatility_quality_score": 80.0,
        "minimum_entry_quality_score": 60.0,
        "model_expected_return": 0.04,
        "spread_pct": 1.0,
        "maximum_entry_spread_pct": 10.0,
        "option_open_interest": 5000,
        "volume": 5000,
        "minimum_entry_open_interest": 100,
        "minimum_entry_volume": 10,
    }
    base.update(overrides)
    return base


def test_bounded_helper_defaults_and_bounds():
    assert _bounded_campaign_float(None, default=2.0, low=1.5, high=4.0) == 2.0
    assert _bounded_campaign_float("n/a", default=2.0, low=1.5, high=4.0) == 2.0
    assert _bounded_campaign_float(0, default=2.0, low=1.5, high=4.0) == 2.0
    assert _bounded_campaign_float(1.0, default=2.0, low=1.5, high=4.0) == 1.5
    assert _bounded_campaign_float(9.0, default=2.0, low=1.5, high=4.0) == 4.0
    assert _bounded_campaign_float(2.5, default=2.0, low=1.5, high=4.0) == 2.5


def test_default_ratio_is_two():
    """默认仍是 2.0：成本 1.58% 时，预期 3.10% 应被拦、3.20% 应放行。"""
    campaign = _campaign(estimated_total_cost_pct=0.0158, model_expected_return=0.0310)
    ok, reason, checks = volatility_entry_quality_allowed(campaign)
    assert ok is False
    assert checks["min_return_cost_ratio"] == 2.0
    assert "的2倍" in reason

    campaign = _campaign(estimated_total_cost_pct=0.0158, model_expected_return=0.0320)
    ok, _reason, checks = volatility_entry_quality_allowed(campaign)
    assert ok is True
    assert checks["min_return_cost_ratio"] == 2.0


def test_ratio_is_configurable_downward_within_floor():
    """配成 1.6：预期 2.60% ≥ 1.58%×1.6=2.53% → 放行（默认 2.0 会拦）。"""
    campaign = _campaign(
        estimated_total_cost_pct=0.0158,
        model_expected_return=0.0260,
        min_return_cost_ratio=1.6,
    )
    ok, _reason, checks = volatility_entry_quality_allowed(campaign)
    assert ok is True
    assert checks["min_return_cost_ratio"] == 1.6


def test_ratio_below_floor_is_clamped_not_honoured():
    """配成 0.5 想「关掉闸门」→ 夹到 1.5 底线，不能真的失效。"""
    campaign = _campaign(
        estimated_total_cost_pct=0.0158,
        model_expected_return=0.0100,
        min_return_cost_ratio=0.5,
    )
    ok, _reason, checks = volatility_entry_quality_allowed(campaign)
    assert ok is False
    assert checks["min_return_cost_ratio"] == 1.5


def test_allowance_is_configurable():
    """allowance 0.01 → 0.02 时成本变高，同一笔交易由放行变拦截。"""
    base = dict(spread_pct=0.58, model_expected_return=0.0330)
    ok, _r, checks = volatility_entry_quality_allowed(_campaign(**base))
    assert ok is True
    assert checks["fee_slippage_allowance_pct"] == pytest.approx(0.01)
    assert checks["estimated_total_cost_pct"] == pytest.approx(0.0158)

    ok, _r, checks = volatility_entry_quality_allowed(
        _campaign(**base, fee_slippage_allowance_pct=0.02)
    )
    assert ok is False
    assert checks["estimated_total_cost_pct"] == pytest.approx(0.0258)


def test_explicit_cost_overrides_fallback():
    campaign = _campaign(estimated_total_cost_pct=0.03, model_expected_return=0.07)
    ok, _r, checks = volatility_entry_quality_allowed(campaign)
    assert ok is True
    assert checks["estimated_total_cost_pct"] == pytest.approx(0.03)
