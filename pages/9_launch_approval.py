from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from futu_quant.approval import approval_version_status
from futu_quant.acceptance import strategy_acceptance
from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, save_runtime_settings
from futu_quant.execution_quality import execution_quality_report
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.stress import stress_test
from futu_quant.replay import run_resilience_drill
from futu_quant.service_control import restart as restart_service, status as service_status
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="上线审批", page_icon="✅", layout="wide")
settings = Settings.load()
render_sidebar(settings)
st.title("策略上线审批与验收")
store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

approval_tab, reconcile_tab, quality_tab, stress_tab = st.tabs(
    ["策略审批", "三账对账", "执行偏差", "压力测试"],
    default="策略审批", key="approval_tabs", on_change="rerun",
)

strategies = [
    "core_satellite", "earnings_gamma", "volatility_long_gamma", "SMA Cross", "RSI Mean Reversion",
    "20 Day Breakout", "earnings_gamma_manual",
]

with approval_tab:
    enforced = os.getenv("REQUIRE_STRATEGY_APPROVAL", "true").lower() == "true"
    selected_enforcement = st.toggle(
        "PAPER 新增仓位强制要求当前版本审批", value=enforced, disabled=not admin,
        help="审批失效时禁止 BUY/加仓，但仍允许已有仓位 SELL 退出。",
    )
    if selected_enforcement != enforced:
        save_runtime_settings({"REQUIRE_STRATEGY_APPROVAL": str(selected_enforcement).lower()})
        store.audit(
            "APPROVAL_ENFORCEMENT_CHANGED",
            {"actor": "streamlit_admin", "enabled": selected_enforcement},
        )
        st.rerun()
    approvals = {row["strategy_id"]: row for row in store.approvals()}

    with st.container(border=True):
        st.subheader("Core Satellite 快速上线")
        st.caption(
            "Core Satellite 已设为免版本审批策略。按钮只启用该策略并切换到富途 PAPER；"
            "急停、亏损线、仓位和购买力风控仍然生效。"
        )
        core_enabled = store.get_setting("strategy_enabled:Core Satellite", "false") == "true"
        core_mode = (
            store.get_setting("strategy_execution_mode:core_satellite", "")
            or store.get_setting("execution_mode", "PAPER")
        ).upper()
        with st.container(horizontal=True):
            st.metric("自动调度", "已启用" if core_enabled else "未启用", border=True)
            st.metric("执行模式", core_mode, border=True)
            if st.button(
                "一键上线 Core Satellite", type="primary", icon=":material/rocket_launch:",
                disabled=not admin or (core_enabled and core_mode == "PAPER"),
            ):
                store.set_setting("strategy_enabled:Core Satellite", "true")
                store.set_setting("strategy_execution_mode:core_satellite", "PAPER")
                store.audit(
                    "CORE_SATELLITE_QUICK_LAUNCH",
                    {"actor": "streamlit_admin", "mode": "PAPER", "approval_exempt": True},
                    "core_satellite",
                )
                st.success("Core Satellite 已启用并切换为 PAPER 模拟交易。")
                st.rerun()
        if store.kill_switch():
            st.warning("系统急停当前已开启；策略虽已上线，但不会新增仓位。")
        if store.get_setting("strategies_paused", "false") == "true":
            st.warning("全局策略暂停当前已开启；请先到风控中心恢复策略调度。")
    status_rows = []
    for strategy in strategies:
        version = approval_version_status(store, strategy)
        status_rows.append({
            "策略": strategy,
            "当前代码版本": version["current_fingerprint"][:12],
            "服务运行版本": version["runtime_fingerprint"][:12] or "未上报",
            "已批准版本": version["approved_fingerprint"][:12] or "未批准",
            "版本一致": "是" if version["exempt"] or version["runtime_aligned"] else "否",
            "审批状态": (
                "免审批" if version["exempt"] else
                "当前运行版本已批准" if version["approved"] else
                "服务需要重启" if version["runtime_fingerprint"] and not version["runtime_aligned"] else
                "未批准或服务未上报版本"
            ),
            "批准时间": approvals.get(strategy, {}).get("approved_at"),
        })
    st.dataframe(china_time_columns(pd.DataFrame(status_rows).rename(
        columns={"批准时间": "approved_at"}
    )).rename(columns={"approved_at": "批准时间（中国）"}), hide_index=True)
    strategy = st.selectbox("选择策略", strategies)
    selected_version = approval_version_status(store, strategy)
    selected_service = service_status()
    runtime_ready = bool(
        selected_version["exempt"] or (
            selected_service.running and selected_version["runtime_aligned"]
        )
    )
    if not selected_version["exempt"] and not selected_service.running:
        st.error("统一交易服务未运行，无法确认将要批准的实际运行版本。请先启动服务。")
    elif not selected_version["exempt"] and not selected_version["runtime_aligned"]:
        st.error(
            "当前代码与交易服务运行版本不一致。必须重启服务后再审批，"
            "否则页面批准与实际下单版本会再次错位。"
        )
        if st.button(
            "重启统一交易服务并同步版本", type="primary",
            icon=":material/restart_alt:", disabled=not admin,
        ):
            restarted = restart_service()
            if restarted.running:
                st.success(f"交易服务已重启，PID {restarted.pid}；请刷新后批准当前运行版本。")
            else:
                st.error(restarted.message)
    acceptance = strategy_acceptance(store, strategy)
    gate_names = {
        "shadow_days": "Shadow 有效天数", "shadow_orders": "Shadow 订单数",
        "fill_rate": "Shadow 成交率", "slippage": "平均不利滑点",
        "reconciliation": "对账差异数",
        "backtest": "回测验收", "drill": "故障演练", "risk_review": "风险复核",
    }
    # 兼容 Streamlit 热重载期间仍驻留的旧模块：服务错误即使存在于旧返回值中，
    # 也只能作为诊断，不能重新成为上线审批门槛。
    approval_gates = {
        key: gate for key, gate in acceptance["gates"].items()
        if key != "service_errors"
    }
    acceptance_passed = all(gate["passed"] for gate in approval_gates.values())
    st.dataframe(pd.DataFrame([
        {"验收项": gate_names.get(key, key), "结果": "通过" if gate["passed"] else "未通过",
         "当前值": gate["value"], "要求": gate["required"]}
        for key, gate in approval_gates.items()
    ]), hide_index=True)
    failed_gates = [gate_names.get(key, key) for key, gate in approval_gates.items() if not gate["passed"]]
    if failed_gates:
        st.warning("最终批准按钮暂不可用，还未通过：" + "、".join(failed_gates))
    elif not admin:
        st.warning("验收项已通过，但需要先完成管理员登录才能批准。")
    else:
        st.success("全部自动验收项已通过，可以填写理由并批准当前版本。")
    st.caption("观察天数、订单数、成交率、滑点和三账对账由系统自动计算。")
    diagnostic = acceptance.get("details", {})
    st.info(
        "系统日志会永久保留用于审计；服务错误数仅作为运行健康度诊断，"
        "不再作为上线审批的强制门槛。"
    )
    st.metric(
        "服务错误数（仅诊断）",
        int(diagnostic.get("service_errors", 0)),
        help=f"最近 {int(diagnostic.get('service_error_window_hours', 24))} 小时内未确认且未恢复的 ERROR。",
    )
    evidence_cols = st.columns(3)
    if evidence_cols[0].button("管理员手动登记回测", disabled=not admin,
                               help="推荐从策略回测页保存实际结果；此按钮仅用于外部回测结果。"):
        store.record_acceptance_evidence(
            strategy, "BACKTEST", True, details={"actor": "streamlit_admin"}
        )
        st.rerun()
    if evidence_cols[1].button("运行并登记故障演练", disabled=not admin):
        drill_result = run_resilience_drill()
        passed = not drill_result.empty and (drill_result["结果"] == "通过").all()
        store.record_acceptance_evidence(
            strategy, "DRILL", passed, details={"rows": drill_result.to_dict("records")}
        )
        st.rerun()
    if evidence_cols[2].button("确认风险参数复核", disabled=not admin):
        store.record_acceptance_evidence(
            strategy, "RISK_REVIEW", True, details={"actor": "streamlit_admin"}
        )
        st.rerun()
    acceptance = strategy_acceptance(store, strategy)
    with st.form("approve_strategy"):
        reason = st.text_input("批准理由/验收记录")
        submitted = st.form_submit_button(
            "批准当前运行版本进入 PAPER",
            disabled=not admin or not acceptance_passed or not runtime_ready,
        )
        if submitted:
            selected_version = approval_version_status(store, strategy)
            approval_fingerprint = (
                selected_version["runtime_fingerprint"]
                or selected_version["current_fingerprint"]
            )
            if not acceptance_passed or not reason.strip() or not approval_fingerprint:
                st.error("自动验收门槛、运行版本同步和批准理由必须全部满足。")
            elif not selected_version["runtime_aligned"]:
                st.error("提交前检测到版本再次变化，请重启统一交易服务后重试。")
            else:
                store.approve_strategy(
                    strategy, approval_fingerprint, "streamlit_admin",
                    True, True, True, True,
                )
                store.audit(
                    "STRATEGY_APPROVED",
                    {"actor": "streamlit_admin", "strategy": strategy,
                     "version_hash": approval_fingerprint,
                     "service_pid": selected_version["service_pid"],
                     "reason": reason.strip()},
                    strategy,
                )
                st.success("交易服务当前实际运行版本已批准；相关代码变化后必须重启并重新审批。")
                st.rerun()

