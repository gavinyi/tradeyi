from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_option_contracts, save_option_contracts
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="期权配置", page_icon="🧩", layout="wide")
render_sidebar(Settings.load())
st.title("期权配置")
st.warning("仅用于美股模拟盘；第一版只保存候选合约，不会自动提交期权订单。")

settings = Settings.load()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

with st.expander("默认风险边界", expanded=True):
    st.markdown(
        "- 只考虑买入 Call/Put，不允许裸卖\n"
        "- 允许选择当日到期（0DTE）合约\n"
        "- 默认未平仓量至少 100 张\n"
        "- 默认买卖价差不超过中间价的 20%\n"
        "- 委托必须使用限价，数量单位为合约"
    )

underlying = st.selectbox("正股/ETF", settings.watchlist)

@st.cache_data(ttl=900, max_entries=20, show_spinner=False)
def load_expirations(code: str) -> pd.DataFrame:
    return broker.option_expirations(code)


try:
    expirations = load_expirations(underlying)
    min_dte = st.number_input("最小剩余天数（DTE）", 0, 365, 0)
    eligible = expirations[expirations["option_expiry_date_distance"] >= min_dte]
    if eligible.empty:
        st.warning("没有符合 DTE 要求的到期日。")
        st.stop()
    expiration = st.selectbox(
        "到期日",
        eligible["strike_time"].tolist(),
        format_func=lambda value: (
            f"{value} · {int(eligible.loc[eligible['strike_time'] == value, 'option_expiry_date_distance'].iloc[0])} DTE"
        ),
    )
    option_type = st.radio("方向", ["CALL", "PUT"], horizontal=True)
    min_oi = st.number_input("最低未平仓量", 0, 1_000_000, 100, step=50)
    max_spread_pct = st.number_input("最大买卖价差（%）", 1.0, 100.0, 20.0, step=1.0)

    if st.button("载入并筛选合约链", type="primary"):
        with st.spinner("正在读取期权链和行情……"):
            chain = broker.option_chain(underlying, expiration)
            chain = chain[chain["option_type"] == option_type]
            quotes = broker.snapshots(chain["code"].tolist()[:300])
            chain = chain[chain["code"].isin(quotes["code"])]
            merged = chain.merge(quotes, on="code", suffixes=("", "_quote"))
            mid = (merged["bid_price"] + merged["ask_price"]) / 2
            merged["mid_price"] = mid
            merged["spread_pct"] = ((merged["ask_price"] - merged["bid_price"]) / mid * 100).where(mid > 0)
            filtered = merged[
                (merged["bid_price"] > 0)
                & (merged["ask_price"] > 0)
                & (merged["option_open_interest"] >= min_oi)
                & (merged["spread_pct"] <= max_spread_pct)
                & (~merged["suspension"])
            ].sort_values("strike_price")
            st.session_state.option_results = filtered

    results = st.session_state.get("option_results", pd.DataFrame())
    if not results.empty:
        columns = [
            "code", "option_type", "strike_price", "bid_price", "ask_price", "mid_price",
            "spread_pct", "volume", "option_open_interest", "option_implied_volatility",
            "option_delta", "option_gamma", "option_theta", "option_contract_multiplier",
        ]
        st.dataframe(chinese_columns(results[columns]), width="stretch", hide_index=True)
        selected_code = st.selectbox("选择合约", results["code"].tolist())
        quantity = st.number_input("合约数量", 1, 100, 1)
        max_premium = st.number_input("单笔最大权利金（美元）", 1.0, 1_000_000.0, 1000.0)
        selected = results[results["code"] == selected_code].iloc[0]
        estimated = float(selected["ask_price"]) * float(selected["option_contract_multiplier"]) * quantity
        st.metric("按 Ask 估算权利金", f"${estimated:,.2f}")
        if estimated > max_premium:
            st.error("超过单笔最大权利金，不能加入配置。")
        elif st.button("加入期权候选配置", disabled=not admin):
            contracts = [item for item in load_option_contracts() if item.get("code") != selected_code]
            contracts.append(
                {
                    "code": selected_code, "underlying": underlying, "option_type": option_type,
                    "expiration": expiration, "strike": float(selected["strike_price"]),
                    "quantity": int(quantity), "contract_multiplier": float(selected["option_contract_multiplier"]),
                    "max_premium": float(max_premium), "enabled": True,
                }
            )
            save_option_contracts(contracts)
            st.success(f"已保存 {selected_code}，尚未下单。")
except BrokerError as exc:
    st.error(f"富途期权接口错误：{exc}")

st.subheader("已保存的期权候选")
saved = load_option_contracts()
if saved:
    st.dataframe(chinese_columns(pd.DataFrame(saved)), width="stretch", hide_index=True)
    remove = st.selectbox("删除候选", [item["code"] for item in saved])
    if st.button("删除选中候选", disabled=not admin):
        save_option_contracts([item for item in saved if item["code"] != remove])
        st.rerun()
else:
    st.info("尚未保存期权合约。")
