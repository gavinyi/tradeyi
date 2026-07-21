from __future__ import annotations

from datetime import date

from futu_quant import earnings_calendar
from futu_quant.monitoring import heartbeat_display_status, heartbeat_stale_after


def test_calendar_filters_and_normalizes(monkeypatch):
    def fake_fetch(day: date, timeout: float):
        del timeout
        return [{"symbol": "NVDA", "time": "time-after-hours"}] if day.weekday() < 5 else []

    monkeypatch.setattr(earnings_calendar, "_fetch_day", fake_fetch)
    events = earnings_calendar.fetch_earnings_events(("US.NVDA",), days=2)

    assert events
    assert events[0]["symbol"] == "US.NVDA"
    assert events[0]["session"] == "盘后"


def test_heartbeat_display_distinguishes_running_error_and_stale():
    assert heartbeat_display_status("RUNNING", 10) == "正在执行"
    assert heartbeat_display_status("ERROR", 10) == "执行异常"
    assert heartbeat_display_status("HEALTHY", 121) == "离线（心跳超时）"
    assert heartbeat_stale_after("earnings_calendar") == 25 * 60 * 60
    assert heartbeat_stale_after("system_health") == 7 * 60
    assert heartbeat_stale_after("trading_service") == 2 * 60
