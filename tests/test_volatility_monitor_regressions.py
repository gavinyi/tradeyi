"""Offline volatility monitor regressions: fake broker and temporary ledger only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
from futu_quant.alerts import AlertManager
from futu_quant.store import TradingStore
from futu_quant.volatility_optimizer import decide_volatility_action

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_service  # noqa: E402
import run_volatility_strategy as runner  # noqa: E402
from run_gamma_strategy import _campaign_naked_short_rows  # noqa: E402


def test_protected_backspread_is_not_misclassified_as_naked_short():
    campaign = {
        "legs": [
            {"code": "US.GLD261009P380000", "side": "SELL", "ratio": 1},
            {"code": "US.GLD261009P375000", "side": "BUY", "ratio": 2},
        ],
    }
    positions = pd.DataFrame([
        {"code": "US.GLD261009P380000", "qty": -1},
        {"code": "US.GLD261009P375000", "qty": 2},
    ])
    assert _campaign_naked_short_rows(campaign, positions).empty


def test_unprotected_short_remains_naked_short():
    campaign = {
        "legs": [
            {"code": "US.GLD261009P380000", "side": "SELL", "ratio": 1},
            {"code": "US.GLD261009P375000", "side": "BUY", "ratio": 2},
        ],
    }
    positions = pd.DataFrame([
        {"code": "US.GLD261009P380000", "qty": -1},
        {"code": "US.GLD261009P375000", "qty": 0},
    ])
    rows = _campaign_naked_short_rows(campaign, positions)
    assert rows["code"].tolist() == ["US.GLD261009P380000"]


def test_single_contract_runner_evaluates_tier_lock_before_hold():
    now = datetime.now(timezone.utc)
    kwargs = dict(
        now=now, opened_at=now - timedelta(minutes=20), quantity=1,
        stages_filled=1, max_stages=2, pnl_ratio=.18, peak_pnl_ratio=.35,
        drawdown_from_peak=.16, directional_confirmation=True, signal_active=True,
        profit_target=.50, stop_loss=.50, trailing_stop=.50, max_hold_hours=12,
        profit_protection_drawdown=.90, single_contract_take_profit=.18,
    )
    action, reason = decide_volatility_action(**kwargs)
    assert action == "SELL"
    assert "阶梯锁盈" in reason
    action, _ = decide_volatility_action(**{**kwargs, "pnl_ratio": .34})
    assert action == "HOLD"  # runner must not pyramid after reaching the single-lot target


def test_campaign_error_is_not_reported_as_success(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    code = "US.AAPL271217C200000"
    campaign = {"code": code, "underlying": "US.AAPL", "strike_time": "2027-12-17",
                "enabled": True}

    class FakeBroker:
        def __init__(self, _settings):
            pass

        def positions(self):
            return pd.DataFrame([{"code": code, "qty": 1}])

        def orders(self):
            return pd.DataFrame()

    class FakeExecutor:
        def __init__(self, _broker):
            self.store = store

    monkeypatch.setattr(runner.Settings, "load", lambda: SimpleNamespace(enable_paper_orders=True))
    monkeypatch.setattr(runner, "FutuPaperBroker", FakeBroker)
    monkeypatch.setattr(runner, "ExecutionManager", FakeExecutor)
    monkeypatch.setattr(runner, "load_volatility_campaigns", lambda: [campaign])
    monkeypatch.setattr(runner, "load_state", lambda: {})
    monkeypatch.setattr(runner, "save_state", lambda _state: None)
    monkeypatch.setattr(runner, "stop_requested", lambda: False)
    monkeypatch.setattr(runner, "manage_volatility_orphans", lambda **_kwargs: [])
    monkeypatch.setattr(runner, "protected_volatility_state_keys", lambda *_args: set())
    monkeypatch.setattr(runner, "run_campaign", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("mock failure")))
    monkeypatch.setattr(runner, "close_stale_expired_combos", lambda *_args: 0)
    monkeypatch.setattr(runner, "find_open_combo_for_campaign", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "prune_campaign_states", lambda *_args: None)
    monkeypatch.setattr(store, "shadow_position", lambda *_args: (0, 0))

    succeeded = run_service.run_task(
        store, AlertManager(store), "volatility_long_gamma",
        lambda: runner.run_once(reconcile=False),
    )
    assert not succeeded
    assert store.task_runs(limit=1)[0]["status"] == "ERROR"
    events = store.operational_events(limit=20)
    assert any(event["event_type"] == "CAMPAIGN_EXECUTION_FAILED" for event in events)
    assert any(event["event_type"] == "CAMPAIGN_EXECUTION_DURATION" for event in events)


def test_held_exit_precedes_flat_expression_review(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    held = {"code": "US.AAPL271217C200000", "underlying": "US.AAPL",
            "strike_time": "2027-12-17", "enabled": True}
    flat = {"code": "US.MSFT271217C400000", "underlying": "US.MSFT",
            "strike_time": "2027-12-17", "enabled": True}
    calls = []

    class FakeBroker:
        def __init__(self, _settings):
            pass

        def positions(self):
            return pd.DataFrame([{"code": held["code"], "qty": 1}])

        def orders(self):
            return pd.DataFrame()

    class FakeExecutor:
        def __init__(self, _broker):
            self.store = store

    monkeypatch.setattr(runner.Settings, "load", lambda: SimpleNamespace(enable_paper_orders=True))
    monkeypatch.setattr(runner, "FutuPaperBroker", FakeBroker)
    monkeypatch.setattr(runner, "ExecutionManager", FakeExecutor)
    monkeypatch.setattr(runner, "load_volatility_campaigns", lambda: [flat, held])
    monkeypatch.setattr(runner, "load_state", lambda: {})
    monkeypatch.setattr(runner, "save_state", lambda _state: None)
    monkeypatch.setattr(runner, "stop_requested", lambda: False)
    monkeypatch.setattr(runner, "manage_volatility_orphans", lambda **_kwargs: [])
    monkeypatch.setattr(runner, "protected_volatility_state_keys", lambda *_args: set())
    monkeypatch.setattr(runner, "run_campaign", lambda _broker, _executor, campaign, *_args, **_kwargs: calls.append(("execute", campaign["code"])))
    monkeypatch.setattr(runner, "roll_campaign", lambda _broker, campaign, _store: (calls.append(("review", campaign["code"])) or campaign, "same"))
    monkeypatch.setattr(runner, "should_replace_campaign", lambda *_args: (False, "same"))
    monkeypatch.setattr(runner, "close_stale_expired_combos", lambda *_args: 0)
    monkeypatch.setattr(runner, "find_open_combo_for_campaign", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "prune_campaign_states", lambda *_args: None)
    monkeypatch.setattr(store, "shadow_position", lambda *_args: (0, 0))

    runner.run_once(reconcile=False)
    assert calls.index(("execute", held["code"])) < calls.index(("review", flat["code"]))
    assert calls.count(("execute", held["code"])) == 1


def test_decision_if_changed_deduplicates_in_memory():
    test_db = Path("data/test_decision_dedup.db")
    try:
        test_db.unlink(missing_ok=True)
        store = TradingStore(str(test_db))
        recorded1 = store.decision_if_changed(
            "volatility_long_gamma", "US.TEST", "BLOCKED", "价差过大", repeat_after_seconds=900,
        )
        assert recorded1 is True
        recorded2 = store.decision_if_changed(
            "volatility_long_gamma", "US.TEST", "BLOCKED", "价差过大", repeat_after_seconds=900,
        )
        assert recorded2 is False
        recorded3 = store.decision_if_changed(
            "volatility_long_gamma", "US.TEST", "BLOCKED", "量比不足", repeat_after_seconds=900,
        )
        assert recorded3 is True
    finally:
        test_db.unlink(missing_ok=True)

