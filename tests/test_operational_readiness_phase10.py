from __future__ import annotations

from datetime import datetime, timezone
import inspect

from futu_quant.operational_readiness import (
    audit_streamlit_pages, automatic_recovery_cycle, dynamic_allocation_plan,
    idempotency_audit, operations_report, paper_e2e_check, strategy_paper_acceptance,
)
from futu_quant.portfolio_allocator import ensure_default_budgets
from futu_quant.store import TradingStore


def test_page_audit_detects_invalid_streamlit_usage(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "app.py").write_text("import streamlit as st\nst.button('ok', icon=':material/refresh:')")
    (tmp_path / "pages" / "bad.py").write_text(
        "import streamlit as st\nst.column_config.NumberColumn('x', 0, 1)"
    )
    result = audit_streamlit_pages(tmp_path)
    assert not result["passed"]
    assert result["errors"] == 1


def test_page_audit_detects_form_and_result_state_key_collision(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "app.py").write_text(
        "import streamlit as st\n"
        "with st.form('results'):\n    st.form_submit_button('go')\n"
        "st.session_state.results = {'ok': True}\n"
    )
    result = audit_streamlit_pages(tmp_path)
    assert not result["passed"]
    assert "表单键冲突" in result["issues"][0]["问题"]


def test_isolated_paper_e2e_and_idempotency(tmp_path):
    store = TradingStore(tmp_path / "e2e.db")
    assert paper_e2e_check(store)["passed"]
    assert idempotency_audit(store)["passed"]


def test_automatic_recovery_closes_task_incident_after_success(tmp_path):
    store = TradingStore(tmp_path / "recovery.db")
    store.record_incident("STRATEGY_ERROR:test_task", "ERROR", "失败")
    run_id = store.start_task_run("test_task", 10, 1)
    store.finish_task_run(run_id, "SUCCESS", 0.1)
    result = automatic_recovery_cycle(store)
    assert "STRATEGY_ERROR:test_task" in result["recovered"]
    assert store.incidents()[0]["status"] == "RECOVERED"


def test_automatic_recovery_closes_stale_transient_incidents(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "transient-recovery.db")
    monkeypatch.setenv("TRANSIENT_INCIDENT_AUTO_RECOVER_HOURS", "1")
    store.record_incident("ORDER_FAILED", "ERROR", "一次性下单失败")
    stale = "2026-01-01T00:00:00+00:00"
    with store.connection() as connection:
        connection.execute(
            "UPDATE incidents SET last_seen=? WHERE incident_key=?",
            (stale, "ORDER_FAILED"),
        )

    result = automatic_recovery_cycle(store)

    assert "ORDER_FAILED" in result["recovered"]
    assert store.incidents()[0]["status"] == "RECOVERED"


def test_automatic_recovery_keeps_legacy_incidents_open_without_explicit_recovery(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "legacy-pending.db")
    monkeypatch.setenv("TRANSIENT_INCIDENT_AUTO_RECOVER_HOURS", "1")
    stale = "2026-01-01T00:00:00+00:00"
    for key in ("VOLATILITY_CAPACITY_PAUSED", "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK"):
        store.record_incident(key, "ERROR", "旧版本降级事件")
        with store.connection() as connection:
            connection.execute(
                "UPDATE incidents SET last_seen=? WHERE incident_key=?", (stale, key)
            )

    result = automatic_recovery_cycle(store)

    assert not result["recovered"]
    assert set(result["pending"]) >= {
        "VOLATILITY_CAPACITY_PAUSED", "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK",
    }


def test_automatic_recovery_closes_legacy_incidents_with_explicit_recovery(tmp_path):
    store = TradingStore(tmp_path / "legacy-recovered.db")
    for key in ("VOLATILITY_CAPACITY_PAUSED", "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK"):
        store.record_incident(key, "ERROR", "旧版本降级事件")
        store.operational_event("test", f"{key}_RECOVERED", "INFO", {"confirmed": True})

    result = automatic_recovery_cycle(store)

    assert set(result["recovered"]) >= {
        "VOLATILITY_CAPACITY_PAUSED", "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK",
    }
    assert not store.incidents("OPEN")
    audits = store.audit_events(limit=20)
    assert any(row["event_type"] == "INCIDENT_RECOVERED" for row in audits)


def test_automatic_recovery_keeps_real_risk_incidents_open(tmp_path):
    store = TradingStore(tmp_path / "risk-recovery.db")
    store.record_incident("PORTFOLIO_STRESS_BREACH", "ERROR", "压力超限")
    with store.connection() as connection:
        connection.execute(
            "UPDATE incidents SET last_seen=? WHERE incident_key=?",
            ("2026-01-01T00:00:00+00:00", "PORTFOLIO_STRESS_BREACH"),
        )

    result = automatic_recovery_cycle(store)

    assert "PORTFOLIO_STRESS_BREACH" in result["pending"]
    assert store.incidents()[0]["status"] == "OPEN"


def test_paper_acceptance_and_dynamic_allocation(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_ACCEPTANCE_MIN_DAYS", "1")
    monkeypatch.setenv("PAPER_ACCEPTANCE_MIN_ORDERS", "1")
    store = TradingStore(tmp_path / "acceptance.db")
    intent, _ = store.create_intent("alpha", "one:PAPER", "US.NVDA", "BUY", 1, 100)
    store.update_shadow_intent(intent["id"], "FILLED", 1, 100, 0)
    assert strategy_paper_acceptance(store, "alpha")["passed"]
    ensure_default_budgets(store)
    plan = dynamic_allocation_plan(store)
    assert 0 <= plan["cash_reserve_ratio"] <= 1
    assert sum(item["recommended_capital_ratio"] for item in plan["recommendations"] if item["enabled"]) <= 0.900001


def test_operations_report_is_chinese_and_structured(tmp_path):
    store = TradingStore(tmp_path / "report.db")
    run_id = store.start_task_run("healthy", 10, 1)
    store.finish_task_run(run_id, "SUCCESS", 0.1)
    report = operations_report(store)
    assert "模拟盘每日运营报告" in report["markdown"]
    assert report["status"] in {"HEALTHY", "WARNING"}
    assert datetime.fromisoformat(store.operational_events()[0]["created_at"]).tzinfo == timezone.utc


def test_operations_report_warns_when_incident_is_pending(tmp_path):
    store = TradingStore(tmp_path / "warning-report.db")
    store.record_incident("MANUAL_REVIEW", "ERROR", "需要人工复核")
    assert operations_report(store)["status"] == "WARNING"
