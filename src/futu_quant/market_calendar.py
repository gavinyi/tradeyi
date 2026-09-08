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
