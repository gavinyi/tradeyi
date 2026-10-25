from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.option_pnl import build_option_pnl_ledger, option_pnl_summary
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import (
    STRATEGY_LABELS,
    china_time_columns,
    chinese_columns,
    render_sidebar,
    translate_common_values,
    get_current_tenant,
    get_tenant_broker,
)


st.set_page_config(page_title="期权盈亏台账", page_icon="💹", layout="wide")
settings = Settings.load()
render_sidebar(settings)
current_tenant = get_current_tenant(settings)
is_real = current_tenant.get("is_real", False)
env_title = f"{current_tenant.get('short_name', '租户')}期权盈亏台账"

st.title(env_title)
if is_real:
    st.error(f"🔴 当前呈现：[{current_tenant.get('label')}] 期权成交与已实现盈亏台账")
else:
    st.caption(f"🟢 当前呈现：[{current_tenant.get('label')}] 期权成交与已实现盈亏台账")


@st.cache_data(ttl=20, max_entries=4, show_spinner=False)
def load_option_pnl(refresh_quotes: bool, tenant_key: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    store = TradingStore()
    target_env = current_tenant.get("env", "SIMULATE")
    if refresh_quotes:
        broker = get_tenant_broker(settings)
    else:
        broker = None
    return build_option_pnl_ledger(store, broker=broker, env=target_env)


def strategy_label(value: str) -> str:
    return STRATEGY_LABELS.get(str(value), str(value))


def money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"${number:,.2f}"


def pnl_badge(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number > 0:
        return f"盈利 {money(number)}"
    if number < 0:
        return f"亏损 {money(abs(number))}"
    return "$0.00"


refresh_quotes = st.toggle("刷新当前期权行情并计算未实现盈亏", value=True)
ledger, orders, quote_status = load_option_pnl(refresh_quotes, tenant_key=current_tenant.get("id", "default"))
st.caption(f"行情状态：{quote_status}")

if ledger.empty and orders.empty:
    st.info("当前还没有期权订单或成交记录。")
    st.stop()

filters = st.container(border=True)
with filters:
    st.subheader("筛选")
    left, middle, right = st.columns(3)
    strategies = sorted(set(ledger.get("strategy_id", pd.Series(dtype=str)).astype(str)).union(
        set(orders.get("strategy_id", pd.Series(dtype=str)).astype(str))
    ))
    selected_strategy = left.selectbox(
        "策略",
        ["全部"] + strategies,
        format_func=lambda item: "全部" if item == "全部" else strategy_label(item),
    )
    underlyings = sorted(set(ledger.get("underlying", pd.Series(dtype=str)).dropna().astype(str)).union(
        set(orders.get("underlying", pd.Series(dtype=str)).dropna().astype(str))
    ))
    selected_underlying = middle.selectbox("正股代码", ["全部"] + underlyings)
    status_options = sorted(ledger.get("status", pd.Series(dtype=str)).dropna().astype(str).unique())
    selected_status = right.selectbox("持仓状态", ["全部"] + status_options)


filtered_ledger = ledger.copy()
filtered_orders = orders.copy()
if selected_strategy != "全部":
    filtered_ledger = filtered_ledger[filtered_ledger["strategy_id"].astype(str) == selected_strategy]
    filtered_orders = filtered_orders[filtered_orders["strategy_id"].astype(str) == selected_strategy]
if selected_underlying != "全部":
    filtered_ledger = filtered_ledger[filtered_ledger["underlying"].astype(str) == selected_underlying]
    filtered_orders = filtered_orders[filtered_orders["underlying"].astype(str) == selected_underlying]
if selected_status != "全部" and not filtered_ledger.empty:
    filtered_ledger = filtered_ledger[filtered_ledger["status"].astype(str) == selected_status]

summary = option_pnl_summary(filtered_ledger)
with st.container(horizontal=True):
    st.metric("当前期权持仓", f"{summary['open_contracts']:.0f} 张", border=True)
    st.metric("已平仓批次", f"{summary['closed_lots']:.0f} 笔", border=True)
    st.metric("已实现盈亏", money(summary["realized_pnl"]), border=True)
    st.metric("未实现盈亏", money(summary["unrealized_pnl"]), border=True)
    st.metric("总盈亏", pnl_badge(summary["total_pnl"]), border=True)

ledger_tab, closed_tab, orders_tab, rejected_tab = st.tabs(
    ["逐笔盈亏", "已平仓历史", "期权订单流水", "拒单/异常"]
)

ledger_columns = [
    "strategy_id", "underlying", "symbol", "option_type", "strike", "expiration",
    "status", "open_time", "open_time_source", "close_time", "close_time_source",
    "open_quantity", "closed_quantity",
    "remaining_quantity", "open_price", "avg_close_price", "last_price",
    "realized_pnl", "unrealized_pnl", "total_pnl", "return_ratio", "fees",
    "entry_tier", "trigger_mode", "reason",
]


def display_ledger(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame[[column for column in ledger_columns if column in frame.columns]].copy()
    result["strategy_id"] = result["strategy_id"].map(strategy_label)
    result = translate_common_values(chinese_columns(china_time_columns(result)))
    result = result.rename(columns={"开盘价": "开仓价"})
    return result


def display_orders(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    columns = [
        "id", "strategy_id", "underlying", "symbol", "option_type", "strike", "expiration",
        "side", "quantity", "limit_price", "status", "filled_quantity",
        "average_fill_price", "fees", "order_amount", "filled_amount", "reason",
        "error", "intent_key", "created_at", "updated_at", "last_reprice_at",
    ]
    result = frame[[column for column in columns if column in frame.columns]].copy()
    result["strategy_id"] = result["strategy_id"].map(strategy_label)
    result = translate_common_values(chinese_columns(china_time_columns(result)))
    return result


ledger_view = display_ledger(filtered_ledger)
closed_view = display_ledger(
    filtered_ledger[
        pd.to_numeric(filtered_ledger.get("remaining_quantity", 0), errors="coerce").fillna(0) <= 0
    ].copy()
) if not filtered_ledger.empty else pd.DataFrame()
orders_view = display_orders(filtered_orders)
rejected = filtered_orders[
    filtered_orders["status"].astype(str).isin(["REJECTED", "FAILED", "SHADOW_EXPIRED", "CANCELLED"])
    | filtered_orders["error"].fillna("").astype(str).ne("")
].copy() if not filtered_orders.empty else pd.DataFrame()
rejected_view = display_orders(rejected)

with ledger_tab:
    st.subheader("逐笔盈亏")
    st.caption("每行代表一次买入开仓批次；部分卖出会回填到同一行，按照 FIFO 计算盈亏。")
    if ledger_view.empty:
        st.info("当前筛选条件下没有成交批次。")
    else:
        st.dataframe(
            ledger_view,
            width="stretch",
            hide_index=True,
            column_config={
                "开仓价": st.column_config.NumberColumn(format="$%.2f"),
                "平仓均价": st.column_config.NumberColumn(format="$%.2f"),
                "最新价": st.column_config.NumberColumn(format="$%.2f"),
                "已实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "总盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "收益率": st.column_config.NumberColumn(format="percent"),
                "费用": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        st.download_button(
            "下载逐笔盈亏 CSV",
            ledger_view.to_csv(index=False).encode("utf-8-sig"),
            file_name="option_pnl_ledger.csv",
            mime="text/csv",
        )

with closed_tab:
    st.subheader("已平仓历史")
    st.caption("只显示剩余数量为 0 的历史批次，适合复盘到底是哪一笔赚/亏。")
    if closed_view.empty:
        st.info("当前筛选条件下没有已平仓期权批次。")
    else:
        st.dataframe(closed_view, width="stretch", hide_index=True)
        st.download_button(
            "下载已平仓历史 CSV",
            closed_view.to_csv(index=False).encode("utf-8-sig"),
            file_name="option_closed_history.csv",
            mime="text/csv",
        )

with orders_tab:
    st.subheader("期权订单流水")
    st.caption("这里保留所有期权订单，包括未成交、Shadow 失效、风控拒绝和富途模拟盘订单。")
    if orders_view.empty:
        st.info("当前筛选条件下没有期权订单流水。")
    else:
        st.dataframe(orders_view, width="stretch", hide_index=True)
        st.download_button(
            "下载期权订单流水 CSV",
            orders_view.to_csv(index=False).encode("utf-8-sig"),
            file_name="option_order_history.csv",
            mime="text/csv",
        )

with rejected_tab:
    st.subheader("拒单/异常")
    st.caption("集中查看为什么没有成交、为什么被拦截或为什么 Shadow 订单失效。")
    if rejected_view.empty:
        st.success("当前筛选条件下没有拒单或异常订单。")
    else:
        st.dataframe(rejected_view, width="stretch", hide_index=True)
