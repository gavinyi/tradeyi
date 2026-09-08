from __future__ import annotations

import streamlit as st

from futu_quant.config import Settings
from futu_quant.crypto_swap_service_control import status
from futu_quant.crypto_swap_watchlist import load_swap_radar_managed_symbols
from futu_quant.okx_config import OkxSettings
from futu_quant.store import TradingStore
from futu_quant.ui import render_sidebar

st.set_page_config(page_title="合约量化总览", page_icon=":material/radar:", layout="wide")
render_sidebar(Settings.load())
st.title("合约量化总览")
st.caption("欧易 Demo USDT 永续总览。策略服务、Shadow 持仓、雷达托管和执行入口集中查看。")
settings = OkxSettings.from_env()
service = status()
managed = load_swap_radar_managed_symbols()
store = TradingStore()
shadow = [row for row in store.virtual_allocations() if str(row.get("strategy_id", "")).startswith("crypto_volatility_swap") and float(row.get("quantity", 0) or 0) != 0]
with st.container(horizontal=True):
    st.metric("交易环境", "欧易模拟盘", border=True)
    st.metric("API 凭证", "已配置" if settings.credentials_configured else "未配置", border=True)
    st.metric("合约服务", "运行中" if service.running else "未运行", border=True)
    st.metric("雷达托管标的", len(managed), border=True)
    st.metric("Shadow 持仓", len(shadow), border=True)
if not settings.credentials_configured:
    st.warning("尚未配置欧易 Demo API 凭证。")
overview_tab, shadow_tab, action_tab = st.tabs(["运行总览", "Shadow 合约持仓", "常用入口"])
with overview_tab:
    st.subheader("雷达托管标的")
    if managed:
        st.dataframe({"合约": managed, "来源": ["合约雷达托管"] * len(managed)}, hide_index=True)
    else:
        st.info("当前没有雷达托管标的。")
with shadow_tab:
    st.subheader("Shadow 合约持仓")
    if shadow:
        st.dataframe(shadow, hide_index=True)
    else:
        st.info("当前没有 Shadow 合约持仓。")
with action_tab:
    st.page_link("pages/36_perpetual_futures_strategy.py", label="打开永续合约策略", icon=":material/swap_vert:")
    st.page_link("pages/37_futures_strategy_review.py", label="打开合约策略复盘", icon=":material/assessment:")
    st.page_link("pages/35_futures_account_overview.py", label="打开合约账户总览", icon=":material/account_balance:")
