from __future__ import annotations

import streamlit as st

from futu_quant.config import Settings
from futu_quant.crypto_service_control import status
from futu_quant.crypto_watchlist import load_crypto_watchlist
from futu_quant.okx_config import OkxSettings
from futu_quant.store import TradingStore
from futu_quant.ui import render_sidebar

st.set_page_config(page_title="币量化总览", page_icon=":material/currency_bitcoin:", layout="wide")
render_sidebar(Settings.load())
st.title("币量化总览")
st.caption("欧易 Demo 币现货策略总览。账户、Shadow、雷达和策略配置集中到一个入口。")
settings = OkxSettings.from_env()
service = status()
watchlist = load_crypto_watchlist()
store = TradingStore()
shadow = [row for row in store.virtual_allocations() if str(row.get("strategy_id", "")).startswith("crypto_") and float(row.get("quantity", 0) or 0) != 0]
with st.container(horizontal=True):
    st.metric("交易环境", "欧易模拟盘", border=True)
    st.metric("API 凭证", "已配置" if settings.credentials_configured else "未配置", border=True)
    st.metric("币策略服务", "运行中" if service.running else "未运行", border=True)
    st.metric("策略自选", len(watchlist), border=True)
    st.metric("Shadow 持仓", len(shadow), border=True)
if not settings.credentials_configured:
    st.warning("尚未配置欧易 Demo API 凭证。")
overview_tab, shadow_tab, action_tab = st.tabs(["运行总览", "Shadow 策略持仓", "常用入口"])
with overview_tab:
    st.subheader("当前币策略自选")
    if watchlist:
        st.dataframe({"代码": watchlist, "来源": ["币策略自选"] * len(watchlist)}, hide_index=True)
    else:
        st.info("当前没有币策略自选标的。")
with shadow_tab:
    st.subheader("Shadow 虚拟持仓")
    if shadow:
        st.dataframe(shadow, hide_index=True)
    else:
        st.info("当前没有币策略 Shadow 持仓。")
with action_tab:
    st.page_link("pages/32_crypto_strategy_config_backtest.py", label="打开币策略配置与回测", icon=":material/tune:")
    st.page_link("pages/33_crypto_strategy_review.py", label="打开币策略复盘", icon=":material/summarize:")
    st.page_link("pages/34_okx_account_overview.py", label="打开欧易账户总览", icon=":material/account_balance_wallet:")
