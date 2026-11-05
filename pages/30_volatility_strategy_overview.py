from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import (
    Settings, load_all_volatility_campaigns, load_volatility_symbols,
)
from futu_quant.radar_control import start as radar_start, status as radar_status, stop as radar_stop
from futu_quant.service_control import status as service_status
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import (
    china_time_columns, get_current_tenant, render_admin_login, render_sidebar, translate_common_values,
)
from futu_quant.volatility_orphans import orphan_rows_for_display


st.set_page_config(page_title="波动率策略总览", page_icon=":material/waves:", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
store = TradingStore()


@st.cache_resource(show_spinner=False)
def _broker_singleton() -> FutuPaperBroker:
    """进程级 broker 单例（整进程一条 OpenD 交易连接）。

    改动前是两处各建一条：模块顶部 `FutuPaperBroker(settings)`，以及
    `load_broker_snapshot` 每次 60 秒缓存过期时又 `FutuPaperBroker(Settings.load())`
    ——两条 OpenD 连接 + 两次配置解析。本页对 broker 只做 positions/orders 只读
    展示，不经过任何下单路径，共用一个实例是安全的。

    配置解析走 `Settings.load_cached()`：模块顶部已经渲染过一次侧边栏，
    这里再来一次 `load()` 会白付 0.13–0.18s（缓存命中是 10µs 级）。
    """
    return FutuPaperBroker(Settings.load_cached())


broker = _broker_singleton()
admin = render_admin_login()
_current_tenant = get_current_tenant(settings)
_overview_tenant_id = str(_current_tenant.get("tenant_id") or "")

st.title("波动率策略总览")
st.caption("只看当前状态、持仓和阻断原因；参数和策略维护请进入高级配置页面。")

symbols = load_volatility_symbols()
# #1 键级分区：总览页**故意读全局**（含各租户键）—— 它是跨租户的状态看板，
# 且**只读**（本页不写任何 campaign 键）。
campaigns = load_all_volatility_campaigns()


@st.cache_data(ttl=60, show_spinner=False)
def _cached_decisions(strategy_id: str) -> list[dict]:
    """决策日志查询按 60 秒缓存。

    本页每次交互都整页重跑，模块顶的 decisions 查询每次都直打 SQLite
    （decision_logs 单行 ≈2.4 KB payload）。看板是只读展示，60 秒陈旧度
    可接受——真正下单的执行路径不经过本页。

    改动前是 `store.decisions(limit=500)` 拉全局最近 500 行、再在 Python 里
    按 strategy_id 过滤一遍（两个策略各过滤一次，同一批 payload 搬两趟）。
    现在 SQL 侧 `WHERE strategy_id=? ORDER BY id DESC LIMIT ?`，只取本策略。
    """
    return store.decisions_for_strategy(strategy_id, limit=500)


decisions = _cached_decisions("volatility_long_gamma")
scanner_decisions = _cached_decisions("volatility_scanner")


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


@st.cache_data(ttl=60, show_spinner=False)
def load_broker_snapshot() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    # 60 秒缓存：positions+orders 两次 OpenD RPC 实测 ≈1.7s，是本页 rerun 的
    # 最大单项。总览页只展示不执行，60 秒陈旧度可接受。
    try:
        return broker.positions(), broker.orders(), ""
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


@st.cache_data(ttl=60, show_spinner=False)
def _cached_shadow_positions() -> list[dict]:
    """Shadow 虚拟账本 60 秒缓存（build_shadow_map 每轮实测 0.06s，且每次 rerun 都跑）。

    Shadow 是虚拟成交账本，看板展示 60 秒陈旧度可接受；下单与对账不经过本缓存。
    """
    return store.shadow_open_positions()


def build_shadow_map() -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for item in _cached_shadow_positions():
        if str(item.get("strategy_id", "")) != "volatility_long_gamma":
            continue
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
service = service_status()
page_loaded_at = datetime.now().astimezone()
positions, orders, broker_error = load_broker_snapshot()
position_map = build_position_map(positions)
shadow_map = build_shadow_map()
orphan_rows = [] if broker_error else orphan_rows_for_display(store, positions, orders, broker, campaigns)
orphan_frame = pd.DataFrame(orphan_rows)
pending_count = order_pending_count(orders)
paper_position_count = sum(position_map.get(str(item.get("code")), {}).get("quantity", 0) > 0 for item in campaigns)
shadow_position_count = sum(shadow_map.get(str(item.get("code")), {}).get("quantity", 0) > 0 for item in campaigns)

with st.container(horizontal=True):
    st.metric("监控正股", len(symbols), border=True)
    st.metric("自动策略", len(campaigns), border=True)
    st.metric("模拟盘持仓策略", paper_position_count, border=True)
    st.metric("Shadow 持仓策略", shadow_position_count, border=True)
    st.metric("未接管风险仓位", len(orphan_rows), border=True)
    st.metric("待成交订单", pending_count, border=True)
    radar_label = (
        "扫描与交易在线" if radar.running and service.running and not service.restart_pending
        else "仅扫描，不交易" if radar.running else "未运行"
    )
    st.metric("雷达状态", radar_label, border=True)

if radar.running and (not service.running or service.restart_pending):
    st.warning(
        "独立雷达仍会扫描自选标的，但统一交易服务未就绪，当前不会执行策略决策或提交模拟单。"
    )

with st.container(border=True):
    st.caption("运行一次会完成：持仓/订单对账 → 最佳期权表达维护 → 策略决策 → 模拟下单或 Shadow → 退出检查。")
    with st.container(horizontal=True):
        if st.button("启动独立雷达", icon=":material/power:", disabled=radar.running, key="overview_start_radar"):
            radar_start()
            st.rerun()
        if st.button("停止独立雷达", icon=":material/stop_circle:", disabled=not radar.running, key="overview_stop_radar"):
            radar_stop()
            st.rerun()
        if st.button("刷新数据", icon=":material/refresh:", key="overview_refresh_data"):
            load_broker_snapshot.clear()
            st.rerun()
        if st.button("强制刷新账户缓存", icon=":material/sync:", key="overview_refresh_account_cache"):
            load_broker_snapshot.clear()
            st.rerun()

# Keep the daily workflow visible in one place. The detailed legacy sections
# below remain available for exports and backward-compatible deep links.
# 8 个 tab 改动态渲染：只有 `on_change="rerun"` 时 Streamlit 才会给 tab 填 `.open`，
# 未选中的 tab 整块不执行。重 tab（选品漏斗 / 执行诊断 / 导出）因此只在点进去时才算。
overview_tabs = st.tabs(
    [
        "雷达流水线", "🔍 选品漏斗（为什么没单）", "策略决策", "当前持仓", "未接管风险",
        "待成交订单", "执行诊断", "导出复盘",
    ],
    on_change="rerun", key="overview_tab",
)
if overview_tabs[0].open:
    with overview_tabs[0]:
        st.caption("雷达只扫描已配置的自选标的，不会扫描应用市场。")
        if scanner_decisions:
            scan_view = china_time_columns(pd.DataFrame(scanner_decisions[:100])).rename(columns={
                "symbol": "正股代码", "decision": "决策", "reason": "原因", "created_at": "时间（中国）",
            }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
            st.dataframe(translate_common_values(scan_view), hide_index=True, height=360)
        else:
            st.info("暂无雷达扫描记录。")
if overview_tabs[1].open:
    with overview_tabs[1]:
        st.subheader("🔍 候选池选品漏斗与无单归因 (Candidate Quality Funnel)")
        st.caption(
            f"当前操作租户：**{_overview_tenant_id or '全局'}** · "
            "全景解析雷达扫描中「为什么某些标的没有开仓」，逐笔穿透行情停滞、价差过大、流动性匮乏等硬排除原因。"
        )

        hours_choice = st.selectbox(
            "统计时间窗口", [24, 48, 72, 168], format_func=lambda h: f"最近 {h} 小时", index=0, key="overview_funnel_hours"
        )

        summary_rows = store.candidate_exclusion_summary(_overview_tenant_id, hours=hours_choice)
        recent_details = store.recent_candidate_exclusions(_overview_tenant_id, limit=200)

        hard_excludes = sum(r["count"] for r in summary_rows if r.get("phase") == "HARD_EXCLUDE")
        soft_demotes = sum(r["count"] for r in summary_rows if r.get("phase") == "SOFT_DEMOTE")
        admitted_count = sum(r["count"] for r in summary_rows if r.get("phase") == "ADMITTED")
        total_candidates = hard_excludes + soft_demotes + admitted_count

        with st.container(horizontal=True, border=True):
            st.metric("扫描候选总计", f"{total_candidates} 份", border=True)
            st.metric("硬排除过滤", f"{hard_excludes} 份", delta=f"-{hard_excludes}" if hard_excludes else None, delta_color="inverse", border=True)
            st.metric("软降权处理", f"{soft_demotes} 份", border=True)
            st.metric("最终入选候选池", f"{admitted_count} 份", border=True)

        REASON_MAP = {
            "QUOTE_STALE": "⏱️ 行情停滞（超180s未跳价）",
            "SPREAD_TOO_WIDE": "↔️ 买卖价差过大（流动性差）",
            "BOOK_MISSING": "📭 盘口买卖价缺失（无报价）",
            "VOLUME_TOO_LOW": "📉 成交量过低（缺乏对手盘）",
            "EXPIRED_T0": "⌛ 当日到期期权（到期排除）",
        }

        col_chart, col_reasons = st.columns([3, 2])
        with col_chart:
            st.markdown("**📊 排除原因分布统计**")
            if summary_rows:
                dist_data = []
                for r in summary_rows:
                    dist_data.append({
                        "排除原因": REASON_MAP.get(r["reason"], r["reason"]),
                        "过滤阶段": "硬排除" if r["phase"] == "HARD_EXCLUDE" else ("软降权" if r["phase"] == "SOFT_DEMOTE" else "入选"),
                        "拦截笔数": r["count"],
                    })
                st.dataframe(pd.DataFrame(dist_data), hide_index=True, width="stretch")
            else:
                st.info("所选窗口内暂无候选排除流水记录（非交易时段或全量放行）。")

        with col_reasons:
            st.markdown("**💡 无单常见原因排查指引**")
            st.markdown(
                "- **⏱️ 行情停滞**：期权超过 180 秒未刷新盘口跳价，防范流动性枯竭冷门合约；\n"
                "- **↔️ 价差过大**：买卖价差比超过 5%~10%，坚决不进场吃滑点；\n"
                "- **⌛ 当日到期**：到期日当天属于 T-0 尾部高 Gamma 剧烈衰减区，系统自动禁止新买入；\n"
                "- **📉 交易量偏低**：日内成交量不足 10 张或未平仓量 < 800 张，防范平仓踩踏。"
            )

        st.markdown("**📜 最近 200 笔候选排除与降权明细下钻流水**")
        if recent_details:
            detail_rows = []
            for d in recent_details:
                detail_rows.append({
                    "记录时间": d.get("created_at", "")[:19].replace("T", " "),
                    "标的代码": d.get("option_code", ""),
                    "正股代码": d.get("underlying", ""),
                    "排除阶段": "硬排除" if d.get("phase") == "HARD_EXCLUDE" else "软降权",
                    "中文原因": REASON_MAP.get(d.get("reason", ""), d.get("reason", "")),
                    "盘口买价": f"${float(d.get('bid', 0)):.2f}" if d.get("bid") is not None else "—",
                    "盘口卖价": f"${float(d.get('ask', 0)):.2f}" if d.get("ask") is not None else "—",
                    "价差比例": f"{float(d.get('spread_pct', 0)):.1f}%" if d.get("spread_pct") is not None else "—",
                    "行情停滞": f"{int(d.get('age_seconds', 0))} 秒" if d.get("age_seconds") is not None else "—",
                })
            st.dataframe(pd.DataFrame(detail_rows), hide_index=True, width="stretch")
        else:
            st.info("暂无最近排除明细流水。")

        st.markdown("---")
        st.markdown("**📡 雷达扫描阶段宏观漏斗 (正股级)**")
        total = len(scanner_decisions)
        active = sum(bool(parse_payload(item.get("payload_json")).get("active")) for item in scanner_decisions)
        generated = sum(bool(parse_payload(item.get("payload_json")).get("campaign_created")) for item in scanner_decisions)
        blocked = sum(parse_payload(item.get("payload_json")).get("campaign_created") is False for item in scanner_decisions)
        with st.container(horizontal=True):
            st.metric("扫描记录", total, border=True)
            st.metric("信号触发", active, border=True)
            st.metric("生成自动策略", generated, border=True)
            st.metric("候选阻断", blocked, border=True)
        if scanner_decisions:
            funnel = pd.DataFrame({"阶段": ["扫描", "信号触发", "生成策略", "候选阻断"], "数量": [total, active, generated, blocked]})
            st.bar_chart(funnel, x="阶段", y="数量")
        else:
            st.info("暂无足够的扫描数据。")

        st.markdown("---")
        st.markdown("**🚫 候选拦截明细（含租户，让模拟盘「为什么没下单」可见）**")
        st.caption(
            "数据来源：`decision_logs` 的 `CANDIDATE_BLOCKED`（雷达候选全被入场过滤器拒绝时落库，"
            "与实盘 `LIVE_MAX_ORDER_NOTIONAL` 前置拦截审计同源的更早一层）。"
            "展示**全部租户**的拦截——模拟盘（paper）的拦截此前只在库里、界面看不到，这里补齐。"
        )
        blocked_rows = [
            item for item in scanner_decisions
            if str(item.get("decision", "")).upper() == "CANDIDATE_BLOCKED"
        ]
        if blocked_rows:
            blocked_view = china_time_columns(pd.DataFrame(blocked_rows[:100])).rename(columns={
                "symbol": "正股代码", "reason": "拦截原因", "created_at": "时间（中国）",
                "tenant_id": "租户",
            })
            blocked_view["租户"] = blocked_view.get("租户", "").fillna("").map(
                lambda tid: {
                    "futu_real_master": "实盘 (real)",
                    "futu_paper_sandbox": "模拟盘 (paper)",
                }.get(str(tid), str(tid) or "未标记(旧数据)")
            )
            st.dataframe(
                translate_common_values(
                    blocked_view[["时间（中国）", "租户", "正股代码", "拦截原因"]]
                    if all(col in blocked_view.columns for col in ("时间（中国）", "租户", "正股代码", "拦截原因"))
                    else blocked_view
                ),
                hide_index=True, height=300,
            )
        else:
            st.info("暂无候选拦截记录（所有触发标的的候选均已放行或未触发信号）。")
if overview_tabs[2].open:
    with overview_tabs[2]:
        decision_view = pd.DataFrame(decisions[:100]) if decisions else pd.DataFrame()
        if not decision_view.empty:
            decision_view = china_time_columns(decision_view).rename(columns={"symbol": "期权代码", "decision": "决策", "reason": "原因", "created_at": "时间（中国）"}).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
            st.dataframe(translate_common_values(decision_view), hide_index=True, height=420)
        else:
            st.info("暂无策略决策记录。")
if overview_tabs[3].open:
    with overview_tabs[3]:
        holding_rows = []
        for campaign in campaigns:
            code = str(campaign.get("code", ""))
            paper = position_map.get(code, {})
            shadow = shadow_map.get(code, {})
            paper_quantity = float(paper.get("quantity", 0) or 0)
            shadow_quantity = float(shadow.get("quantity", 0) or 0)
            if paper_quantity and shadow_quantity:
                source = "模拟盘 + Shadow"
            elif paper_quantity:
                source = "富途模拟盘"
            elif shadow_quantity:
                source = "Shadow 虚拟账本"
            else:
                source = "无持仓"
            holding_rows.append({
                "期权代码": code,
                "正股代码": campaign.get("underlying", ""),
                "富途模拟盘数量": paper_quantity,
                "模拟盘开仓价": float(paper.get("cost", 0) or 0),
                "Shadow 数量": shadow_quantity,
                "Shadow 开仓价": float(shadow.get("cost", 0) or 0),
                "合计数量": paper_quantity + shadow_quantity,
                "持仓归类": source,
            })
        if not holding_rows:
            st.info("暂无已保存的波动率策略。")
        else:
            holdings = pd.DataFrame(holding_rows)
            paper_holdings = holdings[holdings["富途模拟盘数量"] != 0]
            shadow_holdings = holdings[holdings["Shadow 数量"] != 0]
            dual_holdings = holdings[
                (holdings["富途模拟盘数量"] != 0)
                & (holdings["Shadow 数量"] != 0)
            ]
            inactive_holdings = holdings[holdings["合计数量"] == 0]
            with st.container(horizontal=True):
                st.metric("模拟盘持仓", len(paper_holdings), border=True)
                st.metric("Shadow 持仓", len(shadow_holdings), border=True)
                st.metric("双账并存", len(dual_holdings), border=True)
                st.metric("未持仓配置", len(inactive_holdings), border=True)
            paper_tab, shadow_tab, dual_tab, inactive_tab, all_tab = st.tabs([
                "富途模拟盘", "Shadow 虚拟账本", "双账并存", "未持仓配置", "全部策略",
            ])
            holding_config = {
                "富途模拟盘数量": st.column_config.NumberColumn(format="%.0f"),
                "模拟盘开仓价": st.column_config.NumberColumn(format="$%.2f"),
                "Shadow 数量": st.column_config.NumberColumn(format="%.0f"),
                "Shadow 开仓价": st.column_config.NumberColumn(format="$%.2f"),
                "合计数量": st.column_config.NumberColumn(format="%.0f"),
            }
            holding_views = (
                (paper_tab, paper_holdings, "当前没有富途模拟盘持仓。"),
                (shadow_tab, shadow_holdings, "当前没有该策略的 Shadow 持仓。"),
                (dual_tab, dual_holdings, "当前没有模拟盘与 Shadow 双账并存的合约。"),
                (inactive_tab, inactive_holdings, "当前没有未持仓的策略配置。"),
                (all_tab, holdings, "暂无策略。"),
            )
            for tab, frame, empty_message in holding_views:
                with tab:
                    if frame.empty:
                        st.info(empty_message)
                    else:
                        st.dataframe(
                            frame,
                            hide_index=True,
                            height=360,
                            column_config=holding_config,
                        )
if overview_tabs[4].open:
    with overview_tabs[4]:
        st.caption("这里显示：富途模拟盘仍有数量，但不在当前波动率策略配置和状态中的期权。默认会先自动接管，纳入止盈、止损、盈利保护和方向退出。")
        if orphan_frame.empty:
            st.success("当前没有未接管的波动率期权风险仓位。")
        else:
            display = orphan_frame.rename(columns={
                "code": "期权代码",
                "underlying": "正股代码",
                "quantity": "富途数量",
                "can_sell_quantity": "可卖数量",
                "cost_price": "成本价",
                "nominal_price": "当前价",
                "market_value": "市值",
                "pnl_value": "盈亏金额",
                "pnl_ratio": "盈亏比例",
                "strategy_net_quantity": "策略净成交数量",
                "pending_order_count": "待成交订单数",
                "status": "状态",
                "reason": "原因",
            })
            status_map = {
                "SELLABLE_ORPHAN": "未接管，可平仓",
                "PENDING_EXIT_ORDER": "已有退出挂单",
                "LOCKED_ORPHAN": "未接管，暂不可卖",
            }
            if "状态" in display.columns:
                display["状态"] = display["状态"].map(lambda value: status_map.get(str(value), value))
            st.error(f"发现 {len(display)} 个未接管风险仓位。请优先处理，系统自动服务默认会先接管；如配置为 CLOSE 才会直接减仓退出。")
            st.dataframe(
                display,
                hide_index=True,
                height=320,
                column_config={
                    "富途数量": st.column_config.NumberColumn(format="%.0f"),
                    "可卖数量": st.column_config.NumberColumn(format="%.0f"),
                    "成本价": st.column_config.NumberColumn(format="$%.2f"),
                    "当前价": st.column_config.NumberColumn(format="$%.2f"),
                    "市值": st.column_config.NumberColumn(format="$%.2f"),
                    "盈亏金额": st.column_config.NumberColumn(format="$%.2f"),
                    "盈亏比例": st.column_config.NumberColumn(format="%.2f"),
                },
            )
if overview_tabs[5].open:
    with overview_tabs[5]:
        if orders.empty:
            st.info("暂无待成交订单。")
        else:
            pending = orders[orders.get("order_status", pd.Series(dtype=str)).astype(str).str.contains("待|提交|未成交|SUBMITTED|SUBMIT", case=False, na=False)]
            if pending.empty:
                st.info("暂无待成交订单。")
            else:
                st.dataframe(
                    translate_common_values(china_time_columns(pending)),
                    hide_index=True,
                    height=360,
                )
if overview_tabs[6].open:
    with overview_tabs[6]:
        blocked_decisions = [item for item in decisions if str(item.get("decision", "")).upper() in {"BLOCKED", "REJECTED", "ERROR"}]
        if blocked_decisions:
            diag = china_time_columns(pd.DataFrame(blocked_decisions[:100])).rename(columns={"symbol": "期权代码", "decision": "决策", "reason": "原因", "created_at": "时间（中国）"}).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
            st.dataframe(translate_common_values(diag), hide_index=True, height=320)
        else:
            st.success("当前没有需要处理的阻断或异常。")
if overview_tabs[7].open:
    with overview_tabs[7]:
        st.caption("详细 CSV 和 Markdown 下载保留在页面下方的导出复盘区域。")
        st.page_link("pages/10_volatility_options_strategy.py", label="进入高级配置与执行", icon=":material/tune:")

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
    if orphan_rows:
        risk_items.append(("高", f"存在 {len(orphan_rows)} 个富途真实期权仓位未被策略接管，普通止盈止损不会管理它们。"))
        for item in orphan_rows:
            action_items.append({
                "优先级": "高",
                "处理事项": f"处理未接管期权 {item.get('code', '')}",
                "原因": item.get("reason", "富途持仓未进入策略状态"),
                "建议入口": "波动率策略总览 / 当前持仓",
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
        st.page_link("pages/4_order_management.py", label="订单管理", icon=":material/receipt_long:")
        st.page_link("pages/26_trade_diagnostics_center.py", label="交易诊断", icon=":material/troubleshoot:")
        st.page_link("pages/6_system_monitor.py", label="系统监控", icon=":material/monitoring:")
        st.page_link("pages/28_options_pnl_ledger.py", label="期权盈亏", icon=":material/price_check:")

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
        frame = china_time_columns(frame).rename(columns={
            "symbol": "期权代码", "decision": "决策", "reason": "原因", "created_at": "时间（中国）",
        }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
        st.dataframe(translate_common_values(frame), hide_index=True, height=380)
    else:
        st.info("暂无执行记录。")

export_panel = st.expander(
    "导出当前复盘数据", expanded=False, on_change="rerun", key="overview_export",
)
with export_panel:
    if export_panel.open:
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

    else:
        st.caption("展开后才会生成 CSV 与 Markdown 复盘（不展开不计算）；避免每轮 rerun 都跑 iterrows。")
with st.container(horizontal=True):
    st.page_link("pages/10_volatility_options_strategy.py", label="进入高级配置与执行", icon=":material/tune:")
    st.page_link("pages/28_options_pnl_ledger.py", label="查看期权盈亏台账", icon=":material/price_check:")
    st.page_link("pages/26_trade_diagnostics_center.py", label="打开交易诊断中心", icon=":material/troubleshoot:")
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
