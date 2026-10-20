from __future__ import annotations

import json
import os

import pandas as pd

import streamlit as st

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings, save_runtime_settings
from futu_quant.store import TradingStore
from futu_quant.ui import china_time_columns, get_current_tenant, render_admin_login, render_sidebar
from futu_quant.risk_calibration import generate_recommendations, recommendation_changes


st.set_page_config(page_title="风控中心", page_icon="🚨", layout="wide")
render_sidebar(Settings.load())
st.title("风控中心")
st.caption("集中管理模拟盘急停、账户风险限额和订单保护参数。")

overview_left, overview_mid, overview_right = st.columns(3, vertical_alignment="center")
with overview_left:
    st.metric(
        "单笔名义上限",
        f"${float(os.getenv('RISK_MAX_ORDER_NOTIONAL', '20000')):,.0f}",
        border=True,
    )
with overview_mid:
    st.metric(
        "高相关组合上限",
        f"{float(os.getenv('RISK_MAX_CORRELATED_DELTA_PCT', '0.50')):.0%} 权益",
        border=True,
    )
with overview_right:
    st.metric("订单超时", f"{int(os.getenv('ORDER_TIMEOUT_SECONDS', '900')) // 60} 分钟", border=True)

store = TradingStore()
broker = FutuPaperBroker(Settings.load())
if not render_admin_login():
    st.info("完成管理员认证后才能查看和修改风控参数。")
    st.stop()


def save_group(label: str, changes: dict[str, str]) -> None:
    """保存一组风控参数并写入审计（各组独立提交，互不覆盖）。"""
    before = {key: os.getenv(key, "") for key in changes}
    save_runtime_settings(changes)
    store.audit(
        "RISK_SETTINGS_CHANGED",
        {"actor": "streamlit_admin", "group": label, "before": before, "after": changes},
    )
    st.success(f"「{label}」已保存；请重启统一交易服务使全部参数生效。")


# ---------------------------------------------------------------------------
# 紧急操作区：始终置顶，保证多账户独立急停与全局急停清晰隔离
# ---------------------------------------------------------------------------
current_tenant = get_current_tenant(Settings.load())
tenant_acc_id = current_tenant.get("acc_id")
tenant_stopped = store.kill_switch(account_id=tenant_acc_id)
global_stopped = store.get_setting("kill_switch", "false").lower() == "true"

with st.container(border=True):
    st.subheader("🚨 紧急操作区（多租户独立隔离）")
    st.caption(f"当前操作租户：**{current_tenant['label']}** · 账户急停仅影响当前选中账户，互不干扰；全局总制动可一键关停全系统。")

    col_tenant, col_global = st.columns(2)
    with col_tenant:
        with st.container(border=True):
            st.markdown(f"**【{current_tenant['short_name']}】独立急停控制**")
            if tenant_stopped:
                st.error("当前账户急停已开启：禁止新增买入与加仓，平仓可用。")
                if st.button(f"解除【{current_tenant['short_name']}】急停", type="primary", width="stretch", key="btn_clear_tenant_kill"):
                    store.set_kill_switch(False, account_id=tenant_acc_id)
                    store.audit("TENANT_KILL_SWITCH_DISABLED", {"account_id": tenant_acc_id, "source": "risk_center"})
                    st.success(f"已恢复【{current_tenant['short_name']}】正常交易。")
                    st.rerun()
            else:
                st.success(f"【{current_tenant['short_name']}】风控正常")
                st.caption("触发后仅对当前账户禁止开仓，其他账户继续运行。")
                if st.button(f"一键急停【{current_tenant['short_name']}】", type="secondary", width="stretch", key="btn_set_tenant_kill"):
                    store.set_kill_switch(True, account_id=tenant_acc_id)
                    store.audit("TENANT_KILL_SWITCH_ENABLED", {"account_id": tenant_acc_id, "source": "risk_center"})
                    st.warning(f"已暂停【{current_tenant['short_name']}】新增交易。")
                    st.rerun()

    with col_global:
        with st.container(border=True):
            st.markdown("**【全系统总制动】Master Kill Switch**")
            if global_stopped:
                st.error("全局总制动已开启：全系统所有账户已统一停止新增交易！")
                if st.button("解除全局总制动", type="primary", width="stretch", key="btn_clear_global_kill"):
                    store.set_kill_switch(False)
                    store.audit("GLOBAL_KILL_SWITCH_DISABLED", {"source": "risk_center"})
                    st.success("全局总制动已解除。")
                    st.rerun()
            else:
                st.info("全局总制动未开启（全系统总闸）")
                st.caption("开启后将一键强行暂停全系统所有账户的新增开仓。")
                with st.form("risk_center_global_kill_switch"):
                    confirmed = st.checkbox("确认对全系统所有账户执行总制动")
                    activate = st.form_submit_button("全系统总制动", type="primary", width="stretch")
                    if activate:
                        if not confirmed:
                            st.error("请先勾选确认。")
                        else:
                            store.set_kill_switch(True)
                            store.audit("GLOBAL_KILL_SWITCH_ENABLED", {"source": "risk_center"})
                            st.rerun()
    cancel_message = st.session_state.pop("risk_cancel_message", None)
    if cancel_message:
        st.warning(cancel_message)


