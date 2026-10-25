from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.crypto_dashboard import (
    china_time_value,
    format_okx_balance,
    format_okx_fills,
    format_okx_orders,
    format_okx_swap_positions,
)
from futu_quant.crypto_swap_service_control import status
from futu_quant.okx_broker import OkxApiError, OkxDemoBroker
from futu_quant.okx_config import OkxSettings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import render_sidebar


@st.cache_data(ttl=15, max_entries=3, show_spinner=False)
def load_swap_account():
    broker = OkxDemoBroker(OkxSettings.from_env())
    return (
        broker.account_balance(),
        broker.positions("SWAP"),
        broker.pending_orders("SWAP"),
        broker.order_history("SWAP"),
        broker.fills("SWAP"),
    )


@st.cache_data(ttl=10, max_entries=5, show_spinner=False)
def load_swap_tickers(symbols: tuple[str, ...]):
    broker = OkxDemoBroker(OkxSettings.from_env())
    return {symbol: broker.ticker(symbol) for symbol in symbols}


st.set_page_config(page_title="合约账户总览", page_icon="📊", layout="wide")
render_sidebar(Settings.load())
st.title("欧易永续合约账户")
st.caption("欧易 Demo USDT 永续账户。平台资产、平台持仓与 Shadow 虚拟持仓分开核算。")

settings = OkxSettings.from_env()
service = status()
with st.container(horizontal=True):
    st.metric("交易环境", "欧易模拟盘", border=True)
    st.metric("产品", "USDT 永续", border=True)
    st.metric("API 凭证", "已配置" if settings.credentials_configured else "未配置", border=True)
    st.metric("合约策略服务", "运行中" if service.running else "未运行", border=True)
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
    with st.spinner("正在读取欧易合约账户、持仓、订单和成交……"):
        account_rows, position_rows, pending_rows, history_rows, fill_rows = (
            load_swap_account()
        )
except (OkxApiError, ValueError) as exc:
    st.error(f"欧易合约账户读取失败：{exc}")
    st.stop()

summary, balances = format_okx_balance(account_rows)
positions = format_okx_swap_positions(position_rows)
position_margin = (
    float(pd.to_numeric(positions["保证金"], errors="coerce").fillna(0).sum())
    if not positions.empty else 0.0
)
position_notional = (
    float(pd.to_numeric(positions["名义价值"], errors="coerce").fillna(0).sum())
    if not positions.empty else 0.0
)
position_upl = (
    float(pd.to_numeric(positions["未实现盈亏"], errors="coerce").fillna(0).sum())
    if not positions.empty else 0.0
)
effective_leverage = (
    position_notional / summary["账户总权益"]
    if summary["账户总权益"] > 0 else 0.0
)

with st.container(horizontal=True):
    st.metric("账户总权益", f"${summary['账户总权益']:,.2f}", border=True)
    st.metric("可用权益", f"${summary['可用权益']:,.2f}", border=True)
    st.metric(
        "合约未实现盈亏", f"${position_upl:,.2f}",
        delta=f"{position_upl:,.2f} USDT", delta_color="normal", border=True,
    )
    st.metric("占用保证金", f"${position_margin:,.2f}", border=True)
    st.metric("合约名义价值", f"${position_notional:,.2f}", border=True)
    st.metric("组合有效杠杆", f"{effective_leverage:.2f} 倍", border=True)

st.caption(
    "数据更新时间（北京时间）："
    + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    + "；页面缓存 15 秒。强平价格及保证金率以欧易接口返回为准。"
)

asset_tab, position_tab, shadow_tab, pending_tab, history_tab, fill_tab = st.tabs([
    "平台资产", "平台合约持仓", "Shadow 合约持仓",
    "待成交订单", "历史订单", "最近成交",
])

