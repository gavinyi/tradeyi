from datetime import datetime
from zoneinfo import ZoneInfo

from futu_quant.data_quality import (
    option_book_status, option_quote_freshness, quote_freshness,
)


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


def test_option_book_status_explains_unusable_and_wide_books():
    usable, reason, _ = option_book_status({"bid_price": 0, "ask_price": 1.2})
    assert not usable
    assert "盘口不可用" in reason

    usable, reason, spread = option_book_status(
        {"bid_price": 1.0, "ask_price": 1.5}, maximum_spread_pct=20,
    )
    assert not usable
    assert spread > 20
    assert "价差过大" in reason

    usable, reason, spread = option_book_status(
        {"bid_price": 1.0, "ask_price": 1.05}, maximum_spread_pct=20,
    )
    assert usable
    assert spread < 20
    assert "盘口可用" in reason
