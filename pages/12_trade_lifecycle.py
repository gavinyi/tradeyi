from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.option_lifecycle import monitor_option_lifecycle
from futu_quant.store import TradingStore
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="交易生命周期", page_icon="🔄", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

st.title("交易生命周期")
st.caption("统一查看订单状态变化，以及期权到期、行权和指派风险。全部数据仅来自富途模拟账户。")

if st.button("立即刷新生命周期检查", icon=":material/refresh:"):
    try:
        with st.spinner("正在读取模拟账户持仓与行情……"):
            rows = monitor_option_lifecycle(broker, store)
        st.success(f"检查完成，共检查 {len(rows)} 个期权持仓。")
    except BrokerError as exc:
        st.error(f"检查失败：{exc}")

lifecycle = pd.DataFrame(store.latest_option_lifecycle())
events = pd.DataFrame(store.order_state_events())

with st.container(horizontal=True):
    total = len(lifecycle)
    critical = int(lifecycle["severity"].isin(["CRITICAL", "HIGH"]).sum()) if not lifecycle.empty else 0
    expiring = int((lifecycle["days_to_expiry"] <= 3).sum()) if not lifecycle.empty else 0
    unknown = int((events["new_status"] == "UNKNOWN").sum()) if not events.empty else 0
    st.metric("期权持仓", total, border=True)
    st.metric("高风险到期项目", critical, border=True)
    st.metric("3 天内到期", expiring, border=True)
    st.metric("历史未知订单状态", unknown, border=True)

option_tab, plan_tab, order_tab, guide_tab = st.tabs(
    ["期权到期风险", "到期处置计划", "订单状态历史", "处置说明"]
)

with option_tab:
    if lifecycle.empty:
        st.info("当前模拟账户没有需要监测的期权持仓，或服务尚未完成首次检查。")
    else:
        display = chinese_columns(lifecycle.drop(columns=["id", "created_at"], errors="ignore"))
        st.dataframe(
            display,
            hide_index=True,
            width="stretch",
            column_config={
                "行权价": st.column_config.NumberColumn(format="$%.3f"),
                "正股价格": st.column_config.NumberColumn(format="$%.2f"),
                "每份内在价值": st.column_config.NumberColumn(format="$%.2f"),
                "预计资金需求": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        st.warning(
            "当前版本默认只监测和告警，不会自动平仓、行权或接受指派。"
            "请在到期前结合流动性、税务和账户资金人工确认处置方案。"
        )

with plan_tab:
    st.subheader("持仓级到期处置计划")
    st.caption(
        "自动平仓默认关闭；只有管理员为具体合约启用后，统一服务才会在指定到期日和收盘窗口内执行。"
    )
    if lifecycle.empty:
        st.info("当前没有期权持仓，暂时无法创建新的持仓处置计划。")
    else:
        codes = lifecycle["code"].astype(str).tolist()
        code = st.selectbox("期权合约", codes, key="lifecycle_plan_code")
        existing = store.option_disposition(code) or {}
        action_labels = {
            "MONITOR": "继续监测",
            "MANUAL_REVIEW": "到期前人工复核",
            "ACCEPT_EXERCISE": "接受行权或指派",
            "AUTO_CLOSE": "到期日前自动平仓",
        }
        actions = list(action_labels)
        current_action = existing.get("action", "MONITOR")
        with st.form("option_disposition_form", border=True):
            action = st.selectbox(
                "处置方式", actions, index=actions.index(current_action),
                format_func=lambda value: action_labels[value],
            )
            close_days = st.number_input(
                "提前多少个交易日进入平仓窗口", min_value=0, max_value=10,
                value=int(existing.get("close_days_before", 0)), step=1,
            )
            close_minutes = st.number_input(
                "收盘前多少分钟允许自动平仓", min_value=15, max_value=180,
                value=int(existing.get("close_window_minutes", 60)), step=15,
            )
            enabled = st.checkbox(
                "启用此处置计划",
                value=bool(existing.get("enabled", False)),
            )
            confirmed = st.checkbox(
                "我确认自动平仓只会提交到富途模拟盘，并接受限价单可能无法成交",
            )
            submitted = st.form_submit_button(
                "保存处置计划", type="primary", disabled=not admin,
            )
            if submitted:
                if action == "AUTO_CLOSE" and enabled and not confirmed:
                    st.error("启用自动平仓前请勾选风险确认。")
                else:
                    store.save_option_disposition(
                        code, action, int(close_days), int(close_minutes), enabled, "admin",
                    )
                    st.success("处置计划已保存。")
                    st.rerun()
    plans = pd.DataFrame(store.option_dispositions())
    if not plans.empty:
        st.dataframe(
            chinese_columns(plans), hide_index=True, width="stretch",
            column_config={"enabled": st.column_config.CheckboxColumn("已启用")},
        )
    st.warning(
        "自动平仓只支持有明确策略归属的正整数期权多头持仓。短仓、归属不足、无有效买一价或"
        "不在常规交易时段时会被阻止，不会绕过统一风控。"
    )

with order_tab:
    if events.empty:
        st.info("暂无统一执行服务产生的订单状态记录。")
    else:
        display = chinese_columns(events.drop(columns=["id"], errors="ignore"))
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption(
            "状态来源包括策略、统一执行器、券商推送、轮询对账和恢复流程；"
            "UNKNOWN 表示券商当前与历史订单中都找不到该订单，需要人工复核。"
        )

with guide_tab:
    st.markdown(
        """
        - **CRITICAL**：已到期、当日到期，或预计行权/指派存在资金和正股缺口。
        - **HIGH**：1 天内到期且价内，或临近到期存在明显交割义务。
        - **WARNING**：3 天内到期，需要明确平仓、行权或接受指派方案。
        - **INFO**：7 天内到期，进入重点观察窗口。

        长 Call 价内可能需要支付行权价并接收正股；长 Put 价内可能需要交付正股；
        短 Call 和短 Put 分别存在交付正股或支付资金接受正股的指派义务。
        """
    )
