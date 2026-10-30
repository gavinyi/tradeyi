from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import exchange_calendars as xcals
import pandas as pd


@dataclass(frozen=True)
class MarketSession:
    is_session: bool
    market_open: datetime | None = None
    market_close: datetime | None = None

    def regular_open(self, now: datetime) -> bool:
        return bool(self.is_session and self.market_open <= now <= self.market_close)

    def extended_open(self, now: datetime) -> bool:
        if not self.is_session:
            return False
        return self.market_open - timedelta(hours=5, minutes=30) <= now <= self.market_close + timedelta(hours=4)

    def after_close(self, now: datetime) -> bool:
        return bool(self.is_session and self.market_close + timedelta(minutes=5) <= now <= self.market_close + timedelta(hours=4))

    def pre_close(self, now: datetime, minutes: int = 15) -> bool:
        """Regular-session window used for orders that must be executable today."""
        return bool(
            self.is_session and self.market_close - timedelta(minutes=minutes) <= now < self.market_close
        )


class USMarketCalendar:
    def __init__(self):
        self.calendar = xcals.get_calendar("XNYS")

    def session(self, now: datetime) -> MarketSession:
        label = pd.Timestamp(now.date())
        if not self.calendar.is_session(label):
            return MarketSession(False)
        schedule = self.calendar.schedule.loc[label:label].iloc[0]
        return MarketSession(
            True,
            schedule["open"].to_pydatetime().astimezone(now.tzinfo),
            schedule["close"].to_pydatetime().astimezone(now.tzinfo),
        )

    def seconds_until_regular_open(self, now: datetime, cap: float = 900.0) -> float:
        """距下一次常规开盘的秒数，上限 ``cap``。

        为什么需要：雷达在非交易时段是「睡一个固定时长再醒」，固定 900 秒
        **会跨越开盘** —— 2026-10-30 21:24:56 睡下，21:30 开盘，直到 21:39:56
        才醒来扫描，开盘后那 10 分钟的波动完全没扫到（波动率策略最吃这段）。
        等待时长因此必须被「距下次开盘」截断，而不是只按固定间隔算。

        异常/日历查不到时返回 ``cap``：宁可多睡一个完整间隔，也不要因日历
        报错而退化成忙循环。
        """
        try:
            next_open = self.calendar.next_open(pd.Timestamp(now))
            target = next_open.to_pydatetime()
            target = (
                target.replace(tzinfo=now.tzinfo)
                if target.tzinfo is None
                else target.astimezone(now.tzinfo)
            )
            return max(0.0, min(float(cap), (target - now).total_seconds()))
        except Exception:  # noqa: BLE001 - 日历异常不得让雷达主循环崩掉
            return float(cap)
