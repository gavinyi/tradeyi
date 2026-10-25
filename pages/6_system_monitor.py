from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.performance import performance_report
from futu_quant.radar_control import status as radar_status
from futu_quant.monitoring import heartbeat_display_status, heartbeat_stale_after
from futu_quant.service_control import status as service_status
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.scheduled_tasks import (
    BOARD_TIMERS, next_board_timer_run, set_timer_enabled, timer_default_enabled, timer_enabled_setting_key,
)
from futu_quant.ui import (
    CHINA_TZ, EVENT_LABELS, china_time_columns, get_current_tenant, readable_payload,
    render_sidebar, translate_common_values,
)


st.set_page_config(page_title="系统监控", page_icon="🛡️", layout="wide")
render_sidebar(Settings.load())
st.title("系统监控与策略归因")
store = TradingStore()
settings = Settings.load()
broker = FutuPaperBroker(settings)


def _current_tenant_id() -> str:
    """当前侧边栏选中的租户 ID（拿不到时返回空串，由调用方回落）。"""
    try:
        return str(get_current_tenant(settings).get("tenant_id") or "")
    except Exception:
        return ""


def _paused_for_current_tenant() -> bool:
    """当前租户的策略暂停状态：**租户键优先、全局键兜底**。

    与 `run_service.py` 的读取口径必须一致，否则界面显示的作用域与实际不符。
    """
    tenant_id = _current_tenant_id()
    tenant_value = (
        store.get_tenant_setting(tenant_id, "strategies_paused", "") if tenant_id else ""
    )
    return (tenant_value or store.get_setting("strategies_paused", "false")).lower() == "true"


@st.cache_data(ttl=60)
def cached_performance_report():
    local_store = TradingStore()
    local_broker = FutuPaperBroker(Settings.load())
    return performance_report(local_store, local_broker)

service = service_status()
radar = radar_status()
with st.container(border=True):
    st.subheader("服务状态（只读）")
    with st.container(horizontal=True):
        st.metric("统一交易服务", "运行中" if service.running else "未运行", border=True)
        st.metric("独立波动率雷达", "运行中" if radar.running else "未运行", border=True)
        st.metric(
            # 暂停状态按**当前租户**读（租户键优先、全局键兜底）—— 读全局键会把
            # 别的租户的暂停状态显示成当前租户的，误导操作者。
            f"全部策略（{_current_tenant_id() or '未识别'}）",
            "已暂停" if _paused_for_current_tenant() else "运行中", border=True,
        )
        st.metric(
            "全局执行模式", store.get_setting("execution_mode", "PAPER").upper(), border=True,
        )
    st.page_link(
        "pages/15_strategy_control_center.py", label="前往策略与资金控制",
        icon=":material/tune:",
    )

if st.button("刷新监控数据", icon=":material/refresh:"):
    st.cache_data.clear()

heartbeats = pd.DataFrame(store.heartbeats())
alerts = pd.DataFrame(store.alerts())
intents = pd.DataFrame(store.intents(limit=10_000))
healthy = 0
if not heartbeats.empty:
    now = datetime.now(timezone.utc)
    age = pd.to_datetime(heartbeats["updated_at"], utc=True).map(lambda value: (now - value).total_seconds())
    stale_limits = heartbeats["strategy_id"].map(heartbeat_stale_after)
    healthy = int((heartbeats["status"].isin(["HEALTHY", "RUNNING"]) & (age < stale_limits)).sum())
    heartbeats["在线状态"] = [
        heartbeat_display_status(status, seconds, stale_after)
        for status, seconds, stale_after in zip(
            heartbeats["status"], age, stale_limits, strict=True
        )
    ]
    heartbeats["心跳延迟秒"] = age.round(0)
    heartbeats["允许间隔秒"] = stale_limits

with st.container(horizontal=True):
    st.metric("服务急停", "已开启" if store.kill_switch() else "未开启", border=True)
    st.metric("独立波动率雷达", "运行中" if radar.running else "未运行", border=True)
    st.metric("健康心跳", f"{healthy}/{len(heartbeats)}", border=True)
    st.metric("风控拒单", int((intents.get("status") == "REJECTED").sum()) if not intents.empty else 0, border=True)
    st.metric("未处理告警", int((alerts.get("delivered") == 0).sum()) if not alerts.empty else 0, border=True)

view = st.segmented_control(
    "监控视图",
    ["运行状态", "调度任务", "策略绩效", "系统健康", "告警中心", "恢复与日报"],
    default="运行状态",
)

