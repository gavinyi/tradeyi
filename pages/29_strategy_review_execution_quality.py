from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.strategy_review import build_strategy_review, review_markdown
from futu_quant.trade_diagnostics import diagnostic_window, latest_us_session_date
from futu_quant.ui import china_time_columns, chinese_columns, render_sidebar


st.set_page_config(page_title="策略复盘与执行质量", page_icon="📋", layout="wide")
settings = Settings.load()
render_sidebar(settings)
st.title("策略复盘与执行质量")
st.caption("一键复盘昨晚策略运行：整体报告、执行质量、波动率期权保护状态、Core Satellite 调仓质量。")


def display_percent_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce").map(
                lambda value: "" if pd.isna(value) else f"{value:.2%}"
            )
    return result


default_session = latest_us_session_date()
left, middle, right = st.columns(3)
session_date = left.date_input("复盘交易日（美东）", value=default_session)
lookback_days = middle.number_input("快速切换天数", 0, 14, 0)
if lookback_days:
    session_date = default_session - timedelta(days=int(lookback_days))
right.metric("实际复盘日", str(session_date), border=True)

if st.button("生成复盘", type="primary", icon=":material/summarize:"):
    st.session_state.strategy_review_session_date = str(session_date)

if "strategy_review_session_date" not in st.session_state:
    st.info("选择交易日后，点击“生成复盘”即可汇总整体报告和执行质量面板。")
    st.stop()

active_date = st.session_state.strategy_review_session_date
window = diagnostic_window(pd.to_datetime(active_date).date())

with st.spinner("正在汇总策略日志、订单、成交和执行质量……"):
    review = build_strategy_review(TradingStore(), window)

st.subheader("整体报告")
with st.container(horizontal=True):
    st.metric("策略决策", review.metrics["策略决策"], border=True)
    st.metric("订单数", review.metrics["订单数"], border=True)
    st.metric("成交订单", review.metrics["成交订单"], border=True)
    st.metric("阻断次数", review.metrics["阻断次数"], border=True)
    st.metric("期权已实现盈亏", f"${float(review.metrics['期权已实现盈亏']):,.2f}", border=True)
    st.metric("盈利期权合约", review.metrics["盈利期权合约"], border=True)
    st.metric("亏损期权合约", review.metrics["亏损期权合约"], border=True)
    st.metric("期权未平持仓", review.metrics["期权未平持仓"], border=True)
    st.metric("运行退化/超时", review.metrics["退化任务"], border=True)
    st.metric("运行异常", review.metrics["异常任务"], border=True)

for item in review.narrative:
    st.info(item)

report_markdown = review_markdown(review)
st.download_button(
    "下载本次复盘报告",
    data=report_markdown,
    file_name=f"策略复盘_{active_date}.md",
    mime="text/markdown",
    icon=":material/download:",
)

pnl_tab, orders_tab, option_tab, core_tab, runtime_tab, blockers_tab, raw_tab = st.tabs([
    "期权盈亏拆解", "订单执行质量", "波动率期权质量", "Core Satellite 调仓", "运行健康", "阻塞原因", "原始报告",
])

with pnl_tab:
    st.subheader("期权盈亏拆解")
    st.caption("按昨晚窗口内的实际成交聚合，每个合约展示买入/卖出均价、平仓数量、剩余持仓和估算盈亏。")
    if review.option_pnl_breakdown.empty:
        st.info("本窗口没有可计算的期权成交盈亏。")
    else:
        pnl_values = pd.to_numeric(review.option_pnl_breakdown["净盈亏"], errors="coerce").fillna(0)
        with st.container(horizontal=True):
            st.metric("净盈亏", f"${pnl_values.sum():,.2f}", border=True)
            st.metric("盈利合约", int(pnl_values.gt(0).sum()), border=True)
            st.metric("亏损合约", int(pnl_values.lt(0).sum()), border=True)
            closed = pd.to_numeric(review.option_pnl_breakdown["平仓数量"], errors="coerce").fillna(0).gt(0)
            wins = pnl_values[closed].gt(0).sum()
            losses = pnl_values[closed].lt(0).sum()
            st.metric("已平胜率", f"{wins / max(1, wins + losses):.0%}", border=True)
            st.metric("毛盈利", f"${float(review.metrics['期权毛盈利']):,.2f}", border=True)
            st.metric("毛亏损", f"${float(review.metrics['期权毛亏损']):,.2f}", border=True)
        view = china_time_columns(review.option_pnl_breakdown)
        st.dataframe(
            view,
            hide_index=True,
            column_config={
                "买入均价": st.column_config.NumberColumn("买入均价", format="$%.2f"),
                "卖出均价": st.column_config.NumberColumn("卖出均价", format="$%.2f"),
                "买入金额": st.column_config.NumberColumn("买入金额", format="$%.2f"),
                "卖出金额": st.column_config.NumberColumn("卖出金额", format="$%.2f"),
                "毛盈亏": st.column_config.NumberColumn("毛盈亏", format="$%.2f"),
                "费用": st.column_config.NumberColumn("费用", format="$%.2f"),
                "净盈亏": st.column_config.NumberColumn("净盈亏", format="$%.2f"),
            },
        )

with orders_tab:
    st.subheader("订单执行质量")
    if review.orders.empty:
        st.info("本窗口没有订单。")
    else:
        view = china_time_columns(review.orders)
        st.dataframe(chinese_columns(view), hide_index=True)

with option_tab:
    st.subheader("波动率期权质量面板")
    st.caption("重点看当前盈亏、峰值盈利、盈利回吐、保护档和为什么没有卖。")
    if review.option_quality.empty:
        st.info("本窗口没有波动率期权记录。")
    else:
        view = display_percent_columns(
            review.option_quality,
            ["当前盈亏", "峰值盈利", "盈利回吐", "保护启动", "回吐阈值", "锁定盈利"],
        )
        st.dataframe(view, hide_index=True)

with core_tab:
    st.subheader("Core Satellite 调仓质量")
    if review.core_quality.empty:
        st.info("本窗口没有 Core Satellite 调仓订单。")
    else:
        view = display_percent_columns(review.core_quality, ["当前权重", "目标权重"])
        st.dataframe(view, hide_index=True)

with runtime_tab:
    st.subheader("运行健康与断点定位")
    st.caption("用于区分：昨晚没交易到底是策略条件不满足，还是 OpenD/对账/期权历史采集等运行层退化。")
    if review.runtime_health.empty:
        st.info("本窗口没有服务任务记录。")
    else:
        view = review.runtime_health.copy()
        if "最后时间" in view:
            view["最后时间（中国）"] = pd.to_datetime(
                view["最后时间"], utc=True, errors="coerce",
            ).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M:%S")
            view = view.drop(columns=["最后时间"])
        st.dataframe(view, hide_index=True)

with blockers_tab:
    st.subheader("阻塞原因")
    if review.blockers.empty:
        st.success("本窗口没有阻塞记录。")
    else:
        st.dataframe(review.blockers, hide_index=True)

with raw_tab:
    st.subheader("Markdown 原始报告")
    st.code(report_markdown, language="markdown")
