from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from futu_quant.config import Settings
from futu_quant.crypto_review import build_crypto_strategy_review, crypto_review_markdown
from futu_quant.store import TradingStore
from futu_quant.ui import render_sidebar


st.set_page_config(page_title="币策略复盘与执行质量", page_icon="📋", layout="wide")
render_sidebar(Settings.load())
st.title("币策略复盘与执行质量")
st.caption("汇总自动雷达、策略判断、Shadow/欧易模拟执行、当前持仓和阻断原因。")

today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
with st.container(horizontal=True):
    session_date = st.date_input("复盘日期（北京时间）", value=today)
    quick_days = st.number_input("向前查看天数", 0, 30, 0)
    active_date = session_date - timedelta(days=int(quick_days))
    st.metric("实际复盘日", str(active_date), border=True)

if st.button("生成复盘", type="primary", icon=":material/summarize:"):
    st.session_state.crypto_review_date = str(active_date)

if "crypto_review_date" not in st.session_state:
    st.info("选择日期后点击“生成复盘”，即可查看策略与执行质量。")
    st.stop()

review = build_crypto_strategy_review(
    TradingStore(), st.session_state.crypto_review_date,
)
with st.container(horizontal=True):
    st.metric("策略判断", review.metrics["策略判断"], border=True)
    st.metric("交易信号", review.metrics["交易信号"], border=True)
    st.metric("执行记录", review.metrics["执行记录"], border=True)
    st.metric("信号执行率", f"{review.metrics['信号执行率']:.1%}", border=True)
    st.metric("执行阻断", review.metrics["执行阻断"], border=True)
    st.metric("雷达自动加入", review.metrics["雷达自动加入"], border=True)
    st.metric("当前持仓", review.metrics["当前持仓"], border=True)

for item in review.narrative:
    st.info(item)

report = crypto_review_markdown(review)
st.download_button(
    "下载本次币策略复盘",
    data=report,
    file_name=f"币策略复盘_{review.session_date}.md",
    mime="text/markdown",
    icon=":material/download:",
)

execution_tab, radar_tab, holding_tab, blocker_tab, decision_tab = st.tabs([
    "执行质量", "自动雷达", "当前持仓", "阻断原因", "策略判断",
])
with execution_tab:
    if review.executions.empty:
        st.info("本日没有执行记录。")
    else:
        st.dataframe(
            review.executions,
            hide_index=True,
            column_config={
                "数量": st.column_config.NumberColumn(format="%.8f"),
                "价格": st.column_config.NumberColumn(format="$%.6f"),
                "金额": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
with radar_tab:
    if review.radar.empty:
        st.info("本日没有自动雷达扫描记录。")
    else:
        st.dataframe(review.radar, hide_index=True)
with holding_tab:
    if review.holdings.empty:
        st.info("当前没有币策略 Shadow 持仓。")
    else:
        st.dataframe(
            review.holdings,
            hide_index=True,
            column_config={
                "当前数量": st.column_config.NumberColumn(format="%.8f"),
                "开仓均价": st.column_config.NumberColumn(format="$%.6f"),
            },
        )
with blocker_tab:
    if review.blockers.empty:
        st.success("本日没有策略执行阻断。")
    else:
        st.dataframe(review.blockers, hide_index=True)
with decision_tab:
    if review.decisions.empty:
        st.info("本日没有策略判断记录。")
    else:
        st.dataframe(review.decisions, hide_index=True)
