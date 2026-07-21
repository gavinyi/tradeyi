from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from futu_quant.gamma_risk import contract_analysis, gamma_add_allowed, realized_volatility
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.performance import performance_report
from futu_quant.store import TradingStore


NY = ZoneInfo("America/New_York")


def test_nyse_calendar_handles_holiday_and_early_close():
    calendar = USMarketCalendar()

    assert not calendar.session(datetime(2026, 7, 3, 12, tzinfo=NY)).is_session
    session = calendar.session(datetime(2026, 11, 27, 12, tzinfo=NY))
    assert session.is_session
    assert session.market_close.hour == 13
    assert session.pre_close(datetime(2026, 11, 27, 12, 50, tzinfo=NY))
    assert not session.pre_close(datetime(2026, 11, 27, 12, 30, tzinfo=NY))


class QuoteBroker:
    @staticmethod
    def snapshots(symbols):
        return pd.DataFrame([{"code": symbol, "last_price": 110} for symbol in symbols])


def test_fifo_performance_report(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    buy, _ = store.create_intent("sma", "buy", "US.NVDA", "BUY", 1, 100)
    store.update_intent(buy["id"], "SUBMITTED", "buy-1")
    store.update_by_broker_order("buy-1", "FILLED", 1, 100)
    sell, _ = store.create_intent("sma", "sell", "US.NVDA", "SELL", 1, 110)
    store.update_intent(sell["id"], "SUBMITTED", "sell-1")
    store.update_by_broker_order("sell-1", "FILLED", 1, 110)

    summary, detail, _ = performance_report(store, QuoteBroker())

    assert detail.empty
    assert summary.iloc[0]["已实现盈亏"] == 10
    assert summary.iloc[0]["胜率"] == 1


def test_equity_drawdown_exists_without_fills(tmp_path):
    store = TradingStore(tmp_path / "test.db")
    store.record_equity(1000, 500, 500)
    store.record_equity(900, 400, 500)

    summary, detail, equity = performance_report(store, QuoteBroker())

    assert summary.empty and detail.empty
    assert equity["回撤"].tolist() == pytest.approx([0, -0.1])


def test_gamma_volatility_and_greeks_limits(tmp_path):
    candles = pd.DataFrame({"close": [100 + index * 0.2 for index in range(90)]})
    store = TradingStore(tmp_path / "test.db")
    row = {
        "code": "US.NVDA260717C200000", "option_implied_volatility": 50,
        "option_expiry_date_distance": 7,
    }
    analysis = contract_analysis(row, 200, realized_volatility(candles), store)
    allowed, _ = gamma_add_allowed(
        {"Delta": 999, "Gamma": 0, "Vega": 0},
        {"option_contract_multiplier": 100, "option_delta": 0.5}, 1,
    )

    assert analysis["隐含波动率"] == 0.5
    assert analysis["预期波动金额"] > 0
    assert not allowed
