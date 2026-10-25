from __future__ import annotations

import re
import json

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError
from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import (
    china_time_columns, chinese_columns, render_admin_login, render_sidebar,
    translate_common_values, get_current_tenant, get_tenant_broker,
)


st.set_page_config(page_title="订单管理", page_icon="🧾", layout="wide")
settings = Settings.load()
render_sidebar(settings)
current_tenant = get_current_tenant(settings)
broker = get_tenant_broker(settings)
battleground = str(current_tenant.get("env", "SIMULATE")).upper()
is_real = current_tenant.get("is_real", False)
env_label = current_tenant.get("short_name", "实盘" if is_real else "模拟盘")

st.title(f"{env_label}订单管理")
if is_real:
    st.error(f"🔴 当前处于实盘战区 [{current_tenant.get('label')}]：撤单操作将直接撤销富途真实账户中的挂单！请谨慎操作。")
    env_suffix = "_real"
else:
    st.warning(f"🟢 当前处于模拟沙盒战区 [{current_tenant.get('label')}]：撤单仅作用于该沙盒专户。")
    env_suffix = "_sim"

store = TradingStore()
admin = render_admin_login()


def is_option(code: str) -> bool:
    return bool(re.search(r"\d{6}[CP]\d+$", str(code)))


def option_underlying(code: str) -> str:
    text = str(code or "")
    match = re.match(r"^(US\.[A-Z0-9]+?)\d{6}[CP]\d+$", text)
    if match:
        return match.group(1)
    return text


