from __future__ import annotations

import json

import pytest

from futu_quant.fee_calibration import evaluate_fee_calibration, record_fee_calibration
from futu_quant.store import TradingStore


def test_fee_calibration_healthy_within_fifteen_percent():
    result = evaluate_fee_calibration(110.0, 100.0)
    assert result.status == "HEALTHY"
    assert result.factor == pytest.approx(1.1)


def test_fee_calibration_degraded_outside_fifteen_percent():
    result = evaluate_fee_calibration(120.0, 100.0)
    assert result.status == "DEGRADED"
    assert result.deviation == pytest.approx(0.2)
    assert "±15%" in result.reason


def test_fee_calibration_fail_closed_without_estimate():
    result = evaluate_fee_calibration(100.0, 0.0)
    assert result.status == "DEGRADED"
    assert result.factor is None


def test_fee_calibration_records_tenant_evidence_without_auto_apply(tmp_path):
    store = TradingStore(tmp_path / "fee-calibration.db")
    result = record_fee_calibration(
        store,
        tenant_id="futu_real_master",
        quarter="2026-Q3",
        statement_total=120.0,
        estimated_total=100.0,
    )
    assert result.status == "DEGRADED"
    events = [
        event for event in store.operational_events()
        if event["event_type"] == "QUARTERLY_FEE_CALIBRATION"
    ]
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["tenant_id"] == "futu_real_master"
    assert payload["quarter"] == "2026-Q3"
    assert payload["auto_apply"] is False
