from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from futu_quant.attribution import attribution_report
from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.experiments import sync_strategy_experiments
from futu_quant.radar_control import (
    start as start_radar,
    status as radar_status,
    stop as stop_radar,
)
from futu_quant.service_control import restart, start, status as service_status, stop
from futu_quant.store import TradingStore
from futu_quant.strategy_budgeting import (
    budget_group_label,
    recommended_active_budget_rows,
)
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="策略与资金控制", page_icon="🎛️", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("策略与资金控制")
st.caption(
    "统一管理策略启停、执行模式、资金配额和后台服务。策略页面只负责各自参数，组合风险页面只负责压力测试。"
)

heartbeats = {row["strategy_id"]: row for row in store.heartbeats()}
budgets = {row["strategy_id"]: row for row in store.strategy_risk_budgets()}
experiments = store.strategy_experiments(limit=1000)
active_versions = {
    row["strategy_id"]: row for row in experiments if row["is_active"]
}
latest_versions = {}
for row in experiments:
    latest_versions.setdefault(row["strategy_id"], row)

rows = []
for plugin in strategy_plugins():
    heartbeat = heartbeats.get(plugin.strategy_id, {})
    budget = budgets.get(plugin.strategy_id, {})
    active = active_versions.get(plugin.strategy_id, {})
    latest = latest_versions.get(plugin.strategy_id, {})
    rows.append({
        "strategy_id": plugin.strategy_id, "display_name": plugin.display_name,
        "service_name": plugin.service_name, "asset_class": plugin.asset_class,
        "enabled": store.get_setting(
            f"strategy_enabled:{plugin.service_name}",
            "false" if plugin.automatically_scheduled else "true",
        ) == "true",
        "execution_mode": store.get_setting(
            f"strategy_execution_mode:{plugin.strategy_id}", ""
        ) or "INHERIT",
        "heartbeat_status": heartbeat.get("status", "NOT_STARTED"),
        "heartbeat_message": heartbeat.get("message", "尚无心跳"),
        "capital_ratio": float(budget.get("capital_ratio", 0)),
        "active_version": str(active.get("code_version", "未登记")),
        "latest_version": str(latest.get("code_version", "未登记")),
        "version_drift": bool(active and latest and active["version_hash"] != latest["version_hash"]),
    })
control = pd.DataFrame(rows)

service = service_status()
radar = radar_status()
with st.container(horizontal=True):
    service_label = "重启中" if service.restart_pending else ("运行中" if service.running else "未运行")
    st.metric("统一服务", service_label, border=True)
    st.metric("已注册策略", len(control), border=True)
    st.metric("启用自动策略", int(control["enabled"].sum()), border=True)
    st.metric("版本漂移", int(control["version_drift"].sum()), border=True)
    st.metric("全局策略状态", "已暂停" if store.get_setting("strategies_paused", "false") == "true" else "运行", border=True)

service_tab, overview_tab, budget_tab, attribution_tab, experiment_tab, protocol_tab = st.tabs(
    ["服务控制", "策略调度", "策略资金", "收益归因", "实验版本", "插件协议"]
)

