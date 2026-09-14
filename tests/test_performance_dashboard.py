from __future__ import annotations

from pathlib import Path

from futu_quant.performance import performance_dashboard_data
from futu_quant.store import TradingStore


def test_dashboard_data_exposes_structure_and_risk_metrics(tmp_path):
    store = TradingStore(Path(tmp_path) / "dashboard.db")
    row, _ = store.create_intent(
        "volatility_long_gamma", "buy", "US.AAPL260918C200000", "BUY", 1, 2.0,
        {"multiplier": 100},
    )
    store.record_fill(
        row["id"], 1, 2.0, 1.0, source="TEST",
        filled_at="2026-09-01T14:00:00+00:00",
        quote={"bid_price": 1.9, "ask_price": 2.1}, multiplier=100,
    )
    store.record_equity(1000, 900, 100)
    data = performance_dashboard_data(store, period="daily")

    assert data["fills"].iloc[0]["标的"] == "US.AAPL"
    assert data["fills"].iloc[0]["期权结构"] == "CALL"
    assert "最大回撤" in data["metrics"]
