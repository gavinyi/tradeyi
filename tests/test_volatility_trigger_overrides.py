from futu_quant.store import TradingStore
from futu_quant.volatility_trigger_config import (
    MOVE_KEY,
    ZSCORE_KEY,
    resolve_volatility_trigger_profile,
)

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"
BASE = {"entry_zscore": 2.0, "minimum_underlying_move": 0.02}


def _set(store, tenant, key, value):
    store.set_setting(f"{key}:{tenant}", str(value))


def test_real_override_can_tighten_but_cannot_loosen(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _set(store, REAL, ZSCORE_KEY, 1.5)
    _set(store, REAL, MOVE_KEY, 0.03)

    result = resolve_volatility_trigger_profile(store, REAL, BASE)

    assert result == {"entry_zscore": 2.0, "minimum_underlying_move": 0.03}
    events = store.operational_events(limit=20)
    assert any(row["event_type"] == "REAL_TRIGGER_OVERRIDE_LOOSEN_REJECTED" for row in events)


def test_paper_override_may_relax_and_is_audited(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _set(store, PAPER, ZSCORE_KEY, 1.5)
    _set(store, PAPER, MOVE_KEY, 0.01)

    result = resolve_volatility_trigger_profile(store, PAPER, BASE)

    assert result == {"entry_zscore": 1.5, "minimum_underlying_move": 0.01}
    audits = store.audit_events(limit=20)
    relaxed = [row for row in audits if row["event_type"] == "PAPER_TRIGGER_OVERRIDE_RELAXED"]
    assert len(relaxed) == 2


def test_tenant_values_are_isolated(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _set(store, PAPER, ZSCORE_KEY, 1.4)
    _set(store, REAL, ZSCORE_KEY, 2.6)

    paper = resolve_volatility_trigger_profile(store, PAPER, BASE)
    real = resolve_volatility_trigger_profile(store, REAL, BASE)

    assert paper["entry_zscore"] == 1.4
    assert real["entry_zscore"] == 2.6
