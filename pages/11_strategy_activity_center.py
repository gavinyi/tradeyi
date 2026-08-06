from __future__ import annotations

from datetime import date
import json

import pandas as pd
import streamlit as st

from futu_quant.acceptance import strategy_acceptance
from futu_quant.config import (
    Settings, load_gamma_campaigns, load_volatility_campaigns,
    save_gamma_campaigns, save_volatility_campaigns,
)
from futu_quant.service_control import status as service_status
from futu_quant.radar_control import status as radar_status
from futu_quant.store import TradingStore
from futu_quant.ui import (
    china_time_columns, readable_payload, render_admin_login, render_sidebar,
    translate_common_values,
)


st.set_page_config(page_title="策略活动中心", page_icon="🧭", layout="wide")
settings = Settings.load()
render_sidebar(settings)
st.title("策略活动中心")
st.caption("统一查看策略生命周期、通知、数据状态、验收门槛和配置历史。")
store = TradingStore()
admin = render_admin_login()

activity_tab, notice_tab, combo_tab, acceptance_tab, version_tab, data_tab = st.tabs(
    ["活动生命周期", "通知中心", "期权组合", "自动验收", "配置版本与回滚", "历史期权数据"],
    default="活动生命周期", key="activity_center_tabs", on_change="rerun",
)

with activity_tab:
    decisions = {}
    for row in store.decisions(limit=3000):
        decisions.setdefault((row["strategy_id"], row["symbol"]), row)
    rows = []
    for strategy, campaigns in (
        ("earnings_gamma", load_gamma_campaigns()),
        ("volatility_long_gamma", load_volatility_campaigns()),
    ):
        for campaign in campaigns:
            latest = decisions.get((strategy, campaign.get("code")), {})
            expiry = pd.to_datetime(campaign.get("strike_time"), errors="coerce")
            if campaign.get("enabled", True) is False:
                lifecycle = "已停用"
            elif not pd.isna(expiry) and expiry.date() < date.today():
                lifecycle = "已过期，需要滚动"
            elif latest.get("decision") == "CLOSED":
                lifecycle = "已关闭"
            elif latest.get("decision") == "BLOCKED":
                lifecycle = "被风控阻断"
            elif latest.get("decision") in {"BUY", "SELL"}:
                lifecycle = "正在执行"
            else:
                lifecycle = "等待信号"
            rows.append({
                "策略": strategy, "正股": campaign.get("underlying"), "合约": campaign.get("code"),
                "生命周期": lifecycle, "执行模式": store.get_setting(
                    f"strategy_execution_mode:{strategy}", "INHERIT"
                ),
                "最新决策": latest.get("decision", ""), "最新原因": latest.get("reason", ""),
                "到期日": campaign.get("strike_time"), "优化器版本": campaign.get("optimizer_version", 0),
            })
    if rows:
        st.dataframe(translate_common_values(pd.DataFrame(rows)), hide_index=True)
    else:
        st.info("当前没有期权策略活动。")
    service = service_status()
    radar = radar_status()
    with st.container(horizontal=True):
        st.metric("统一交易服务", "运行中" if service.running else "未运行")
        st.metric("独立波动率雷达", "运行中" if radar.running else "未运行")