st.divider()

overview_tab, config_tab, capital_tab, calibration_tab = st.tabs(
    ["运行概览", "风控参数", "资金与持仓", "校准建议"]
)

# ---------------------------------------------------------------------------
# 运行概览：日常盯盘数据与高相关簇诊断
# ---------------------------------------------------------------------------
with overview_tab:
    st.caption("日常先看这里的急停状态和高相关簇；阈值调整去「风控参数」。")
    with st.container(horizontal=True):
        st.metric(f"【{current_tenant['short_name']}】急停", "已开启" if tenant_stopped else "正常", border=True)
        st.metric("全局总制动", "已开启" if global_stopped else "正常", border=True)
        st.metric(
            "单笔名义上限",
            f"${float(os.getenv('RISK_MAX_ORDER_NOTIONAL', '20000')):,.0f}",
            border=True,
        )
        st.metric(
            "高相关组合上限",
            f"{float(os.getenv('RISK_MAX_CORRELATED_DELTA_PCT', '0.50')):.0%}",
            border=True,
        )
        st.metric("最大待成交订单", int(os.getenv("RISK_MAX_PENDING_ORDERS", "20")), border=True)

    correlation_threshold = float(os.getenv("RISK_CORRELATION_THRESHOLD", "0.80"))
    correlation_clusters_raw = store.get_setting("correlation_clusters", "{}")
    try:
        correlation_clusters = json.loads(correlation_clusters_raw or "{}")
    except json.JSONDecodeError:
        correlation_clusters = {}

    with st.container(border=True):
        st.subheader("高相关组合诊断")
        st.caption(
            f"当前相关性阈值是 {correlation_threshold:.0%}。系统会把达到这个阈值的标的放进同一个风险簇。"
        )
        if not correlation_clusters:
            st.info("当前还没有相关簇数据。等统一服务或相关性更新任务跑过后，这里会自动展示分组。")
        else:
            rows = []
            for leader, members in sorted(correlation_clusters.items()):
                member_list = sorted({str(item) for item in members})
                rows.append({
                    "风险簇": leader,
                    "簇成员": "、".join(member_list),
                    "成员数": len(member_list),
                })
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                column_config={
                    "风险簇": st.column_config.TextColumn("风险簇", pinned=True),
                    "簇成员": st.column_config.TextColumn("簇成员", width="large"),
                    "成员数": st.column_config.NumberColumn("成员数"),
                },
            )
            st.caption("如果一簇里同时有多只高相关股票和期权，系统会把它们的 Delta 合并后再检查上限。")

    with st.container(border=True):
        st.subheader("快捷入口")
        st.page_link("pages/14_portfolio_risk.py", label="查看组合风险", icon=":material/donut_large:")
        st.page_link(
            "pages/15_strategy_control_center.py", label="前往策略与资金控制",
            icon=":material/tune:",
        )

