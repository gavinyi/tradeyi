from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.operational_readiness import (
    apply_dynamic_allocation, audit_streamlit_pages, automatic_recovery_cycle,
    dynamic_allocation_plan, idempotency_audit, localized_incidents,
    observability_snapshot, operations_report, paper_e2e_check,
    strategy_paper_acceptance,
)
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="稳定性运营中心", page_icon="🫀", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("稳定性与自动化运营中心")
st.caption("集中管理页面巡检、模拟交易验收、运行指标、故障恢复、动态资金建议和每日运营报告。所有交易验证均为隔离演练或富途模拟盘。")

page_tab, e2e_tab, metrics_tab, incident_tab, allocation_tab, acceptance_tab, report_tab = st.tabs(
    ["页面巡检", "端到端验收", "运行指标", "事件与恢复", "动态资金", "模拟验收", "运营报告"]
)

with page_tab:
    page_result = audit_streamlit_pages()
    with st.container(horizontal=True):
        st.metric("检查页面", page_result["pages_checked"], border=True)
        st.metric("错误", page_result["errors"], border=True)
        st.metric("警告", page_result["warnings"], border=True)
        st.metric("结果", "通过" if page_result["passed"] else "未通过", border=True)
    if page_result["issues"]:
        st.dataframe(pd.DataFrame(page_result["issues"]), hide_index=True)
    else:
        st.success("全部页面通过静态兼容性检查。")

with e2e_tab:
    st.caption("在临时数据库和内置模拟券商中验证信号、风控、幂等、成交恢复、急停和组合保护腿顺序，不会提交真实模拟账户订单。")
    if st.button("运行隔离端到端验收", icon=":material/checklist:"):
        st.session_state.paper_e2e_result = paper_e2e_check(store)
    e2e = st.session_state.get("paper_e2e_result")
    if e2e:
        st.metric("验收结果", "通过" if e2e["passed"] else "失败", border=True)
        st.dataframe(pd.DataFrame(e2e["details"]), hide_index=True)

with metrics_tab:
    metrics = observability_snapshot(store, record=False)
    with st.container(horizontal=True):
        st.metric("24 小时任务", metrics["task_runs_24h"], border=True)
        st.metric("任务失败", metrics["task_failures_24h"], border=True)
        st.metric("24 小时订单", metrics["orders_24h"], border=True)
        st.metric("未知订单", metrics["unknown_24h"], border=True)
        st.metric("待处理事件", metrics["open_incidents"], border=True)
    latest = pd.DataFrame(metrics["latest_tasks"])
    if not latest.empty:
        st.dataframe(china_time_columns(latest), hide_index=True)

with incident_tab:
    with st.container(horizontal=True):
        if st.button("执行安全自动恢复", disabled=not admin, icon=":material/healing:"):
            st.session_state.recovery_result = automatic_recovery_cycle(store)
        if st.button("立即执行幂等审计", icon=":material/fingerprint:"):
            st.session_state.idempotency_result = idempotency_audit(store)
    if st.session_state.get("recovery_result"):
        st.json(st.session_state.recovery_result)
    if st.session_state.get("idempotency_result"):
        result = st.session_state.idempotency_result
        (st.success if result["passed"] else st.error)(
            "订单幂等审计通过。" if result["passed"] else "发现重复交易标识，请立即检查。"
        )
    incidents = pd.DataFrame(localized_incidents(store))
    if incidents.empty:
        st.success("当前没有运维事件。")
    else:
        st.dataframe(incidents, hide_index=True)

with allocation_tab:
    plan = dynamic_allocation_plan(store, record=False)
    allocation = pd.DataFrame(plan["recommendations"])
    st.metric("建议现金缓冲", f"{plan['cash_reserve_ratio']:.1%}", border=True)
    if not allocation.empty:
        display = allocation[["strategy_id", "capital_ratio", "recommended_capital_ratio",
                              "max_loss_ratio", "recommended_max_loss_ratio", "factor", "reason"]].rename(columns={
            "strategy_id": "策略", "capital_ratio": "当前资金配额",
            "recommended_capital_ratio": "建议资金配额", "max_loss_ratio": "当前损失预算",
            "recommended_max_loss_ratio": "建议损失预算", "factor": "调整系数", "reason": "原因",
        })
        st.dataframe(display, hide_index=True, column_config={
            key: st.column_config.NumberColumn(format="percent")
            for key in ["当前资金配额", "建议资金配额", "当前损失预算", "建议损失预算", "调整系数"]
        })
    st.warning("系统只自动生成建议，不会自行扩大或修改资金配额。应用建议必须管理员确认。")
    if st.button("应用当前动态资金建议", type="primary", disabled=not admin):
        apply_dynamic_allocation(store, plan, "streamlit_admin")
        st.success("建议已应用到模拟盘策略风险预算。")
        st.rerun()

with acceptance_tab:
    strategies = sorted({plugin.strategy_id for plugin in strategy_plugins()})
    rows = []
    for strategy in strategies:
        result = strategy_paper_acceptance(store, strategy)
        for name, gate in result["gates"].items():
            rows.append({"策略": strategy, "验收项": name, "当前值": gate["value"],
                         "要求": gate["required"], "结果": "通过" if gate["passed"] else "未通过"})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("验收只评价模拟运行质量，不会触发实盘，也不会自动上线策略。")

with report_tab:
    if st.button("生成最新运营报告", type="primary", icon=":material/description:"):
        st.session_state.operations_report = operations_report(store)
    report = st.session_state.get("operations_report")
    if report:
        st.markdown(report["markdown"])
        st.download_button("下载 Markdown", report["markdown"], "模拟盘每日运营报告.md", "text/markdown",
                           icon=":material/download:")
    else:
        st.info("点击按钮生成包含任务、订单、页面、幂等和恢复状态的中文运营报告。")
