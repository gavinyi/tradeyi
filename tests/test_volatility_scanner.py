from __future__ import annotations

import pandas as pd

import futu_quant.volatility_scanner as scanner


class FakeStore:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.decisions: list[dict] = []
        self.heartbeats: list[dict] = []
        self.audits: list[dict] = []

    def heartbeat(self, strategy_id, status, message, pid):
        self.heartbeats.append({
            "strategy_id": strategy_id, "status": status,
            "message": message, "pid": pid,
        })

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def decision(self, strategy_id, symbol, decision, reason, payload=None):
        self.decisions.append({
            "strategy_id": strategy_id, "symbol": symbol,
            "decision": decision, "reason": reason, "payload": payload,
        })

    def decision_if_changed(self, strategy_id, symbol, decision, reason,
                            payload=None, repeat_after_seconds=900):
        self.decision(strategy_id, symbol, decision, reason, payload)
        return True

    def audit(self, event_type, payload, actor):
        self.audits.append({
            "event_type": event_type, "payload": payload, "actor": actor,
        })


class FakeBroker:
    def __init__(self) -> None:
        self.snapshot_requests: list[tuple[str, ...]] = []
        self.catalog_requested = False

    def security_catalog(self):
        self.catalog_requested = True
        return pd.DataFrame({"code": ["US.AAPL"]})

    def snapshots_tolerant(self, symbols):
        self.snapshot_requests.append(tuple(symbols))
        return pd.DataFrame([
            {"code": symbol, "last_price": 100.0, "prev_close_price": 98.0}
            for symbol in symbols
        ])

    def snapshots(self, symbols):
        self.snapshot_requests.append(tuple(symbols))
        return pd.DataFrame([
            {"code": "US.SPY", "last_price": 500.0, "prev_close_price": 500.0}
        ])

    def candles(self, symbol, count=120):
        return pd.DataFrame({"close": [100.0, 101.0, 102.0]})


def test_scanner_defaults_to_configured_volatility_symbols(monkeypatch):
    broker = FakeBroker()
    store = FakeStore()
    monkeypatch.setattr(scanner, "load_volatility_symbols", lambda: ("US.NVDA",))
    monkeypatch.setattr(
        scanner, "volatility_threshold_profile",
        lambda _bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
    )
    monkeypatch.setattr(
        scanner,
        "volatility_regime",
        lambda *_args: {
            "active": False, "reason": "未触发", "checks": [],
            "intraday_return": 0.0, "zscore_20d": 0.0,
            "atr_ratio": 0.0, "volume_ratio": 1.0,
            "direction_score": 0.0,
        },
    )

    results = scanner.scan_volatility_watchlist(broker, store)

    assert [item["symbol"] for item in results] == ["US.NVDA"]
    assert broker.catalog_requested is False
    assert ("US.NVDA",) in broker.snapshot_requests


def test_active_signal_creates_campaign_and_enables_auto_monitor(monkeypatch):
    broker = FakeBroker()
    store = FakeStore()
    saved_campaigns: list[dict] = []

    class FakeAlertManager:
        def __init__(self, _store):
            pass

        def send(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(scanner, "load_volatility_symbols", lambda: ("US.AMD",))
    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda: [])
    monkeypatch.setattr(scanner, "AlertManager", FakeAlertManager)
    monkeypatch.setattr(
        scanner, "volatility_threshold_profile",
        lambda _bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
    )
    monkeypatch.setattr(
        scanner,
        "volatility_regime",
        lambda *_args: {
            "active": True, "reason": "全部动态波动条件已通过", "checks": [],
            "intraday_return": 0.04, "zscore_20d": 2.5,
            "atr_ratio": 1.8, "volume_ratio": 2.0,
            "direction_score": 3.0,
        },
    )
    monkeypatch.setattr(
        scanner,
        "roll_campaign",
        lambda _broker, seed, _store: (
            {**seed, "code": "US.AMD260724C180000"},
            "已选择质量最高的看涨合约",
        ),
    )
    monkeypatch.setattr(
        scanner,
        "upsert_campaign",
        lambda _key, campaign: saved_campaigns.append(campaign),
    )

    results = scanner.scan_volatility_watchlist(broker, store)

    assert results[0]["active"] is True
    assert results[0]["campaign_created"] is True
    assert results[0]["campaign_code"] == "US.AMD260724C180000"
    assert saved_campaigns[0]["code"] == "US.AMD260724C180000"
    assert saved_campaigns[0]["max_hold_hours"] == 24
    assert saved_campaigns[0]["minimum_hold_minutes"] == 90
    assert saved_campaigns[0]["iv_decline_exit_ratio"] == .20
    assert saved_campaigns[0]["efficiency_decline_exit_ratio"] == .40
    assert saved_campaigns[0]["contraction_confirmations_required"] == 3
    assert "strategy_execution_mode:volatility_long_gamma" not in store.settings
    assert store.settings["strategy_enabled:Volatility Long Gamma"] == "true"
    assert any(
        item["event_type"] == "VOLATILITY_AUTO_CAMPAIGN_CREATED"
        for item in store.audits
    )


