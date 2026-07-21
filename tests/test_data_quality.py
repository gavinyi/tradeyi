from datetime import datetime
from zoneinfo import ZoneInfo

from futu_quant.data_quality import option_quote_freshness, quote_freshness


def test_quote_freshness_rejects_old_market_data():
    now = datetime(2026, 7, 16, 12, tzinfo=ZoneInfo("America/New_York"))
    allowed, _, age = quote_freshness({"update_time": "2026-07-16 11:00:00"}, now, 900)
    assert not allowed
    assert age == 3600


def test_option_quote_freshness_allows_quiet_but_usable_book():
    now = datetime(2026, 7, 16, 12, tzinfo=ZoneInfo("America/New_York"))
    allowed, reason, age, degraded = option_quote_freshness(
        {
            "update_time": "2026-07-16 11:56:00",
            "bid_price": 4.90, "ask_price": 5.10,
        },
        now,
    )
    assert allowed and degraded
    assert age == 240
    assert "降级通过" in reason


def test_option_quote_freshness_rejects_stale_or_wide_book():
    now = datetime(2026, 7, 16, 12, tzinfo=ZoneInfo("America/New_York"))
    allowed, _, _, _ = option_quote_freshness(
        {
            "update_time": "2026-07-16 11:56:00",
            "bid_price": 1.0, "ask_price": 2.0,
        },
        now,
    )
    assert not allowed
