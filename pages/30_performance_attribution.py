from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.performance import performance_buckets, performance_dashboard_data
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
def load_dashboard(selected_period: str) -> dict:
    data = performance_dashboard_data(TradingStore(), period=selected_period)
    return {key: value.to_dict() if isinstance(value, pd.DataFrame) else value for key, value in data.items()}

data = load_dashboard(period)
frame = pd.DataFrame(data["buckets"])
equity = pd.DataFrame(data["equity"])
fills = pd.DataFrame(data["fills"])
metrics = data["metrics"]
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
    st.metric("时间加权收益", f"{metrics['TWR']:.2%}", border=True)
    st.metric("最大回撤", f"{metrics['最大回撤']:.2%}", border=True)
    st.metric("Sharpe", f"{metrics['Sharpe']:.2f}", border=True)
    st.metric("Sortino", f"{metrics['Sortino']:.2f}", border=True)

st.subheader("成本构成")
chart = frame.groupby("期间", as_index=False)[["手续费", "价差成本", "价格冲击"]].sum().set_index("期间")
st.bar_chart(chart)

st.subheader("权益曲线与回撤")
if not equity.empty:
    curve = equity.set_index("created_at")[["total_assets"]]
    st.line_chart(curve)
    st.dataframe(equity[["created_at", "total_assets", "cash", "market_value", "期间"]], hide_index=True)
else:
    st.info("暂无权益快照，暂不能计算时间加权收益和回撤。")

st.subheader("按策略 / 标的 / 期权结构统计")
if not fills.empty:
    breakdown = fills.groupby(["strategy_id", "标的", "期权结构"], as_index=False).agg(
        成交笔数=("id", "count"), 成交数量=("quantity", "sum"),
        手续费=("fee_total", "sum"), 价差成本=("spread_cost", "sum"),
        价格冲击=("price_impact_cost", "sum"), 行情覆盖率=("mid_price", lambda s: float(s.notna().mean())),
    )
    st.dataframe(breakdown, hide_index=True, width="stretch")
else:
    st.info("暂无逐笔成交明细。")
st.dataframe(frame, hide_index=True, width="stretch")
st.download_button(
    "下载统计 CSV", frame.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"performance_{period}.csv", mime="text/csv",
)
if metrics["费用覆盖率"] < 0.8:
    st.warning(f"盘口覆盖率只有 {metrics['费用覆盖率']:.1%}，价差成本和价格冲击统计可能低估。")
st.warning("已实现盈亏按 FIFO 平仓成交归因；未平仓浮盈、时间加权收益率和最大回撤需要有效期末权益快照。成交现金流不等于收益。")