# ---------------------------------------------------------------------------
# 风控参数：按主题拆成三个短页签，每组独立保存
# ---------------------------------------------------------------------------
with config_tab:
    st.caption("低频调整项，按主题分组；每组单独保存，互不影响。")
    concentration_tab, execution_tab, greeks_tab = st.tabs(
        ["仓位集中度", "订单与执行", "波动与 Greeks"]
    )

    with concentration_tab:
        with st.container(border=True):
            st.subheader("仓位集中度")
            st.caption("决定系统能不能继续加仓，重点看单票、家族和高相关簇。")
            with st.form("risk_settings_concentration"):
                order_limit = st.number_input(
                    "单笔最大名义金额（美元）", 100.0, 10_000_000.0,
                    float(os.getenv("RISK_MAX_ORDER_NOTIONAL", "20000")),
                )
                daily_limit = st.number_input(
                    "每日累计最大名义金额（美元）", 100.0, 100_000_000.0,
                    float(os.getenv("RISK_MAX_DAILY_NOTIONAL", "100000")),
                )
                st.markdown("**风险降低型卖出额度**")
                st.caption("仅当券商持仓和策略归属均足够时生效；开空和保护性卖出仍使用普通额度。")
                reduction_order_limit = st.number_input(
                    "单笔最大减仓金额（美元）", 100.0, 100_000_000.0,
                    float(os.getenv("RISK_MAX_REDUCTION_ORDER_NOTIONAL", "150000")),
                )
                reduction_daily_limit = st.number_input(
                    "每日累计最大减仓金额（美元）", 100.0, 500_000_000.0,
                    float(os.getenv("RISK_MAX_DAILY_REDUCTION_NOTIONAL", "500000")),
                )
                option_limit = st.number_input(
                    "期权最大权利金敞口（美元）", 100.0, 10_000_000.0,
                    float(os.getenv("RISK_MAX_OPTION_PREMIUM", "30000")),
                )
                loss_limit = st.number_input(
                    "账户亏损急停线（美元）", 100.0, 10_000_000.0,
                    float(os.getenv("RISK_MAX_DAILY_LOSS", "5000")),
                )
                drawdown = st.number_input(
                    "账户日内最大回撤比例", 0.001, 1.0,
                    float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0.05")),
                )
                symbol_weight = st.number_input(
                    "单标的最大仓位比例", 0.01, 1.0,
                    float(os.getenv("RISK_MAX_SYMBOL_WEIGHT", "0.25")),
                )
                gross = st.number_input(
                    "账户最大总持仓比例", 0.01, 1.0,
                    float(os.getenv("RISK_MAX_GROSS_EXPOSURE", "0.85")),
                )
                correlated_delta = st.number_input(
                    "高相关组合最大 Delta / 权益", 0.01, 2.0,
                    float(os.getenv("RISK_MAX_CORRELATED_DELTA_PCT", "0.50")),
                    help="一组高度相关标的合计暴露的上限；你昨晚那条就是这里触发的。",
                )
                new_correlation_threshold = st.number_input(
                    "高相关判定阈值", 0.10, 1.0,
                    float(os.getenv("RISK_CORRELATION_THRESHOLD", "0.80")),
                    help="相关系数达到这个阈值后，系统会把它们视作同一风险簇。",
                )
                if st.form_submit_button("保存仓位集中度", type="primary"):
                    save_group("仓位集中度", {
                        "RISK_MAX_ORDER_NOTIONAL": str(order_limit),
                        "RISK_MAX_DAILY_NOTIONAL": str(daily_limit),
                        "RISK_MAX_REDUCTION_ORDER_NOTIONAL": str(reduction_order_limit),
                        "RISK_MAX_DAILY_REDUCTION_NOTIONAL": str(reduction_daily_limit),
                        "RISK_MAX_OPTION_PREMIUM": str(option_limit),
                        "RISK_MAX_DAILY_LOSS": str(loss_limit),
                        "RISK_MAX_DRAWDOWN_PCT": str(drawdown),
                        "RISK_MAX_SYMBOL_WEIGHT": str(symbol_weight),
                        "RISK_MAX_GROSS_EXPOSURE": str(gross),
                        "RISK_MAX_CORRELATED_DELTA_PCT": str(correlated_delta),
                        "RISK_CORRELATION_THRESHOLD": str(new_correlation_threshold),
                    })

    with execution_tab:
        with st.container(border=True):
            st.subheader("订单与执行")
            st.caption("决定单子多久撤、追价多激进、以及待成交订单上限。")
            with st.form("risk_settings_execution"):
                st.markdown("**组合与订单上限**")
                net_delta = st.number_input(
                    "组合最大净 Delta / 权益", 0.01, 2.0,
                    float(os.getenv("RISK_MAX_NET_DELTA_PCT", "0.70")),
                )
                family_delta = st.number_input(
                    "单一正股家族最大 Delta / 权益", 0.01, 1.0,
                    float(os.getenv("RISK_MAX_FAMILY_DELTA_PCT", "0.30")),
                )
                pending = st.number_input(
                    "最大待成交订单数", 1, 1000,
                    int(os.getenv("RISK_MAX_PENDING_ORDERS", "20")),
                )
                timeout = st.number_input(
                    "订单超时撤单秒数", 60, 86400,
                    int(os.getenv("ORDER_TIMEOUT_SECONDS", "900")),
                )
                webhook = st.text_input(
                    "告警 Webhook（可选）", os.getenv("ALERT_WEBHOOK_URL", ""), type="password",
                )
                st.markdown("**智能限价执行**")
                st.caption("这组是下单方式，不是风控强度。")
                exec_spread = st.number_input(
                    "最大买卖价差比例", 0.001, 1.0,
                    float(os.getenv("EXEC_MAX_SPREAD_PCT", "0.20")),
                )
                exec_slippage = st.number_input(
                    "最大滑点比例", 0.0001, 0.20,
                    float(os.getenv("EXEC_MAX_SLIPPAGE_PCT", "0.01")),
                )
                reprice_interval = st.number_input(
                    "追价间隔秒数", 15, 3600,
                    int(os.getenv("EXEC_REPRICE_INTERVAL_SECONDS", "60")),
                )
                reprice_attempts = st.number_input(
                    "最大追价次数", 0, 20,
                    int(os.getenv("EXEC_MAX_REPRICE_ATTEMPTS", "3")),
                )
                reprice_step = st.number_input(
                    "每次追价比例", 0.0001, 0.10,
                    float(os.getenv("EXEC_REPRICE_STEP_PCT", "0.0025")),
                )
                st.markdown("**Shadow 追价策略**")
                shadow_timeout = st.number_input(
                    "Shadow 超时重算秒数", 60, 86400,
                    int(os.getenv("SHADOW_ORDER_TIMEOUT_SECONDS", "900")),
                )
                shadow_reprice_attempts = st.number_input(
                    "Shadow 最大追价次数", 0, 20,
                    int(os.getenv("SHADOW_MAX_REPRICE_ATTEMPTS", "3")),
                )
                shadow_reprice_step = st.number_input(
                    "Shadow 每次追价比例", 0.0001, 0.10,
                    float(os.getenv("SHADOW_REPRICE_STEP_PCT", "0.0075")),
                )
                shadow_max_slippage = st.number_input(
                    "Shadow 最大总滑点", 0.0001, 0.50,
                    float(os.getenv("SHADOW_MAX_SLIPPAGE_PCT", "0.12")),
                )
                if st.form_submit_button("保存订单与执行参数", type="primary"):
                    save_group("订单与执行", {
                        "RISK_MAX_NET_DELTA_PCT": str(net_delta),
                        "RISK_MAX_FAMILY_DELTA_PCT": str(family_delta),
                        "RISK_MAX_PENDING_ORDERS": str(pending),
                        "ORDER_TIMEOUT_SECONDS": str(timeout),
                        "ALERT_WEBHOOK_URL": webhook,
                        "EXEC_MAX_SPREAD_PCT": str(exec_spread),
                        "EXEC_MAX_SLIPPAGE_PCT": str(exec_slippage),
                        "EXEC_REPRICE_INTERVAL_SECONDS": str(reprice_interval),
                        "EXEC_MAX_REPRICE_ATTEMPTS": str(reprice_attempts),
                        "EXEC_REPRICE_STEP_PCT": str(reprice_step),
                        "SHADOW_ORDER_TIMEOUT_SECONDS": str(shadow_timeout),
                        "SHADOW_MAX_REPRICE_ATTEMPTS": str(shadow_reprice_attempts),
                        "SHADOW_REPRICE_STEP_PCT": str(shadow_reprice_step),
                        "SHADOW_MAX_SLIPPAGE_PCT": str(shadow_max_slippage),
                    })

    with greeks_tab:
        with st.container(border=True):
            st.subheader("波动与 Greeks")
            st.caption("主要给期权和 Gamma 策略使用，通常最后再调。")
            with st.form("risk_settings_greeks"):
                st.markdown("**Gamma Greeks 与数据维护**")
                max_delta = st.number_input(
                    "Gamma 组合最大绝对 Delta", 1.0, 1_000_000.0,
                    float(os.getenv("GAMMA_MAX_ABS_DELTA", "1000")),
                )
                max_gamma = st.number_input(
                    "Gamma 组合最大绝对 Gamma", 1.0, 1_000_000.0,
                    float(os.getenv("GAMMA_MAX_ABS_GAMMA", "500")),
                )
                max_vega = st.number_input(
                    "Gamma 组合最大绝对 Vega", 1.0, 10_000_000.0,
                    float(os.getenv("GAMMA_MAX_ABS_VEGA", "50000")),
                )
                max_iv_rv = st.number_input(
                    "Gamma 最大 IV/RV", 0.1, 20.0,
                    float(os.getenv("GAMMA_MAX_IV_RV", "2.5")),
                )
                max_iv_percentile = st.number_input(
                    "Gamma 最大 IV 历史百分位", 0.01, 1.0,
                    float(os.getenv("GAMMA_MAX_IV_PERCENTILE", "0.95")),
                )
                volatility_max_iv_rv = st.number_input(
                    "波动行情最大 IV/RV", 0.1, 20.0,
                    float(os.getenv("GAMMA_VOLATILITY_MAX_IV_RV", "5.0")),
                )
                volatility_max_iv_percentile = st.number_input(
                    "波动行情最大 IV 历史百分位", 0.01, 1.0,
                    float(os.getenv("GAMMA_VOLATILITY_MAX_IV_PERCENTILE", "1.0")),
                    help="1.0 表示允许历史样本中的最高 IV，但价差、权利金、Greeks 等风控仍然生效。",
                )
                retention = st.number_input(
                    "监控数据保留天数", 30, 3650,
                    int(os.getenv("DATA_RETENTION_DAYS", "365")),
                )
                if st.form_submit_button("保存波动与 Greeks 参数", type="primary"):
                    save_group("波动与 Greeks", {
                        "GAMMA_MAX_ABS_DELTA": str(max_delta),
                        "GAMMA_MAX_ABS_GAMMA": str(max_gamma),
                        "GAMMA_MAX_ABS_VEGA": str(max_vega),
                        "GAMMA_MAX_IV_RV": str(max_iv_rv),
                        "GAMMA_MAX_IV_PERCENTILE": str(max_iv_percentile),
                        "GAMMA_VOLATILITY_MAX_IV_RV": str(volatility_max_iv_rv),
                        "GAMMA_VOLATILITY_MAX_IV_PERCENTILE": str(volatility_max_iv_percentile),
                        "DATA_RETENTION_DAYS": str(retention),
                    })

