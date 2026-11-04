from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.market_information import (
    BENCHMARK_SYMBOLS,
    combined_watchlist,
    normalize_market_snapshots,
    summarize_market_breadth,
)
from futu_quant.ui import render_sidebar
from futu_quant.volatility_optimizer import volatility_regime


st.set_page_config(page_title="市场与自选行情", page_icon="📊", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)

st.title("市场与自选行情")
st.caption("实时交易行情以富途为准；第三方历史数据只用于研究补全，不参与这里的最新价展示。")


@st.cache_data(ttl="30s", max_entries=20, show_spinner=False)
def load_quotes(symbols: tuple[str, ...]) -> pd.DataFrame:
    return FutuPaperBroker(Settings.load()).snapshots_tolerant(list(symbols))


@st.cache_data(ttl="30s", max_entries=4, show_spinner=False)
def load_positions() -> pd.DataFrame:
    return FutuPaperBroker(Settings.load()).positions()


@st.cache_data(ttl="5m", max_entries=30, show_spinner=False)
def load_bars(symbol: str, count: int = 260) -> pd.DataFrame:
    return FutuPaperBroker(Settings.load()).candles(symbol, count=count)


def refresh_market_data() -> None:
    load_quotes.clear()
    load_positions.clear()
    load_bars.clear()


with st.container(horizontal=True, horizontal_alignment="right"):
    if st.button("刷新行情", icon=":material/refresh:"):
        refresh_market_data()
        st.rerun()

try:
    positions = load_positions()
except BrokerError as exc:
    positions = pd.DataFrame()
    st.warning(f"持仓暂不可用，自选行情仍可查看：{exc}")

held_symbols = (
    tuple(positions.loc[positions["qty"] != 0, "code"].astype(str))
    if not positions.empty and {"qty", "code"}.issubset(positions.columns) else ()
)
watchlist = combined_watchlist(settings, held_symbols)
watch_symbols = tuple(watchlist["代码"].astype(str))

view = st.segmented_control(
    "行情功能",
    ["市场总览", "自选行情", "标的详情"],
    default="市场总览",
    selection_mode="single",
)

if view == "市场总览":
    now = datetime.now(ZoneInfo("America/New_York"))
    session = USMarketCalendar().session(now)
    if session.regular_open(now):
        market_status = "常规交易中"
    elif session.extended_open(now):
        market_status = "盘前或盘后"
    elif session.is_session:
        market_status = "今日已休市"
    else:
        market_status = "非交易日"

    try:
        benchmark_raw = load_quotes(BENCHMARK_SYMBOLS)
        benchmark = normalize_market_snapshots(benchmark_raw)
        watch_raw = load_quotes(watch_symbols) if watch_symbols else pd.DataFrame()
        watch_quotes = normalize_market_snapshots(watch_raw)
    except BrokerError as exc:
        st.error(f"富途行情读取失败：{exc}")
        st.stop()

    breadth = summarize_market_breadth(watch_quotes)
    with st.container(horizontal=True):
        st.metric("美股市场状态", market_status, border=True)
        st.metric("自选上涨占比", f"{breadth['上涨占比']:.0%}", border=True)
        st.metric("上涨 / 下跌", f"{breadth['上涨数']} / {breadth['下跌数']}", border=True)
        st.metric("自选平均涨跌", f"{breadth['平均涨跌幅']:+.2%}", border=True)
        st.metric(
            "行情新鲜度",
            f"{int(watch_quotes['行情状态'].eq('新鲜').sum())}/{len(watch_quotes)}",
            border=True,
        )

    with st.container(border=True):
        st.subheader("主要指数与风格基准")
        if benchmark.empty:
            st.info("富途当前没有返回基准行情。")
        else:
            st.dataframe(
                benchmark,
                hide_index=True,
                column_config={
                    "代码": st.column_config.TextColumn("代码", pinned=True),
                    "最新价": st.column_config.NumberColumn(format="%.2f"),
                    "涨跌额": st.column_config.NumberColumn(format="%+.2f"),
                    "涨跌幅": st.column_config.NumberColumn(format="percent"),
                    "日内振幅": st.column_config.NumberColumn(format="percent"),
                    "买卖价差": st.column_config.NumberColumn(format="percent"),
                    "成交量": st.column_config.NumberColumn(format="compact"),
                    "成交额": st.column_config.NumberColumn(format="compact"),
                },
            )

    with st.container(border=True):
        st.subheader("自选异动")
        if watch_quotes.empty:
            st.info("当前自选没有可用行情。")
        else:
            movers = watch_quotes.sort_values("涨跌幅", key=lambda values: values.abs(), ascending=False)
            st.dataframe(
                movers.head(15), hide_index=True,
                column_config={
                    "代码": st.column_config.TextColumn("代码", pinned=True),
                    "最新价": st.column_config.NumberColumn(format="%.2f"),
                    "涨跌幅": st.column_config.NumberColumn(format="percent"),
                    "日内振幅": st.column_config.NumberColumn(format="percent"),
                    "买卖价差": st.column_config.NumberColumn(format="percent"),
                    "成交量": st.column_config.NumberColumn(format="compact"),
                },
            )

