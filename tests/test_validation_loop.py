from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.combo_execution import create_combo
from futu_quant.store import TradingStore
from futu_quant.validation_loop import (
    apply_auto_downgrade, calibrate_capacity, calibrate_scores,
    collect_combo_market_snapshots, combo_walk_forward, execution_bias,
    market_regime, quote_quality, strategy_drift, validation_report,
)


def test_market_regime_and_quote_quality_are_explainable():
    bars = pd.DataFrame({"close": [100 + index * 0.4 for index in range(30)]})
    detail = pd.DataFrame([
        {"mid": 1.0, "spread_pct": 0.10, "volume": 100, "open_interest": 1000, "quote_age_seconds": 1},
        {"mid": 2.0, "spread_pct": 0.05, "volume": 200, "open_interest": 2000, "quote_age_seconds": 1},
    ])

    assert market_regime(bars) == "TREND_UP"
    quality = quote_quality(detail)
    assert quality["grade"] == "A"
    assert quality["score"] == 100


def test_execution_bias_requires_shadow_and_paper_pairs(tmp_path):
    store = TradingStore(tmp_path / "bias.db")
    for mode, price in (("SHADOW", 100), ("PAPER", 101)):
        intent, _ = store.create_intent("sma", f"pair:{mode}", "US.NVDA", "BUY", 1, 100)
        store.update_shadow_intent(intent["id"], "FILLED", 1, price, 0)

    frame, summary = execution_bias(store)

    assert len(frame) == 1
    assert summary["sample_count"] == 1
    assert summary["status"] == "INSUFFICIENT"
    assert summary["mean_adverse_bias"] == pytest.approx(0.01)


def test_score_and_capacity_calibration_refuse_small_samples():
    score, score_summary = calibrate_scores(pd.DataFrame({"score": [80], "realized_return": [0.1]}))
    capacity = calibrate_capacity(pd.DataFrame())

    assert score.empty
    assert score_summary["status"] == "INSUFFICIENT"
    assert capacity["status"] == "INSUFFICIENT"
    assert capacity["recommended_volume_participation"] == 0.05


def test_drift_can_only_downgrade_paper_to_shadow(tmp_path):
    store = TradingStore(tmp_path / "drift.db")
    for index in range(30):
        intent, _ = store.create_intent("test_strategy", f"order:{index}:PAPER", "US.NVDA", "BUY", 1, 100)
        if index < 21:
            store.update_shadow_intent(intent["id"], "FILLED", 1, 100, 0)
        else:
            store.update_intent(intent["id"], "FAILED", error="test")
    store.set_setting("execution_mode", "PAPER")
    store.set_setting("validation_auto_downgrade", "true")
    drift = strategy_drift(store)

    changed = apply_auto_downgrade(store, drift)

    assert changed == ["test_strategy"]
    assert store.get_setting("strategy_execution_mode:test_strategy") == "SHADOW"
    assert apply_auto_downgrade(store, drift) == []


class SnapshotBroker:
    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol, "bid_price": 0.95, "ask_price": 1.05,
            "option_delta": 0.5, "option_gamma": 0.02, "option_theta": -0.03,
            "option_vega": 0.08, "option_volume": 1000, "option_open_interest": 5000,
            "update_time": pd.Timestamp.now(tz="America/New_York").isoformat(),
        } for symbol in symbols])

    def candles(self, symbol, count=60):
        return pd.DataFrame({"close": [100 + index * 0.1 for index in range(count)]})


def test_combo_snapshot_walk_forward_and_reports(tmp_path):
    store = TradingStore(tmp_path / "validation.db")
    legs = [
        {"role": "PROTECTIVE", "symbol": "US.NVDA260724C220000", "side": "BUY", "quantity": 1, "limit_price": 1.0},
        {"role": "PROTECTIVE", "symbol": "US.NVDA260724P200000", "side": "BUY", "quantity": 1, "limit_price": 1.0},
    ]
    combo_id = create_combo(store, {
        "strategy_id": "volatility_long_gamma", "name": "验证跨式", "underlying": "US.NVDA",
        "structure": "STRANGLE", "max_net_debit": 2.5, "min_net_credit": 0,
        "assembly_timeout_seconds": 300, "naked_timeout_seconds": 15, "auto_rollback": True,
    }, legs)
    assert collect_combo_market_snapshots(SnapshotBroker(), store) == 1
    snapshot = store.combo_market_snapshots(combo_id)[0]
    assert snapshot["quality_grade"] == "A"

    times = pd.date_range("2026-07-20 14:30", periods=24, freq="min", tz="UTC")
    histories = {
        leg["symbol"]: pd.DataFrame({
            "time_key": times, "bid": [0.9 + index * 0.01 for index in range(24)],
            "ask": [1.0 + index * 0.01 for index in range(24)], "quality_status": "GOOD",
        }) for leg in legs
    }
    windows, summary = combo_walk_forward(histories, legs, windows=2)
    assert len(windows) == 2
    assert summary["status"] == "READY"

    markdown, report_summary = validation_report(store, "DAILY")
    assert "中国标准时间" in markdown
    assert report_summary["combo_snapshot_count"] == 1
    assert len(store.validation_reports()) == 1
