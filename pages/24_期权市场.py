from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.market_information import combined_watchlist, rank_option_market
from futu_quant.ui import chinese_columns, render_sidebar


st.set_page_config(page_title="期权市场", page_icon="🧭", layout="wide")
settings = Settings.load()
render_sidebar(settings)

st.title("期权市场")
st.caption(
    "只按需读取一个标的、一个到期日的期权链，复用全局缓存和限频器；本页用于观察，不会提交订单。"
)


@st.cache_data(ttl="15m", max_entries=30, show_spinner=False)
def load_expirations(symbol: str) -> pd.DataFrame:
    return FutuPaperBroker(Settings.load()).option_expirations(symbol)


@st.cache_data(ttl="60s", max_entries=20, show_spinner=False)
def load_option_market(symbol: str, expiration: str) -> tuple[float, pd.DataFrame]:
    broker = FutuPaperBroker(Settings.load())
    owner = broker.snapshots([symbol])
    if owner.empty:
        raise BrokerError(f"没有取得 {symbol} 正股行情")
    spot = float(owner.iloc[0].get("last_price", 0) or 0)
    chain = broker.option_chain(symbol, expiration)
    if chain.empty:
        return spot, chain
    codes = chain["code"].astype(str).tolist()
    quotes = broker.snapshots_tolerant(codes)
    quote_columns = [
        column for column in quotes.columns
        if column == "code" or column not in chain.columns
    ]
    merged = chain.merge(quotes[quote_columns], on="code", how="left")
    return spot, rank_option_market(merged, spot)


symbols = tuple(combined_watchlist(settings)["代码"].astype(str))
if not symbols:
    st.info("请先在交易配置中添加标的。")
    st.stop()

underlying = st.selectbox("正股或 ETF", symbols)
try:
    expirations = load_expirations(underlying)
except BrokerError as exc:
    st.error(f"到期日读取失败：{exc}")
    st.stop()

if expirations.empty:
    st.info("该标的没有可用期权到期日，可能不是可交易期权标的。")
    st.stop()

expiration_rows = expirations.sort_values("strike_time")
expiration = st.selectbox(
    "到期日",
    expiration_rows["strike_time"].astype(str).tolist(),
    format_func=lambda value: (
        f"{value} · "
        f"{int(expiration_rows.loc[expiration_rows['strike_time'].astype(str) == value, 'option_expiry_date_distance'].iloc[0])} DTE"
    ),
)

with st.form("option_market_filters", border=True):
    option_type = st.segmented_control(
        "期权类型", ["全部", "CALL", "PUT"], default="全部", selection_mode="single"
    )
    min_open_interest = st.number_input("最低未平仓量", 0, 10_000_000, 100, step=100)
    max_spread = st.number_input(
        "最大买卖价差", 0.01, 2.0, 0.30, step=0.05,
        format="%.2f", help="0.30 表示中间价的 30%。",
    )
    strike_band = st.number_input(
        "最大距平值幅度", 0.01, 2.0, 0.30, step=0.05,
        format="%.2f", help="0.30 表示行权价距离正股价格不超过 30%。",
    )
    submitted = st.form_submit_button(
        "读取并筛选期权链", type="primary", icon=":material/travel_explore:"
    )

request_key = f"{underlying}:{expiration}"
if submitted:
    try:
        with st.spinner("正在读取期权链和实时盘口……"):
            spot, result = load_option_market(underlying, expiration)
        st.session_state.option_market_request_key = request_key
        st.session_state.option_market_spot = spot
        st.session_state.option_market_frame = result
    except BrokerError as exc:
        st.error(f"期权链读取失败：{exc}")

result = (
    st.session_state.get("option_market_frame", pd.DataFrame())
    if st.session_state.get("option_market_request_key") == request_key
    else pd.DataFrame()
)
if result.empty:
    st.info("选择筛选条件后点击“读取并筛选期权链”。切换标的或到期日不会自动请求。")
    st.stop()

spot = float(st.session_state.get("option_market_spot", 0))
filtered = result.copy()
if option_type and option_type != "全部":
    filtered = filtered[filtered["option_type"].astype(str) == option_type]
filtered = filtered[
    (pd.to_numeric(filtered["option_open_interest"], errors="coerce").fillna(0) >= min_open_interest)
    & (filtered["买卖价差"].fillna(float("inf")) <= max_spread)
    & (filtered["距平值幅度"].fillna(float("inf")) <= strike_band)
]

with st.container(horizontal=True):
    st.metric("正股最新价", f"${spot:,.2f}", border=True)
    st.metric("期权链合约数", len(result), border=True)
    st.metric("筛选后合约数", len(filtered), border=True)
    st.metric(
        "中位买卖价差",
        f"{filtered['买卖价差'].median():.2%}" if not filtered.empty else "—",
        border=True,
    )
    st.metric(
        "最高流动性评分",
        f"{filtered['流动性评分'].max():.1f}" if not filtered.empty else "—",
        border=True,
    )

if filtered.empty:
    st.warning("当前没有合约满足筛选条件；可以适度放宽未平仓量、价差或距平值幅度。")
else:
    visible = [
        "code", "option_type", "strike_price", "bid_price", "ask_price", "中间价",
        "买卖价差", "距平值幅度", "流动性评分", "volume", "option_open_interest",
        "option_implied_volatility", "option_delta", "option_gamma", "option_theta",
        "option_vega", "option_contract_multiplier", "update_time",
    ]
    visible = [column for column in visible if column in filtered.columns]
    display = chinese_columns(filtered[visible].head(300))
    st.dataframe(
        display,
        hide_index=True,
        column_config={
            "代码": st.column_config.TextColumn("代码", pinned=True),
            "行权价": st.column_config.NumberColumn(format="%.2f"),
            "买一价": st.column_config.NumberColumn(format="%.3f"),
            "卖一价": st.column_config.NumberColumn(format="%.3f"),
            "中间价": st.column_config.NumberColumn(format="%.3f"),
            "买卖价差": st.column_config.NumberColumn(format="percent"),
            "距平值幅度": st.column_config.NumberColumn(format="percent"),
            "流动性评分": st.column_config.ProgressColumn(min_value=0, max_value=100),
            "隐含波动率": st.column_config.NumberColumn(format="percent"),
        },
    )
    st.caption("流动性评分只用于排序盘口质量，不代表方向判断、胜率或收益预测。")

with st.container(horizontal=True):
    st.page_link(
        "pages/2_期权配置.py", label="前往期权配置保存候选",
        icon=":material/settings_suggest:",
    )
    st.page_link(
        "pages/10_波动率期权策略.py", label="前往波动率策略生成决策",
        icon=":material/waves:",
    )
