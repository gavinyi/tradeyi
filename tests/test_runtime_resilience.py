from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from futu_quant.store import TradingStore


def test_runtime_config_mutations_do_not_lose_concurrent_updates(tmp_path):
    store = TradingStore(tmp_path / "runtime.db")

    def add(value: int):
        store.mutate_runtime_config(
            "items", [], lambda current: sorted({*current, value}), "test",
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(add, range(20)))

    value, version = store.get_runtime_config("items")
    assert value == list(range(20))
    assert version == 20


def test_incident_is_aggregated_and_can_recover(tmp_path):
    store = TradingStore(tmp_path / "incidents.db")

    assert store.record_incident("opend", "ERROR", "断线") == (True, 1)
    assert store.record_incident("opend", "ERROR", "仍然断线") == (False, 2)
    assert store.recover_incident("opend")
    assert not store.recover_incident("opend")
    assert store.record_incident("opend", "ERROR", "再次断线") == (True, 3)


def test_campaign_state_upsert_preserves_other_strategy(tmp_path):
    store = TradingStore(tmp_path / "state.db")
    store.save_campaign_states({"earnings_gamma:a": {"peak": 1}})
    store.save_campaign_states({"volatility_long_gamma:b": {"peak": 2}})

    assert set(store.campaign_states()) == {
        "earnings_gamma:a", "volatility_long_gamma:b",
    }