# ---------------------------------------------------------------------------
# 资金与持仓：策略预算总览 + 虚拟持仓归属
# ---------------------------------------------------------------------------
with capital_tab:
    strategy_names = [
        "core_satellite", "earnings_gamma", "volatility_long_gamma", "SMA Cross",
        "RSI Mean Reversion", "20 Day Breakout", "earnings_gamma_manual",
    ]

    with st.container(border=True):
        st.subheader("策略资金配额")
        st.caption("这里仅展示策略预算；修改资金配额请前往「策略与资金控制」。")
        budget_table = pd.DataFrame(store.strategy_risk_budgets())
        if budget_table.empty:
            st.info("尚未初始化策略预算，请进入组合风险页面完成配置。")
        else:
            st.dataframe(
                budget_table[["strategy_id", "capital_ratio", "max_loss_ratio", "enabled"]],
                hide_index=True,
                column_config={
                    "strategy_id": st.column_config.TextColumn("策略", pinned=True),
                    "capital_ratio": st.column_config.NumberColumn("资金配额", format="percent"),
                    "max_loss_ratio": st.column_config.NumberColumn(
                        "最大损失预算", format="percent"
                    ),
                    "enabled": st.column_config.CheckboxColumn("启用"),
                },
            )
        st.page_link(
            "pages/15_strategy_control_center.py",
            label="前往策略与资金控制统一调整策略预算",
            icon=":material/tune:",
        )

    with st.container(border=True):
        st.subheader("虚拟持仓归属")
        st.caption("可新增分配，也可选择已有记录修改数量/成本。数量设为 0 后仍可再次编辑或删除。")

        @st.cache_data(ttl=30, max_entries=2, show_spinner=False)
        def load_positions_snapshot() -> pd.DataFrame:
            try:
                return broker.positions()
            except Exception:
                cached = store.broker_snapshot("positions")
                return pd.DataFrame((cached or {}).get("payload", []))

        positions = load_positions_snapshot()
        allocations = pd.DataFrame(store.virtual_allocations())
        if not allocations.empty:
            st.dataframe(china_time_columns(allocations).rename(columns={
                "strategy_id": "策略", "symbol": "代码", "quantity": "初始分配数量",
                "average_cost": "初始成本", "updated_at": "更新时间（中国）",
            }), hide_index=True)
        if positions.empty:
            st.info("券商当前没有持仓可分配。")
        else:
            allocation_mode = st.segmented_control(
                "操作", ["编辑已有分配", "新增分配"],
                default="编辑已有分配" if not allocations.empty else "新增分配",
            )
            if allocation_mode == "编辑已有分配" and not allocations.empty:
                allocation_records = allocations.to_dict("records")
                selected_index = st.selectbox(
                    "选择已有记录", range(len(allocation_records)),
                    format_func=lambda index: (
                        f"{allocation_records[index]['strategy_id']} · "
                        f"{allocation_records[index]['symbol']} · "
                        f"当前 {float(allocation_records[index]['quantity']):g}"
                    ),
                )
                selected_allocation = allocation_records[selected_index]
                allocation_strategy = str(selected_allocation["strategy_id"])
                allocation_symbol = str(selected_allocation["symbol"])
                initial_qty = float(selected_allocation["quantity"])
                initial_cost = float(selected_allocation["average_cost"])
            else:
                allocation_strategy = st.selectbox(
                    "策略", strategy_names, key="new_allocation_strategy"
                )
                allocation_symbol = st.selectbox(
                    "券商持仓", positions["code"].tolist(), key="new_allocation_symbol"
                )
                initial_qty = 0.0
                broker_selected = positions[positions["code"] == allocation_symbol].iloc[0]
                initial_cost = float(broker_selected.get("cost_price", 0) or 0)

            broker_rows = positions[positions["code"] == allocation_symbol]
            broker_qty = float(broker_rows.iloc[0]["qty"]) if not broker_rows.empty else 0.0
            record_key = f"{allocation_strategy}:{allocation_symbol}"
            with st.form(f"virtual_allocation:{record_key}"):
                st.write(
                    f"策略：`{allocation_strategy}`　标的：`{allocation_symbol}`　"
                    f"富途持仓：`{broker_qty:g}`"
                )
                allocation_qty = st.number_input(
                    "分配数量", 0.0, max(broker_qty, initial_qty), initial_qty,
                )
                allocation_cost = st.number_input(
                    "初始成本", 0.0, 10_000_000.0, initial_cost,
                )
                if st.form_submit_button("保存分配", type="primary"):
                    other = sum(
                        float(row["quantity"]) for row in store.virtual_allocations()
                        if row["symbol"] == allocation_symbol
                        and row["strategy_id"] != allocation_strategy
                    )
                    if other + allocation_qty > broker_qty:
                        st.error("所有策略的初始分配数量不能超过券商总持仓。")
                    else:
                        store.set_virtual_allocation(
                            allocation_strategy, allocation_symbol, allocation_qty, allocation_cost
                        )
                        store.audit(
                            "VIRTUAL_ALLOCATION_CHANGED",
                            {"actor": "streamlit_admin", "strategy": allocation_strategy,
                             "symbol": allocation_symbol, "quantity": allocation_qty},
                        )
                        st.success("虚拟持仓归属已更新。")
                        st.rerun()
            if allocation_mode == "编辑已有分配" and not allocations.empty:
                if st.button("删除这条错误记录", type="secondary"):
                    store.delete_virtual_allocation(allocation_strategy, allocation_symbol)
                    store.audit(
                        "VIRTUAL_ALLOCATION_DELETED",
                        {"actor": "streamlit_admin", "strategy": allocation_strategy,
                         "symbol": allocation_symbol},
                    )
                    st.rerun()

        strict = os.getenv("ENFORCE_VIRTUAL_POSITIONS", "false").lower() == "true"
        strict_enabled = st.toggle("严格执行策略持仓归属", value=strict)
        if strict_enabled != strict:
            save_runtime_settings({"ENFORCE_VIRTUAL_POSITIONS": str(strict_enabled).lower()})
            store.audit(
                "VIRTUAL_POSITION_ENFORCEMENT_CHANGED",
                {"actor": "streamlit_admin", "enabled": strict_enabled},
            )
            st.rerun()