with notice_tab:
    alerts = pd.DataFrame(store.alerts(limit=1000))
    if alerts.empty:
        st.info("暂无通知。")
    else:
        alerts["message"] = alerts["message"].astype(str)
        display = china_time_columns(alerts).rename(columns={
            "severity": "级别", "event_type": "事件", "message": "说明",
            "delivered": "已通知", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        st.dataframe(translate_common_values(display), hide_index=True)
        alert_id = st.selectbox(
            "选择通知", alerts["id"].tolist(),
            format_func=lambda value: f"#{value} · {alerts[alerts['id'] == value].iloc[0]['message'][:60]}",
        )
        with st.container(horizontal=True):
            if st.button("标记已读", disabled=not admin):
                store.update_alert(int(alert_id), True, "ACKNOWLEDGED")
                st.rerun()
            if st.button("忽略本次信号", disabled=not admin):
                store.update_alert(int(alert_id), True, "IGNORED")
                st.rerun()
            if st.button("暂停相关标的", disabled=not admin):
                selected_alert = alerts[alerts["id"] == alert_id].iloc[0]
                message = str(selected_alert["message"])
                symbol = next((part for part in message.split() if part.startswith("US.")), "")
                if symbol:
                    store.set_setting(f"volatility_scanner_paused:{symbol}", "true")
                store.update_alert(int(alert_id), True, "SYMBOL_PAUSED")
                st.rerun()
            if st.button("恢复相关标的", disabled=not admin):
                selected_alert = alerts[alerts["id"] == alert_id].iloc[0]
                message = str(selected_alert["message"])
                symbol = next((part.rstrip("，。；:：") for part in message.split() if part.startswith("US.")), "")
                if symbol:
                    store.set_setting(f"volatility_scanner_paused:{symbol}", "false")
                    store.update_alert(int(alert_id), True, "SYMBOL_RESUMED")
                    st.rerun()

with combo_tab:
    combo_rows = []
    for strategy, campaigns in (
        ("earnings_gamma", load_gamma_campaigns()),
        ("volatility_long_gamma", load_volatility_campaigns()),
    ):
        for campaign in campaigns:
            legs = campaign.get("legs") or [{"code": campaign.get("code"), "side": "BUY"}]
            for leg_number, leg in enumerate(legs, start=1):
                code = leg.get("code") or campaign.get("code")
                quantity, average_cost = store.shadow_position(strategy, code)
                combo_rows.append({
                    "策略": strategy, "正股": campaign.get("underlying"),
                    "组合标识": campaign.get("campaign_id") or campaign.get("underlying"),
                    "腿序号": leg_number, "合约": code, "方向": leg.get("side", "BUY"),
                    "Shadow数量": quantity, "Shadow平均成本": average_cost,
                    "配置状态": "启用" if campaign.get("enabled", True) else "停用",
                })
    if combo_rows:
        combo_frame = pd.DataFrame(combo_rows)
        st.dataframe(translate_common_values(combo_frame), hide_index=True)
        st.caption("组合按策略与正股归组；每条腿独立记账，撤单仍在“订单管理”的待成交订单中执行。")
    else:
        st.info("当前没有期权组合配置。")

with acceptance_tab:
    strategies = ["core_satellite", "earnings_gamma", "volatility_long_gamma"]
    summaries = []
    for strategy in strategies:
        result = strategy_acceptance(store, strategy)
        summaries.append({
            "策略": strategy, "自动验收": "通过" if result["passed"] else "未通过",
            "未通过项目": "；".join(key for key, gate in result["gates"].items() if not gate["passed"]),
        })
    st.dataframe(translate_common_values(pd.DataFrame(summaries)), hide_index=True)

with version_tab:
    versions = pd.DataFrame(store.config_versions(limit=300))
    if versions.empty:
        st.info("尚无配置版本记录；下一次保存策略配置后自动产生。")
    else:
        display_versions = versions.copy()
        display_versions["value_json"] = display_versions["value_json"].map(readable_payload)
        display = china_time_columns(display_versions).rename(columns={
            "config_key": "配置项", "value_json": "配置内容", "actor": "操作来源",
            "reason": "修改原因", "created_at": "时间（中国）",
        })
        st.dataframe(display.drop(columns=["id"], errors="ignore"), hide_index=True)
        selected_id = st.selectbox("选择要恢复的版本", versions["id"].tolist())
        reason = st.text_input("回滚原因", placeholder="说明为什么恢复此版本")
        if st.button("恢复所选配置版本", type="primary", disabled=not admin or not reason.strip()):
            selected = versions[versions["id"] == selected_id].iloc[0]
            value = json.loads(selected["value_json"])
            if selected["config_key"] == "GAMMA_CAMPAIGNS":
                save_gamma_campaigns(value)
            elif selected["config_key"] == "VOLATILITY_CAMPAIGNS":
                save_volatility_campaigns(value)
            store.audit("CONFIG_ROLLED_BACK", {
                "config_key": selected["config_key"], "version_id": int(selected_id), "reason": reason,
            })
            st.success("配置已恢复；统一服务将在下一调度周期读取。")

with data_tab:
    st.metric("已导入历史期权记录", f"{store.option_history_count():,}")
    st.caption(
        "CSV 必须包含 code、time_key；可选字段：underlying、underlying_price、bid、ask、"
        "iv、delta、gamma、theta、volume、open_interest。"
    )
    upload = st.file_uploader("导入历史期权 CSV", type=["csv"])
    if upload is not None:
        imported = pd.read_csv(upload)
        missing = {"code", "time_key"} - set(imported.columns)
        if missing:
            st.error(f"缺少必要字段：{', '.join(sorted(missing))}")
        else:
            st.dataframe(imported.head(100), hide_index=True)
            if st.button("确认导入历史期权数据", disabled=not admin):
                clean = imported.astype(object).where(pd.notna(imported), None)
                count = store.import_option_history(clean.to_dict("records"))
                store.audit("OPTION_HISTORY_IMPORTED", {"rows": count})
                st.success(f"已导入 {count} 条历史期权记录。")
