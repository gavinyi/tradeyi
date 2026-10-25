from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from futu_quant.approval import approval_enforcement_enabled, approval_version_status
from futu_quant.acceptance import strategy_acceptance
from futu_quant.broker import BrokerError
from futu_quant.config import Settings, save_runtime_settings
from futu_quant.execution_quality import execution_quality_report
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.stress import stress_test
from futu_quant.replay import run_resilience_drill

# run_service 是脚本入口，不在包内；这里只为复用其中的启用键解析逻辑
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_service import resolve_configured_strategies  # noqa: E402
from futu_quant.service_control import restart as restart_service, status as service_status
from futu_quant.ui import (
    china_time_columns, render_admin_login, render_sidebar, get_current_tenant, get_tenant_broker,
)


st.set_page_config(page_title="上线审批", page_icon="✅", layout="wide")
settings = Settings.load()
render_sidebar(settings)
current_tenant = get_current_tenant(settings)
st.title("策略上线审批与验收")
store = TradingStore()
broker = get_tenant_broker(settings)
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
    enforced = approval_enforcement_enabled(store)
    selected_enforcement = st.toggle(
        "PAPER/REAL 新增仓位强制要求当前版本审批", value=enforced, disabled=not admin,
        help="只控制策略版本审批门槛；急停、风控、实盘白名单和额度限制仍然独立生效。",
    )
    if selected_enforcement != enforced:
        save_runtime_settings({"REQUIRE_STRATEGY_APPROVAL": str(selected_enforcement).lower()})
        store.audit(
            "APPROVAL_ENFORCEMENT_CHANGED",
            {"actor": "streamlit_admin", "enabled": selected_enforcement},
         actor="streamlit_admin:panel")
        st.rerun()
    if selected_enforcement:
        st.info("上线审批当前开启：PAPER/REAL 新增 BUY 会要求当前运行版本已批准。")
    else:
        st.success("上线审批当前关闭：策略会直接进入当前执行模式，急停、风控和交易环境限制仍然生效。")
    approvals = {row["strategy_id"]: row for row in store.approvals()}

    with st.container(border=True):
        st.subheader("Core Satellite 快速上线")
        st.caption(
            "Core Satellite 已设为免版本审批策略。按钮只启用该策略并切换到富途 PAPER；"
            "急停、亏损线、仓位和购买力风控仍然生效。"
        )
        # 必须按当前租户判定：读全局 strategy_enabled:Core Satellite 在多租户下是错的
        # （实盘租户键为 true、全局为 false，页面会显示"未启用"而实际在跑）。
        _tenant_id = current_tenant.get("tenant_id") or current_tenant.get("id")
        _configured_env = {
            value.strip() for value in os.getenv(
                "SERVICE_STRATEGIES", "Core Satellite,Earnings Gamma"
            ).split(",") if value.strip()
        }
        core_enabled = (
            "Core Satellite"
            in resolve_configured_strategies(store, _tenant_id, _configured_env)
        )
        # 执行模式同样必须租户感知：execution.py 已按
        # `strategy_execution_mode:{tenant_id}:{strategy_id}` 优先读取，页面若只读全局键，
        # 会显示与真实调度不一致的模式。
        _tenant_mode = (
            store.get_setting(f"strategy_execution_mode:{_tenant_id}:core_satellite", "")
            if _tenant_id else ""
        )
        core_mode = (
            _tenant_mode
            or store.get_setting("strategy_execution_mode:core_satellite", "")
            or store.get_setting("execution_mode", "PAPER")
        ).upper()
        with st.container(horizontal=True):
            st.metric("自动调度", "已启用" if core_enabled else "未启用", border=True)
            st.metric("执行模式", core_mode, border=True)
            if st.button(
                "一键上线 Core Satellite", type="primary", icon=":material/rocket_launch:",
                disabled=not admin or (core_enabled and core_mode == "PAPER"),
            ):
                # 写侧必须覆盖读侧认的每一种拼法与前缀：读侧租户键优先（两种拼法都认），
                # 只写全局键在有租户键的情况下是静默无效的；写全局键还会污染另一个租户的回落值，
                # 因此这里**只写租户级键**，没有租户上下文时不做任何修改。
                if not _tenant_id:
                    st.error("无法确定当前租户，未做任何修改。")
                else:
                    # 逐键显式写出（不用循环变量）：写侧必须覆盖读侧认的每一种拼法与前缀，
                    # 只写全局键在有租户键的情况下是静默无效的；写全局键还会污染另一个
                    # 租户的回落值，因此这里只写租户级键。
                    store.set_setting(f"strategy_enabled:{_tenant_id}:Core Satellite", "true")
                    store.set_setting(f"strategy_enabled:{_tenant_id}:core_satellite", "true")
                    store.set_setting(
                        f"strategy_execution_mode:{_tenant_id}:core_satellite", "PAPER"
                    )
                    store.audit(
                        "CORE_SATELLITE_QUICK_LAUNCH",
                        {
                            "actor": "streamlit_admin", "mode": "PAPER", "approval_exempt": True,
                            "tenant_id": _tenant_id,
                            "enable_keys": [
                                f"strategy_enabled:{_tenant_id}:Core Satellite",
                                f"strategy_enabled:{_tenant_id}:core_satellite",
                            ],
                        },
                        "core_satellite",
                     actor="streamlit_admin:panel")
                    st.success(f"Core Satellite 已在租户 {_tenant_id} 启用并切换为 PAPER 模拟交易。")
                st.rerun()
        if store.kill_switch():
            st.warning("系统急停当前已开启；策略虽已上线，但不会新增仓位。")
        # 暂停状态按**当前租户**读（租户键优先、全局键兜底）—— 读全局键会把
        # 别的租户的暂停状态显示成当前租户的，误导操作者。
        try:
            _tenant_id = str(get_current_tenant(settings).get("tenant_id") or "")
        except Exception:
            _tenant_id = ""
        _paused = (
            store.get_tenant_setting(_tenant_id, "strategies_paused", "") if _tenant_id else ""
        ) or store.get_setting("strategies_paused", "false")
        if _paused.lower() == "true":
            st.warning(
                f"租户 {_tenant_id or '未识别'} 的策略暂停当前已开启；"
                "请先到策略与资金控制页恢复该租户的策略调度（不影响其他租户）。"
            )
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
                "审批总开关关闭" if not version["enforcement_enabled"] else
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
        not selected_version["enforcement_enabled"]
        or selected_version["exempt"] or (
            selected_service.running and selected_version["runtime_aligned"]
        )
    )
    if not selected_version["enforcement_enabled"]:
        st.info("审批总开关已关闭；本页验收结果只作为诊断，不会拦截策略进入 PAPER。")
    elif not selected_version["exempt"] and not selected_service.running:
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
    acceptance_passed = (
        True if not selected_version["enforcement_enabled"]
        else all(gate["passed"] for gate in approval_gates.values())
    )
    st.dataframe(pd.DataFrame([
        {"验收项": gate_names.get(key, key), "结果": "通过" if gate["passed"] else "未通过",
         "当前值": gate["value"], "要求": gate["required"]}
        for key, gate in approval_gates.items()
    ]), hide_index=True)
    failed_gates = [gate_names.get(key, key) for key, gate in approval_gates.items() if not gate["passed"]]
    if not selected_version["enforcement_enabled"]:
        st.success("审批总开关已关闭；未通过项目不会阻塞 PAPER 下单。")
    elif failed_gates:
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
            disabled=(
                not selected_version["enforcement_enabled"]
                or not admin or not acceptance_passed or not runtime_ready
            ),
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
                 actor="streamlit_admin:panel")
                st.success("交易服务当前实际运行版本已批准；相关代码变化后必须重启并重新审批。")
                st.rerun()

