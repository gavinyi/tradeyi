"""滚档滞回带：抑制相邻行权价来回滚动（2026-10-30 实盘 COIN 复盘）。

复盘对象（真实 audit_events）：
    13:42:06  real  P140000 → P145000
    13:47:44  real  P145000 → P140000     ← 5 分 38 秒后又滚回来
    候选表当年：P140000 exp 0.2151 / P145000 exp 0.1895（差 13%）

旧实现 `should_replace_campaign` 的兜底是 `return True, "当前最佳候选已变化"`
—— 只要候选换个名字就换约，于是「谁领先一点点」都能触发一次滚档。
本文件把新的三道闸口逐条锁死：

1. 时间滞回：距上次**实际**换约不足 `ROLL_MIN_INTERVAL_MINUTES` → 不换；
2. 领先幅度：质量分没到 +8 且预期收益领先不足 → 不换；
3. 硬性例外：方向切换、当前合约质量变差 → 立即换（滞回带不该拖住这两类）。

判据可证伪：把 `ROLL_MIN_INTERVAL_MINUTES` 改成 0，第一条必须转红。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from futu_quant.volatility_roll import (
    ROLL_MIN_IMPROVEMENT_PCT,
    ROLL_MIN_INTERVAL_MINUTES,
    should_replace_campaign,
)

NOW = datetime(2026, 10, 30, 13, 47, 44, tzinfo=timezone.utc)


def _current(**overrides) -> dict:
    """当前合约：实盘 COIN 的真实数值（P140000）。"""
    data = {
        "code": "US.COIN261120P140000",
        "option_type": "PUT",
        "volatility_quality_score": 77.75,
        "volatility_filter_reason": "全部条件通过",
        "model_expected_return": 0.2151,
    }
    data.update(overrides)
    return data


def _candidate(code="US.COIN261120P145000", *, score=83.71, exp=0.1895, **overrides) -> dict:
    """新候选（P145000）：质量分更高（83.71 > 77.75）但预期收益更低。"""
    data = {
        "code": code,
        "option_type": "PUT",
        "_roll_diagnostics": {
            "top_candidates": [{
                "code": code, "eligible": True, "score": score, "selection_value": exp,
            }],
        },
    }
    data.update(overrides)
    return data


def test_roll_back_inside_interval_is_blocked():
    """复盘重放：5 分 38 秒前刚换过 → 不许再滚回去。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=5, seconds=38)).isoformat(),
    )

    replace, reason = should_replace_campaign(current, _candidate(), now=NOW)

    assert replace is False, "5 分 38 秒内的再次滚档必须被拦住 —— 这正是当晚的 whipsaw"
    assert "最小间隔" in reason
    assert "5 分钟" in reason


def test_roll_allowed_after_interval_when_score_clearly_better():
    """过了间隔 + 质量分领先 ≥8 → 换（滞回带只延迟，不禁止）。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=16)).isoformat(),
    )

    replace, reason = should_replace_campaign(
        current, _candidate(score=86.0), now=NOW
    )

    assert replace is True
    assert "明显高于" in reason


def test_coin_case_marginal_lead_does_not_roll_even_after_interval():
    """复盘重放：即使时间闸口已过，当晚那次滚档也不该发生。

    P145000 质量分只有 83.71（比当前高 **5.96**，不到 +8），
    预期收益 0.1895 反而比当前 0.2151 **低** 12%。
    旧实现会因为「候选变了名字」直接返回 True —— 这就是 whipsaw 的另一半原因。
    """
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=30)).isoformat(),
    )

    replace, reason = should_replace_campaign(current, _candidate(), now=NOW)

    assert replace is False, "质量分只高 5.96、收益还更低，不该换约"
    assert "未达替换门槛" in reason


def test_roll_blocked_after_interval_when_improvement_is_marginal():
    """过了间隔但领先不足 → 仍然不换（分数差 3 分、收益还更低）。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=30)).isoformat(),
    )
    # score 只高 3 分（<8），预期收益 0.19 还低于当前 0.215
    replace, reason = should_replace_campaign(
        current, _candidate(score=80.75, exp=0.1900), now=NOW
    )

    assert replace is False
    assert "未达替换门槛" in reason


def test_roll_allowed_when_expected_return_lead_is_large():
    """质量分没到 +8，但预期收益领先 ≥ 门槛 → 换。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=30)).isoformat(),
    )
    lead = 0.2151 * (1 + ROLL_MIN_IMPROVEMENT_PCT) * 1.05

    replace, reason = should_replace_campaign(
        current, _candidate(score=79.0, exp=lead), now=NOW
    )

    assert replace is True
    assert "预期收益" in reason and "领先" in reason


def test_direction_flip_ignores_hysteresis():
    """看跌变看涨必须立刻换 —— 方向反了还等 15 分钟是危险行为。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    candidate = _candidate(code="US.COIN261120C140000")
    candidate["option_type"] = "CALL"

    replace, reason = should_replace_campaign(current, candidate, now=NOW)

    assert replace is True
    assert "方向表达" in reason


def test_broken_current_contract_ignores_hysteresis():
    """当前合约已经不合格（流动性/IV 越界）→ 必须换，不受滞回约束。"""
    current = _current(
        volatility_filter_reason="最差腿价差 7.2% 超过 6%",
        last_roll_at=(NOW - timedelta(minutes=1)).isoformat(),
    )

    replace, reason = should_replace_campaign(current, _candidate(), now=NOW)

    assert replace is True
    assert "质量变差" in reason


def test_legacy_campaign_without_timestamp_still_respects_thresholds():
    """旧 campaign 没有 last_roll_at → 不因时间拦，但仍要过领先幅度这一关。"""
    replace, reason = should_replace_campaign(_current(), _candidate(), now=NOW)

    assert replace is False, "没有换约时间不等于可以直接换 —— 领先幅度闸口仍然生效"
    assert "未达替换门槛" in reason


def test_missing_diagnostics_keeps_current_contract():
    """没有诊断数据就证明不了「更好」→ 保持不动（旧实现在这里无条件返回 True）。"""
    replace, reason = should_replace_campaign(
        _current(), {"code": "US.COIN261120P145000", "option_type": "PUT"}, now=NOW
    )

    assert replace is False
    assert "缺少候选诊断数据" in reason


def test_same_code_is_never_replaced():
    replace, reason = should_replace_campaign(
        _current(), {"code": "US.COIN261120P140000", "option_type": "PUT"}, now=NOW
    )

    assert replace is False
    assert "仍是" in reason


@pytest.mark.parametrize("minutes", [0.5, 5, 14.9])
def test_interval_boundary_is_inclusive_of_blocking(minutes):
    """间隔内一律拦（含边界前的 14.9 分钟）。"""
    current = _current(
        last_roll_at=(NOW - timedelta(minutes=minutes)).isoformat(),
    )

    replace, _ = should_replace_campaign(current, _candidate(), now=NOW)

    assert replace is False