with reconcile_tab:
    st.write("比较富途实际持仓、本地成交账本和策略虚拟持仓。")
    if st.button("立即运行三账对账", type="primary"):
        try:
            report = reconcile_ledgers(store, broker)
            st.session_state.reconciliation_result = report
        except BrokerError as exc:
            st.error(str(exc))
    report = st.session_state.get("reconciliation_result")
    if isinstance(report, pd.DataFrame):
        st.dataframe(report.rename(columns={
            "symbol": "代码", "broker_quantity": "富途持仓",
            "ledger_quantity": "虚拟账本持仓", "difference": "差异",
            "status": "状态", "affected_strategies": "受影响策略",
        }), hide_index=True)
    history = pd.DataFrame(store.reconciliation_reports())
    if not history.empty:
        st.subheader("最近对账历史")
        st.dataframe(china_time_columns(history), hide_index=True)

with quality_tab:
    summary, comparison = execution_quality_report(store)
    if summary.empty:
        st.info("暂无统一执行层订单数据。")
    else:
        st.dataframe(summary, hide_index=True, column_config={
            "平均不利滑点": st.column_config.NumberColumn(format="percent"),
            "成交率": st.column_config.NumberColumn(format="percent"),
        })
        st.subheader("Shadow 虚拟成交价与模拟盘成交价")
        st.dataframe(china_time_columns(comparison), hide_index=True, column_config={
            "价格偏差": st.column_config.NumberColumn(format="percent"),
        })

with stress_tab:
    st.write("压力测试只读取当前模拟持仓和行情，不会提交订单。")
    if st.button("运行组合压力测试", type="primary"):
        try:
            positions = broker.positions()
            quotes = broker.snapshots(positions["code"].tolist()) if not positions.empty else pd.DataFrame()
            info = broker.account_info()
            equity = float(info.iloc[0]["total_assets"]) if not info.empty else 0
            result = stress_test(positions, quotes, equity)
            st.dataframe(result, hide_index=True, column_config={
                "估算损益": st.column_config.NumberColumn(format="$%.2f"),
                "压力后权益": st.column_config.NumberColumn(format="$%.2f"),
                "权益损失比例": st.column_config.NumberColumn(format="percent"),
            })
        except BrokerError as exc:
            st.error(str(exc))
