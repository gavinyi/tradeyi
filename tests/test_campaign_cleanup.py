from datetime import date

import pandas as pd

from futu_quant import campaign_cleanup
from futu_quant.campaign_cleanup import campaign_expired, cleanup_expired_campaigns
from futu_quant.store import TradingStore


class Broker:
    def __init__(self, positions=None, orders=None):
        self._positions = pd.DataFrame(positions or [])
        self._orders = pd.DataFrame(orders or [])

    def positions(self, refresh=False):
        return self._positions

    def orders(self, historical=False):
        return self._orders

    @staticmethod
    def is_cancellable_status(status):
        return str(status).upper() in {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}


def test_campaign_expired_requires_all_legs_expired():
    campaign = {
        "code": "US.AAPL260729C300000",
        "legs": [
            {"code": "US.AAPL260729C300000"},
            {"code": "US.AAPL260731C305000"},
        ],
    }

    expired, reason = campaign_expired(campaign, as_of=date(2026, 7, 30))

    assert expired is False
    assert "仍有未到期合约" in reason


def test_cleanup_removes_expired_campaign_without_open_risk(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    gamma_saved = [{"campaign_id": "US.AAPL:2026-07-29", "code": "US.AAPL260729C300000"}]
    volatility_saved = [{"code": "US.NVDA260729P180000"}]
    saved = {}
    monkeypatch.setattr(campaign_cleanup, "load_gamma_campaigns", lambda: list(gamma_saved))
    monkeypatch.setattr(campaign_cleanup, "load_volatility_campaigns", lambda *_a, **_k: list(volatility_saved))
    monkeypatch.setattr(campaign_cleanup, "save_gamma_campaigns", lambda rows: saved.setdefault("gamma", rows))
    monkeypatch.setattr(
        campaign_cleanup, "save_volatility_campaigns",
        lambda rows, **_k: saved.setdefault("volatility", rows),
    )
    store.save_campaign_states({
        "earnings_gamma:US.AAPL260729C300000": {"peak": 1},
        "volatility_long_gamma:US.NVDA260729P180000": {"peak": 1},
    })

    result = cleanup_expired_campaigns(store, Broker(), as_of=date(2026, 7, 30))

    assert result["removed_total"] == 2
    assert saved["gamma"] == []
    assert saved["volatility"] == []
    assert store.campaign_states() == {}


def test_cleanup_keeps_expired_campaign_with_broker_position(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    volatility_saved = [{"code": "US.NVDA260729P180000"}]
    saved = {}
    monkeypatch.setattr(campaign_cleanup, "load_gamma_campaigns", lambda: [])
    monkeypatch.setattr(campaign_cleanup, "load_volatility_campaigns", lambda *_a, **_k: list(volatility_saved))
    monkeypatch.setattr(campaign_cleanup, "save_gamma_campaigns", lambda rows: saved.setdefault("gamma", rows))
    monkeypatch.setattr(
        campaign_cleanup, "save_volatility_campaigns",
        lambda rows, **_k: saved.setdefault("volatility", rows),
    )
    broker = Broker(positions=[{"code": "US.NVDA260729P180000", "qty": 1}])

    result = cleanup_expired_campaigns(store, broker, as_of=date(2026, 7, 30))

    assert result["removed_total"] == 0
    assert result["skipped_total"] == 1
    assert "volatility" not in saved
