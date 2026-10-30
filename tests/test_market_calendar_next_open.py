"""雷达等待时长的开盘截断 —— `USMarketCalendar.seconds_until_regular_open`。

回归的是 2026-10-30 实盘事故：雷达在非交易时段固定睡 900 秒，
21:24:56 睡下、21:30 开盘，直到 21:39:56 才醒来扫描 —— **开盘后 10 分钟
没有扫过市场**，波动率策略最吃的那段波动被整段错过。

判据是「等待时长必须被距下次开盘的时间截断」，而不是「改了 main 循环的某一行」，
所以这里只测时间计算，不 mock 整个雷达循环。
"""

from datetime import datetime

from zoneinfo import ZoneInfo

from futu_quant.market_calendar import USMarketCalendar

NY = ZoneInfo("America/New_York")


def test_wait_is_truncated_to_next_open():
    """开盘前 5 分 4 秒：即使 cap 是 900 秒，也只能等这 304 秒。"""
    calendar = USMarketCalendar()
    now = datetime(2026, 10, 30, 9, 24, 56, tzinfo=NY)  # 周五，EDT

    wait = calendar.seconds_until_regular_open(now, cap=900)

    assert 300 <= wait <= 310, f"应截断到开盘前剩余秒数，实际 {wait}"


def test_wait_is_not_inflated_beyond_remaining_time():
    """回归点：修复前这里会返回 900（固定间隔），开盘被跨过去。"""
    calendar = USMarketCalendar()
    now = datetime(2026, 10, 30, 9, 24, 56, tzinfo=NY)

    assert calendar.seconds_until_regular_open(now, cap=900) < 900


def test_cap_applies_when_open_is_far_away():
    """距开盘一小时：等待时长回落 cap，不一次睡到开盘。"""
    calendar = USMarketCalendar()
    now = datetime(2026, 10, 30, 8, 30, tzinfo=NY)

    assert calendar.seconds_until_regular_open(now, cap=900) == 900


def test_weekend_wait_is_capped_not_zero():
    """周六：下一次开盘是周一，等待必须回落到 cap（不能返回 0 变成忙循环）。"""
    calendar = USMarketCalendar()
    now = datetime(2026, 10, 31, 12, 0, tzinfo=NY)  # 周六

    assert calendar.seconds_until_regular_open(now, cap=900) == 900


def test_holiday_session_without_regular_open_is_capped():
    """半日市/闭市日也要安全回落：任何非交易时刻都不得返回负值。"""
    calendar = USMarketCalendar()
    for hour in (0, 6, 12, 20):
        now = datetime(2026, 11, 26, hour, 0, tzinfo=NY)  # 感恩节
        wait = calendar.seconds_until_regular_open(now, cap=900)
        assert 0 <= wait <= 900