with service_tab:
    st.subheader("服务控制")
    with st.container(border=True):
        st.markdown("**统一交易服务**")
        if service.running:
            hours, remainder = divmod(int(service.uptime_seconds), 3600)
            minutes = remainder // 60
            st.success(
                f"{service.message} · PID {service.pid} · 已运行 {hours}小时{minutes}分钟"
            )
        else:
            st.error(service.message or "服务未运行")
        with st.container(horizontal=True):
            if st.button(
                "启动统一服务", disabled=service.running or not admin,
                icon=":material/play_arrow:", key="automation_start_service",
            ):
                result = start()
                store.audit(
                    "SERVICE_START",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                )
                st.toast(result.message)
                st.rerun()
            if st.button(
                "停止统一服务", disabled=not service.running or not admin,
                icon=":material/stop:", key="automation_stop_service",
            ):
                result = stop()
                store.audit(
                    "SERVICE_STOP",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                )
                st.toast(result.message)
                st.rerun()
            if st.button(
                "重启统一服务", disabled=not admin or service.restart_pending,
                icon=":material/restart_alt:", key="automation_restart_service",
            ):
                result = restart()
                store.audit(
                    "SERVICE_RESTART",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                )
                st.toast(result.message)
                st.rerun()

    with st.container(border=True):
        st.markdown("**独立波动率雷达**")
        if radar.running:
            if service.running and not service.restart_pending:
                st.success(f"{radar.message} · PID {radar.pid} · 扫描与交易执行链均在线")
            else:
                st.warning(
                    f"{radar.message} · PID {radar.pid} · 当前仅扫描，不会执行自动交易；"
                    "请先恢复统一交易服务"
                )
        else:
            st.warning(radar.message or "雷达未运行")
        st.caption("启动统一服务时会一并启动雷达；这里保留独立启停，用于只运行自选标的扫描。")
        with st.container(horizontal=True):
            if st.button(
                "启动雷达", disabled=radar.running or not admin,
                icon=":material/radar:", key="automation_start_radar",
            ):
                result = start_radar()
                store.audit(
                    "RADAR_START",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                )
                st.toast(result.message)
                st.rerun()
            if st.button(
                "停止雷达", disabled=not radar.running or not admin,
                icon=":material/stop_circle:", key="automation_stop_radar",
            ):
                result = stop_radar()
                store.audit(
                    "RADAR_STOP",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                )
                st.toast(result.message)
                st.rerun()

    with st.container(border=True):
        st.markdown("**全局运行方式**")
        paused = store.get_setting("strategies_paused", "false") == "true"
        mode = store.get_setting(
            "execution_mode", os.getenv("EXECUTION_MODE", "PAPER")
        ).upper()
        selected_mode = st.segmented_control(
            "全局执行模式", ["SHADOW", "PAPER"], default=mode,
            help="SHADOW 不向富途提交订单；PAPER 仍然只提交到富途模拟盘。",
            disabled=not admin, key="automation_global_execution_mode",
        )
        if selected_mode and selected_mode != mode:
            store.set_setting("execution_mode", selected_mode)
            store.audit(
                "EXECUTION_MODE_CHANGED",
                {"actor": "streamlit_admin", "source": "automation_control_center",
                 "before": mode, "after": selected_mode},
            )
            st.rerun()
        if st.button(
            "恢复全部策略" if paused else "暂停全部策略",
            disabled=not admin, key="automation_pause_all",
        ):
            store.set_setting("strategies_paused", str(not paused).lower())
            store.audit(
                "STRATEGIES_PAUSE_CHANGED",
                {"actor": "streamlit_admin", "source": "automation_control_center",
                 "paused": not paused},
            )
            st.rerun()
        st.caption("暂停策略不会停止订单对账、风控、持仓监控和退出管理。")

with overview_tab:
    st.info(
        "这里管策略是否运行、运行到 SHADOW 还是 PAPER；资金比例请到右侧“策略资金”Tab 调整。",
        icon=":material/info:",
    )
    editable = control[["strategy_id", "display_name", "enabled", "execution_mode"]].copy()
    edited = st.data_editor(
        editable, hide_index=True, width="stretch",
        disabled=["strategy_id", "display_name"],
        column_config={
            "strategy_id": st.column_config.TextColumn("策略ID"),
            "display_name": st.column_config.TextColumn("策略"),
            "enabled": st.column_config.CheckboxColumn("启用自动调度"),
            "execution_mode": st.column_config.SelectboxColumn(
                "执行模式", options=["INHERIT", "SHADOW", "PAPER"]
            ),
        }, key="unified_strategy_control",
    )
    if st.button("保存策略控制", type="primary", disabled=not admin):
        by_id = {plugin.strategy_id: plugin for plugin in strategy_plugins()}
        changes = []
        for item in edited.to_dict("records"):
            plugin = by_id[item["strategy_id"]]
            store.set_setting(
                f"strategy_enabled:{plugin.service_name}", str(bool(item["enabled"])).lower()
            )
            mode = item["execution_mode"]
            store.set_setting(
                f"strategy_execution_mode:{plugin.strategy_id}",
                "" if mode == "INHERIT" else str(mode),
            )
            changes.append(item)
        store.audit(
            "UNIFIED_STRATEGY_CONTROL_CHANGED",
            {"source": "automation_control_center", "changes": changes},
        )
        st.success("策略控制已保存，下一个调度周期生效。")
        st.rerun()
    st.dataframe(
        control.drop(columns=["enabled", "execution_mode"], errors="ignore"),
        hide_index=True, width="stretch",
        column_config={
            "capital_ratio": st.column_config.NumberColumn("资金配额", format="percent"),
            "version_drift": st.column_config.CheckboxColumn("版本漂移"),
        },
    )