if view == "运行状态":
    if heartbeats.empty:
        st.info("统一交易服务尚未启动，因此还没有心跳。")
    else:
        display = translate_common_values(china_time_columns(heartbeats).rename(columns={
            "strategy_id": "策略/服务", "status": "状态", "message": "最近消息",
            "pid": "进程号", "updated_at": "最后心跳（中国）",
        }))
        st.dataframe(display, hide_index=True)
        st.caption("高频交易任务允许 2 分钟，系统维护允许 7 分钟，每日任务允许 25 小时；ERROR 始终表示执行异常。")

elif view == "调度任务":
    with st.container(border=True):
        st.subheader("任务看板定时器")
        timer_rows = []
        for timer in BOARD_TIMERS:
            enabled = store.get_setting(timer_enabled_setting_key(timer), str(timer_default_enabled(timer)).lower()) == "true"
            timer_rows.append({
                "timer": timer, "task_id": timer.task_id, "title": timer.title,
                "schedule": f"每天 {timer.time:%H:%M}（中国时间）",
                "next_run": next_board_timer_run(store, timer), "enabled": enabled,
            })
        timer_view = pd.DataFrame(timer_rows)
        timer_view["next_run"] = timer_view["next_run"].map(lambda value: value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M"))
        timer_display = timer_view[["title", "task_id", "schedule", "next_run", "enabled"]]
        st.dataframe(timer_display.rename(columns={"task_id": "任务代码", "title": "任务", "schedule": "定时器", "next_run": "下次预计执行", "enabled": "已启用"}), hide_index=True)
        with st.expander("启用/关闭定时器"):
            for row in timer_rows:
                key = f"timer_{row['task_id']}_enabled"
                st.toggle(
                    f"{row['title']}：{row['schedule']}",
                    value=row["enabled"], key=key,
                    label_visibility="collapsed",
                    on_change=lambda _checked, code=row["task_id"]: set_timer_enabled(store, dict((t.task_id, t) for t in BOARD_TIMERS)[code], _checked),
                )
        st.caption("策略复盘定时器每天 05:15 生成 HTML 并登记待投递；标的池 ML 精排轮换每天 05:30 自动执行；电脑真实关机定时器每天 06:00 准时执行。")

    task_runs = pd.DataFrame(store.task_runs(limit=300))
    if task_runs.empty:
        st.info("服务尚无任务执行记录；启动后会记录每个任务的耗时与超时状态。")
    else:
        now = datetime.now(timezone.utc)
        started = pd.to_datetime(task_runs["started_at"], utc=True)
        running_age = (now - started).dt.total_seconds()
        overdue = (
            task_runs["status"].eq("RUNNING")
            & running_age.gt(pd.to_numeric(task_runs["timeout_seconds"], errors="coerce"))
        )
        abandoned = task_runs["status"].eq("RUNNING") & task_runs["pid"].ne(service.pid)
        task_runs.loc[overdue, "status"] = "OVERDUE"
        task_runs.loc[abandoned & ~overdue, "status"] = "INTERRUPTED"
        statuses = task_runs["status"]
        with st.container(horizontal=True):
            st.metric("正在执行", int(statuses.eq("RUNNING").sum()), border=True)
            st.metric("软超时", int(statuses.isin(["SLOW", "OVERDUE"]).sum()), border=True)
            st.metric("执行异常", int(statuses.eq("ERROR").sum()), border=True)
            completed = pd.to_numeric(task_runs["duration_seconds"], errors="coerce").dropna()
            st.metric("最近平均耗时", f"{completed.head(50).mean():.1f} 秒" if not completed.empty else "—", border=True)
        task_names = {
            "shadow_matching": "Shadow 成交匹配", "daily_strategy_review": "每日策略复盘与执行质量",
            "computer_shutdown": "每日电脑关机", "order_reconciliation": "订单对账",
            "option_combo_monitor": "期权组合执行监控",
            "combo_market_snapshot": "组合行情快照",
            "validation_calibration": "策略验证校准",
            "validation_report": "验证日报/周报",
            "parquet_archive": "Parquet 行情归档", "recovery_point": "增量恢复点",
            "disaster_recovery_drill": "隔离灾备演练", "storage_monitor": "存储容量监控",
            "option_lifecycle": "期权生命周期", "option_disposition": "期权处置",
            "option_history": "期权历史采集", "portfolio_stress": "组合压力测试",
            "ledger_reconciliation": "三账对账", "earnings_gamma": "财报 Gamma",
            "volatility_long_gamma": "波动率期权", "core_satellite": "核心卫星",
            "system_health": "系统健康", "earnings_calendar": "财报日历",
            "daily_report": "每日运行报告", "correlation_risk": "相关性风险",
            "page_compatibility": "页面兼容性巡检", "paper_e2e": "模拟交易端到端验收",
            "observability": "可观测指标采集", "automatic_recovery": "自动故障恢复",
            "idempotency_audit": "订单幂等审计", "operations_report": "每日运营报告",
            "dynamic_allocation": "动态资金调度",
            "long_term_validation": "长期策略验证",
        }
        status_names = {
            "RUNNING": "正在执行", "SUCCESS": "成功", "SLOW": "完成但超时",
            "OVERDUE": "执行中且已超时", "ERROR": "异常", "INTERRUPTED": "进程中断",
        }
        task_runs["task_id"] = task_runs["task_id"].map(
            lambda value: task_names.get(str(value), value)
        )
        task_runs["status"] = task_runs["status"].map(
            lambda value: status_names.get(str(value), value)
        )
        display = china_time_columns(task_runs).rename(columns={
            "task_id": "任务", "status": "状态", "timeout_seconds": "软超时（秒）",
            "duration_seconds": "耗时（秒）", "message": "说明", "pid": "进程号",
            "started_at": "开始时间（中国）", "finished_at": "结束时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        st.dataframe(display, hide_index=True, column_config={
            "耗时（秒）": st.column_config.NumberColumn(format="%.2f"),
            "软超时（秒）": st.column_config.NumberColumn(format="%.0f"),
        })
        st.caption("软超时不会强杀正在进行的下单或对账，避免任务被中断后重复提交；完成后会降级告警。")

elif view == "策略绩效":
    st.caption("绩效归因只在当前视图打开时加载，并缓存 60 秒；避免打开系统监控时被行情归因阻塞。")
    try:
        with st.spinner("正在加载策略绩效归因…"):
            summary, detail, equity = cached_performance_report()
        if summary.empty:
            st.info("统一执行层尚无成交记录，成交后会显示策略级估算盈亏。")
        else:
            st.dataframe(translate_common_values(summary), hide_index=True, column_config={
                "胜率": st.column_config.NumberColumn(format="percent"),
                "费用": st.column_config.NumberColumn(format="$%.2f"),
                "已实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="$%.2f"),
                "总盈亏": st.column_config.NumberColumn(format="$%.2f"),
            })
            st.dataframe(translate_common_values(detail), hide_index=True)
        if not equity.empty:
            equity_chart = china_time_columns(equity).rename(
                columns={"created_at": "时间（中国）", "total_assets": "账户权益"}
            )
            st.line_chart(equity_chart, x="时间（中国）", y="账户权益")
            if "回撤" in equity.columns:
                st.metric("历史最大回撤", f"{float(equity['回撤'].min()):.2%}")
        st.caption("采用 FIFO 成交批次计算；费用取富途订单返回的 commission 字段。")
    except BrokerError as exc:
        st.error(f"绩效行情读取失败：{exc}")

elif view == "系统健康":
    backups = sorted(Path("data/backups").glob("trading-*.db"), reverse=True)
    service_log = Path("data/service.log")
    with st.container(horizontal=True):
        st.metric("数据库完整性", store.integrity_check(), border=True)
        st.metric("备份数量", len(backups), border=True)
        st.metric(
            "服务日志", f"{service_log.stat().st_size / 1024 / 1024:.2f} MB"
            if service_log.exists() else "暂无", border=True,
        )
    if backups:
        st.caption(f"最近备份：{backups[0]} · 每日自动备份")
    health_rows = heartbeats[heartbeats["strategy_id"] == "system_health"] if not heartbeats.empty else heartbeats
    if health_rows.empty:
        st.warning("尚未执行系统健康检查；启动统一交易服务后每 5 分钟检查一次。")
    else:
        health_display = translate_common_values(china_time_columns(health_rows).rename(columns={
            "strategy_id": "检查项", "status": "状态", "message": "检查结果",
            "pid": "进程号", "updated_at": "检查时间（中国）",
        }))
        st.dataframe(health_display, hide_index=True)
    st.markdown(
        "健康维护包括 OpenD 连通性、行情有效性、K 线缺口/空值、SQLite 完整性、"
        "每日备份、日志轮转和过期监控数据清理。"
    )

elif view == "告警中心":
    if alerts.empty:
        st.success("暂无告警。")
    else:
        alert_display = china_time_columns(alerts).rename(columns={
            "severity": "级别", "event_type": "事件", "message": "内容",
            "delivered": "Webhook已送达", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        alert_display["级别"] = alert_display["级别"].map({
            "ERROR": "错误", "WARNING": "警告", "INFO": "提醒",
        }).fillna(alert_display["级别"])
        alert_display["事件"] = alert_display["事件"].map(
            lambda value: EVENT_LABELS.get(str(value), value)
        )
        alert_display["Webhook已送达"] = alert_display["Webhook已送达"].map(
            lambda value: "是" if bool(value) else "否"
        )
        st.dataframe(alert_display, hide_index=True)

        with st.expander("🤖 飞书群机器人战报推送配置 (多账号共享)"):
            st.caption("配置飞书群自定义机器人的 Webhook 地址，收盘战报将以交互卡片形式推送到群聊，群内所有成员均可实时查看。")
            current_wh = store.get_setting("FEISHU_WEBHOOK_URL", "")
            current_sec = store.get_setting("FEISHU_WEBHOOK_SECRET", "")
            current_group_chat = store.get_setting("FEISHU_GROUP_CHAT_ID", "")

            with st.form("feishu_config_form"):
                new_wh = st.text_input("飞书群机器人 Webhook 地址", value=current_wh, placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...")
                new_sec = st.text_input("安全密钥 Secret (可选)", value=current_sec, type="password", placeholder="若机器人在飞书后台开启了签名校验请填入")
                new_group = st.text_input("飞书群 Chat ID (可选)", value=current_group_chat, placeholder="oc_xxxxxxxxxxxx (若将 147 机器人拉入群聊)")
                saved = st.form_submit_button("保存飞书推送配置", type="primary")
                if saved:
                    store.set_setting("FEISHU_WEBHOOK_URL", new_wh.strip())
                    store.set_setting("FEISHU_WEBHOOK_SECRET", new_sec.strip())
                    store.set_setting("FEISHU_GROUP_CHAT_ID", new_group.strip())
                    st.success("飞书群推送配置已保存！")
                    st.rerun()

            if st.button("向飞书群机器人发送测试卡片", icon=":material/send:"):
                from futu_quant.feishu_reporter import FeishuReporter
                reporter = FeishuReporter(store=store)
                res = reporter.send_test_message()
                if res.get("ok"):
                    st.success(f"✅ 飞书测试消息已成功发送至【{res.get('channel')}】！请在群聊中查收。")
                else:
                    st.error(f"❌ 发送失败：{res.get('error', '未知异常')}。请确保已填入有效的群 Chat ID 或 Webhook 地址。")

elif view == "恢复与日报":
    events = pd.DataFrame(store.operational_events())
    if events.empty:
        st.info("暂无恢复或运行事件。")
    else:
        event_display = china_time_columns(events).rename(columns={
            "component": "组件", "event_type": "事件", "status": "状态",
            "payload_json": "详情", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        event_display["事件"] = event_display["事件"].map(
            lambda value: EVENT_LABELS.get(str(value), value)
        )
        event_display["详情"] = event_display["详情"].map(readable_payload)
        st.dataframe(translate_common_values(event_display), hide_index=True)

    st.subheader("最近策略决策")
    decisions = pd.DataFrame(store.decisions(limit=500))
    if decisions.empty:
        st.info("暂无策略决策记录。")
    else:
        decision_display = china_time_columns(decisions).rename(columns={
            "strategy_id": "策略", "symbol": "代码", "decision": "决策",
            "reason": "原因", "payload_json": "决策明细", "created_at": "时间（中国）",
        }).drop(columns=["id"], errors="ignore")
        decision_display["决策明细"] = decision_display["决策明细"].map(readable_payload)
        st.dataframe(translate_common_values(decision_display), hide_index=True)
    degraded = store.get_setting("connectivity_degraded", "false") == "true"
    recovery_blocked = store.get_setting("recovery_blocked", "false") == "true"
    if degraded:
        st.error("OpenD/券商连接异常：禁止新增风险。")
    elif recovery_blocked:
        st.warning("启动恢复发现三账差异：完成持仓归属并重新对账前禁止新增风险。")
    else:
        st.success("连接与恢复状态正常。")
