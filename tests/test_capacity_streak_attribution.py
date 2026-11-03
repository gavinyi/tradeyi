"""容量空轮的**归因**：只有「买不起」才计入连续空轮（Reviewer 2026-11-04 口径）。

背景（真实事故）：2026-11-02 那次暂停的 `reasons` 是
`{"PREMIUM_OVER_CAPACITY": 9, "MONEYNESS_TOO_FAR": 1}` —— 只要有一条**质量**拒绝，
整轮结果就是 empty，旧代码无条件 `streak += 1`，于是「质量门槛挡住的轮次」
被算成了「资金容量不足」，连 3 轮就暂停新开仓，**实盘两天没开仓**。

口径：
- `PREMIUM_OVER_CAPACITY` → 计入空轮，可触发暂停；
- 虚值 / DTE / 价差 / 深度 / 盘口 / 报价过期 → **只记录，不计数、不暂停、也不清零**。
"""

from __future__ import annotations

import pytest

from futu_quant.capacity_precheck import (
    BUYOUT_ROUNDS_KEY,
    MARKER_KEY,
    REASON_BOOK_MISSING,
    REASON_DEPTH,
    REASON_DTE,
    REASON_MONEYNESS,
    REASON_PREMIUM,
    REASON_SPREAD,
    STREAK_KEY,
    CapacityPrecheck,
    counts_as_capacity_empty_round,
    record_selection_round,
)
from futu_quant.store import TradingStore
from futu_quant.volatility_pause import (
    set_volatility_new_entries_paused,
    volatility_new_entries_paused_for_tenant,
)

TENANT = "futu_real_master"


def _empty(reasons: dict[str, int]) -> CapacityPrecheck:
    """构造一轮「筛完为空」的结果（买得起的最便宜一张记为 $175）。"""
    return CapacityPrecheck(
        equity=6640.15, line=199.2, reasons=dict(reasons), cheapest_premium=175.0,
    )


def _passing() -> CapacityPrecheck:
    import pandas as pd

    return CapacityPrecheck(
        equity=6640.15, line=398.76,
        eligible=pd.DataFrame([{"code": "US.AMZN261120C250000"}]),
        reasons={}, cheapest_premium=190.0,
    )


def _streak(store) -> int:
    return int(float(store.get_setting(STREAK_KEY.format(tenant_id=TENANT), "0") or 0))


# --------------------------------------------------------------------------- #
# 判定函数本身
# --------------------------------------------------------------------------- #


def test_only_premium_is_capacity_attributed():
    assert counts_as_capacity_empty_round(_empty({REASON_PREMIUM: 9})) is True
    # 只要有一条买不起就算（混轮也是容量问题）
    assert counts_as_capacity_empty_round(
        _empty({REASON_PREMIUM: 1, REASON_MONEYNESS: 5})
    ) is True
    for reason in (
        REASON_MONEYNESS, REASON_DTE, REASON_SPREAD, REASON_DEPTH, REASON_BOOK_MISSING,
    ):
        assert counts_as_capacity_empty_round(_empty({reason: 3})) is False
    assert counts_as_capacity_empty_round(_empty({})) is False
    # 有合格候选就不是空轮
    assert counts_as_capacity_empty_round(_passing()) is False


# --------------------------------------------------------------------------- #
# 落库行为：质量类拒绝不计数
# --------------------------------------------------------------------------- #


def test_quality_rejections_never_increment_or_pause(tmp_path):
    store = TradingStore(tmp_path / "attribution.db")
    for reasons in (
        {REASON_MONEYNESS: 1}, {REASON_DTE: 2}, {REASON_SPREAD: 1},
        {REASON_DEPTH: 1}, {REASON_BOOK_MISSING: 1},
    ):
        state = record_selection_round(store, TENANT, _empty(reasons))
        assert state["action"] == "quality_only"
        assert state["streak"] == 0

    assert _streak(store) == 0
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT), "") == ""


