from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import (
    Settings, delete_campaign, load_gamma_campaigns, normalize_us_symbol,
    upsert_campaign,
    save_gamma_symbols, save_trading_preferences,
)
from futu_quant.earnings_gamma import screen_gamma_contracts
from futu_quant.earnings_optimizer import build_structure_candidates, earnings_profile, recommend_timing
from futu_quant.earnings_calendar import (
    discover_upcoming_earnings, filter_optionable_earnings, refresh_earnings_events,
)
from futu_quant.execution import ExecutionManager
from futu_quant.gamma_risk import contract_analysis, realized_volatility
from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues
from futu_quant.option_ranking import rank_long_option_candidates
from futu_quant.option_signal import automatic_option_direction
from futu_quant.campaign_timing import campaign_time_display
from futu_quant.store import TradingStore
from futu_quant.ui import (
    china_time_columns, chinese_columns, render_admin_login, render_sidebar,
    translate_common_values,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.run_gamma_strategy import run_once as run_gamma_debug_once  # noqa: E402


st.set_page_config(page_title="财报 Gamma 策略", page_icon="⚡", layout="wide")
render_sidebar(Settings.load())
st.title("财报 Gamma 事件策略")
st.warning("高风险模拟策略：只使用最大亏损可计算的结构；机会评分不合格时自动放弃交易。")

settings = Settings.load()
broker = FutuPaperBroker(settings)
executor = ExecutionManager(broker)
store = TradingStore()
admin = render_admin_login()


@st.cache_data(ttl=3600, show_spinner="正在载入并筛选未来 7 天财报标的……")
def load_upcoming_earnings() -> tuple[pd.DataFrame, pd.DataFrame]:
    events = discover_upcoming_earnings(days=7)
    accepted, rejected = filter_optionable_earnings(events, broker, max_expiry_days=7)
    return pd.DataFrame(accepted), pd.DataFrame(rejected)


def _decision_payload(value: object) -> dict:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ratio(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_date_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if not pd.isna(parsed):
        return parsed.strftime("%Y-%m-%d")
    return str(value).replace("T", " ")


def _format_china_time_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return str(value).replace("T", " ")
    return parsed.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")


def _polish_time_display(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    time_markers = ("时间", "创建", "更新", "抓取", "入场开始", "入场截止")
    date_markers = ("日期", "到期日", "财报日")
    for column in result.columns:
        label = str(column)
        if label in {"发布时间", "时间已确认"}:
            continue
        if any(marker in label for marker in time_markers):
            result[column] = result[column].map(_format_china_time_value)
        elif any(marker in label for marker in date_markers):
            result[column] = result[column].map(_format_date_value)
    return result


def _event_timestamp(campaign: dict) -> pd.Timestamp | None:
    try:
        event_day = pd.to_datetime(campaign.get("event_date"), errors="raise").date()
    except (TypeError, ValueError):
        return None
    hour = 8 if campaign.get("event_session") == "盘前" else 16
    minute = 0 if campaign.get("event_session") == "盘前" else 5
    return pd.Timestamp(
        datetime.combine(event_day, datetime.min.time()).replace(hour=hour, minute=minute),
        tz="America/New_York",
    ).tz_convert("Asia/Shanghai")


def _distance_text(target: pd.Timestamp | None) -> str:
    if target is None:
        return "时间无效"
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    hours = (target - now).total_seconds() / 3600
    if hours >= 24:
        return f"约 {hours / 24:.1f} 天后"
    if hours >= 0:
        return f"约 {hours:.1f} 小时后"
    if hours >= -24:
        return f"已过去 {abs(hours):.1f} 小时"
    return f"已过去 {abs(hours) / 24:.1f} 天"


def _gamma_decision_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    payloads = frame.get("payload_json", pd.Series(["{}"] * len(frame))).map(_decision_payload)
    details = pd.DataFrame([{
        "当前持仓": payload.get("position_quantity", payload.get("quantity", 0)),
        "计划下单": payload.get("planned_order_quantity", 0),
        "盈亏比例": _ratio(payload.get("pnl_ratio")),
        "峰值回撤": _ratio(payload.get("drawdown")),
        "触发模式": "波动行情" if payload.get("trigger_mode") == "VOLATILITY" else "财报事件",
        "自动方向依据": payload.get("direction_reason", ""),
        "机会评分": payload.get("opportunity_score"),
        "历史波动优势": payload.get("expected_move_edge"),
    } for payload in payloads])
    display = pd.concat([
        frame.drop(columns=["id", "strategy_id", "payload_json"], errors="ignore"),
        details,
    ], axis=1).rename(columns={
        "symbol": "期权代码", "decision": "决策", "reason": "原因",
        "created_at": "时间（中国）",
    })
    return _polish_time_display(translate_common_values(china_time_columns(display)))


def _shadow_position_frame() -> pd.DataFrame:
    rows = []
    for strategy_id, label in (
        ("earnings_gamma", "自动策略"),
        ("earnings_gamma_manual", "手动单"),
    ):
        for symbol, (quantity, cost) in store.shadow_positions_for_strategy(strategy_id).items():
            if abs(float(quantity)) <= 1e-9:
                continue
            rows.append({
                "来源": label, "期权代码": symbol, "当前持仓": float(quantity),
                "Shadow 平均成本": float(cost),
            })
    return pd.DataFrame(rows)


data_tab, config_tab = st.tabs(["常用数据", "参数配置"])

with data_tab:
    st.subheader("策略总览")
    st.caption("日常先看这里：运行状态、策略质量、最近决策、财报日历和 Shadow 持仓。低频参数放在第二个页签。")
    campaigns = load_gamma_campaigns()
    decisions = [
        row for row in store.decisions(limit=200)
        if row.get("strategy_id") in {"earnings_gamma", "earnings_gamma_manual"}
    ]
    intents = [
        row for row in store.intents(limit=1000)
        if row.get("strategy_id") in {"earnings_gamma", "earnings_gamma_manual"}
    ]
    events = pd.DataFrame(store.earnings_events(limit=1000))
    campaign_issues = {}
    campaign_rows = []
    campaign_event_times: list[tuple[pd.Timestamp, dict]] = []
    for campaign in campaigns:
        issues = diagnose_campaign(campaign, events.to_dict("records") if not events.empty else [])
        hard_issues = hard_campaign_issues(campaign, issues)
        campaign_issues[str(campaign.get("code", ""))] = issues
        time_info = campaign_time_display(campaign)
        event_at = _event_timestamp(campaign)
        if event_at is not None:
            campaign_event_times.append((event_at, campaign))
        campaign_rows.append({
            "期权代码": campaign.get("code", ""),
            "正股代码": campaign.get("underlying", ""),
            "财报日期": campaign.get("event_date", ""),
            "发布时间": campaign.get("event_session", ""),
            "期权结构": campaign.get("structure_label", campaign.get("structure", "LONG_OPTION")),
            "方向": campaign.get("option_type", ""),
            "机会评分": campaign.get("opportunity_score", 0),
            "最低评分": campaign.get("minimum_score", 0),
            "允许自动交易": "是" if campaign.get("trade_allowed", False) else "否",
            "事件已确认": "是" if campaign.get("event_confirmed", False) else "否",
            "执行状态": "阻断" if hard_issues else "提醒" if issues else "正常",
            "诊断": "；".join(issues) or "无",
            "启用": "是" if campaign.get("enabled", True) else "否",
            **time_info,
        })
    gamma_mode = store.get_setting("strategy_execution_mode:earnings_gamma", "") or "INHERIT"
    manual_mode = store.get_setting("strategy_execution_mode:earnings_gamma_manual", "") or "INHERIT"
    gamma_enabled = store.get_setting("strategy_enabled:Earnings Gamma", "false") == "true"
    pending_intents = [
        row for row in intents
        if str(row.get("status", "")).upper() in {"SUBMITTED", "WORKING", "SHADOW", "SHADOW_PART"}
    ]
    blocked_recent = [
        row for row in decisions
        if str(row.get("decision", "")).upper() in {"BLOCKED", "REJECTED"}
    ]
    shadow_positions = _shadow_position_frame()
    now_cn = pd.Timestamp.now(tz="Asia/Shanghai")
    future_events = sorted(
        (item for item in campaign_event_times if item[0] >= now_cn),
        key=lambda item: item[0],
    )
    next_event_time, next_event_campaign = future_events[0] if future_events else (None, None)

    with st.container(border=True):
        st.subheader("今天先看")
        with st.container(horizontal=True):
            st.metric("Gamma 标的", len(settings.gamma_symbols), border=True)
            st.metric("自动策略", len(campaigns), border=True)
            st.metric("可自动交易", sum(bool(item.get("trade_allowed", False)) for item in campaigns), border=True)
            st.metric("阻断/异常", len(blocked_recent), border=True)
            st.metric("Shadow 持仓", len(shadow_positions), border=True)
        with st.container(horizontal=True):
            st.metric("自动策略模式", gamma_mode, border=True)
            st.metric("手动单模式", manual_mode, border=True)
            st.metric("自动调度", "已启用" if gamma_enabled else "已关闭", border=True)
            st.metric("待处理订单", len(pending_intents), border=True)
        with st.container(horizontal=True):
            st.metric(
                "下一场财报",
                (
                    f"{next_event_campaign.get('underlying')} · {next_event_campaign.get('event_session')}"
                    if next_event_campaign else "暂无未来策略"
                ),
                border=True,
            )
            st.metric(
                "财报时间（中国）",
                next_event_time.strftime("%Y-%m-%d %H:%M") if next_event_time is not None else "—",
                border=True,
            )
            st.metric("距离财报", _distance_text(next_event_time), border=True)
            st.metric(
                "校验方式",
                (
                    "人工确认调试"
                    if next_event_campaign
                    and next_event_campaign.get("event_validation_policy") == "MANUAL_OVERRIDE"
                    else "双源强校验"
                    if next_event_campaign else "—"
                ),
                border=True,
            )
        with st.container(horizontal=True):
            st.page_link("pages/15_strategy_control_center.py", label="执行模式/资金配置", icon=":material/tune:")
            st.page_link("pages/26_trade_diagnostics_center.py", label="执行诊断", icon=":material/troubleshoot:")
            st.page_link("pages/28_options_pnl_ledger.py", label="期权盈亏台账", icon=":material/price_check:")
            st.page_link("pages/4_order_management.py", label="订单管理/撤单", icon=":material/receipt_long:")

    overview_tab, campaign_tab, decision_tab, calendar_tab, position_tab = st.tabs([
        "一屏概览", "已保存策略", "最近决策", "财报日历", "持仓与订单",
    ])
    with overview_tab:
        latest_decision = decisions[0] if decisions else None
        if latest_decision:
            st.info(
                f"最近决策：{latest_decision.get('symbol', '—')} · "
                f"{latest_decision.get('decision', '—')} · "
                f"{latest_decision.get('reason', '暂无原因')}"
            )
        else:
            st.info("暂无财报 Gamma 决策记录。")
        if campaign_rows:
            compact = _polish_time_display(pd.DataFrame(campaign_rows))[[
                "期权代码", "正股代码", "财报日期", "发布时间", "机会评分",
                "允许自动交易", "执行状态", "诊断",
            ]]
            st.dataframe(
                compact,
                hide_index=True,
                height=260,
                column_config={
                    "机会评分": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                },
            )
        else:
            st.info("暂无已保存自动策略；到“参数配置”页签生成候选并保存。")

    with campaign_tab:
        if campaign_rows:
            campaign_view = _polish_time_display(pd.DataFrame(campaign_rows))
            st.dataframe(
                campaign_view,
                hide_index=True,
                height=420,
                column_config={
                    "机会评分": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                    "最低评分": st.column_config.NumberColumn(format="%.1f"),
                },
            )
            st.caption("执行状态为“阻断”的策略不会有效自动开仓；展开诊断列可以看到原因。")
        else:
            st.info("暂无已保存的财报 Gamma 自动策略。")

    with decision_tab:
        decision_view = _gamma_decision_frame(decisions[:80])
        if not decision_view.empty:
            st.dataframe(
                decision_view,
                hide_index=True,
                height=460,
                column_config={
                    "盈亏比例": st.column_config.NumberColumn(format="percent"),
                    "峰值回撤": st.column_config.NumberColumn(format="percent"),
                    "机会评分": st.column_config.NumberColumn(format="%.1f"),
                    "历史波动优势": st.column_config.NumberColumn(format="percent"),
                },
            )
        else:
            st.info("暂无财报 Gamma 执行记录。")

    with calendar_tab:
        if events.empty:
            st.info("暂无财报日历缓存。可到“参数配置 / 自动财报日历”刷新。")
        else:
            event_display = events.rename(columns={
                "symbol": "正股代码", "event_date": "财报日期", "session": "发布时间",
                "source": "来源", "confirmed": "时间已确认", "fetched_at": "抓取时间（中国）",
            })
            st.dataframe(
                _polish_time_display(china_time_columns(event_display)).sort_values(
                    ["财报日期", "正股代码"], ascending=[True, True],
                ),
                hide_index=True,
                height=420,
            )

    with position_tab:
        if shadow_positions.empty:
            st.info("暂无财报 Gamma Shadow 持仓。")
        else:
            st.dataframe(
                shadow_positions,
                hide_index=True,
                column_config={
                    "Shadow 平均成本": st.column_config.NumberColumn(format="$%.2f"),
                    "当前持仓": st.column_config.NumberColumn(format="%.0f"),
                },
            )
        if intents:
            intent_view = pd.DataFrame(intents[:80]).rename(columns={
                "symbol": "期权代码", "side": "方向", "quantity": "数量",
                "limit_price": "限价", "status": "状态", "error": "错误",
                "created_at": "创建时间（中国）",
            }).drop(columns=["metadata_json", "updated_at"], errors="ignore")
            st.dataframe(
                _polish_time_display(translate_common_values(china_time_columns(intent_view))),
                hide_index=True,
                height=360,
            )
        else:
            st.info("暂无财报 Gamma 订单意图。")


with config_tab:
    st.subheader("参数配置")
    st.caption("这里放低频调整项：标的、财报日历、候选生成、策略保存、清理和隔离调试。")

    with st.expander("搜索并添加财报 Gamma 标的"):
        gamma_query = st.text_input("输入代码或名称", placeholder="NVDA、AAPL、苹果、Google")
        if gamma_query:
            try:
                catalog = broker.security_catalog()
                needle = gamma_query.strip().upper()
                exact_code = None
                try:
                    exact_code = normalize_us_symbol(gamma_query)
                except ValueError:
                    pass
                code_match = catalog["code"].str.upper().str.contains(needle, regex=False)
                name_match = catalog["name"].astype(str).str.upper().str.contains(needle, regex=False)
                exact_match = catalog["code"].eq(exact_code) if exact_code else False
                matches = catalog[code_match | name_match | exact_match].head(30)
                if matches.empty:
                    st.warning("没有找到匹配标的。可尝试输入完整代码，例如 US.AAPL。")
                else:
                    match_index = st.selectbox(
                        "搜索结果", matches.index.tolist(),
                        format_func=lambda index: f"{matches.loc[index, 'code']} · {matches.loc[index, 'name']}",
                    )
                    add_code = str(matches.loc[match_index, "code"])
                    if st.button(f"添加 {add_code} 到财报 Gamma", disabled=not admin):
                        updated = tuple(dict.fromkeys((*settings.gamma_symbols, add_code)))
                        save_gamma_symbols(updated)
                        st.cache_data.clear()
                        st.session_state["gamma_symbol_added"] = add_code
                        st.rerun()
            except BrokerError as exc:
                st.error(f"证券搜索失败：{exc}")

    added = st.session_state.pop("gamma_symbol_added", None)
    if added:
        st.success(f"已添加 {added}，下方正股列表已更新。")

    with st.expander("自动财报日历", expanded=False):
        st.write("加载未来 7 个日历日的 Nasdaq 财报，并在展示前检查富途期权链的可交易性。")
        st.caption("预筛选标准：财报后 7 天内有到期日，且至少一张 Call/Put 满足 Bid/Ask > 0、OI ≥ 100、价差 ≤ 20%、价内外距离 ≤ 10%。")
        if st.button("加载并筛选未来 7 天财报", icon=":material/calendar_month:"):
            try:
                accepted, rejected = load_upcoming_earnings()
                st.session_state.upcoming_earnings = accepted
                st.session_state.filtered_earnings = rejected
            except Exception as exc:
                st.error(f"财报候选加载失败：{exc}")
        upcoming = st.session_state.get("upcoming_earnings", pd.DataFrame())
        if not upcoming.empty:
            display = upcoming.rename(columns={
                "symbol": "正股代码", "name": "公司名称", "event_date": "财报日期",
                "session": "发布时间", "market_cap": "市值", "fiscal_quarter": "财务季度",
                "eps_forecast": "预期 EPS", "analyst_count": "分析师数量", "source": "来源",
                "qualified_contracts": "合格合约数", "nearest_qualified_expiration": "最近合格到期日",
            })
            display["已在标的列表"] = display["正股代码"].isin(settings.gamma_symbols)
            st.dataframe(_polish_time_display(display), hide_index=True)
            records = upcoming.to_dict("records")
            by_symbol = {str(item["symbol"]): item for item in records}
            selected_symbols = st.multiselect(
                "选择要加入的标的",
                [symbol for symbol in by_symbol if symbol not in settings.gamma_symbols],
                format_func=lambda symbol: (
                    f"{symbol} · {by_symbol[symbol].get('name', '')} · "
                    f"{by_symbol[symbol].get('event_date')} {by_symbol[symbol].get('session')}"
                ),
                placeholder="可输入代码或公司名称筛选",
            )
            with st.container(horizontal=True):
                if st.button(
                    f"加入财报 Gamma（{len(selected_symbols)}）",
                    type="primary", disabled=not admin or not selected_symbols,
                ):
                    save_gamma_symbols(tuple(dict.fromkeys((*settings.gamma_symbols, *selected_symbols))))
                    st.cache_data.clear()
                    st.session_state["calendar_add_result"] = (
                        f"已加入 {len(selected_symbols)} 个财报 Gamma 标的。"
                    )
                    st.rerun()
                if st.button(
                    f"加入系统自选（{len(selected_symbols)}）",
                    disabled=not admin or not selected_symbols,
                ):
                    updated_watchlist = tuple(dict.fromkeys((*settings.watchlist, *selected_symbols)))
                    quantities = dict(settings.order_quantities)
                    for symbol in selected_symbols:
                        quantities.setdefault(symbol, settings.order_quantity)
                    save_trading_preferences(updated_watchlist, quantities)
                    st.cache_data.clear()
                    st.session_state["calendar_add_result"] = (
                        f"已加入 {len(selected_symbols)} 个系统自选标的，"
                        f"默认下单数量为 {settings.order_quantity}。"
                    )
                    st.rerun()
        elif "upcoming_earnings" in st.session_state:
            st.info("未来 7 天没有查到满足期权可交易性条件的财报标的。")

        filtered = st.session_state.get("filtered_earnings", pd.DataFrame())
        if not filtered.empty:
            with st.expander(f"已过滤 {len(filtered)} 个不满足条件的标的"):
                filtered_display = filtered.rename(columns={
                    "symbol": "正股代码", "name": "公司名称", "event_date": "财报日期",
                    "session": "发布时间", "filter_reason": "过滤原因",
                })
                filtered_columns = [
                    column for column in ("正股代码", "公司名称", "财报日期", "发布时间", "过滤原因")
                    if column in filtered_display.columns
                ]
                st.dataframe(_polish_time_display(filtered_display[filtered_columns]), hide_index=True)

        calendar_add_result = st.session_state.pop("calendar_add_result", None)
        if calendar_add_result:
            st.success(calendar_add_result)

        st.divider()
        st.caption("以下是已配置标的的双源校验缓存。")
        if st.button("立即刷新财报日历", disabled=not admin):
            try:
                count = refresh_earnings_events(store, settings.gamma_symbols, broker=broker)
                st.success(f"已刷新，找到 {count} 条已配置标的事件。")
            except Exception as exc:
                st.error(f"财报日历刷新失败：{exc}")
        events = pd.DataFrame(store.earnings_events())
        if events.empty:
            st.info("暂无缓存事件。启动统一服务或点击刷新后显示。")
        else:
            st.dataframe(_polish_time_display(china_time_columns(events).rename(columns={
                "symbol": "正股代码", "event_date": "财报日期", "session": "发布时间",
                "source": "来源", "confirmed": "时间已确认", "fetched_at": "抓取时间（中国）",
            })), hide_index=True)

    with st.expander("策略规则", expanded=True):
        st.markdown(
            "1. **财报事件**：必须通过富途与 Nasdaq 双源日期/时段校验，合约必须覆盖财报日。\n"
            "2. **人工确认调试**：当双源日历缺失或异常时，可由人工确认财报时间后保存调试；系统会保留风险提醒。\n"
            "3. 两种模式都过滤低未平仓量和过宽价差，并执行权利金、IV、Greeks 与组合风控。\n"
            "4. 分批入场，只使用限价单；按止盈、硬止损、峰值回撤或最长24小时退出。\n"
            "5. 自动比较历史跳空与期权隐含波动，低于60分或波动优势为负时放弃交易。\n"
            "6. 支持买入认购/认沽、跨式、宽跨式与有限风险借方价差；多腿仅在 Shadow 验证，避免拆腿风险。"
        )

    left, middle, right = st.columns(3)
    trigger_mode = "EARNINGS"
    st.info("本页只配置财报事件策略；波动行情已拆分到“波动率期权策略”。")
    underlying = left.selectbox("正股", settings.gamma_symbols)
    event_date = middle.date_input("财报日期", date.today())
    event_session = right.radio("发布时间", ["盘前", "盘后"], horizontal=True)
    validation_choice = st.segmented_control(
        "财报时间校验方式",
        ["双源强校验", "人工确认调试"],
        default="双源强校验",
        help=(
            "双源强校验：要求富途与 Nasdaq 财报时间一致，适合正式自动财报事件。"
            "人工确认调试：允许双源缺失/不一致时保存，但会保留风险提醒。"
        ),
    )
    event_source = st.text_input("财报时间来源", placeholder="例如：公司投资者关系页面 URL 或已核对的来源")
    event_confirmed = st.checkbox("我已核对财报日期与盘前/盘后时间")
    direction_choice = left.segmented_control("方向", ["自动判断", "CALL", "PUT"], default="自动判断")
    max_days = middle.number_input("财报后最远到期天数", 0, 30, 7)
    min_oi = right.number_input("最低未平仓量", 0, 1_000_000, 100, step=50)
    max_spread = left.number_input("最大买卖价差（%）", 1.0, 100.0, 20.0)
    max_moneyness = middle.number_input("最大价外/价内距离（%）", 0.0, 50.0, 10.0)
    contracts_per_stage = right.number_input("每批合约数", 1, 100, 1)
    stages = left.number_input("计划分批数", 1, 5, 3)
    max_premium = middle.number_input("每批最大权利金（美元）", 1.0, 1_000_000.0, 1000.0)
    entry_hours = right.number_input("财报前入场窗口（小时）", 1, 48, 6)
    profit_target = left.number_input("止盈比例", 0.05, 10.0, 0.50, step=0.05)
    stop_loss = middle.number_input("硬止损比例", 0.05, 1.0, 0.40, step=0.05)
    trailing_stop = right.number_input("峰值回撤退出比例", 0.05, 1.0, 0.25, step=0.05)
    with st.expander("单笔止损限额（策略保存后强制执行）", expanded=True):
        limit_left, limit_right = st.columns(2)
        max_position_loss_ratio = limit_left.number_input(
            "单笔相对止损（权利金亏损比例）", 0.01, 1.0, 0.25, step=0.05,
            help="单笔权利金相对亏损达到该比例时退出；随持仓规模自动缩放。",
        )
        max_position_loss_usd = limit_right.number_input(
            "单笔绝对止损（美元）", 1.0, 100_000.0, 300.0, step=10.0,
            help="单笔允许的最大美元亏损，防止极端跳空下仅靠比例止损失守。",
        )
        st.caption(
            "两层止损取较紧者先生效（OR）：任一阈值触发即退出；"
            "保存时会显式写入策略，避免配置缺失被静默兜底。"
        )
    max_hold_hours = left.number_input("财报后最长持有小时", 1, 48, 8)
    automatic_timing = st.checkbox("自动优化入场与退出时间", value=True)
    minimum_move = st.number_input("自动方向最低当日涨跌幅", 0.0, 0.20, 0.003, step=0.001, format="%.3f")

    if st.button("生成期权候选", type="primary"):
        try:
            expirations = broker.option_expirations(underlying).copy()
            anchor_date = event_date
            earliest = anchor_date + timedelta(days=1) if (
                event_session == "盘后"
            ) else anchor_date
            latest = anchor_date + timedelta(days=int(max_days))
            valid = expirations[
                (pd.to_datetime(expirations["strike_time"]).dt.date >= earliest)
                & (pd.to_datetime(expirations["strike_time"]).dt.date <= latest)
            ]
            if valid.empty:
                raise ValueError("指定窗口内没有有效到期日；盘后财报不会选择当日已到期合约。")
            expiration = str(valid.sort_values("strike_time").iloc[0]["strike_time"])
            chain = broker.option_chain(underlying, expiration)
            underlying_quote = broker.snapshots([underlying]).iloc[0]
            spot = float(underlying_quote["last_price"])
            option_type = direction_choice
            direction_reason = "使用手动选择的方向"
            if direction_choice == "自动判断":
                option_type, direction_reason, _ = automatic_option_direction(
                    underlying_quote, broker.candles(underlying, count=90), trigger_mode,
                    float(minimum_move),
                )
            option_quotes = broker.snapshots(chain["code"].tolist())
            directions = [option_type] if option_type in {"CALL", "PUT"} else ["CALL", "PUT"]
            candidates = pd.concat([
                screen_gamma_contracts(
                    chain, option_quotes, spot, kind, int(min_oi),
                    float(max_spread), float(max_moneyness),
                ) for kind in directions
            ], ignore_index=True)
            candidates = rank_long_option_candidates(
                candidates, spot, profit_target=float(profit_target), stop_loss=float(stop_loss),
            )
            profile = earnings_profile(broker.candles(underlying, count=300))
            structures = build_structure_candidates(
                candidates, spot, profile, option_type, bool(event_confirmed),
            )
            st.session_state.gamma_candidates = candidates
            st.session_state.gamma_structures = structures
            st.session_state.gamma_profile = profile
            st.session_state.gamma_spot = spot
            st.session_state.gamma_expiration = expiration
            st.session_state.gamma_direction_reason = direction_reason
        except (BrokerError, ValueError) as exc:
            st.error(str(exc))

    candidates = st.session_state.get("gamma_candidates", pd.DataFrame())
    if not candidates.empty:
        st.metric("正股最新价", f"${st.session_state.gamma_spot:.2f}")
        st.info(st.session_state.get("gamma_direction_reason", ""))
        structures = st.session_state.get("gamma_structures", pd.DataFrame())
        if not structures.empty:
            st.subheader("财报机会评分与结构建议")
            st.caption(
                "单腿买方可按策略执行模式运行；跨式、宽跨式、借方价差等多腿结构目前只允许 Shadow 自动验证，"
                "不会提交富途模拟盘。"
            )
            profile = st.session_state.get("gamma_profile", {})
            with st.container(horizontal=True):
                st.metric("历史跳空中位数", f"{profile.get('median_gap', 0):.2%}", border=True)
                st.metric("历史跳空75分位", f"{profile.get('p75_gap', 0):.2%}", border=True)
                st.metric("历史跳空90分位", f"{profile.get('p90_gap', 0):.2%}", border=True)
                st.metric("近5日动量", f"{profile.get('momentum_5d', 0):+.2%}", border=True)
            structure_view = structures.drop(columns=["legs", "score_reason"], errors="ignore").rename(columns={
                "structure_label": "结构", "opportunity_score": "机会评分", "minimum_score": "最低门槛",
                "expected_move_edge": "历史波动优势", "trade_allowed": "允许交易",
                "net_debit": "每股净权利金", "implied_move": "期权隐含波动",
                "max_loss": "每组最大亏损", "spread_pct": "最宽价差(%)",
                "open_interest": "最低未平仓量",
            })
            st.dataframe(_polish_time_display(structure_view), hide_index=True, column_config={
                "机会评分": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                "历史波动优势": st.column_config.NumberColumn(format="percent"),
                "期权隐含波动": st.column_config.NumberColumn(format="percent"),
                "每组最大亏损": st.column_config.NumberColumn(format="$%.2f"),
            })
            selected_structure_index = st.selectbox(
                "选择自动策略结构", structures.index.tolist(),
                format_func=lambda index: (
                    f"{structures.loc[index, 'structure_label']} · "
                    f"评分 {structures.loc[index, 'opportunity_score']:.1f} · "
                    f"{'允许交易' if structures.loc[index, 'trade_allowed'] else '自动放弃'}"
                ),
            )
            selected_structure = structures.loc[selected_structure_index]
            st.caption(selected_structure["score_reason"])
            timing = recommend_timing(
                selected_structure["opportunity_score"], selected_structure["expected_move_edge"], event_session,
            )
            st.info(
                f"自动时间方案：财报前 {timing['entry_hours']} 小时开始，财报后最长持有 "
                f"{timing['max_hold_hours']} 小时。{timing['timing_reason']}"
            )
        else:
            selected_structure = None
        columns = [
            "code", "model_expected_return", "estimated_profit_probability",
            "probability_confidence", "breakeven_price", "gamma_theta_efficiency",
            "option_type", "strike_price", "strike_time", "bid_price", "ask_price",
            "mid_price", "spread_pct", "moneyness_pct", "option_open_interest", "volume",
            "option_implied_volatility", "option_delta", "option_gamma", "option_theta",
            "option_contract_multiplier",
        ]
        visible = [column for column in columns if column in candidates]
        st.caption("已按模型期望收益从高到低排序：结合盈利概率、止盈/止损和半个买卖价差成本。")
        candidate_view = _polish_time_display(chinese_columns(candidates[visible]))
        st.dataframe(candidate_view, hide_index=True, column_config={
            "模型盈利概率": st.column_config.ProgressColumn(
                "模型盈利概率", min_value=0.0, max_value=1.0, format="percent"
            ),
            "模型期望收益": st.column_config.NumberColumn("模型期望收益", format="percent"),
            "盈亏平衡价": st.column_config.NumberColumn("盈亏平衡价", format="$%.2f"),
        })
        selected_code = st.selectbox("选择合约", candidates["code"].tolist())
        selected = candidates[candidates["code"] == selected_code].iloc[0]
        selected_option_type = str(selected["option_type"])
        @st.cache_data(ttl=300, max_entries=20, show_spinner=False)
        def load_rv(code: str) -> float:
            return realized_volatility(broker.candles(code, count=90))

        risk_analysis = contract_analysis(
            selected, float(st.session_state.gamma_spot),
            load_rv(underlying), store,
            peer_contracts=candidates,
        )
        with st.container(horizontal=True):
            st.metric(
                "IV 历史百分位",
                f"{risk_analysis['IV历史百分位']:.1%}" if risk_analysis["IV百分位可用"] else "样本不足",
                help=(
                    f"样本来源：{risk_analysis['IV样本来源']}；"
                    f"样本数：{risk_analysis['IV历史样本数']}；"
                    f"置信度：{risk_analysis['IV样本置信度']}。"
                ),
                border=True,
            )
            st.metric("IV / 实现波动", f"{risk_analysis['IV/RV']:.2f}", border=True)
            st.metric("到期预期波动", f"${risk_analysis['预期波动金额']:.2f}", border=True)
            st.metric("IV 变化", f"{risk_analysis['IV变化']:+.1%}", border=True)
        estimated = (
            float(selected["ask_price"]) * float(selected["option_contract_multiplier"])
            * int(contracts_per_stage)
        )
        st.metric("本批按 Ask 估算权利金", f"${estimated:,.2f}")
        st.caption(f"计划共 {stages} 批；必须逐批确认，系统不会一次性追价。")
        if estimated > max_premium:
            st.error("本批超过最大权利金，禁止提交。")
        elif not settings.enable_paper_orders:
            st.info("ENABLE_PAPER_ORDERS=false；当前只生成计划。")
        else:
            st.caption(
                "期权手工交易（兼容 ID：earnings_gamma_manual）：只提交本批订单，不负责后续自动加仓或退出。"
            )
        if estimated <= max_premium and st.button(
            "提交本批模拟限价买单", type="primary",
            disabled=not admin or not settings.enable_paper_orders
        ):
            counter_key = f"gamma_manual_stage:{event_date}:{selected_code}"
            stage_number = int(st.session_state.get(counter_key, 1))
            result = executor.submit_limit(
                "earnings_gamma_manual", f"{event_date}:{selected_code}:BUY:stage{stage_number}",
                selected_code, "BUY", int(contracts_per_stage), float(selected["ask_price"]),
                {"multiplier": float(selected["option_contract_multiplier"]), "underlying": underlying},
            )
            if result.status == "SUBMITTED":
                st.session_state[counter_key] = stage_number + 1
                st.success("模拟限价单已提交；后续可在订单管理页单笔或一键撤单。")
                if result.broker_response is not None:
                    st.dataframe(chinese_columns(result.broker_response), hide_index=True)
            else:
                st.error(result.message)

        if st.button("保存为自动 Gamma 策略", disabled=not admin):
            validation_policy = (
                "MANUAL_OVERRIDE"
                if validation_choice == "人工确认调试"
                else "STRICT_DOUBLE_SOURCE"
            )
            if not event_confirmed:
                st.error("保存自动策略前必须人工确认财报发布时间。")
                st.stop()
            if validation_policy == "STRICT_DOUBLE_SOURCE" and not event_source.strip():
                st.error("双源强校验模式下，保存前必须填写财报时间来源。")
                st.stop()
            if selected_structure is None:
                st.error("没有可保存的有限风险结构。")
                st.stop()
            structure_legs = selected_structure["legs"]
            primary_code = next((leg["code"] for leg in structure_legs if leg["side"] == "BUY"), selected_code)
            primary_contract = candidates[candidates["code"] == primary_code].iloc[0]
            execution_mode_override = None
            if len(structure_legs) > 1:
                execution_mode_override = "SHADOW"
                st.warning(
                    "已保存为多腿 Shadow 验证策略：系统会逐腿虚拟撮合，不会提交富途模拟盘。"
                    "等期权组合原子订单接入后，才允许多腿进入 PAPER。"
                )
            if validation_policy == "MANUAL_OVERRIDE":
                st.warning(
                    "已按人工确认调试模式保存：双源日历缺失或不一致不会阻断运行，"
                    "但策略诊断会持续显示校验提醒。"
                )
            saved_timing = recommend_timing(
                selected_structure["opportunity_score"], selected_structure["expected_move_edge"], event_session,
            ) if automatic_timing else {
                "entry_hours": int(entry_hours), "max_hold_hours": float(max_hold_hours),
                "timing_reason": "使用页面手动时间设置",
            }
            campaigns = [item for item in load_gamma_campaigns() if item.get("campaign_id", item.get("code")) != f"{underlying}:{event_date}"]
            campaigns.append(
                {
                    "campaign_id": f"{underlying}:{event_date}", "code": primary_code,
                    "optimizer_version": 1,
                    "symbol_profile": st.session_state.get("gamma_profile", {}),
                    "underlying": underlying, "event_date": event_date.isoformat(),
                    "event_session": event_session, "option_type": str(primary_contract["option_type"]),
                    "structure": selected_structure["structure"], "structure_label": selected_structure["structure_label"],
                    "legs": structure_legs, "opportunity_score": float(selected_structure["opportunity_score"]),
                    "minimum_score": float(selected_structure["minimum_score"]),
                    "expected_move_edge": float(selected_structure["expected_move_edge"]),
                    "trade_allowed": bool(selected_structure["trade_allowed"]),
                    "score_reason": str(selected_structure["score_reason"]),
                    "structure_net_debit": float(selected_structure["net_debit"]),
                    "contracts_per_stage": int(contracts_per_stage), "max_stages": int(stages),
                    "max_premium_per_stage": float(max_premium), "entry_hours": int(saved_timing["entry_hours"]),
                    "profit_target": float(profit_target), "stop_loss": float(stop_loss),
                    "trailing_stop": float(trailing_stop), "enabled": True,
                    # 单笔止损限额必须显式入库：执行侧「缺省即拒绝开仓」（VOLATILITY_STOP_LIMIT_MISSING），
                    # 页面不写就等于保存了一条永远开不了仓的策略。
                    "max_position_loss_ratio": float(max_position_loss_ratio),
                    "max_position_loss_usd": float(max_position_loss_usd),
                    "max_hold_hours": float(saved_timing["max_hold_hours"]),
                    "timing_reason": str(saved_timing["timing_reason"]),
                    "minimum_underlying_move": float(minimum_move),
                    "event_source": event_source.strip(), "event_confirmed": bool(event_confirmed),
                    "trigger_mode": trigger_mode,
                    "event_validation_policy": validation_policy,
                    "execution_mode_override": execution_mode_override,
                    "multi_leg_shadow_only": bool(len(structure_legs) > 1),
                }
            )
            upsert_campaign("GAMMA_CAMPAIGNS", campaigns[-1])
            if len(structure_legs) > 1:
                store.audit(
                    "GAMMA_MULTI_LEG_SHADOW_CAMPAIGN_SAVED",
                    {
                        "campaign_id": campaigns[-1]["campaign_id"],
                        "code": primary_code,
                        "structure": selected_structure["structure"],
                        "legs": structure_legs,
                        "execution_mode": "SHADOW",
                    },
                    "earnings_gamma",
                 actor="streamlit_admin:panel")
            st.success("自动策略已保存。启动常驻运行器后才会开始监控和模拟交易。")

    st.subheader("已保存的自动 Gamma 策略")
    campaigns = load_gamma_campaigns()
    if campaigns:
        event_rows = store.earnings_events()
        campaign_rows = []
        all_issues = {}
        for campaign in campaigns:
            issues = diagnose_campaign(campaign, event_rows)
            hard_issues = hard_campaign_issues(campaign, issues)
            all_issues[campaign["code"]] = issues
            campaign_rows.append({
                **campaign,
                **campaign_time_display(campaign),
                "调试状态": "阻断" if hard_issues else "警告" if issues else "正常",
                "诊断": "；".join(issues),
            })
        st.dataframe(_polish_time_display(chinese_columns(pd.DataFrame(campaign_rows))), hide_index=True)
        remove_campaign = st.selectbox("删除自动策略", [item["code"] for item in campaigns])
        if st.button("删除选中的自动策略", disabled=not admin):
            shadow_quantity, _ = store.shadow_position("earnings_gamma", remove_campaign)
            try:
                positions = broker.positions()
                orders = broker.orders()
                paper_rows = positions[positions["code"].astype(str) == remove_campaign] if not positions.empty else positions
                paper_quantity = float(paper_rows["qty"].sum()) if not paper_rows.empty else 0
                pending = orders[(orders["code"].astype(str) == remove_campaign)
                    & orders["order_status"].map(broker.is_cancellable_status)] if not orders.empty else orders
            except BrokerError as exc:
                st.error(f"无法确认持仓与待成交订单，暂不允许删除：{exc}")
            else:
                if shadow_quantity > 0 or paper_quantity > 0:
                    st.error(
                        f"仍有持仓（Shadow {shadow_quantity:g} 张，模拟盘 {paper_quantity:g} 张），"
                        "请先平仓后再删除。"
                    )
                elif not pending.empty:
                    st.error(f"仍有 {len(pending)} 笔待成交订单，请先撤单。")
                else:
                    identity = next(
                        item.get("campaign_id", item.get("code"))
                        for item in campaigns if item["code"] == remove_campaign
                    )
                    delete_campaign("GAMMA_CAMPAIGNS", identity)
                    st.rerun()
    else:
        st.info("尚未保存自动 Gamma 策略。")

    st.subheader("Gamma 隔离调试")
    st.caption("Gamma 的启用开关、SHADOW/PAPER 模式和资金配额统一在策略与资金控制管理。")
    gamma_mode = store.get_setting("strategy_execution_mode:earnings_gamma", "") or "INHERIT"
    gamma_enabled = store.get_setting("strategy_enabled:Earnings Gamma", "false") == "true"
    with st.container(horizontal=True):
        st.metric("Gamma 执行模式", gamma_mode, border=True)
        st.metric("自动调度", "已启用" if gamma_enabled else "已关闭", border=True)
    st.page_link(
        "pages/15_strategy_control_center.py", label="前往策略与资金控制设置 Gamma",
        icon=":material/tune:",
    )
    blocking_issues = [
        issue for campaign in campaigns for issue in hard_campaign_issues(
            campaign, all_issues.get(campaign["code"], [])
        )
    ] if campaigns else []
    warning_issues = [
        issue for issues in (all_issues.values() if campaigns else []) for issue in issues
        if issue not in blocking_issues
    ]
    if blocking_issues:
        st.error("当前配置不能运行有效调试：\n\n- " + "\n- ".join(blocking_issues))
        st.info("请按双源日历重新选择财报日期，并生成到期日晚于财报日的新期权合约。")
    elif warning_issues:
        st.warning("以下校验仅作提醒，不阻止运行：\n\n- " + "\n- ".join(warning_issues))
    if st.button(
        "立即运行一次 Gamma 调试", disabled=not admin or not gamma_enabled or bool(blocking_issues)
    ):
        try:
            run_gamma_debug_once()
            st.success("单次调试已完成，请在下方决策记录或“运行日志”查看结果。")
        except Exception as exc:
            st.error(f"Gamma 单次调试失败：{exc}")
    gamma_decisions = pd.DataFrame([
        row for row in store.decisions(limit=200) if row["strategy_id"] == "earnings_gamma"
    ])
    if not gamma_decisions.empty:
        def decision_detail(value: str) -> pd.Series:
            payload = json.loads(value or "{}")
            return pd.Series({
                "当前持仓": payload.get("position_quantity", payload.get("quantity", 0)),
                "计划下单": payload.get("planned_order_quantity", 0),
                "持仓来源": {
                    "SHADOW_LEDGER": "Shadow 虚拟成交账本",
                    "FUTU_ACCOUNT": "富途模拟账户",
                }.get(payload.get("position_source"), "历史记录"),
                "盈亏比例": payload.get("pnl_ratio", 0),
                "峰值回撤": payload.get("drawdown", 0),
                "触发模式": "波动行情" if payload.get("trigger_mode") == "VOLATILITY" else "财报事件",
                "自动方向依据": payload.get("direction_reason", "历史记录未保存"),
                "入场开始（中国）": pd.to_datetime(
                    payload.get("entry_start"), errors="coerce", utc=True
                ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("entry_start") else "",
                "入场截止（中国）": pd.to_datetime(
                    payload.get("event_time"), errors="coerce", utc=True
                ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("event_time") else "",
            })

        details = gamma_decisions["payload_json"].apply(decision_detail)
        gamma_display = pd.concat([gamma_decisions.drop(columns=["payload_json"]), details], axis=1)
        localized_gamma_display = _polish_time_display(translate_common_values(china_time_columns(gamma_display).rename(columns={
            "symbol": "期权代码", "decision": "决策", "reason": "原因",
            "created_at": "时间（中国）",
        }).drop(columns=["id", "strategy_id"], errors="ignore")))
        st.dataframe(localized_gamma_display, hide_index=True,
            column_config={
                "盈亏比例": st.column_config.NumberColumn(format="percent"),
                "峰值回撤": st.column_config.NumberColumn(format="percent"),
            })
