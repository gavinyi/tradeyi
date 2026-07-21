from __future__ import annotations

from datetime import datetime, timezone

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
