from futu_quant.store import TradingStore


def test_repeated_hold_reason_is_deduplicated(tmp_path):
    store = TradingStore(tmp_path / "decisions.db")
    assert store.decision_if_changed("strategy", "US.AAPL", "HOLD", "等待信号")
    assert not store.decision_if_changed("strategy", "US.AAPL", "HOLD", "等待信号")
    assert store.decision_if_changed("strategy", "US.AAPL", "HOLD", "等待新信号")
    assert len(store.decisions()) == 2