def prepare_display(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    display = frame.copy()
    if "symbol" in display.columns and "标的" not in display.columns:
        display["标的"] = display["symbol"].map(option_underlying)
    if "intent_key" in display.columns and "交易意图键" not in display.columns:
        display["交易意图键"] = display["intent_key"].astype(str)
    display = china_time_columns(display)
    display = chinese_columns(display)
    display = translate_common_values(display)
    if "标的" not in display.columns and "代码" in display.columns:
        display["标的"] = display["代码"].map(option_underlying)
    return display


def show_cancel_result() -> None:
    result = st.session_state.get("cancel_result")
    if isinstance(result, pd.DataFrame):
        if result.empty:
            st.info("没有找到可撤销的未完成订单。")
        else:
            succeeded = int((result["result"] == "SUCCESS").sum())
            st.success(f"撤单处理完成：成功 {succeeded} 笔，共处理 {len(result)} 笔。")
            st.dataframe(prepare_display(result), width="stretch", hide_index=True)


def pending_order_sheet(active: pd.DataFrame) -> None:
    st.subheader("待成交订单")
    st.caption("这里只显示仍可撤销的订单；正股、ETF和期权订单统一在此操作撤单。")
    if active.empty:
        st.info("当前没有待成交或可撤销的订单。")
        return
    display = prepare_display(active)
    columns = [
        column for column in (
            "订单ID", "策略", "标的", "代码", "交易方向", "方向", "数量", "限价",
            "订单状态", "状态", "已成交数量", "成交均价", "创建时间（中国）",
            "更新时间（中国）", "错误信息",
        ) if column in display.columns
    ]
    st.dataframe(display[columns], width="stretch", hide_index=True)
    selected = st.selectbox(
        "选择单笔订单",
        active.index.tolist(),
        key="selected_pending",
        format_func=lambda index: (
            f"{display.loc[index, '订单ID']} · {display.loc[index, '标的']} · "
            f"{display.loc[index, '交易方向' if '交易方向' in display.columns else '方向']} · "
            f"{display.loc[index, '订单状态' if '订单状态' in display.columns else '状态']}"
        ),
    )
    left, right = st.columns(2)
    if left.button("撤销选中订单", key="cancel_one_pending", type="primary", disabled=not admin):
        order = active.loc[[selected]]
        st.session_state.cancel_result = broker.cancel_orders(order)
        st.rerun()
    with right.form("cancel_all_pending"):
        confirmed = st.checkbox("确认撤销以上全部待成交订单")
        submitted = st.form_submit_button("一键撤销全部待成交订单", disabled=not admin)
        if submitted:
            if not confirmed:
                st.error("请先勾选确认。")
            else:
                st.session_state.cancel_result = broker.cancel_orders(active)
                st.rerun()


def shadow_order_sheet() -> None:
    st.subheader("Shadow 订单")
    st.caption(
        "这里显示本地策略的 Shadow 虚拟订单，不会出现在富途模拟账户订单里。"
        "限价、成交数量和状态都从本地账本读取。"
    )
    active_tid = current_tenant.get("tenant_id") or current_tenant.get("id")
    intents = pd.DataFrame(store.intents(limit=2000, tenant_id=active_tid))
    if intents.empty:
        st.info("当前没有本地 Shadow 订单。")
        return
    shadow = intents[
        intents["intent_key"].astype(str).str.contains(":SHADOW", na=False)
        | intents["status"].astype(str).str.startswith("SHADOW", na=False)
    ].copy()
    if shadow.empty:
        st.info("当前没有本地 Shadow 订单。")
        return
    display = shadow.copy()
    metadata = display["metadata_json"].map(
        lambda value: json.loads(value or "{}") if isinstance(value, str) else {}
    )
    display["标的"] = metadata.map(
        lambda item: item.get("underlying") or item.get("combo_primary") or ""
    )
    display["说明"] = metadata.map(lambda item: item.get("reason", "")) 
    display = display.rename(columns={
        "intent_key": "交易意图键", "strategy_id": "策略", "symbol": "代码",
        "side": "方向", "quantity": "数量", "limit_price": "限价",
        "status": "状态", "filled_quantity": "已成交数量",
        "average_fill_price": "成交均价", "error": "错误信息",
        "created_at": "创建时间（中国）", "updated_at": "更新时间（中国）",
        "last_reprice_at": "最后追价时间（中国）", "reprice_count": "追价次数",
        "metadata_json": "附加信息",
    })
    display = translate_common_values(
        chinese_columns(china_time_columns(display)).drop(columns=["附加信息"], errors="ignore")
    )
    columns = [
        column for column in (
            "策略", "标的", "代码", "方向", "数量", "限价", "状态", "已成交数量",
            "成交均价", "追价次数", "最后追价时间（中国）", "说明", "错误信息",
            "交易意图键", "创建时间（中国）", "更新时间（中国）",
        ) if column in display.columns
    ]
    st.dataframe(display[columns], width="stretch", hide_index=True)
    pending_shadow = display[display["状态"].isin(["Shadow 待撮合", "Shadow 部分成交"])]
    if pending_shadow.empty:
        st.info("当前没有待撮合的 Shadow 订单。")
    else:
        st.caption("下方是仍可能继续撮合/追价的 Shadow 订单。")
        st.dataframe(pending_shadow[columns], width="stretch", hide_index=True)


@st.cache_data(ttl=15, max_entries=4, show_spinner=False)
def load_orders_snapshot(target_env: str) -> pd.DataFrame:
    try:
        return broker.orders()
    except Exception:
        cached = store.broker_snapshot("orders" + env_suffix) or store.broker_snapshot("orders")
        return pd.DataFrame((cached or {}).get("payload", []))


try:
    orders = load_orders_snapshot(battleground)
    if orders.empty:
        stock_orders = option_orders = orders
    else:
        option_mask = orders["code"].map(is_option)
        stock_orders = orders[~option_mask].copy()
        option_orders = orders[option_mask].copy()
    pending_orders = (
        orders[orders["order_status"].map(broker.is_cancellable_status)].copy()
        if not orders.empty else orders
    )

    show_cancel_result()
    pending_tab, stock_tab, option_tab, shadow_tab, all_tab = st.tabs(
        ["待成交订单", "正股 / ETF 订单", "期权订单", "Shadow 订单", "全部订单"]
    )
    with pending_tab:
        pending_order_sheet(pending_orders)
    with stock_tab:
        st.caption("仅供查看；需要撤单请前往“待成交订单”。")
        st.dataframe(prepare_display(stock_orders), width="stretch", hide_index=True)
    with option_tab:
        st.caption("仅供查看；需要撤单请前往“待成交订单”。")
        st.dataframe(prepare_display(option_orders), width="stretch", hide_index=True)
    with shadow_tab:
        shadow_order_sheet()
    with all_tab:
        st.caption("账户全部订单，仅供查看；需要撤单请前往“待成交订单”。")
        st.dataframe(prepare_display(orders), width="stretch", hide_index=True)
except BrokerError as exc:
    st.error(f"订单接口错误：{exc}")
