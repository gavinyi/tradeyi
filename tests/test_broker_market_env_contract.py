from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.broker import BrokerError, FutuPaperBroker


def _broker() -> FutuPaperBroker:
    return object.__new__(FutuPaperBroker)


def test_quote_snapshot_delegates_to_authoritative_snapshots(monkeypatch):
    broker = _broker()
    observed = []

    def snapshots(symbols):
        observed.append(symbols)
        return pd.DataFrame([{"code": "US.VIX", "last_price": 18.5}])

    monkeypatch.setattr(broker, "snapshots", snapshots)
    assert broker.quote_snapshot(["US.VIX"]) == [
        {"code": "US.VIX", "last_price": 18.5}
    ]
    assert observed == [["US.VIX"]]


def test_get_history_kline_delegates_to_paginated_range(monkeypatch):
    broker = _broker()
    observed = []

    def candles_range(symbol, start, end):
        observed.append((symbol, start, end))
        return pd.DataFrame([{"time_key": "2026-10-28", "close": 690.0}])

    monkeypatch.setattr(broker, "candles_range", candles_range)
    assert broker.get_history_kline(
        "US.SPY", "2026-10-01", "2026-10-28", ktype="K_DAY"
    ) == [{"time_key": "2026-10-28", "close": 690.0}]
    assert observed == [("US.SPY", "2026-10-01", "2026-10-28")]


def test_get_history_kline_rejects_non_daily_bar_before_broker_call(monkeypatch):
    broker = _broker()
    monkeypatch.setattr(
        broker,
        "candles_range",
        lambda *args: pytest.fail("unsupported bar must not reach Futu"),
    )
    with pytest.raises(BrokerError, match="Unsupported history ktype"):
        broker.get_history_kline("US.SPY", "2026-10-01", "2026-10-28", "K_1M")
