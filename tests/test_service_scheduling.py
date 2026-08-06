from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from futu_quant.core_validation import enable_core_daily_validation
from futu_quant.market_calendar import MarketSession
from futu_quant.store import TradingStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_service import core_rebalance_window  # noqa: E402


NY = ZoneInfo("America/New_York")


def test_daily_validation_catches_up_after_missed_session(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    now = datetime(2026, 8, 5, 11, 0, tzinfo=NY)
    session = MarketSession(
        True,
        datetime(2026, 8, 5, 9, 30, tzinfo=NY),
        datetime(2026, 8, 5, 16, 0, tzinfo=NY),
    )
    enable_core_daily_validation(store, now=now - timedelta(days=2))
    store.set_setting("last_run:core_satellite", (now - timedelta(days=2)).isoformat())

    should_run, reason = core_rebalance_window(store, session, now)

    assert should_run is True
    assert "补偿调仓" in reason


def test_formal_core_mode_still_waits_for_preclose(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    now = datetime(2026, 8, 5, 11, 0, tzinfo=NY)
    session = MarketSession(
        True,
        datetime(2026, 8, 5, 9, 30, tzinfo=NY),
        datetime(2026, 8, 5, 16, 0, tzinfo=NY),
    )

    should_run, reason = core_rebalance_window(store, session, now)

    assert should_run is False
    assert "收盘前" in reason