with budget_tab:
    st.subheader("策略资金")
    st.caption(
        "这是策略资金配额的主配置入口。资金配额合计不能超过 100%；未分配部分就是现金和风险缓冲。"
    )
    budget_rows = store.strategy_risk_budgets()
    if not budget_rows:
        st.info("策略预算尚未初始化，请先进入组合风险页面生成默认预算。")
    else:
        budget_frame = pd.DataFrame(budget_rows)
        budget_frame["strategy_group"] = budget_frame["strategy_id"].map(budget_group_label)
        enabled_total = float(
            budget_frame[budget_frame["enabled"].astype(bool)]["capital_ratio"].sum()
        )
        with st.container(horizontal=True):
            st.metric("启用资金配额", f"{enabled_total:.0%}", border=True)
            st.metric("现金/未分配缓冲", f"{max(0.0, 1 - enabled_total):.0%}", border=True)
            st.metric(
                "波动率期权配额",
                f"{float(budget_frame.loc[budget_frame['strategy_id'] == 'volatility_long_gamma', 'capital_ratio'].sum()):.0%}",
                border=True,
            )
            st.metric(
                "财报策略配额",
                f"{float(budget_frame[budget_frame['strategy_id'].isin(['earnings_gamma', 'earnings_gamma_manual'])]['capital_ratio'].sum()):.0%}",
                border=True,
            )
        if st.button(
            "应用当前推荐：Core 65% + 波动率期权 25%",
            type="primary",
            disabled=not admin,
            icon=":material/auto_fix_high:",
        ):
            rows_to_save = recommended_active_budget_rows(budget_rows)
            store.save_strategy_risk_budgets(rows_to_save)
            store.audit(
                "STRATEGY_BUDGET_PRESET_APPLIED",
                {"actor": "streamlit_admin", "preset": "core65_volatility25"},
            )
            st.success("推荐资金配置已应用：财报自动/手工策略资金已转给波动率期权。")
            st.rerun()
        edit_columns = [
            "strategy_group", "strategy_id", "capital_ratio", "max_loss_ratio",
            "option_premium_ratio", "enabled",
        ]
        edited_budget = st.data_editor(
            budget_frame[edit_columns],
            hide_index=True,
            width="stretch",
            disabled=["strategy_group", "strategy_id"],
            column_config={
                "strategy_group": st.column_config.TextColumn("策略分组"),
                "strategy_id": st.column_config.TextColumn("策略ID", pinned=True),
                "capital_ratio": st.column_config.NumberColumn(
                    "资金配额", min_value=0.0, max_value=1.0, format="percent"
                ),
                "max_loss_ratio": st.column_config.NumberColumn(
                    "最大损失预算", min_value=0.0, max_value=1.0, format="percent"
                ),
                "option_premium_ratio": st.column_config.NumberColumn(
                    "期权权利金配额", min_value=0.0, max_value=1.0, format="percent"
                ),
                "enabled": st.column_config.CheckboxColumn("参与风控预算"),
            },
            key="strategy_budget_control_editor",
        )
        if st.button("保存策略资金配额", disabled=not admin):
            rows_to_save = edited_budget.drop(columns=["strategy_group"]).to_dict("records")
            try:
                store.save_strategy_risk_budgets(rows_to_save)
                st.success("策略资金配额已保存，统一下单风控立即生效。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        st.caption(
            "建议当前阶段：Core Satellite 是股票主策略；volatility_long_gamma 是期权主策略；"
            "earnings_gamma / earnings_gamma_manual 暂停资金配额，只保留页面和历史记录。"
        )