def test_active_signal_records_visible_rejection_when_no_contract_passes(monkeypatch):
    broker = FakeBroker()
    store = FakeStore()

    class FakeAlertManager:
        def __init__(self, _store):
            pass

        def send(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda: [])
    monkeypatch.setattr(scanner, "AlertManager", FakeAlertManager)
    monkeypatch.setattr(
        scanner, "volatility_threshold_profile",
        lambda _bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
    )
    monkeypatch.setattr(
        scanner, "volatility_regime",
        lambda *_args: {
            "active": True, "reason": "全部条件通过", "checks": [],
            "intraday_return": .04, "zscore_20d": 2.5, "atr_ratio": 2,
            "volume_ratio": 2, "direction_score": 3,
        },
    )
    monkeypatch.setattr(
        scanner, "roll_campaign",
        lambda _broker, seed, _store: ({
            **seed,
            "_roll_diagnostics": {
                "top_candidates": [{"code": "US.AMD-X", "reason": "权利金超预算"}],
            },
        }, "没有合约通过权利金预算"),
    )

    results = scanner.scan_volatility_watchlist(broker, store, ("US.AMD",))

    assert results[0]["active"] is True
    assert results[0]["campaign_created"] is False
    assert results[0]["campaign_reason"] == "没有合约通过权利金预算"
    assert store.decisions[-1]["decision"] == "CANDIDATE_BLOCKED"
    assert any(
        item["event_type"] == "VOLATILITY_AUTO_CAMPAIGN_REJECTED"
        for item in store.audits
    )


def test_rejected_candidate_enters_cooldown_until_signal_strengthens(monkeypatch):
    broker = FakeBroker()
    store = FakeStore()
    roll_calls = []

    class FakeAlertManager:
        def __init__(self, _store):
            pass

        def send(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda: [])
    monkeypatch.setattr(scanner, "AlertManager", FakeAlertManager)
    monkeypatch.setattr(
        scanner, "volatility_threshold_profile",
        lambda _bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
    )
    monkeypatch.setattr(
        scanner, "volatility_regime",
        lambda *_args: {
            "active": True, "reason": "全部条件通过", "checks": [],
            "intraday_return": .04, "zscore_20d": 2.5, "atr_ratio": 2,
            "volume_ratio": 2, "direction_score": 3,
        },
    )

    def reject(_broker, seed, _store):
        roll_calls.append(seed)
        return {**seed, "_roll_diagnostics": {}}, "IV 成本过高"

    monkeypatch.setattr(scanner, "roll_campaign", reject)

    scanner.scan_volatility_watchlist(broker, store, ("US.AMD",))
    second = scanner.scan_volatility_watchlist(broker, store, ("US.AMD",))

    assert len(roll_calls) == 1
    assert "冷却中" in second[0]["campaign_reason"]


def test_scanner_limits_broker_snapshot_to_scanned_symbols(monkeypatch):
    broker = FakeBroker()
    store = FakeStore()
    monkeypatch.setenv("RADAR_MAX_SYMBOLS_PER_SCAN", "5")
    symbols = tuple(f"US.TEST{i}" for i in range(10))
    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda: [])
    monkeypatch.setattr(scanner, "volatility_threshold_profile", lambda _: {
        "entry_zscore": 2.0, "minimum_underlying_move": .02,
    })
    monkeypatch.setattr(scanner, "volatility_regime", lambda *_: {
        "active": False, "reason": "等待触发", "checks": [],
        "intraday_return": .0, "zscore_20d": .0, "atr_ratio": .0,
        "volume_ratio": 1.0, "direction_score": .0,
    })

    results = scanner.scan_volatility_watchlist(broker, store, symbols)

    assert len(results) == 5
    assert len(broker.snapshot_requests[0]) == 5
    assert set(broker.snapshot_requests[0]) == {result["symbol"] for result in results}
