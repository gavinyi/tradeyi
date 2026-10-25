from __future__ import annotations

from datetime import datetime, time, timezone
import json
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import render_sidebar


st.set_page_config(page_title="合约策略复盘", page_icon="📋", layout="wide")
render_sidebar(Settings.load())
st.title("永续合约策略复盘")
st.caption("按北京时间复盘开多、开空、平仓、阻断和 Shadow 执行。")

selected = st.date_input("复盘日期（北京时间）", datetime.now(ZoneInfo("Asia/Shanghai")).date())
start = datetime.combine(selected, time.min, ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
end = datetime.combine(selected, time.max, ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
rows = []
for row in TradingStore().decisions(limit=50_000):
    if row.get("strategy_id") != "crypto_volatility_swap":
        continue
    created = pd.to_datetime(row.get("created_at"), utc=True, errors="coerce")
    if pd.isna(created) or not start <= created.to_pydatetime() <= end:
        continue
    payload = json.loads(row.get("payload_json") or "{}")
    order = payload.get("order") or {}
    rows.append({
        "时间": created.tz_convert("Asia/Shanghai"),
        "合约": row.get("symbol"), "决策": row.get("decision"),
        "原因": row.get("reason"), "执行模式": payload.get("mode"),
        "执行状态": order.get("status"), "方向": order.get("side"),
        "张数": order.get("contracts"), "价格": order.get("price"),
        "名义价值": order.get("notional"),
    })
frame = pd.DataFrame(rows)
if frame.empty:
    st.info("该日期没有永续合约策略记录。")
else:
    actionable = frame["决策"].isin(
        ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"]
    )
    executed = frame["执行状态"].notna()
    with st.container(horizontal=True):
        st.metric("策略判断", len(frame), border=True)
        st.metric("交易信号", int(actionable.sum()), border=True)
        st.metric("执行记录", int(executed.sum()), border=True)
        st.metric("阻断次数", int(frame["决策"].eq("BLOCKED").sum()), border=True)
    st.dataframe(frame, hide_index=True)
    st.download_button(
        "下载合约复盘 CSV", frame.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"合约策略复盘_{selected}.csv", mime="text/csv",
        icon=":material/download:",
    )
