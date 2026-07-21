from datetime import date

import futu_quant.earnings_calendar as earnings_calendar


def test_discover_upcoming_earnings_returns_all_valid_symbols(monkeypatch) -> None:
    monkeypatch.setattr(earnings_calendar, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 7, 16)),
    }))

    def fake_fetch(day, timeout):
        if day.isoformat() == "2026-07-16":
            return [
                {"symbol": "AAPL", "name": "Apple", "time": "time-after-hours",
                 "epsForecast": "$1.20", "marketCap": "$3T"},
                {"symbol": "BAD SYMBOL", "name": "Invalid"},
            ]
        if day.isoformat() == "2026-07-17":
            return [{"symbol": "NVDA", "name": "NVIDIA", "time": "time-pre-market"}]
        return []

    monkeypatch.setattr(earnings_calendar, "_fetch_day", fake_fetch)
    result = earnings_calendar.discover_upcoming_earnings(3)

    assert [item["symbol"] for item in result] == ["US.AAPL", "US.NVDA"]
    assert result[0]["session"] == "盘后"
    assert result[1]["session"] == "盘前"