with reconcile_tab:
    st.write("比较富途实际持仓、本地成交账本和策略虚拟持仓。")
    if st.button("立即运行三账对账", type="primary"):
        try:
            report = reconcile_ledgers(store, broker, tenant_id=current_tenant.get("tenant_id") or current_tenant.get("id"))
            st.session_state.reconciliation_result = report
        except BrokerError as exc:
            st.error(str(exc))
    report = st.session_state.get("reconciliation_result")
    if isinstance(report, pd.DataFrame):
        if report.empty:
            st.success("三账对账完成：当前账户无在仓标的或未平账目，富途与本地账本完全一致（0 差异）。")
        else:
            st.dataframe(report.rename(columns={
                "symbol": "代码", "broker_quantity": "富途持仓",
                "ledger_quantity": "虚拟账本持仓", "difference": "差异",
                "status": "状态", "affected_strategies": "受影响策略",
            }), hide_index=True)
            mismatches = report[report["status"].astype(str).eq("MISMATCH")] if "status" in report.columns else pd.DataFrame()
            if not mismatches.empty:
                st.subheader("差异快速调整")
                st.caption("适用于你已在富途手动卖出/买入的情况。系统会把虚拟账本调整到富途当前实际数量，并保留审计记录。")
            for row in mismatches.to_dict("records"):
                symbol = str(row["symbol"])
                owners = [item.strip() for item in str(row.get("affected_strategies", "")).split(",") if item.strip()]
                allocations = [item for item in store.virtual_allocations() if item["symbol"] == symbol]
                owner_names = owners or [str(item["strategy_id"]) for item in allocations]
                with st.container(border=True):
                    st.write(
                        f"**{symbol}**：富途 {float(row['broker_quantity']):g}，"
                        f"虚拟账本 {float(row['ledger_quantity']):g}，差异 {float(row['difference']):g}"
                    )
                    if not admin:
                        st.info("解锁管理员操作后可调整。")
                        continue
                    if len(owner_names) == 1:
                        strategy = owner_names[0]
                        current_cost = next(
                            (float(item.get("average_cost", 0) or 0) for item in allocations
                             if str(item["strategy_id"]) == strategy), 0.0
                        )
                        if st.button(
                            f"按富途持仓同步 · {strategy}",
                            key=f"reconcile_sync:{symbol}:{strategy}",
                        ):
                            adjustment = store.sync_virtual_allocation_to_target(
                                strategy, symbol, float(row["broker_quantity"]), current_cost,
                                tenant_id=current_tenant.get("tenant_id") or current_tenant.get("id"),
                                account_id=current_tenant.get("acc_id"),
                            )
                            store.audit(
                                "RECONCILIATION_MANUAL_SYNC",
                                {"actor": "streamlit_admin", "symbol": symbol,
                                 "strategy": strategy, "broker_quantity": float(row["broker_quantity"]),
                                 "previous_ledger_quantity": float(row["ledger_quantity"]),
                                 **adjustment},
                                strategy,
                             actor="streamlit_admin:panel")
                            st.success(
                                f"已完成 {symbol} 对账：目标 {adjustment['target_quantity']:g}，"
                                f"历史成交净量 {adjustment['net_fills']:g}，"
                                f"归属残差 {adjustment['residual']:g}。"
                            )
                            st.rerun()
                    else:
                        st.warning("该标的有多个策略归属，请到风险中心手动拆分后再对账。")
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
            cached_positions = store.broker_snapshot("positions")
            cached_info = store.broker_snapshot("account_info")
            positions = pd.DataFrame((cached_positions or {}).get("payload", []))
            info = pd.DataFrame((cached_info or {}).get("payload", []))
            if positions.empty:
                positions = broker.positions()
            quotes = broker.snapshots(positions["code"].tolist()) if not positions.empty else pd.DataFrame()
            if info.empty:
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
