from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.crypto_dashboard import (
    format_crypto_shadow_holdings,
    format_okx_balance,
    format_okx_orders,
)
from futu_quant.crypto_service_control import status as crypto_service_status
from futu_quant.okx_broker import OkxApiError, OkxDemoBroker
from futu_quant.okx_config import OkxSettings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import render_sidebar


@st.cache_data(ttl=15, max_entries=3, show_spinner=False)
def load_okx_account_overview():
    broker = OkxDemoBroker(OkxSettings.from_env())
    return (
        broker.account_balance(),
        broker.pending_orders(),
        broker.order_history(),
        broker.fills(),
    )


@st.cache_data(ttl=10, max_entries=5, show_spinner=False)
def load_shadow_tickers(symbols: tuple[str, ...]) -> dict[str, dict]:
    broker = OkxDemoBroker(OkxSettings.from_env())
    return {symbol: broker.ticker(symbol) for symbol in symbols}


st.set_page_config(page_title="欧易账户总览", page_icon="🪙", layout="wide")
render_sidebar(Settings.load())
st.title("欧易账户总览")
st.caption("欧易 Demo Trading 账户信息。平台账户与策略 Shadow 虚拟持仓分开核算。")

settings = OkxSettings.from_env()
service = crypto_service_status()
with st.container(horizontal=True):
    st.metric("交易环境", "欧易模拟盘", border=True)
    st.metric("API 凭证", "已配置" if settings.credentials_configured else "未配置", border=True)
    st.metric("币策略服务", "运行中" if service.running else "未运行", border=True)
    st.metric(
        "模拟下单开关",
        "已开启" if settings.enable_demo_orders else "未开启",
        border=True,
    )
    if st.button("刷新账户", icon=":material/refresh:"):
        st.cache_data.clear()
        st.rerun()

if not settings.credentials_configured:
    st.warning("尚未配置欧易 Demo API 凭证。请到“币策略配置与回测 → 配置中心”填写。")
    st.page_link(
        "pages/32_crypto_strategy_config_backtest.py",
        label="打开币策略配置",
        icon=":material/settings:",
    )
    st.stop()

try:
    with st.spinner("正在读取欧易模拟账户、订单和成交……"):
        account_rows, pending_rows, history_rows, fill_rows = load_okx_account_overview()
except (OkxApiError, ValueError) as exc:
    st.error(f"欧易账户读取失败：{exc}")
    st.stop()

summary, balances = format_okx_balance(account_rows)
frozen_value = float(balances["冻结余额"].sum()) if not balances.empty else 0.0
asset_count = len(balances)
with st.container(horizontal=True):
    st.metric("账户总权益", f"${summary['账户总权益']:,.2f}", border=True)
    st.metric("可用权益", f"${summary['可用权益']:,.2f}", border=True)
    st.metric(
        "未实现盈亏",
        f"${summary['未实现盈亏']:,.2f}",
        delta_color="normal",
        border=True,
    )
    st.metric("非零币种", asset_count, border=True)
    st.metric("冻结数量合计", f"{frozen_value:,.8f}", border=True)

st.caption(
    "数据更新时间（北京时间）："
    + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    + "；页面数据缓存 15 秒。"
)