# ---------------------------------------------------------------------------
# 校准建议：逐条人工确认
# ---------------------------------------------------------------------------
with calibration_tab:
    with st.container(border=True):
        st.subheader("风险参数自动校准建议")
        st.caption("系统只根据模拟成交生成建议，不会自动放宽或修改阈值；必须由管理员逐条确认。")
        if st.button("根据模拟数据生成校准建议"):
            generated = generate_recommendations(store)
            if generated:
                st.success(f"已生成 {len(generated)} 条建议。")
            else:
                st.info("数据不足，暂时无法生成建议。")
        recommendations = pd.DataFrame(store.risk_recommendations())
        if recommendations.empty:
            pending_recommendations = recommendations
        else:
            pending_recommendations = recommendations[
                recommendations["status"] == "PENDING"
            ]
        if pending_recommendations.empty:
            st.info("暂无待审核建议。")
        else:
            st.dataframe(china_time_columns(pending_recommendations).rename(columns={
                "parameter": "参数", "current_value": "当前值", "recommended_value": "建议值",
                "reason": "依据", "created_at": "生成时间（中国）",
            }).drop(columns=["id", "status", "decided_at"], errors="ignore"), hide_index=True)
            recommendation_id = st.selectbox(
                "选择建议", pending_recommendations["id"].tolist()
            )
            approve_col, reject_col = st.columns(2)
            if approve_col.button("批准并应用建议", type="primary"):
                row = pending_recommendations[
                    pending_recommendations["id"] == recommendation_id
                ].iloc[0].to_dict()
                changes = recommendation_changes(row)
                save_runtime_settings(changes)
                store.decide_risk_recommendation(int(recommendation_id), "APPROVED")
                store.audit(
                    "RISK_RECOMMENDATION_APPROVED",
                    {"id": int(recommendation_id), "changes": changes},
                )
                st.rerun()
            if reject_col.button("拒绝建议"):
                store.decide_risk_recommendation(int(recommendation_id), "REJECTED")
                store.audit("RISK_RECOMMENDATION_REJECTED", {"id": int(recommendation_id)})
                st.rerun()
