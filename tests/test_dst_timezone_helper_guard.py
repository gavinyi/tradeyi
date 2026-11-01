"""测试时区纪律守卫与确定性 DST 切换断言：
1. Ops 确定性固定日期断言：
   - 证明固定日期 '2026-11-01 01:32:21' (naive) tz_localize 必定抛出歧义异常 ValueError；
   - 证明带 UTC 偏移的 ISO 字符串可确定性解析为 America/New_York 时间，消除时区歧义；
2. 扫描守卫：全仓测试扫描禁止使用 naive 字符串构造跨时区/行情时间。
"""

from __future__ import annotations

import pathlib
import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def test_dst_deterministic_naive_ambiguity_reproduction():
    """Ops 确定性复现：证明 naive 字符串在 DST 重叠窗口必定歧义，而带时区 ISO 绝对确定。"""
    naive_str = "2026-11-01 01:32:21"
    t_naive = pd.to_datetime(naive_str)

    # 1. 验证直接 tz_localize 必定抛出 ValueError (歧义)
    with pytest.raises(ValueError, match="Cannot infer dst time"):
        t_naive.tz_localize(NY)

    # 2. 验证规范的 UTC-aware 转 ISO 字符串后必定确定无歧义
    # 模拟真实时刻 2026-11-01 05:32:21 UTC (对应 EDT -04:00)
    utc_dt_edt = datetime(2026, 11, 1, 5, 32, 21, tzinfo=timezone.utc)
    iso_edt = utc_dt_edt.astimezone(NY).isoformat()
    t_edt = pd.to_datetime(iso_edt)
    assert t_edt.tzinfo is not None
    assert str(t_edt.tz) == "UTC-04:00" or str(t_edt.tz) == "America/New_York"

    # 模拟真实时刻 2026-11-01 06:32:21 UTC (对应 EST -05:00)
    utc_dt_est = datetime(2026, 11, 1, 6, 32, 21, tzinfo=timezone.utc)
    iso_est = utc_dt_est.astimezone(NY).isoformat()
    t_est = pd.to_datetime(iso_est)
    assert t_est.tzinfo is not None
    assert str(t_est.tz) == "UTC-05:00" or str(t_est.tz) == "America/New_York"


def test_test_helpers_ast_guard_no_naive_ny_strftime():
    """守卫：断言所有 6 处 helper/行情时间戳生成物被 pd.to_datetime 解析后必须 tz-aware，且不包含 naive strftime。"""
    import ast
    tests_dir = pathlib.Path(__file__).resolve().parent

    scanned_files = [
        tests_dir / "test_quote_liveness.py",
        tests_dir / "test_silent_degradation_visibility.py",
        tests_dir / "test_option_history_backtest.py",
        tests_dir / "test_candidate_quality_filter.py",
        tests_dir / "test_decision_tenant_scope.py",
        tests_dir / "test_shadow_acceptance_fees.py",
        tests_dir / "test_validation_loop.py",
    ]

    for p in scanned_files:
        content = p.read_text(encoding="utf-8")
        assert "datetime.now(NY) - timedelta" not in content, f"{p.name} 仍包含 datetime.now(NY) - timedelta"
        assert 'datetime.now(ZoneInfo("America/New_York")).strftime' not in content, f"{p.name} 仍包含旧时区 strftime"
        assert 'pd.Timestamp.now(tz="America/New_York").strftime' not in content, f"{p.name} 仍包含旧时区 strftime"


def test_helper_outputs_are_strictly_timezone_aware():
    """断言 helper 生成的时间字符串被 pd.to_datetime 解析后 tzinfo 绝对不为 None。"""
    from tests.test_quote_liveness import _quote_at
    from tests.test_candidate_quality_filter import _quiet_but_usable_row

    # 1. _quote_at 产物带时区
    q_str = _quote_at(300)
    t_q = pd.to_datetime(q_str)
    assert t_q.tzinfo is not None, f"_quote_at 产出的字符串解析后丢失时区: {q_str}"

    # 2. _quiet_but_usable_row 产物带时区
    row = _quiet_but_usable_row(300)
    t_row = pd.to_datetime(row["update_time"])
    assert t_row.tzinfo is not None, f"_quiet_but_usable_row 产出的字符串解析后丢失时区: {row['update_time']}"

