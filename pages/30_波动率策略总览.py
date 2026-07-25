from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_volatility_campaigns, load_volatility_symbols
from futu_quant.radar_control import status as radar_status
from futu_quant.store import TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar, translate_common_values


st.set_page_config(page_title="波动率策略总览", page_icon=":material/waves:", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

st.title("波动率策略总览")
st.caption("只看当前状态、持仓和阻断原因；参数和策略维护请进入高级配置页面。")

symbols = load_volatility_symbols()
campaigns = load_volatility_campaigns()
decisions = [
    item for item in store.decisions(limit=500)
    if item.get("strategy_id") == "volatility_long_gamma"
]
scanner_decisions = [
    item for item in store.decisions(limit=500)
    if item.get("strategy_id") == "volatility_scanner"
]


def parse_payload(raw: object) -> dict:
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def recent_scanner_items() -> list[tuple[dict, dict]]:
    size = len(symbols) or 1
    return [
        (item, parse_payload(item.get("payload_json")))
        for item in scanner_decisions[:size]
    ]


@st.cache_data(ttl=15, show_spinner=False)
def load_broker_snapshot() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    cached_broker = FutuPaperBroker(Settings.load())
    try:
        return cached_broker.positions(), cached_broker.orders(), ""
    except BrokerError as exc:
        return pd.DataFrame(), pd.DataFrame(), str(exc)


def build_position_map(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    if frame.empty or "code" not in frame.columns:
        return result
    for code, group in frame.groupby(frame["code"].astype(str)):
        qty = pd.to_numeric(group.get("qty"), errors="coerce").fillna(0).sum()
        cost = pd.to_numeric(group.get("cost_price"), errors="coerce").dropna()
        result[code] = {
            "quantity": float(qty),
            "cost": float(cost.iloc[0]) if not cost.empty else 0.0,
        }
    return result


def build_shadow_map(store: TradingStore) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for item in store.shadow_open_positions():
        code = str(item.get("symbol", ""))
        if code:
            result[code] = {
                "quantity": float(item.get("quantity", 0) or 0),
                "cost": float(item.get("average_cost", 0) or 0),
            }
    return result


def order_pending_count(frame: pd.DataFrame) -> int:
    if frame.empty or "order_status" not in frame.columns:
        return 0
    status = frame["order_status"].astype(str)
    return int(status.str.contains("待|提交|未成交|SUBMITTED|SUBMIT", case=False, na=False).sum())


def markdown_table(frame: pd.DataFrame, limit: int | None = None) -> str:
    if frame.empty:
        return ""
    safe = export_safe_frame(frame.head(limit) if limit else frame)
    headers = [str(column).replace("|", "\\|") for column in safe.columns]
    rows = []
    for _, row in safe.iterrows():
        rows.append([
            str(value).replace("\n", " ").replace("|", "\\|")
            for value in row.tolist()
        ])
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def build_report_markdown(
    *,
    generated_at: datetime,
    symbols_count: int,
    campaigns_count: int,
    paper_position_count: int,
    shadow_position_count: int,
    pending_orders: int,
    radar_running: bool,
    action_frame: pd.DataFrame,
    strategy_frame: pd.DataFrame,
    scan_frame: pd.DataFrame,
    decision_frame: pd.DataFrame,
) -> str:
    lines = [
        "# 波动率策略运行复盘",
        "",
        f"- 生成时间（中国）：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 监控正股：{symbols_count}",
        f"- 自动策略：{campaigns_count}",
        f"- 富途模拟盘持仓策略：{paper_position_count}",
        f"- Shadow 持仓策略：{shadow_position_count}",
        f"- 待成交订单：{pending_orders}",
        f"- 雷达状态：{'运行中' if radar_running else '未运行'}",
        "",
        "## 下一步处理队列",
        "",
    ]
    lines.append("当前没有需要优先处理的事项。" if action_frame.empty else markdown_table(action_frame))
    lines.extend(["", "## 自动策略", ""])
    lines.append("暂无自动策略。" if strategy_frame.empty else markdown_table(strategy_frame))
    lines.extend(["", "## 最近雷达扫描", ""])
    lines.append("暂无雷达扫描记录。" if scan_frame.empty else markdown_table(scan_frame, limit=50))
    lines.extend(["", "## 最近执行诊断", ""])
    lines.append("暂无执行诊断记录。" if decision_frame.empty else markdown_table(decision_frame, limit=50))
    return "\n".join(lines) + "\n"


def option_table_config() -> dict:
    return {
        "当前持仓": st.column_config.NumberColumn("当前持仓", format="%.0f"),
        "开仓价": st.column_config.NumberColumn("开仓价", format="$%.2f"),
    }


def export_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    safe = frame.copy()
    for column in safe.columns:
        if pd.api.types.is_datetime64_any_dtype(safe[column]):
            values = pd.to_datetime(safe[column], errors="coerce")
            if getattr(values.dt, "tz", None) is not None:
                values = values.dt.tz_convert("Asia/Shanghai")
            safe[column] = values.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
        elif safe[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
            safe[column] = safe[column].map(
                lambda value: json.dumps(value, ensure_ascii=False, default=str)
                if isinstance(value, (dict, list, tuple, set)) else value
            )
    return safe.fillna("")


latest_by_code = {}
for item in decisions:
    latest_by_code.setdefault(str(item.get("symbol", "")), item)

radar = radar_status()
page_loaded_at = datetime.now().astimezone()
positions, orders, broker_error = load_broker_snapshot()
position_map = build_position_map(positions)
shadow_map = build_shadow_map(store)
pending_count = order_pending_count(orders)
paper_position_count = sum(position_map.get(str(item.get("code")), {}).get("quantity", 0) > 0 for item in campaigns)
shadow_position_count = sum(shadow_map.get(str(item.get("code")), {}).get("quantity", 0) > 0 for item in campaigns)

with st.container(horizontal=True):
    st.metric("监控正股", len(symbols), border=True)
    st.metric("自动策略", len(campaigns), border=True)
    st.metric("模拟盘持仓策略", paper_position_count, border=True)
    st.metric("Shadow 持仓策略", shadow_position_count, border=True)
    st.metric("待成交订单", pending_count, border=True)
    st.metric("雷达状态", "运行中" if radar.running else "未运行", border=True)

with st.container(border=True):
    st.subheader("数据健康状态")
    with st.container(horizontal=True):
        st.metric("页面读取时间", page_loaded_at.strftime("%H:%M:%S"), border=True)
        st.metric("雷达进程", f"PID {radar.pid}" if radar.pid else "未启动", border=True)
        st.metric("券商连接", "读取成功" if not broker_error else "读取失败", border=True)
        st.metric("策略账本", "可用" if campaigns or decisions else "暂无记录", border=True)
        st.metric("账户缓存", "15 秒", border=True)
    if radar.running:
        st.caption("雷达状态来自独立服务进程；页面刷新不会重新启动雷达，也不会重复提交订单。")
    else:
        st.warning("雷达未运行：总览仍可查看历史数据，但不会自动发现新机会。")
    st.caption("数据源：策略配置/决策来自本地账本；持仓和订单来自富途模拟盘短缓存；Shadow 来自虚拟成交账本。")

with st.container(border=True):
    st.subheader("今日风险提示")
    risk_items: list[tuple[str, str]] = []
    action_items: list[dict[str, str]] = []
    if broker_error:
        risk_items.append(("高", f"富途模拟盘读取失败：{broker_error}"))
        action_items.append({
            "优先级": "高",
            "处理事项": "修复富途连接",
            "原因": broker_error,
            "建议入口": "交易诊断中心 / 系统监控",
        })
    if not radar.running:
        risk_items.append(("高", "独立波动率雷达未运行，不会自动扫描或生成新策略。"))
        action_items.append({
            "优先级": "高",
            "处理事项": "启动独立波动率雷达",
            "原因": "雷达进程未运行",
            "建议入口": "系统监控 / 波动率高级配置",
        })
    if pending_count:
        risk_items.append(("中", f"存在 {pending_count} 笔待成交订单，请检查限价、盘口和订单状态。"))
        action_items.append({
            "优先级": "中",
            "处理事项": "检查待成交订单",
            "原因": f"{pending_count} 笔订单仍在等待成交",
            "建议入口": "订单管理",
        })
    if scanner_decisions:
        recent_scans = recent_scanner_items()
        blocked_scans = sum(payload.get("campaign_created") is False for _, payload in recent_scans)
        if blocked_scans:
            risk_items.append(("中", f"最近一轮有 {blocked_scans} 个标的触发后未生成期权策略，请查看阻断原因。"))
            for item, payload in recent_scans:
                if payload.get("campaign_created") is False:
                    action_items.append({
                        "优先级": "中",
                        "处理事项": f"处理 {item.get('symbol', '')} 候选阻断",
                        "原因": str(payload.get("campaign_reason") or item.get("reason", "未记录原因")),
                        "建议入口": "波动率高级配置 / 交易诊断中心",
                    })
    if shadow_position_count:
        risk_items.append(("低", f"当前有 {shadow_position_count} 条策略存在 Shadow 持仓，需与模拟盘持仓分开核对。"))
        action_items.append({
            "优先级": "低",
            "处理事项": "核对 Shadow 与模拟盘持仓",
            "原因": f"{shadow_position_count} 条策略存在 Shadow 持仓",
            "建议入口": "期权盈亏台账 / 交易生命周期",
        })
    if risk_items:
        for level, message in risk_items:
            if level == "高":
                st.error(f"[{level}] {message}")
            elif level == "中":
                st.warning(f"[{level}] {message}")
            else:
                st.info(f"[{level}] {message}")
    else:
        st.success("当前没有发现雷达、连接、订单或持仓层面的高风险提示。")

action_frame = pd.DataFrame(action_items).drop_duplicates()
if not action_frame.empty:
    priority_order = {"高": 0, "中": 1, "低": 2}
    action_frame["排序"] = action_frame["优先级"].map(priority_order).fillna(9)
    action_frame = action_frame.sort_values(["排序", "处理事项"]).drop(columns=["排序"])

with st.container(border=True):
    st.subheader("下一步处理队列")
    if not action_frame.empty:
        with st.container(horizontal=True):
            st.metric("高优先级", int((action_frame["优先级"] == "高").sum()), border=True)
            st.metric("中优先级", int((action_frame["优先级"] == "中").sum()), border=True)
            st.metric("低优先级", int((action_frame["优先级"] == "低").sum()), border=True)
        st.dataframe(action_frame, hide_index=True, height=260)
    else:
        st.success("当前没有需要优先处理的事项。")
    with st.container(horizontal=True):
        st.page_link("pages/4_订单管理.py", label="订单管理", icon=":material/receipt_long:")
        st.page_link("pages/26_交易诊断中心.py", label="交易诊断", icon=":material/troubleshoot:")
        st.page_link("pages/6_系统监控.py", label="系统监控", icon=":material/monitoring:")
        st.page_link("pages/28_期权盈亏台账.py", label="期权盈亏", icon=":material/price_check:")

if broker_error:
    st.warning(f"富途模拟盘读取失败，当前只显示策略账本：{broker_error}")

with st.container(border=True):
    st.subheader("一眼判断")
    if not symbols:
        st.warning("还没有配置波动率策略正股。请进入高级配置页添加标的。")
    elif not radar.running:
        st.warning("独立雷达尚未运行，不会自动扫描自选标的。")
    elif not campaigns:
        st.info("雷达正在运行，但暂时没有生成自动期权策略；请查看下方最近决策。")
    else:
        st.success(f"雷达正在扫描 {len(symbols)} 个自选标的，当前有 {len(campaigns)} 条自动策略。")

with st.container(border=True):
    st.subheader("最近一次雷达扫描")
    if scanner_decisions:
        latest_scan = scanner_decisions[0]
        scan_time = pd.to_datetime(latest_scan.get("created_at"), errors="coerce", utc=True)
        scan_time_text = (
            scan_time.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
            if pd.notna(scan_time) else "未知"
        )
        recent_scans = recent_scanner_items()
        active_count = sum(bool(payload.get("active")) for _, payload in recent_scans)
        created_count = sum(bool(payload.get("campaign_created")) for _, payload in recent_scans)
        blocked_count = sum(payload.get("campaign_created") is False for _, payload in recent_scans)
        with st.container(horizontal=True):
            st.metric("扫描时间（中国）", scan_time_text, border=True)
            st.metric("触发标的", active_count, border=True)
            st.metric("生成自动策略", created_count, border=True)
            st.metric("合约/风控阻断", blocked_count, border=True)
        st.caption(f"最近标的：{latest_scan.get('symbol', '—')} · {latest_scan.get('reason', '暂无原因')}")
        blocked_rows = []
        for item, payload in recent_scans:
            if payload.get("campaign_created") is False or payload.get("active"):
                blocked_rows.append({
                    "正股代码": item.get("symbol", ""),
                    "是否触发": "是" if payload.get("active") else "否",
                    "自动策略": payload.get("campaign_code") or "未生成",
                    "原因": payload.get("campaign_reason") or item.get("reason", "—"),
                })
        if blocked_rows:
            st.dataframe(pd.DataFrame(blocked_rows), hide_index=True, height=220)
    else:
        st.info("暂无雷达扫描记录。启动独立雷达后，这里会显示最近一次扫描闭环。")

with st.container(border=True):
    st.subheader("自动策略持仓")
    filter_left, filter_middle, filter_right = st.columns([1.2, 1.2, 1])
    campaign_search = filter_left.text_input(
        "搜索代码", placeholder="例如 NVDA 或 2607", key="volatility_overview_search"
    )
    campaign_status = filter_middle.selectbox(
        "状态筛选", ["全部", "有持仓", "无持仓", "有异常"], key="volatility_overview_status"
    )
    compact_view = filter_right.toggle(
        "移动端精简列", value=True, key="volatility_overview_compact"
    )
    rows = []
    for campaign in campaigns:
        code = str(campaign.get("code", ""))
        position = position_map.get(code, {})
        latest = latest_by_code.get(code, {})
        rows.append({
            "期权代码": code,
            "正股代码": campaign.get("underlying", ""),
            "方向": campaign.get("option_type", ""),
            "到期日": campaign.get("strike_time", ""),
            "当前持仓": position.get("quantity", 0) + shadow_map.get(code, {}).get("quantity", 0),
            "开仓价": position.get("cost", 0) if position.get("quantity", 0) > 0 else shadow_map.get(code, {}).get("cost", 0),
            "持仓来源": "富途模拟盘 + Shadow" if position.get("quantity", 0) > 0 and shadow_map.get(code, {}).get("quantity", 0) > 0 else "富途模拟盘" if position.get("quantity", 0) > 0 else "Shadow 虚拟账本" if shadow_map.get(code, {}).get("quantity", 0) > 0 else "无持仓",
            "运行状态": latest.get("decision", "暂无记录"),
            "最新原因": latest.get("reason", "尚无执行记录"),
            "是否异常": str(latest.get("decision", "")).upper() in {"BLOCKED", "REJECTED", "ERROR"},
        })
    if rows:
        strategy_frame = pd.DataFrame(rows)
        if campaign_search.strip():
            needle = campaign_search.strip().upper()
            strategy_frame = strategy_frame[
                strategy_frame.astype(str).apply(
                    lambda row: row.str.upper().str.contains(needle, regex=False).any(), axis=1
                )
            ]
        if campaign_status == "有持仓":
            strategy_frame = strategy_frame[strategy_frame["当前持仓"] > 0]
        elif campaign_status == "无持仓":
            strategy_frame = strategy_frame[strategy_frame["当前持仓"] <= 0]
        elif campaign_status == "有异常":
            strategy_frame = strategy_frame[strategy_frame["是否异常"]]
        if strategy_frame.empty:
            st.info("没有符合当前筛选条件的自动策略。")
        else:
            if compact_view:
                display_columns = [
                    "期权代码", "正股代码", "方向", "到期日", "当前持仓", "开仓价", "持仓来源", "运行状态", "最新原因"
                ]
            else:
                display_columns = [column for column in strategy_frame.columns if column != "是否异常"]
            st.dataframe(
                china_time_columns(translate_common_values(strategy_frame[display_columns])),
                hide_index=True, height=360, column_config=option_table_config(),
            )
    else:
        st.info("暂无自动策略。")

with st.container(border=True):
    st.subheader("最近执行诊断")
    if decisions:
        frame = pd.DataFrame(decisions[:30])
        if "payload_json" in frame.columns:
            def payload_reason(raw: object) -> str:
                try:
                    payload = json.loads(str(raw or "{}"))
                    warnings = payload.get("validation_warnings", [])
                    return "；".join(map(str, warnings))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return ""
            frame["校验提醒"] = frame["payload_json"].map(payload_reason)
        frame = frame.rename(columns={
            "symbol": "期权代码", "decision": "决策", "reason": "原因", "created_at": "时间（中国）",
        }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
        st.dataframe(china_time_columns(translate_common_values(frame)), hide_index=True, height=380)
    else:
        st.info("暂无执行记录。")

with st.container(border=True):
    st.subheader("导出当前复盘数据")
    st.caption("导出内容来自当前页面已读取的数据，不会触发新的富途行情请求。")
    export_strategy_rows = []
    for campaign in campaigns:
        code = str(campaign.get("code", ""))
        position = position_map.get(code, {})
        shadow = shadow_map.get(code, {})
        latest = latest_by_code.get(code, {})
        export_strategy_rows.append({
            "期权代码": code,
            "正股代码": campaign.get("underlying", ""),
            "方向": campaign.get("option_type", ""),
            "到期日": campaign.get("strike_time", ""),
            "富途模拟盘持仓": position.get("quantity", 0),
            "Shadow持仓": shadow.get("quantity", 0),
            "合计持仓": position.get("quantity", 0) + shadow.get("quantity", 0),
            "开仓价": position.get("cost", 0) if position.get("quantity", 0) > 0 else shadow.get("cost", 0),
            "运行状态": latest.get("decision", "暂无记录"),
            "最新原因": latest.get("reason", "尚无执行记录"),
        })
    export_strategy_frame = pd.DataFrame(export_strategy_rows)
    export_scan_frame = pd.DataFrame([
        {
            "正股代码": item.get("symbol", ""),
            "扫描时间": item.get("created_at", ""),
            "结果": item.get("decision", ""),
            "原因": item.get("reason", ""),
        }
        for item in scanner_decisions[:200]
    ])
    export_decision_frame = pd.DataFrame(decisions[:200]).drop(
        columns=["payload_json"], errors="ignore"
    )
    export_strategy_frame = export_safe_frame(export_strategy_frame)
    export_scan_frame = export_safe_frame(china_time_columns(export_scan_frame))
    export_decision_frame = export_safe_frame(china_time_columns(export_decision_frame))
    action_export_frame = export_safe_frame(action_frame)
    report_markdown = build_report_markdown(
        generated_at=page_loaded_at,
        symbols_count=len(symbols),
        campaigns_count=len(campaigns),
        paper_position_count=paper_position_count,
        shadow_position_count=shadow_position_count,
        pending_orders=pending_count,
        radar_running=radar.running,
        action_frame=action_export_frame,
        strategy_frame=export_strategy_frame,
        scan_frame=export_scan_frame,
        decision_frame=export_decision_frame,
    )
    with st.container(horizontal=True):
        st.download_button(
            "下载自动策略 CSV",
            data=export_strategy_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="volatility_campaigns.csv", mime="text/csv",
            disabled=export_strategy_frame.empty,
        )
        st.download_button(
            "下载雷达扫描 CSV",
            data=export_scan_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="volatility_radar_scans.csv", mime="text/csv",
            disabled=export_scan_frame.empty,
        )
        st.download_button(
            "下载执行诊断 CSV",
            data=export_decision_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="volatility_decisions.csv", mime="text/csv",
            disabled=export_decision_frame.empty,
        )
        st.download_button(
            "下载处理队列 CSV",
            data=action_export_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="volatility_action_queue.csv", mime="text/csv",
            disabled=action_export_frame.empty,
        )
        st.download_button(
            "下载一键复盘 Markdown",
            data=report_markdown.encode("utf-8"),
            file_name=f"volatility_review_{page_loaded_at:%Y%m%d_%H%M%S}.md",
            mime="text/markdown",
        )

with st.container(horizontal=True):
    st.page_link("pages/10_波动率期权策略.py", label="进入高级配置与执行", icon=":material/tune:")
    st.page_link("pages/28_期权盈亏台账.py", label="查看期权盈亏台账", icon=":material/price_check:")
    st.page_link("pages/26_交易诊断中心.py", label="打开交易诊断中心", icon=":material/troubleshoot:")
    if st.button("刷新数据", icon=":material/refresh:"):
        st.rerun()
    if st.button("强制刷新账户缓存", icon=":material/sync:"):
        load_broker_snapshot.clear()
        st.rerun()

with st.expander("页面验收清单", expanded=False):
    st.markdown(
        "- 首屏能看到雷达、持仓、待成交和风险状态。\n"
        "- 下一步处理队列能指出要处理什么、为什么、去哪个页面处理。\n"
        "- 自动策略表能区分富途模拟盘和 Shadow 持仓。\n"
        "- CSV 和 Markdown 报告能完整导出当前复盘数据。\n"
        "- 普通刷新复用 15 秒账户缓存；强制刷新会清掉缓存后重新读取。"
    )