asset_tab, shadow_tab, pending_tab, history_tab, fills_tab = st.tabs([
    "平台资产", "Shadow 策略持仓", "待成交订单", "历史订单", "最近成交",
])
with asset_tab:
    st.subheader("欧易 Demo 平台资产")
    if balances.empty:
        st.info("账户已连接，但当前没有非零币种余额。")
    else:
        st.dataframe(
            balances,
            hide_index=True,
            column_config={
                "总余额": st.column_config.NumberColumn(format="%.8f"),
                "可用余额": st.column_config.NumberColumn(format="%.8f"),
                "冻结余额": st.column_config.NumberColumn(format="%.8f"),
                "美元权益": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

with shadow_tab:
    st.subheader("策略 Shadow 虚拟持仓")
    st.caption("这里只是内部模拟成交账本，不属于欧易账户余额。")
    store = TradingStore()
    allocation_rows = [
        row for row in store.virtual_allocations()
        if row.get("strategy_id") == "crypto_volatility_spot"
        and float(row.get("quantity") or 0) > 0
    ]
    if not allocation_rows:
        st.info("当前没有币策略 Shadow 持仓。")
    else:
        symbols = tuple(str(row["symbol"]) for row in allocation_rows)
        try:
            tickers = load_shadow_tickers(symbols)
        except OkxApiError as exc:
            tickers = {}
            st.warning(f"Shadow 当前行情读取失败，成本和数量仍可查看：{exc}")
        position_state = {
            symbol: {
                "peak_price": store.get_setting(f"crypto_peak_price:{symbol}", "0"),
                "opened_at": store.get_setting(f"crypto_opened_at:{symbol}", ""),
            }
            for symbol in symbols
        }
        holdings = format_crypto_shadow_holdings(
            allocation_rows, tickers, position_state,
        )
        pnl_total = float(
            pd.to_numeric(holdings["未实现盈亏"], errors="coerce").fillna(0).sum()
        )
        market_value = float(
            pd.to_numeric(holdings["当前市值"], errors="coerce").fillna(0).sum()
        )
        with st.container(horizontal=True):
            st.metric("Shadow 持仓数", len(holdings), border=True)
            st.metric("Shadow 当前市值", f"${market_value:,.2f}", border=True)
            st.metric("Shadow 未实现盈亏", f"${pnl_total:,.2f}", border=True)
        st.dataframe(
            holdings,
            hide_index=True,
            column_config={
                "当前数量": st.column_config.NumberColumn(format="%.8f"),
                "开仓均价": st.column_config.NumberColumn(format="$%.6f"),
                "当前价": st.column_config.NumberColumn(format="$%.6f"),
                "持仓成本": st.column_config.NumberColumn(format="$%.2f"),
                "当前市值": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "收益率": st.column_config.NumberColumn(format="percent"),
                "峰值价": st.column_config.NumberColumn(format="$%.6f"),
                "持有小时": st.column_config.NumberColumn(format="%.1f"),
            },
        )

with pending_tab:
    pending = format_okx_orders(pending_rows)
    if pending.empty:
        st.info("当前没有欧易模拟盘待成交订单。")
    else:
        st.dataframe(pending, hide_index=True)

with history_tab:
    history = format_okx_orders(history_rows)
    if history.empty:
        st.info("当前没有欧易模拟盘历史订单。")
    else:
        st.dataframe(history, hide_index=True, height=420)

with fills_tab:
    if not fill_rows:
        st.info("当前没有欧易模拟盘成交记录。")
    else:
        fills = pd.DataFrame(fill_rows).rename(columns={
            "instId": "标的", "side": "方向", "fillSz": "成交数量",
            "fillPx": "成交价格", "fee": "手续费", "feeCcy": "手续费币种",
            "ordId": "订单号", "tradeId": "成交号", "ts": "成交时间",
        })
        if "方向" in fills:
            fills["方向"] = fills["方向"].map(
                lambda value: {"buy": "买入", "sell": "卖出"}.get(
                    str(value).lower(), value
                )
            )
        if "成交时间" in fills:
            fills["成交时间"] = pd.to_datetime(
                pd.to_numeric(fills["成交时间"], errors="coerce"),
                unit="ms", utc=True, errors="coerce",
            ).dt.tz_convert("Asia/Shanghai")
        visible = [
            column for column in (
                "标的", "方向", "成交数量", "成交价格", "手续费",
                "手续费币种", "订单号", "成交号", "成交时间",
            ) if column in fills
        ]
        st.dataframe(fills[visible], hide_index=True, height=420)
