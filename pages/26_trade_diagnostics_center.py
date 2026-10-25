from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.trade_diagnostics import (
    build_diagnostic_report, diagnostic_markdown, diagnostic_timeline,
    diagnostic_window, latest_us_session_date,
)
from futu_quant.ui import STRATEGY_LABELS, render_sidebar, translate_common_values


st.set_page_config(page_title="交易诊断中心", page_icon="🔎", layout="wide")
render_sidebar(Settings.load())
st.title("交易诊断中心")
st.caption("从信号、候选、交易前检查、统一风控、订单到成交，一处查看最终停在哪一关。")

@st.cache_data(ttl=30, max_entries=10, show_spinner=False)
def load_diagnostic_report(selected_date: date) -> dict:
    return build_diagnostic_report(TradingStore(), diagnostic_window(selected_date))


session_date = st.date_input(
    "美股交易日", value=latest_us_session_date(), max_value=date.today(),
    help="按美股交易日归档；页面时间统一显示为中国时间。",
)
window = diagnostic_window(session_date)
report = load_diagnostic_report(session_date)
st.caption(f"本次诊断窗口（中国时间）：{window.china_label}")

metrics = report["metrics"]
with st.container(horizontal=True):
    st.metric("扫描标的", metrics["扫描标的"], border=True)
    st.metric("触发标的", metrics["触发标的"], border=True)
    st.metric("生成自动策略", metrics["生成自动策略"], border=True)
    st.metric("订单意图", metrics["订单意图"], border=True)
    st.metric("拒绝或失败", metrics["已拒绝/失败"], border=True)
    st.metric("已提交或 Shadow", metrics["已提交或Shadow"], border=True)
    st.metric("Shadow 成交", metrics["Shadow成交"], border=True)

summary = report["summary"].copy()
if summary.empty:
    st.info("该交易日暂无策略诊断记录。")
    st.stop()

strategies = sorted(summary["策略"].astype(str).unique())
selected_strategies = st.multiselect(
    "策略筛选", strategies, default=strategies,
    format_func=lambda value: STRATEGY_LABELS.get(value, value),
)
query = st.text_input("代码筛选", placeholder="例如 AAPL、US.AAPL260724P335000")
visible = summary[summary["策略"].isin(selected_strategies)].copy()
if query.strip():
    visible = visible[
        visible["代码"].astype(str).str.contains(query.strip(), case=False, regex=False)
    ]

blocked_outcomes = {"已拒绝", "提交失败", "未形成订单", "候选被拒绝", "被阻断"}
problem_count = int(visible["最终结果"].isin(blocked_outcomes).sum())
if problem_count:
    st.warning(
        f"发现 {problem_count} 条未完成执行的策略链路。"
        "下表已把最重要的失败记录排在前面。",
        icon=":material/warning:",
    )
else:
    st.success("当前筛选范围没有发现阻断或失败记录。", icon=":material/check_circle:")

if not visible.empty:
    visible["最后时间"] = pd.to_datetime(
        visible["最后时间"], errors="coerce", utc=True,
    ).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    preferred_columns = [
        "策略", "代码", "交易方向", "组合分区", "择时/择标的依据",
        "当前权重", "目标权重", "分区预算", "现金缓冲",
        "完整目标数量", "当前执行批次", "总批数",
        "计划/订单数量", "限价", "计划名义金额", "完整目标金额", "执行节奏原因",
        "已成交数量", "成交均价", "实际成交金额", "最终阶段", "最终结果", "最终原因",
        "买卖信号次数", "阻断次数", "订单意图数", "Shadow成交数", "最后时间",
    ]
    visible = visible[[column for column in preferred_columns if column in visible.columns]]
st.subheader("最终诊断结果")
st.caption("计划名义金额不代表成交；只有已成交数量大于 0 时，实际成交金额才表示真实撮合结果。")
st.dataframe(
    translate_common_values(visible), hide_index=True,
    column_config={
        "策略": st.column_config.TextColumn(pinned=True),
        "代码": st.column_config.TextColumn(pinned=True),
        "完整目标数量": st.column_config.NumberColumn(format="%.0f"),
        "当前执行批次": st.column_config.NumberColumn(format="%.0f"),
        "总批数": st.column_config.NumberColumn(format="%.0f"),
        "计划/订单数量": st.column_config.NumberColumn(format="%.0f"),
        "限价": st.column_config.NumberColumn(format="$%.2f"),
        "计划名义金额": st.column_config.NumberColumn(format="$%.2f"),
        "完整目标金额": st.column_config.NumberColumn(format="$%.2f"),
        "已成交数量": st.column_config.NumberColumn(format="%.0f"),
        "成交均价": st.column_config.NumberColumn(format="$%.2f"),
        "实际成交金额": st.column_config.NumberColumn(format="$%.2f"),
        "当前权重": st.column_config.NumberColumn(format="percent"),
        "目标权重": st.column_config.NumberColumn(format="percent"),
        "分区预算": st.column_config.NumberColumn(format="percent"),
        "现金缓冲": st.column_config.NumberColumn(format="percent"),
        "最后时间": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
    },
)

left, right = st.columns(2)
with left:
    with st.container(border=True):
        st.subheader("未下单原因汇总")
        if report["blockers"].empty:
            st.info("本窗口没有未下单原因。")
        else:
            st.bar_chart(report["blockers"], x="未下单原因", y="次数", horizontal=True)
            st.dataframe(report["blockers"], hide_index=True)
with right:
    with st.container(border=True):
        st.subheader("下一步去哪里处理")
        st.page_link(
            "pages/10_volatility_options_strategy.py", label="调整波动率候选与预算",
            icon=":material/waves:",
        )
        st.page_link(
            "pages/7_risk_center.py", label="检查全局风控与急停",
            icon=":material/shield:",
        )
        st.page_link(
            "pages/4_order_management.py", label="查看订单、Shadow 与撤单",
            icon=":material/receipt_long:",
        )
        st.page_link(
            "pages/1_trading_config.py", label="调整 Core Satellite 参数",
            icon=":material/settings:",
        )

st.subheader("单笔执行时间线")
choices = [f"{row['策略']}｜{row['代码']}" for _, row in visible.iterrows()]
if choices:
    selected = st.selectbox(
        "选择策略与代码", choices,
        format_func=lambda value: value.replace(
            value.split("｜", 1)[0],
            STRATEGY_LABELS.get(value.split("｜", 1)[0], value.split("｜", 1)[0]),
            1,
        ),
    )
    selected_strategy, selected_symbol = selected.split("｜", 1)
    timeline = diagnostic_timeline(report, selected_strategy, selected_symbol)
    if timeline.empty:
        st.info("该记录没有更细的时间线。")
    else:
        timeline["时间"] = pd.to_datetime(
            timeline["时间"], errors="coerce", utc=True,
        ).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        st.dataframe(
            translate_common_values(timeline), hide_index=True,
            column_config={
                "时间": st.column_config.DatetimeColumn(
                    "时间（中国）", format="YYYY-MM-DD HH:mm:ss",
                ),
            },
        )

report_markdown = diagnostic_markdown(report)
st.download_button(
    "下载本交易日诊断报告", data=report_markdown,
    file_name=f"交易诊断_{session_date}.md", mime="text/markdown",
    icon=":material/download:",
)