elif view == "自选行情":
    source_filter = st.pills(
        "自选来源",
        ["全部", "普通策略", "Core Satellite", "财报 Gamma", "波动率期权", "当前持仓"],
        default="全部",
        selection_mode="single",
    )
    filtered = watchlist
    if source_filter and source_filter != "全部":
        if source_filter == "当前持仓":
            filtered = filtered[filtered["当前持仓"] == "是"]
        else:
            filtered = filtered[filtered["来源"].str.contains(source_filter, regex=False)]
    symbols = tuple(filtered["代码"].astype(str))
    try:
        quotes = normalize_market_snapshots(load_quotes(symbols)) if symbols else pd.DataFrame()
    except BrokerError as exc:
        st.error(f"自选行情读取失败：{exc}")
        st.stop()
    display = filtered.merge(quotes, on="代码", how="left")
    st.dataframe(
        display, hide_index=True,
        column_config={
            "代码": st.column_config.TextColumn("代码", pinned=True),
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌额": st.column_config.NumberColumn(format="%+.2f"),
            "涨跌幅": st.column_config.NumberColumn(format="percent"),
            "日内振幅": st.column_config.NumberColumn(format="percent"),
            "买卖价差": st.column_config.NumberColumn(format="percent"),
            "成交量": st.column_config.NumberColumn(format="compact"),
            "成交额": st.column_config.NumberColumn(format="compact"),
        },
    )
    st.caption("来源字段可同时包含多个策略；这里只展示行情，不会修改各策略的标的配置。")

elif view == "标的详情":
    if not watch_symbols:
        st.info("请先在交易配置中添加自选标的。")
        st.stop()
    symbol = st.selectbox("查看标的", watch_symbols)
    try:
        raw_quote = load_quotes((symbol,))
        bars = load_bars(symbol)
    except BrokerError as exc:
        st.error(f"标的详情读取失败：{exc}")
        st.stop()
    if raw_quote.empty or bars.empty:
        st.warning("该标的没有足够行情数据。")
        st.stop()
    quote = normalize_market_snapshots(raw_quote).iloc[0]
    regime = volatility_regime(raw_quote.iloc[0], bars)
    with st.container(horizontal=True):
        st.metric("最新价", f"${quote['最新价']:,.2f}", f"{quote['涨跌幅']:+.2%}", border=True)
        st.metric("日内振幅", f"{quote['日内振幅']:.2%}", border=True)
        st.metric("20日波动 Z 值", f"{regime['zscore_20d']:.2f}", border=True)
        st.metric("ATR 倍数", f"{regime['atr_ratio']:.2f}", border=True)
        st.metric("年化实现波动率", f"{regime['rv60']:.2%}", border=True)
        st.metric("动态波动状态", "已激活" if regime["active"] else "未激活", border=True)

    chart = bars[["time_key", "close"]].copy()
    chart["time_key"] = pd.to_datetime(chart["time_key"], errors="coerce")
    chart["20日均线"] = chart["close"].rolling(20).mean()
    chart["60日均线"] = chart["close"].rolling(60).mean()
    chart = chart.rename(columns={"time_key": "日期", "close": "收盘价"})
    with st.container(border=True):
        st.subheader("价格趋势")
        st.line_chart(chart, x="日期", y=["收盘价", "20日均线", "60日均线"])

    detail_left, detail_right = st.columns(2)
    with detail_left:
        with st.container(border=True):
            st.subheader("实时盘口摘要")
            st.dataframe(
                pd.DataFrame([quote]), hide_index=True,
                column_config={
                    "涨跌幅": st.column_config.NumberColumn(format="percent"),
                    "日内振幅": st.column_config.NumberColumn(format="percent"),
                    "买卖价差": st.column_config.NumberColumn(format="percent"),
                },
            )
    with detail_right:
        with st.container(border=True):
            st.subheader("波动状态检查")
            st.dataframe(pd.DataFrame(regime["checks"]), hide_index=True)
            st.caption(regime["reason"])
