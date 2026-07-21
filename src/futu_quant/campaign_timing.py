from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
CHINA = ZoneInfo("Asia/Shanghai")


def campaign_time_display(campaign: dict, state: dict | None = None) -> dict[str, str]:
    hours = int(campaign.get("entry_hours", 6))
    if str(campaign.get("trigger_mode", "EARNINGS")).upper() == "VOLATILITY":
        started_at = (state or {}).get("started_at")
        if not started_at:
            return {"入场开始时间（中国）": "首次运行时开始", "入场截止时间（中国）": f"开始后 {hours} 小时"}
        start = datetime.fromisoformat(str(started_at))
        end = start + timedelta(hours=hours)
    else:
        event_clock = time(8, 0) if campaign.get("event_session") == "盘前" else time(16, 5)
        end = datetime.combine(
            datetime.fromisoformat(str(campaign["event_date"])).date(), event_clock, tzinfo=NEW_YORK,
        )
        start = end - timedelta(hours=hours)
    return {
        "入场开始时间（中国）": start.astimezone(CHINA).strftime("%Y-%m-%d %H:%M"),
        "入场截止时间（中国）": end.astimezone(CHINA).strftime("%Y-%m-%d %H:%M"),
    }
