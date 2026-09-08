from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.replay import run_resilience_drill
from futu_quant.store import TradingStore
from futu_quant.ui import (
    CHINA_TZ, STRATEGY_LABELS, china_time_columns, readable_payload,
    render_admin_login, render_sidebar, translate_common_values,
)


st.set_page_config(page_title="运行日志", page_icon="📋", layout="wide")
render_sidebar(Settings.load())
st.title("策略运行日志与验收演练")
store = TradingStore()
admin = render_admin_login()

now = datetime.now(ZoneInfo("America/New_York"))
session = USMarketCalendar().session(now)
if session.is_session:
    china_open = session.market_open.astimezone(CHINA_TZ)
    china_close = session.market_close.astimezone(CHINA_TZ)
    schedule_text = f"本交易日常规时段（中国时间）：{china_open:%Y-%m-%d %H:%M}–{china_close:%H:%M}"
else:
    next_session = USMarketCalendar().calendar.date_to_session(now.date(), direction="next")
    schedule_text = f"当前休市；下一交易日：{next_session:%Y-%m-%d}"
st.info(schedule_text)

if session.is_session:
    china_open = session.market_open.astimezone(CHINA_TZ)
    china_close = session.market_close.astimezone(CHINA_TZ)
    gamma_open = session.market_open.replace(hour=4, minute=0).astimezone(CHINA_TZ)
    schedule = pd.DataFrame([
        {"策略": "Earnings Gamma", "下一执行窗口（中国时间）": f"{gamma_open:%Y-%m-%d %H:%M}–延长时段结束"},
        {"策略": "Volatility Long Gamma", "下一执行窗口（中国时间）": f"{gamma_open:%Y-%m-%d %H:%M}–延长时段结束"},
        {"策略": "基础策略", "下一执行窗口（中国时间）": f"{china_open:%Y-%m-%d %H:%M}–{china_close:%H:%M}"},
        {"策略": "Core Satellite", "下一执行窗口（中国时间）": f"{china_close:%Y-%m-%d %H:%M} 收盘后 5 分钟起"},
    ])
    st.dataframe(schedule, hide_index=True)

decision_tab, snapshot_tab, ownership_tab, audit_tab, drill_tab = st.tabs(
    ["策略决策", "决策快照", "虚拟持仓", "操作审计", "故障演练"],
    default="策略决策", key="runtime_log_tabs", on_change="rerun",
)

with decision_tab:
    decisions = pd.DataFrame(store.decisions())
    if decisions.empty:
        st.info("暂无策略决策记录；启动统一交易服务后会记录买、卖、持有和未到期原因。")
    else:
        strategies = sorted(decisions["strategy_id"].unique())
        selected = st.multiselect(
            "策略筛选", strategies, default=strategies,
            format_func=lambda value: STRATEGY_LABELS.get(str(value), str(value)),
        )
        visible = decisions[decisions["strategy_id"].isin(selected)].copy()
        visible["payload_json"] = visible["payload_json"].map(readable_payload)
        display = china_time_columns(visible).rename(columns={
            "strategy_id": "策略", "symbol": "代码", "decision": "判断",
            "reason": "原因", "payload_json": "计算明细", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        st.dataframe(translate_common_values(display), hide_index=True)

with snapshot_tab:
    snapshots = pd.DataFrame(store.decision_snapshots())
    if snapshots.empty:
        st.info("暂无可复现决策快照。")
    else:
        snapshots["snapshot_json"] = snapshots["snapshot_json"].map(readable_payload)
        display = china_time_columns(snapshots).rename(columns={
            "strategy_id": "策略", "symbol": "代码", "snapshot_json": "完整上下文",
            "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        st.dataframe(translate_common_values(display), hide_index=True)

with ownership_tab:
    virtual = pd.DataFrame(store.virtual_positions())
    shadow = pd.DataFrame(store.shadow_open_positions())
    if virtual.empty and shadow.empty:
        st.info("暂无策略虚拟持仓或 Shadow 成交持仓。")
    else:
        frames = []
        if not virtual.empty:
            frames.append(
                china_time_columns(virtual).rename(columns={
                    "strategy_id": "策略", "symbol": "代码", "quantity": "持仓数量",
                    "average_cost": "平均成本", "updated_at": "更新时间（中国）",
                }).assign(持仓来源="策略虚拟归属")
            )
        if not shadow.empty:
            shadow_display = pd.DataFrame(shadow).rename(columns={
                "strategy_id": "策略", "symbol": "代码", "quantity": "持仓数量",
                "average_cost": "平均成本",
            })
            shadow_display["更新时间（中国）"] = ""
            frames.append(shadow_display.assign(持仓来源="Shadow 虚拟成交账本"))
        display = pd.concat(frames, ignore_index=True, sort=False)
        display = translate_common_values(display)
        preferred_columns = [
            "持仓来源", "策略", "代码", "持仓数量", "平均成本", "更新时间（中国）",
        ]
        display = display[[column for column in preferred_columns if column in display.columns]]
        st.dataframe(display, hide_index=True)

with audit_tab:
    events = pd.DataFrame(store.audit_events())
    if events.empty:
        st.info("暂无操作审计记录。")
    else:
        events["payload_json"] = events["payload_json"].map(readable_payload)
        display = china_time_columns(events).rename(columns={
            "event_type": "事件", "strategy_id": "策略",
            "payload_json": "变更明细", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        st.dataframe(translate_common_values(display), hide_index=True)

with drill_tab:
    st.write(
        "演练使用临时数据库和内置模拟券商，不会连接富途，也不会影响当前订单与持仓。"
    )
    if st.button("运行完整故障演练", type="primary", disabled=not admin):
        with st.status("正在演练重复信号、部分成交、重启恢复、断线和急停……"):
            result = run_resilience_drill()
        st.dataframe(result, hide_index=True)
        passed = int((result["结果"] == "通过").sum())
        if passed == len(result):
            st.success(f"全部 {passed} 项演练通过。")
        else:
            st.error(f"通过 {passed}/{len(result)} 项，请检查失败项目。")
