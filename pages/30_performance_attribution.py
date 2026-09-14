from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.performance import performance_buckets
from futu_quant.store import TradingStore
from futu_quant.ui import render_sidebar
from futu_quant.config import Settings

st.set_page_config(page_title="收益与交易成本", page_icon="📊", layout="wide")
settings = Settings.load()
render_sidebar(settings)
st.title("收益与交易成本分析")
st.caption("按成熟绩效系统的口径拆分：成交金额、手续费、价差成本、价格冲击和行情覆盖率。")

period_label = st.segmented_control("统计周期", ["日", "周", "月"], default="日")
period = {"日": "daily", "周": "weekly", "月": "monthly"}[period_label or "日"]

@st.cache_data(ttl=30, max_entries=3, show_spinner=False)
def load_buckets(selected_period: str) -> pd.DataFrame:
    return performance_buckets(TradingStore(), period=selected_period)

frame = load_buckets(period)
if frame.empty:
    st.info("当前还没有可统计的逐笔成交数据。")
    st.stop()

with st.container(horizontal=True):
    st.metric("成交笔数", f"{int(frame['成交笔数'].sum()):,}", border=True)
    st.metric("成交金额", f"${frame['成交金额'].sum():,.2f}", border=True)
    st.metric("手续费", f"${frame['手续费'].sum():,.2f}", border=True)
    st.metric("价差成本", f"${frame['价差成本'].sum():,.2f}", border=True)
    st.metric("价格冲击", f"${frame['价格冲击'].sum():,.2f}", border=True)
    st.metric("行情覆盖率", f"{frame['行情覆盖率'].mean():.1%}", border=True)

st.subheader("成本构成")
chart = frame.groupby("期间", as_index=False)[["手续费", "价差成本", "价格冲击"]].sum().set_index("期间")
st.bar_chart(chart)

st.subheader("按策略统计")
st.dataframe(frame, hide_index=True, width="stretch")
st.download_button(
    "下载统计 CSV", frame.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"performance_{period}.csv", mime="text/csv",
)
st.warning("已实现盈亏按 FIFO 平仓成交归因；未平仓浮盈、时间加权收益率和最大回撤需要结合期末权益快照与行情估值。成交现金流不等于收益。")