def test_premium_rounds_still_pause_after_three(tmp_path):
    store = TradingStore(tmp_path / "attribution.db")
    for expected in (1, 2, 3):
        state = record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 4}))
        assert state["streak"] == expected

    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT)) == "true"
    assert any("VOLATILITY_CAPACITY_PAUSED" in str(row) for row in store.alerts(limit=50))


def test_quality_round_resets_the_capacity_streak(tmp_path):
    """「连续」语义：中间只要有一轮不是买不起，连续计数就归零。

    变异守卫：把清零去掉（改回「质量轮次只跳过」），`_streak(store) == 0`
    与后面的「不会暂停」两条都会红 —— 否则买不起每三天来一次也能攒到 3 轮锁死。
    """
    store = TradingStore(tmp_path / "attribution.db")
    record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
    record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
    assert _streak(store) == 2

    for quality in ({REASON_MONEYNESS: 3}, {REASON_DTE: 1}):
        state = record_selection_round(store, TENANT, _empty(quality))
        assert state["action"] == "quality_only"
        assert _streak(store) == 0, "质量轮次必须把连续计数归零"

    # 归零后重新累计：两轮不够暂停
    record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
    record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
    assert _streak(store) == 2
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False


def test_quality_round_reports_the_real_pause_state(tmp_path):
    """质量轮次里 `state["paused"]` 不能恒为 False（人工暂停时更要说真话）。"""
    store = TradingStore(tmp_path / "attribution.db")
    set_volatility_new_entries_paused(store, TENANT, True)

    state = record_selection_round(store, TENANT, _empty({REASON_MONEYNESS: 1}))
    assert state["action"] == "quality_only"
    assert state["paused"] is True


def test_passing_round_releases_and_resets(tmp_path):
    store = TradingStore(tmp_path / "attribution.db")
    for _ in range(3):
        record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True

    state = record_selection_round(store, TENANT, _passing())
    assert state["action"] == "released"
    assert _streak(store) == 0
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False


@pytest.mark.parametrize("reasons", [{REASON_MONEYNESS: 1}, {REASON_DTE: 1}])
def test_quality_only_round_reports_reason_text(tmp_path, reasons):
    store = TradingStore(tmp_path / "attribution.db")
    state = record_selection_round(store, TENANT, _empty(reasons))
    assert "质量门槛拦截" in state["message"]
    assert "不计入容量空轮" in state["message"]


# --------------------------------------------------------------------------- #
# 频繁买不起：独立 WARNING，与暂停解耦（Reviewer 2026-11-04）
# --------------------------------------------------------------------------- #


def test_frequent_buyouts_warn_without_pausing(tmp_path):
    """买不起与质量轮次交替：永远攒不到「连续 3 轮」，但 24h 内 ≥5 次要告警。"""
    store = TradingStore(tmp_path / "attribution.db")
    for _ in range(5):
        record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}))
        record_selection_round(store, TENANT, _empty({REASON_MONEYNESS: 1}))

    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert _streak(store) == 0
    warnings = [
        row for row in store.alerts(limit=100)
        if "VOLATILITY_CAPACITY_BUYOUT_FREQUENT" in str(row)
    ]
    assert warnings, "24h 内买不起 ≥5 轮必须发 WARNING（独立于暂停）"


def test_buyout_window_drops_stale_rounds(tmp_path):
    """24h 滑动窗口：窗口外的旧轮次不参与计数，否则告警会永久粘住。"""
    from datetime import datetime, timedelta, timezone

    store = TradingStore(tmp_path / "attribution.db")
    old = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    for _ in range(5):
        record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}), now=old)

    fresh = old + timedelta(hours=48)
    record_selection_round(store, TENANT, _empty({REASON_PREMIUM: 1}), now=fresh)

    import json

    stamps = json.loads(
        store.get_setting(BUYOUT_ROUNDS_KEY.format(tenant_id=TENANT), "[]") or "[]"
    )
    assert len(stamps) == 1, "48 小时前的旧轮次必须被移出窗口，否则告警会永久粘住"