with asset_tab:
    st.subheader("欧易 Demo 平台资产")
    st.caption("合约与现货共用账户权益时，这里展示欧易账户实际返回的非零币种资产。")
    if balances.empty:
        st.info("账户已连接，但当前没有非零币种余额。")
    else:
        st.dataframe(
            balances, hide_index=True,
            column_config={
                "总余额": st.column_config.NumberColumn(format="%.8f"),
                "可用余额": st.column_config.NumberColumn(format="%.8f"),
                "冻结余额": st.column_config.NumberColumn(format="%.8f"),
                "美元权益": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

with position_tab:
    st.subheader("欧易 Demo 永续合约持仓")
    if positions.empty:
        st.info("当前没有欧易 Demo 永续合约持仓。")
    else:
        danger_count = int(
            (pd.to_numeric(positions["距强平比例"], errors="coerce") < .15)
            .fillna(False).sum()
        )
        with st.container(horizontal=True):
            st.metric("持仓合约数", len(positions), border=True)
            st.metric("多仓数", int(positions["方向"].eq("多仓").sum()), border=True)
            st.metric("空仓数", int(positions["方向"].eq("空仓").sum()), border=True)
            st.metric("强平缓冲低于 15%", danger_count, border=True)
        if danger_count:
            st.error(
                f"有 {danger_count} 个持仓距离预估强平价不足 15%，请优先检查保证金和杠杆。"
            )
        st.dataframe(
            positions, hide_index=True, height=420,
            column_config={
                "持仓张数": st.column_config.NumberColumn(format="%.4f"),
                "开仓均价": st.column_config.NumberColumn(format="$%.6f"),
                "标记价格": st.column_config.NumberColumn(format="$%.6f"),
                "预估强平价": st.column_config.NumberColumn(format="$%.6f"),
                "距强平比例": st.column_config.ProgressColumn(
                    min_value=0, max_value=1, format="percent",
                ),
                "杠杆": st.column_config.NumberColumn(format="%.1f 倍"),
                "保证金": st.column_config.NumberColumn(format="$%.2f"),
                "保证金率": st.column_config.NumberColumn(format="percent"),
                "名义价值": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "收益率": st.column_config.NumberColumn(format="percent"),
                "已实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "资金费用": st.column_config.NumberColumn(format="$%.4f"),
                "更新时间": st.column_config.DatetimeColumn(
                    format="YYYY-MM-DD HH:mm:ss",
                ),
            },
        )

with shadow_tab:
    st.subheader("策略 Shadow 虚拟持仓")
    st.caption("这里只是内部模拟成交账本，不属于欧易平台持仓。")
    store = TradingStore()
    rows = [
        row for row in store.virtual_allocations()
        if row["strategy_id"] == "crypto_volatility_swap"
        and float(row.get("quantity") or 0) != 0
    ]
    if not rows:
        st.info("当前没有 Shadow 永续合约持仓。")
    else:
        try:
            tickers = load_swap_tickers(tuple(str(row["symbol"]) for row in rows))
        except OkxApiError as exc:
            tickers = {}
            st.warning(f"Shadow 当前行情读取失败，成本和张数仍可查看：{exc}")
        records = []
        for row in rows:
            symbol = str(row["symbol"])
            contracts = float(row["quantity"])
            average = float(row["average_cost"])
            ticker = tickers.get(symbol, {})
            current = float(ticker.get("last", 0) or 0)
            pnl_ratio = (
                current / average - 1 if contracts > 0 and average > 0
                else average / current - 1 if contracts < 0 and current > 0
                else 0
            )
            margin_value = float(
                store.get_setting(f"crypto_swap_margin:{symbol}", "0") or 0
            )
            leverage = float(os.getenv("CRYPTO_SWAP_LEVERAGE", "2"))
            quote_time = pd.to_datetime(
                pd.to_numeric(ticker.get("ts"), errors="coerce"),
                unit="ms", utc=True, errors="coerce",
            )
            records.append({
                "合约": symbol,
                "方向": "多仓" if contracts > 0 else "空仓",
                "持仓张数": abs(contracts),
                "开仓均价": average,
                "当前价": current or None,
                "保证金预算": margin_value,
                "名义金额": margin_value * leverage,
                "未实现盈亏": margin_value * leverage * pnl_ratio,
                "收益率": pnl_ratio,
                "行情时间": (
                    quote_time.tz_convert("Asia/Shanghai").isoformat(
                        sep=" ", timespec="seconds"
                    )
                    if not pd.isna(quote_time) else ""
                ),
                "账本更新时间": china_time_value(row.get("updated_at", "")),
            })
        shadow = pd.DataFrame(records)
        shadow_pnl = float(shadow["未实现盈亏"].sum())
        with st.container(horizontal=True):
            st.metric("Shadow 持仓数", len(shadow), border=True)
            st.metric("Shadow 保证金", f"${shadow['保证金预算'].sum():,.2f}", border=True)
            st.metric("Shadow 名义金额", f"${shadow['名义金额'].sum():,.2f}", border=True)
            st.metric("Shadow 未实现盈亏", f"${shadow_pnl:,.2f}", border=True)
        st.dataframe(
            shadow, hide_index=True,
            column_config={
                "持仓张数": st.column_config.NumberColumn(format="%.4f"),
                "开仓均价": st.column_config.NumberColumn(format="$%.6f"),
                "当前价": st.column_config.NumberColumn(format="$%.6f"),
                "保证金预算": st.column_config.NumberColumn(format="$%.2f"),
                "名义金额": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "收益率": st.column_config.NumberColumn(format="percent"),
                "行情时间": st.column_config.DatetimeColumn(
                    format="YYYY-MM-DD HH:mm:ss",
                ),
            },
        )

with pending_tab:
    pending = format_okx_orders(pending_rows)
    if pending.empty:
        st.info("当前没有欧易模拟盘合约待成交订单。")
    else:
        st.dataframe(pending, hide_index=True)
        labels = {
            f"{row.get('instId')} · {row.get('ordId')}": row
            for row in pending_rows
        }
        selected = st.selectbox("选择待撤订单", list(labels))
        with st.container(horizontal=True):
            cancel_one = st.button("撤销选中订单", icon=":material/cancel:")
            cancel_all = st.button(
                "撤销全部合约订单", icon=":material/cancel_schedule_send:",
            )
        if cancel_one:
            row = labels[selected]
            try:
                OkxDemoBroker(settings).cancel_order(
                    row.get("instId", ""), row.get("ordId", ""),
                )
                st.cache_data.clear()
                st.success("撤单请求已提交。")
                st.rerun()
            except OkxApiError as exc:
                st.error(str(exc))
        if cancel_all:
            broker = OkxDemoBroker(settings)
            errors = []
            for row in pending_rows:
                try:
                    broker.cancel_order(row.get("instId", ""), row.get("ordId", ""))
                except OkxApiError as exc:
                    errors.append(str(exc))
            st.cache_data.clear()
            if errors:
                st.error(f"部分撤单失败：{'；'.join(errors[:3])}")
            else:
                st.success(f"已提交 {len(pending_rows)} 笔撤单请求。")
                st.rerun()

with history_tab:
    history = format_okx_orders(history_rows)
    if history.empty:
        st.info("当前没有欧易模拟盘合约历史订单。")
    else:
        st.dataframe(history, hide_index=True, height=420)

with fill_tab:
    fills = format_okx_fills(fill_rows)
    if fills.empty:
        st.info("当前没有欧易模拟盘合约成交记录。")
    else:
        st.dataframe(
            fills, hide_index=True, height=420,
            column_config={
                "成交数量": st.column_config.NumberColumn(format="%.6f"),
                "成交价格": st.column_config.NumberColumn(format="$%.6f"),
                "手续费": st.column_config.NumberColumn(format="%.8f"),
                "成交时间": st.column_config.DatetimeColumn(
                    format="YYYY-MM-DD HH:mm:ss",
                ),
            },
        )
