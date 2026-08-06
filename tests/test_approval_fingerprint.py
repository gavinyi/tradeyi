from futu_quant.approval import (
    _PROCESS_FINGERPRINTS, approval_version_status,
    publish_approval_runtime_manifest, strategy_fingerprint,
)
from futu_quant.store import TradingStore


def test_volatility_fingerprint_ignores_live_campaign_and_universe_state(
    tmp_path, monkeypatch,
):
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr("futu_quant.approval.TradingStore", lambda: store)
    monkeypatch.setattr(
        "futu_quant.config.refresh_runtime_environment", lambda override=False: {}
    )
    monkeypatch.setenv("VOLATILITY_CAMPAIGNS", '[{"code":"US.A"}]')
    monkeypatch.setenv("VOLATILITY_SYMBOLS", "US.AAPL")
    first = strategy_fingerprint("volatility_long_gamma")

    monkeypatch.setenv("VOLATILITY_CAMPAIGNS", '[{"code":"US.B"}]')
    monkeypatch.setenv("VOLATILITY_SYMBOLS", "US.GOOG")
    second = strategy_fingerprint("volatility_long_gamma")

    assert first == second


def test_volatility_fingerprint_changes_on_policy_revision(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr("futu_quant.approval.TradingStore", lambda: store)
    monkeypatch.setattr(
        "futu_quant.config.refresh_runtime_environment", lambda override=False: {}
    )
    first = strategy_fingerprint("volatility_long_gamma")
    store.set_setting("strategy_policy_revision:volatility_long_gamma", "2")
    second = strategy_fingerprint("volatility_long_gamma")

    assert first != second


def test_approval_status_detects_running_service_source_drift(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr(
        "futu_quant.config.refresh_runtime_environment", lambda override=False: {}
    )
    _PROCESS_FINGERPRINTS.clear()
    manifest = publish_approval_runtime_manifest(
        store, 1234, ("volatility_long_gamma",),
    )
    store.approve_strategy(
        "volatility_long_gamma",
        manifest["fingerprints"]["volatility_long_gamma"],
        "test", True, True, True, True,
    )

    before = approval_version_status(store, "volatility_long_gamma")
    assert before["runtime_aligned"] is True
    assert before["approved"] is True

    store.set_setting("strategy_policy_revision:volatility_long_gamma", "2")
    after = approval_version_status(store, "volatility_long_gamma")
    assert after["runtime_aligned"] is False
    assert after["approved"] is False
    assert after["runtime_fingerprint"] != after["current_fingerprint"]