with attribution_tab:
    @st.cache_data(ttl=30, max_entries=2, show_spinner=False)
    def load_attribution():
        return attribution_report(TradingStore(), FutuPaperBroker(Settings.load()))

    try:
        summary, detail = load_attribution()
        if summary.empty:
            st.info("当前统一成交账本和虚拟持仓中没有可归因数据。")
        else:
            with st.container(horizontal=True):
                st.metric("已实现盈亏", f"${summary['realized_pnl'].sum():,.2f}", border=True)
                st.metric("未实现盈亏", f"${summary['unrealized_pnl'].sum():,.2f}", border=True)
                st.metric("累计费用", f"${summary['fees'].sum():,.2f}", border=True)
                st.metric("不利滑点", f"${summary['slippage_cost'].sum():,.2f}", border=True)
                st.metric("归因总盈亏", f"${summary['total_pnl'].sum():,.2f}", border=True)
            st.bar_chart(summary.set_index("strategy_id")[["realized_pnl", "unrealized_pnl"]])
            summary_tab, detail_tab = st.tabs(["按策略", "按标的"])
            summary_tab.dataframe(chinese_columns(summary), hide_index=True, width="stretch")
            detail_tab.dataframe(chinese_columns(detail), hide_index=True, width="stretch")
            detail_tab.download_button(
                "下载归因明细 CSV", chinese_columns(detail).to_csv(index=False).encode("utf-8-sig"),
                file_name="strategy_attribution.csv", mime="text/csv",
            )
            st.caption("已实现盈亏采用 FIFO；未实现盈亏使用策略虚拟持仓成本和当前行情。Greeks 列表示当前风险敞口，不是已实现收益。")
    except BrokerError as exc:
        st.error(f"归因行情读取失败：{exc}")

with experiment_tab:
    if st.button("登记当前代码与配置版本", disabled=not admin):
        with st.spinner("正在计算策略版本指纹……"):
            sync_strategy_experiments(store, settings)
        st.success("当前策略版本已登记。")
        st.rerun()
    experiment_frame = pd.DataFrame(store.strategy_experiments(limit=1000))
    if experiment_frame.empty:
        st.info("尚无实验版本。统一服务下次启动时会自动登记。")
    else:
        st.dataframe(
            chinese_columns(experiment_frame.drop(columns=["version_hash"], errors="ignore")),
            hide_index=True, width="stretch",
        )
        selected = st.selectbox(
            "选择要设为当前运行版本的实验", experiment_frame["id"].tolist(),
            format_func=lambda value: (
                f"#{value} · {experiment_frame[experiment_frame['id'] == value].iloc[0]['strategy_id']} · "
                f"{experiment_frame[experiment_frame['id'] == value].iloc[0]['code_version']}"
            ),
        )
        confirmed = st.checkbox("确认将此实验标记为当前运行版本")
        if st.button("切换当前版本标记", disabled=not admin or not confirmed):
            store.promote_experiment(int(selected))
            st.success("当前运行版本标记已更新。代码部署仍需通过文件发布流程完成。")
            st.rerun()

with protocol_tab:
    protocol = pd.DataFrame([plugin.as_record() for plugin in strategy_plugins()])
    st.dataframe(protocol, hide_index=True, width="stretch")
    st.caption(
        "所有策略现在都声明标的提供器、信号提供器、仓位模型、退出模型、风险档案、"
        "回测适配器和统一执行适配器。现有算法通过适配器接入，后续新策略必须满足同一协议。"
    )
