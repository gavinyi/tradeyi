from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import (
    Settings, load_volatility_campaigns, load_volatility_radar_budgets,
    load_volatility_symbols, delete_campaign, save_volatility_radar_budgets,
    save_volatility_campaigns, save_volatility_symbols,
    update_volatility_symbol, upsert_campaign,
    # 身份（`{tenant}|{code}`）的唯一实现就在 config：这里必须复用而不是另拼一份，
    # 否则页面删的身份口径与存储的去重口径一旦漂移，就会"删不掉"或"删错租户"。
    _campaign_identity,
)
from futu_quant.earnings_gamma import screen_gamma_contracts
from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues
from futu_quant.gamma_risk import contract_analysis, realized_volatility
from futu_quant.option_ranking import rank_long_option_candidates
from futu_quant.option_signal import automatic_option_direction
from futu_quant.radar_control import status as radar_status
from futu_quant.radar_schedule import (
    adaptive_interval_seconds,
    load_radar_schedule_state,
)
from futu_quant.volatility_scanner import scan_volatility_watchlist
from futu_quant.volatility_optimizer import rank_volatility_contracts, volatility_regime
from futu_quant.option_expectancy import add_iv_history_percentiles
from futu_quant.volatility_structures import build_volatility_structure_candidates
from futu_quant.volatility_orphans import volatility_state_keys
from futu_quant.campaign_timing import campaign_time_display
from futu_quant.store import TradingStore
from futu_quant.execution_mode import resolve_configured_execution_mode
from futu_quant.volatility_pause import set_volatility_new_entries_paused
from futu_quant.tenant import (
    campaign_belongs_to_tenant, campaigns_config_key_lenient, tenant_id_of_broker,
)
from futu_quant.security_search import search_securities, security_label
from futu_quant.ui import (
    china_time_columns, chinese_columns, render_admin_login, render_sidebar,
    translate_common_values,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.run_volatility_strategy import run_once as run_volatility_once  # noqa: E402
from scripts.run_gamma_strategy import load_state  # noqa: E402


st.set_page_config(page_title="波动率期权策略", page_icon="🌊", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
# #1 键级分区：本页所有 campaign 读写都按**当前页租户**（broker 所属租户）走租户键。
# 在页面顶部算一次，两个页签共用 —— 避免"读租户键、写全局键"的半状态。
_page_tenant_id = tenant_id_of_broker(broker) or ""
_page_campaigns_key, _page_key_fell_back = campaigns_config_key_lenient(_page_tenant_id)
if _page_key_fell_back:
    store.operational_event(
        "volatility_long_gamma", "CAMPAIGNS_KEY_TENANT_UNRESOLVED", "ERROR",
        {"tenant_id": _page_tenant_id or None,
         "message": "无法把 campaign 清单解析到租户键 —— 回落无前缀旧键（未隔离，已留痕）"},
    )
st.title("波动率期权策略")
st.caption("独立策略 ID：volatility_long_gamma · 不依赖财报日历 · 仅限美股模拟盘")
st.warning("高风险期权买方策略：只在异常波动已确认且 IV 成本合理时入场，最大损失仍可达全部权利金。")

with st.container(border=True):
    st.subheader("当前工作位置")
    st.caption("这里用于修改策略和执行操作；只查看运行状态请返回总览。")
    with st.container(horizontal=True):
        st.page_link("pages/30_volatility_strategy_overview.py", label="返回波动率策略总览", icon=":material/dashboard:")
        st.page_link("pages/26_trade_diagnostics_center.py", label="查看执行诊断", icon=":material/troubleshoot:")
        st.page_link("pages/28_options_pnl_ledger.py", label="查看期权盈亏", icon=":material/price_check:")
        st.page_link("pages/15_strategy_control_center.py", label="设置执行模式", icon=":material/tune:")
    with st.container(horizontal=True):
        # P1-6：读侧走单源（租户键 > 旧键 > 全局 > env），页面不再自己拼键。
        _current_mode = resolve_configured_execution_mode(
            store, _page_tenant_id, "volatility_long_gamma",
        ) or "INHERIT"
        _current_enabled = store.get_setting("strategy_enabled:Volatility Long Gamma", "false") == "true"
        st.metric("执行模式", _current_mode, border=True)
        st.metric("自动调度", "已启用" if _current_enabled else "已关闭", border=True)
        st.metric("交易范围", "仅模拟盘", border=True)

admin = render_admin_login()

# P1-9「暂停新开仓」：生效值走单源；写只写当前租户键（拿不到租户就拒绝写）。
with st.container(border=True):
    st.subheader("暂停新开仓")
    st.caption(
        "只停**新开仓**，已有持仓的止盈/止损/到期退出管理照常运行；"
        f"只作用于当前租户（`{_page_tenant_id or '未识别'}`）。"
    )
    from futu_quant.strategy_pause import is_strategy_entries_paused
    _paused_now = is_strategy_entries_paused(store, "volatility_long_gamma", _page_tenant_id)
    st.metric("当前状态", "已暂停新开仓" if _paused_now else "允许新开仓", border=True)
    _pause_toggle = st.toggle(
        "暂停波动率新开仓", value=_paused_now, disabled=not admin,
        key="volatility_pause_new_entries",
    )
    if _pause_toggle != _paused_now:
        try:
            _written_key = set_volatility_new_entries_paused(
                store, _page_tenant_id, _pause_toggle,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            store.audit(
                "VOLATILITY_NEW_ENTRIES_PAUSE_CHANGED",
                {"tenant_id": _page_tenant_id, "paused": _pause_toggle, "key": _written_key},
                actor="streamlit_admin:panel",
            )
            st.rerun()


# 工作台：把“今天要看什么”放在页面最前面，详细参数和明细表下沉到后面。
# 这些读取只使用本地策略账本，不触发富途行情请求，避免打开页面就产生额外调用。
_workbench_campaigns = [
    item for item in load_volatility_campaigns(_page_tenant_id)
    if campaign_belongs_to_tenant(item, _page_tenant_id)
]
_workbench_decisions = [
    row for row in store.decisions(limit=500)
    if row.get("strategy_id") == "volatility_long_gamma"
]
_workbench_blocked = sum(
    str(row.get("decision", "")).upper() in {"BLOCKED", "REJECTED", "ERROR"}
    for row in _workbench_decisions
)
_workbench_symbols = load_volatility_symbols()

with st.container(border=True):
    st.subheader("策略工作台")
    st.caption("先看状态和异常，再进入雷达、自动策略或参数配置；详细诊断始终保留在页面底部。")
    with st.container(horizontal=True):
        st.metric("监控标的", len(_workbench_symbols), border=True)
        st.metric("自动策略", len(_workbench_campaigns), border=True)
        st.metric(
            "最近持仓策略",
            sum(float(item.get("当前持仓数", 0) or 0) > 0 for item in _workbench_campaigns),
            border=True,
        )
        st.metric("最近阻断/异常", _workbench_blocked, border=True)
    overview_tab, operation_tab, troubleshooting_tab = st.tabs(
        ["今天先看", "操作路径", "问题排查"]
    )
    with overview_tab:
        if _workbench_decisions:
            latest = _workbench_decisions[0]
            st.info(
                f"最近记录：{latest.get('symbol', '—')} · "
                f"{latest.get('decision', '—')} · {latest.get('reason', '暂无原因')}"
            )
        else:
            st.info("暂无波动率策略执行记录；可先在“机会雷达”中扫描已选标的。")
    with operation_tab:
        st.markdown(
            "**推荐顺序**：1. 机会雷达确认信号 → 2. 自动策略查看持仓与待执行 → "
            "3. 参数配置保存规则 → 4. 执行诊断查看阻断原因。"
        )
    with troubleshooting_tab:
        st.markdown(
            "**没有下单时**先看：行情新鲜度/盘口 → 合约候选原因 → 账户风控 → "
            "Shadow 撮合条件。每条记录的中文原因和目标值在“执行诊断”区域。"
        )


def setting_float(key: str, default: float) -> float:
    try:
        return float(store.get_setting(key, str(default)) or default)
    except ValueError:
        return default


def setting_int(key: str, default: int) -> int:
    try:
        return int(float(store.get_setting(key, str(default)) or default))
    except ValueError:
        return default


def setting_bool(key: str, default: bool) -> bool:
    raw = store.get_setting(key, str(default).lower())
    return str(raw).lower() in {"1", "true", "yes", "y", "是"}


data_tab, config_tab = st.tabs(["常用数据", "参数配置"])

with data_tab:
    st.subheader("常用数据")
    st.caption("日常只看这里：雷达状态、已保存策略、最近执行和快捷入口。参数调整放在第二个页签。")
    radar = radar_status()
    # 只看当前页租户的 campaign —— 跨租户记录不显示、也不参与任何清理。
    campaigns = [
        item for item in load_volatility_campaigns(_page_tenant_id)
        if campaign_belongs_to_tenant(item, _page_tenant_id)
    ]
    decisions = [
        row for row in store.decisions(limit=200)
        if row.get("strategy_id") == "volatility_long_gamma"
    ]
    scanner_rows = [
        row for row in store.decisions(limit=200)
        if row.get("strategy_id") == "volatility_scanner"
    ]
    with st.container(horizontal=True):
        st.metric("雷达状态", "运行中" if radar.running else "未运行", border=True)
        st.metric("自动策略", len(campaigns), border=True)
        st.metric("最近执行记录", len(decisions), border=True)
        st.metric("最近扫描记录", len(scanner_rows), border=True)
    with st.container(horizontal=True):
        st.page_link("pages/30_volatility_strategy_overview.py", label="打开波动率策略总览", icon=":material/dashboard:")
        st.page_link("pages/30_volatility_strategy_overview.py", label="查看候选池选品漏斗（为什么没单）", icon=":material/filter_alt:")
        st.page_link("pages/28_options_pnl_ledger.py", label="查看期权盈亏", icon=":material/price_check:")
        st.page_link("pages/26_trade_diagnostics_center.py", label="查看执行诊断", icon=":material/troubleshoot:")
        st.page_link("pages/4_order_management.py", label="查看订单", icon=":material/receipt_long:")
    if campaigns:
        campaign_view = pd.DataFrame([{
            "期权代码": item.get("code", ""),
            "正股代码": item.get("underlying", ""),
            "方向": item.get("option_type", ""),
            "到期日": item.get("strike_time", ""),
            "每批合约数": item.get("contracts_per_stage", 0),
            "每批最大权利金": item.get("max_premium_per_stage", 0),
            "启用": "是" if item.get("enabled", True) else "否",
        } for item in campaigns])
        st.dataframe(
            campaign_view,
            hide_index=True,
            height=320,
            column_config={
                "每批合约数": st.column_config.NumberColumn("每批合约数", format="%.0f"),
                "每批最大权利金": st.column_config.NumberColumn("每批最大权利金", format="$%.0f"),
            },
        )
    else:
        st.info("暂无已保存的波动率自动策略。")
    if decisions:
        decision_view = china_time_columns(pd.DataFrame(decisions[:30])).rename(columns={
            "symbol": "期权代码",
            "decision": "决策",
            "reason": "原因",
            "created_at": "时间（中国）",
        }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore")
        st.dataframe(
            translate_common_values(decision_view),
            hide_index=True,
            height=360,
        )
    else:
        st.info("暂无波动率策略执行记录。")


with config_tab:
    st.subheader("参数配置")
    st.caption("这里放低频调整项：标的、雷达预算、候选生成、入场/退出/保护参数和策略维护。")

    with st.expander("参数配置 · 分层入场预算倍数", expanded=False):
        st.caption("正股信号越强，允许的期权预算越高；预期收益、行情新鲜度和组合风控仍是硬性条件。")
        tier_left, tier_middle, tier_right = st.columns(3)
        normal_factor = tier_left.number_input(
            "常规模式预算倍数", 0.1, 5.0,
            float(store.get_setting("volatility_tier_normal_budget_factor", "1.0") or 1.0),
            step=0.1, key="volatility_tier_normal_factor",
        )
        dynamic_factor = tier_middle.number_input(
            "动态波动模式预算倍数", 0.1, 5.0,
            float(store.get_setting("volatility_tier_dynamic_budget_factor", "1.5") or 1.5),
            step=0.1, key="volatility_tier_dynamic_factor",
        )
        strong_factor = tier_right.number_input(
            "强波动模式预算倍数", 0.1, 5.0,
            float(store.get_setting("volatility_tier_strong_budget_factor", "2.0") or 2.0),
            step=0.1, key="volatility_tier_strong_factor",
        )
        if st.button("保存分层预算倍数", disabled=not admin, key="save_volatility_tier_factors"):
            for key, value in (
                ("volatility_tier_normal_budget_factor", normal_factor),
                ("volatility_tier_dynamic_budget_factor", dynamic_factor),
                ("volatility_tier_strong_budget_factor", strong_factor),
            ):
                store.set_setting(key, str(float(value)))
            st.success("分层预算倍数已保存，下一轮雷达/策略循环生效。")


    @st.cache_data(ttl=3600, show_spinner="正在从富途载入美股证券目录……")
    def load_catalog() -> pd.DataFrame:
        return broker.security_catalog()


    with st.expander("机会雷达 · 搜索并添加策略标的", expanded=False):
        with st.form("volatility_symbol_search"):
            search_mode = st.segmented_control(
                "查询方式", ["模糊查询", "代码精确查询"], default="模糊查询"
            )
            query = st.text_input("代码或名称", placeholder="例如 NVDA、英伟达、AAPL")
            submitted = st.form_submit_button("查找", icon=":material/search:")
        if submitted:
            try:
                st.session_state.volatility_symbol_results = search_securities(
                    load_catalog(), query, exact=search_mode == "代码精确查询"
                )
            except (BrokerError, ValueError) as exc:
                st.error(str(exc))
        symbol_results = st.session_state.get("volatility_symbol_results", pd.DataFrame())
        if not symbol_results.empty:
            result_index = st.selectbox(
                "搜索结果", symbol_results.index.tolist(),
                format_func=lambda index: security_label(symbol_results.loc[index]),
            )
            st.dataframe(chinese_columns(symbol_results[[
                column for column in ("code", "name", "stock_type", "lot_size", "listing_date")
                if column in symbol_results
            ]]), hide_index=True)
            add_code = str(symbol_results.loc[result_index, "code"])
            if st.button(f"添加 {add_code}", type="primary", disabled=not admin):
                save_volatility_symbols(tuple(dict.fromkeys((*settings.volatility_symbols, add_code))))
                st.cache_data.clear()
                st.success(f"已添加 {add_code} 到波动率策略标的。")
                st.rerun()

    st.caption("已添加标的的批量维护与删除，请前往“交易配置”页面。")

    with st.expander("机会雷达 · 自动扫描与候选生成", expanded=False):
        radar = radar_status()
        st.status(
            f"独立雷达服务：{'运行中' if radar.running else '未运行'}"
            + (f" · PID {radar.pid}" if radar.pid else ""),
            state="complete" if radar.running else "error",
        )
        current_radar_symbols = load_volatility_symbols()
        radar_budgets = load_volatility_radar_budgets()
        schedule_state = load_radar_schedule_state(store)
        estimated_interval = adaptive_interval_seconds(len(current_radar_symbols))
        effective_interval = (
            schedule_state.interval_seconds if schedule_state else estimated_interval
        )

        def format_interval(seconds: int) -> str:
            minutes, remainder = divmod(max(0, int(seconds)), 60)
            return f"{minutes} 分 {remainder} 秒" if remainder else f"{minutes} 分钟"

        with st.container(horizontal=True):
            st.metric("扫描标的数", len(current_radar_symbols), border=True)
            st.metric("当前扫描间隔", format_interval(effective_interval), border=True)
            st.metric(
                "上次扫描耗时",
                f"{schedule_state.scan_duration_seconds:.1f} 秒"
                if schedule_state else "等待首次扫描",
                border=True,
            )

        with st.expander("自动雷达权利金预算"):
            st.caption(
                "预算按“标的单独预算 → 默认预算”取值，只限制自动雷达新生成的策略；"
                "流动性、Delta、模型预期收益和账户风控仍会独立校验。"
            )
            budget_rows = pd.DataFrame([
                {
                    "正股代码": symbol,
                    "使用单独预算": symbol in radar_budgets["symbols"],
                    "每批最大权利金（美元）": float(
                        radar_budgets["symbols"].get(symbol, radar_budgets["default"])
                    ),
                }
                for symbol in current_radar_symbols
            ])
            with st.form("volatility_radar_budget_form"):
                default_radar_budget = st.number_input(
                    "自动雷达默认每批最大权利金（美元）",
                    min_value=1.0, max_value=1_000_000.0,
                    value=float(radar_budgets["default"]), step=100.0,
                )
                edited_budgets = st.data_editor(
                    budget_rows,
                    hide_index=True,
                    disabled=["正股代码"],
                    column_config={
                        "使用单独预算": st.column_config.CheckboxColumn(
                            "使用单独预算",
                            help="勾选后，该标的不再使用默认预算。",
                        ),
                        "每批最大权利金（美元）": st.column_config.NumberColumn(
                            "每批最大权利金（美元）", min_value=1.0,
                            max_value=1_000_000.0, step=100.0, format="$%.0f",
                        ),
                    },
                    key="volatility_radar_budget_editor",
                )
                save_radar_budget = st.form_submit_button(
                    "保存自动雷达预算", type="primary", icon=":material/save:",
                    disabled=not admin,
                )
            if save_radar_budget:
                symbol_budgets = {
                    str(row["正股代码"]): float(row["每批最大权利金（美元）"])
                    for _, row in edited_budgets.iterrows()
                    if bool(row["使用单独预算"])
                }
                saved_budgets = save_volatility_radar_budgets(
                    float(default_radar_budget), symbol_budgets,
                )
                store.audit(
                    "VOLATILITY_RADAR_BUDGETS_UPDATED", saved_budgets, "streamlit",
                 actor="streamlit_admin:panel")
                st.success("自动雷达权利金预算已保存，下一轮扫描立即生效。")
                st.rerun()
            st.metric(
                "连续错误",
                schedule_state.consecutive_errors if schedule_state else 0,
                border=True,
            )

        if schedule_state:
            next_scan = pd.to_datetime(
                schedule_state.next_scan_at, errors="coerce", utc=True
            )
            if pd.notna(next_scan):
                st.caption(
                    "预计下次扫描（中国时间）："
                    f"{next_scan.tz_convert('Asia/Shanghai'):%Y-%m-%d %H:%M:%S}"
                )
            if schedule_state.backoff_active:
                st.warning(
                    f"雷达正在异常退避：已连续失败 {schedule_state.consecutive_errors} 次，"
                    f"当前重试间隔 {format_interval(schedule_state.interval_seconds)}。"
                )
        else:
            st.caption("预计下次扫描：等待雷达服务完成首次动态调度。")
        st.write(
            "独立雷达服务在美股常规交易时段只扫描你已加入的波动率策略标的，"
            "并按标的数量和最近扫描耗时动态调整，间隔为 2 至 15 分钟。"
            "达到动态波动条件后，会写入雷达决策、自动选择期权合约、保存为自动期权监控策略，"
            "并开启 volatility_long_gamma 的 SHADOW 自动调度。"
        )
        st.caption(
            "当前雷达扫描范围："
            + ("、".join(current_radar_symbols) if current_radar_symbols else "尚未配置")
        )
        if st.button(
            "立即扫描已选标的", icon=":material/radar:", disabled=not admin or not current_radar_symbols,
        ):
            try:
                with st.spinner(f"正在扫描 {len(current_radar_symbols)} 个波动率策略标的……"):
                    scan_results = scan_volatility_watchlist(broker, store, current_radar_symbols)
                triggered = [item["symbol"] for item in scan_results if item.get("active")]
                created = [
                    item.get("campaign_code") for item in scan_results
                    if item.get("campaign_created") and item.get("campaign_code")
                ]
                blocked = [
                    f"{item['symbol']}：{item.get('campaign_reason', '未找到合格合约')}"
                    for item in scan_results
                    if item.get("active") and item.get("campaign_created") is False
                ]
                if created:
                    st.success(
                        "扫描完成，已生成并进入自动期权监控：" + "、".join(created)
                    )
                if blocked:
                    st.warning("正股信号已触发，但合约风控未通过：\n\n" + "\n\n".join(blocked))
                elif triggered and not created:
                    st.info("正股信号已触发，但已有启用中的自动期权策略，本轮不重复创建。")
                elif not triggered:
                    st.info("扫描完成，当前没有标的满足动态波动触发条件。")
            except Exception as exc:
                st.error(f"扫描失败：{exc}")
        scanner_rows = pd.DataFrame([
            row for row in store.decisions(limit=500)
            if row["strategy_id"] == "volatility_scanner"
        ])
        if scanner_rows.empty:
            st.info("暂无雷达扫描记录；美股常规交易时段启动服务后自动产生。")
        else:
            latest_scans = scanner_rows.drop_duplicates("symbol").copy()
            details = latest_scans["payload_json"].map(
                lambda value: json.loads(value or "{}")
            )
            latest_scans["当日涨跌"] = details.map(lambda item: item.get("intraday_return"))
            latest_scans["波动Z值"] = details.map(lambda item: item.get("zscore_20d"))
            latest_scans["ATR倍数"] = details.map(lambda item: item.get("atr_ratio"))
            latest_scans["量比"] = details.map(lambda item: item.get("volume_ratio"))
            latest_scans["方向分"] = details.map(lambda item: item.get("direction_score"))
            latest_scans["信号生命周期"] = details.map(lambda item: item.get("lifecycle"))
            latest_scans["相对SPY强弱"] = details.map(lambda item: item.get("relative_strength"))
            latest_scans["目标Z值"] = details.map(
                lambda item: (item.get("profile") or {}).get("entry_zscore")
            )
            latest_scans["目标最小涨跌"] = details.map(
                lambda item: (item.get("profile") or {}).get("minimum_underlying_move")
            )
            latest_scans["自动期权状态"] = details.map(
                lambda item: (
                    "已加入自动监控" if item.get("campaign_created")
                    else "合约风控未通过" if item.get("campaign_created") is False
                    else "未触发生成"
                )
            )
            latest_scans["自动期权代码"] = details.map(
                lambda item: item.get("campaign_code") or "—"
            )
            latest_scans["合约生成说明"] = details.map(
                lambda item: item.get("campaign_reason") or "—"
            )
            latest_scans["本轮权利金预算"] = details.map(
                lambda item: item.get("campaign_premium_budget")
            )
            latest_scans["最高分候选详情"] = details.map(
                lambda item: "；".join(
                    f"{candidate.get('code', '')}：{candidate.get('reason', '')}"
                    for candidate in (
                        (item.get("campaign_diagnostics") or {}).get("top_candidates") or []
                    )[:3]
                ) or "—"
            )
            display = translate_common_values(china_time_columns(latest_scans).rename(columns={
                "symbol": "正股代码", "decision": "监测结果", "reason": "原因",
                "created_at": "扫描时间（中国）",
            }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore"))
            displayed_volatility_symbols = load_volatility_symbols()
            display.insert(
                0, "操作",
                display["正股代码"].map(
                    lambda code: (
                        ":material/remove: 移除波动率标的"
                        if code in displayed_volatility_symbols
                        else "非当前标的"
                    )
                ),
            )

            def toggle_radar_volatility_symbol() -> None:
                if not admin:
                    st.session_state["radar_volatility_action_error"] = "请先解锁管理员操作。"
                    return
                click = st.session_state.get("radar_toggle_volatility")
                if not click:
                    return
                code = str(display.iloc[int(click["row"])]["正股代码"])
                current_volatility_symbols = load_volatility_symbols()
                if code not in current_volatility_symbols:
                    st.session_state["radar_volatility_action_error"] = (
                        f"{code} 不是当前波动率策略标的，雷达不会继续扫描它。"
                    )
                    return
                remaining = tuple(
                    symbol for symbol in current_volatility_symbols if symbol != code
                )
                if not remaining:
                    st.session_state["radar_volatility_action_error"] = (
                        "至少需要保留一个波动率策略标的，无法移除最后一个标的。"
                    )
                    return
                update_volatility_symbol(code, False)
                store.set_setting(f"volatility_scanner_paused:{code}", "true")
                store.audit(
                    "VOLATILITY_SYMBOL_REMOVED_FROM_RADAR",
                    {"symbol": code, "scanner_paused": True},
                    "streamlit",
                 actor="streamlit_admin:panel")
                st.session_state["radar_volatility_removed"] = code

            st.dataframe(display, hide_index=True, height=420, column_config={
                "操作": st.column_config.ButtonColumn(
                    "操作", pinned=True, type="tertiary",
                    on_click=toggle_radar_volatility_symbol, key="radar_toggle_volatility",
                ),
                "当日涨跌": st.column_config.NumberColumn(format="percent"),
                "相对SPY强弱": st.column_config.NumberColumn(format="percent"),
                "目标最小涨跌": st.column_config.NumberColumn(format="percent"),
            })
            removed_code = st.session_state.pop("radar_volatility_removed", None)
            if removed_code:
                st.success(
                    f"{removed_code} 已从波动率策略标的中移除，并暂停雷达自动重新加入。"
                )
            action_error = st.session_state.pop("radar_volatility_action_error", None)
            if action_error:
                st.error(action_error)

    with st.expander("参数配置 · 策略规则", expanded=False):
        st.markdown(
            "1. 默认根据当日涨跌、5日动量和20日趋势自动选择 Call 或 Put，也保留手动覆盖能力。\n"
            "2. 使用独立 IV 阈值，同时保留价差、权利金、Greeks 和组合风控。\n"
            "3. 分批入场，只顺势加仓，不对亏损仓位摊低成本。\n"
            "4. 按止盈、硬止损、峰值回撤或最长持有时间退出。\n"
            "5. 与 earnings_gamma 拥有独立配置、执行模式、预算、日志和 Shadow 持仓。\n"
            "6. 采用20日波动Z值、ATR、量比和趋势确认；使用滞回、冷却以及IV/波动衰减退出。"
        )

    left, middle, right = st.columns(3)
    underlying = left.selectbox("正股", load_volatility_symbols())
    direction_choice = middle.segmented_control("方向", ["自动判断", "CALL", "PUT"], default="自动判断")
    maturity_min_dte = left.number_input("最短到期天数（DTE）", 1, 45, setting_int("volatility_default_maturity_min_dte", 5))
    max_days = right.number_input("最远到期天数（DTE）", maturity_min_dte, 90, setting_int("volatility_default_maturity_max_dte", 21))
    min_oi = left.number_input("最低未平仓量", 0, 1_000_000, 500, step=50)
    max_spread = middle.number_input("绝对最大买卖价差（%）", 1.0, 100.0, 12.0)
    max_moneyness = right.number_input("最大价内/价外距离（%）", 0.0, 50.0, 10.0)
    contracts_per_stage = left.number_input("每批合约数", 1, 100, 1)
    stages = middle.number_input("计划分批数", 1, 5, 3)
    max_premium = right.number_input("每批最大权利金（美元）", 1.0, 1_000_000.0, 1000.0)
    entry_hours = left.number_input("初始入场窗口（小时）", 1, 48, 6)
    profit_target = middle.number_input("止盈比例", 0.05, 10.0, 0.50, step=0.05)
    stop_loss = right.number_input("硬止损比例", 0.05, 1.0, 0.40, step=0.05)
    trailing_stop = left.number_input("峰值回撤退出比例", 0.05, 1.0, 0.25, step=0.05)
    with st.expander("参数配置 · 独立平仓参数", expanded=False):
        st.markdown(
            "退出优先级：到期/行权风险 → 硬止损 → 整单止盈 → 分批止盈 → "
            "最低锁定盈利 → 盈利回吐保护 → IV/Gamma 衰减 → 方向失效/最长持有。"
        )
        exit_left, exit_middle, exit_right = st.columns(3)
        max_hold_hours = exit_left.number_input(
            "最长持有小时", 1, 168,
            value=setting_int("volatility_default_max_hold_hours", 24),
            help="从本轮持仓建立时间开始计算，不再使用财报事件时间。",
        )
        minimum_hold_minutes = exit_middle.number_input(
            "衰减退出最短持有分钟", 0, 1440,
            value=setting_int("volatility_default_minimum_hold_minutes", 90),
            help="硬止损、止盈和盈利峰值回撤始终生效；IV/效率衰减退出至少等待该时长，并要求价格或方向也不利。",
        )
        contraction_confirmations = exit_right.number_input(
            "波动收缩连续确认次数", 1, 10,
            value=setting_int("volatility_default_contraction_confirmations_required", 3),
            help="避免单次行情噪声造成误平仓。",
        )
        iv_decline_exit_ratio = exit_left.number_input(
            "IV较实际入场下降比例", 0.05, 0.90,
            value=setting_float("volatility_default_iv_decline_exit_ratio", 0.20),
            step=0.05,
        )
        efficiency_decline_exit_ratio = exit_middle.number_input(
            "Gamma/Theta效率下降比例", 0.05, 0.90,
            value=setting_float("volatility_default_efficiency_decline_exit_ratio", 0.40),
            step=0.05,
        )
        minimum_effective_delta = exit_right.number_input(
            "最低有效 Delta", 0.05, 0.50,
            value=setting_float("volatility_default_minimum_effective_delta", 0.18),
            step=0.01,
            help="亏损持仓的绝对 Delta 低于该值，且方向连续失效后，视为期权表达衰减。",
        )
        adverse_confirmations = exit_left.number_input(
            "方向失效连续确认次数", 1, 10,
            value=setting_int("volatility_default_adverse_signal_confirmations_required", 2),
            help="正股方向与期权表达连续不一致达到该次数后，允许提前退出。",
        )
        adverse_exit_hold = exit_middle.number_input(
            "方向失效退出最短持有分钟", 0, 1440,
            value=setting_int("volatility_default_adverse_exit_minimum_hold_minutes", 20),
            help="只用于连续方向失效或 Delta 衰减；硬止损仍立即生效。",
        )
        entry_confirmation_minutes = exit_right.number_input(
            "入场二次确认基准分钟", 0.0, 30.0,
            value=setting_float("volatility_default_entry_confirmation_minutes", 5.0),
            step=0.5,
            help="强信号自动缩短到最多1分钟，完整确认信号最多2.5分钟，边缘信号使用这里的基准值。",
        )
        stability_left, stability_middle, stability_right = st.columns(3)
        entry_confirmation_min_volume_ratio = stability_left.number_input(
            "入场稳定最低量比", 0.0, 5.0,
            value=setting_float("volatility_default_entry_confirmation_min_volume_ratio", 0.80),
            step=0.05,
            help="二次确认结束时仍需达到的最低量比。用于过滤冲高后量能快速衰减的尾端单。",
        )
        entry_confirmation_min_volume_retention = stability_middle.number_input(
            "入场量能保持率", 0.0, 1.0,
            value=setting_float("volatility_default_entry_confirmation_min_volume_retention", 0.65),
            step=0.05,
            help="当前量比 / 信号刚触发时量比。低于该比例说明行情没有持续跟进，暂不入场。",
        )
        entry_confirmation_require_active = stability_right.toggle(
            "确认时必须仍处于动态波动",
            value=setting_bool("volatility_default_entry_confirmation_require_active", True),
            help="开启后，二次确认结束时动态波动状态已经失效则不再开仓。",
        )
        profit_protection_start = exit_left.number_input(
            "盈利保护启动阈值", 0.01, 2.0,
            value=setting_float("volatility_default_profit_protection_start", 0.15),
            step=0.01,
            help="浮盈达到这个水平后，才启用盈利回吐保护。",
        )
        profit_protection_drawdown = exit_middle.number_input(
            "盈利回吐阈值", 0.01, 1.0,
            value=setting_float("volatility_default_profit_protection_drawdown", 0.25),
            step=0.01,
            help="按峰值盈利的回吐比例计算，不是按期权价格从高点回撤计算。例如峰值盈利 26%、阈值 25%，盈利降到约 19.5% 就触发。",
        )
        minimum_locked_profit = exit_right.number_input(
            "最低锁定盈利", 0.0, 1.0,
            value=setting_float("volatility_default_minimum_locked_profit", 0.08),
            step=0.01,
            help="盈利保护启动后，如果当前盈利跌到这条线以下，直接退出，避免盈利单变亏损单。",
        )
        dynamic_stop_enabled = st.toggle(
            "启用时间衰减动态止损",
            value=setting_bool("volatility_default_dynamic_time_stop_enabled", True),
            help="亏损仓位持有越久，允许亏损线越低；用于避免长 Gamma 期权一路耗到硬止损才退出。",
        )
        dynamic_left, dynamic_middle, dynamic_right = st.columns(3)
        dynamic_stop_floor = dynamic_left.number_input(
            "动态止损最低阈值", 0.01, 1.0,
            value=setting_float("volatility_default_dynamic_time_stop_floor", 0.12),
            step=0.01,
            help="动态止损最终收紧到这条亏损线。例：12% 表示持仓较久后亏损达到 12% 就可退出。",
        )
        dynamic_stop_start = dynamic_middle.number_input(
            "开始衰减分钟", 0, 240,
            value=setting_int("volatility_default_dynamic_time_stop_start_minutes", 15),
            step=5,
            help="开仓后先给策略一点反应时间；超过该时间才从硬止损线开始收紧。",
        )
        dynamic_stop_full = dynamic_right.number_input(
            "完全衰减分钟", 5, 720,
            value=setting_int("volatility_default_dynamic_time_stop_full_decay_minutes", 90),
            step=5,
            help="持仓达到该分钟数后，动态止损线收紧到最低阈值。",
        )
    with st.expander("参数配置 · 按信号强度分层保护", expanded=False):
        st.caption("行业口径：信号越强，允许更大的盈利空间和回吐；普通信号更快保护利润。")
        tier_columns = st.columns(4)
        tier_columns[0].markdown("**层级**")
        tier_columns[1].markdown("**启动阈值**")
        tier_columns[2].markdown("**盈利回吐**")
        tier_columns[3].markdown("**锁定盈利**")
        near_label, near_start_col, near_giveback_col, near_lock_col = st.columns(4)
        near_label.write("临近到期")
        protection_start_near = near_start_col.number_input("近到期启动", 0.01, 2.0, value=setting_float("volatility_default_profit_protection_start_near_expiry", 0.10), step=0.01, label_visibility="collapsed")
        protection_giveback_near = near_giveback_col.number_input("近到期回吐", 0.01, 1.0, value=setting_float("volatility_default_profit_protection_giveback_near_expiry", 0.20), step=0.01, label_visibility="collapsed")
        locked_profit_near = near_lock_col.number_input("近到期锁定", 0.0, 1.0, value=setting_float("volatility_default_minimum_locked_profit_near_expiry", 0.05), step=0.01, label_visibility="collapsed")
        near_days = st.number_input(
            "临近到期保护天数", 0, 7,
            value=setting_int("volatility_default_near_expiry_protection_days", 1),
            help="剩余到期天数小于等于该值时，优先使用临近到期档。0 表示仅到期日当天。",
        )
        normal_label, normal_start_col, normal_giveback_col, normal_lock_col = st.columns(4)
        normal_label.write("常规")
        protection_start_normal = normal_start_col.number_input("常规启动", 0.01, 2.0, value=setting_float("volatility_default_profit_protection_start_normal", 0.10), step=0.01, label_visibility="collapsed")
        protection_giveback_normal = normal_giveback_col.number_input("常规回吐", 0.01, 1.0, value=setting_float("volatility_default_profit_protection_giveback_normal", 0.20), step=0.01, label_visibility="collapsed")
        locked_profit_normal = normal_lock_col.number_input("常规锁定", 0.0, 1.0, value=setting_float("volatility_default_minimum_locked_profit_normal", 0.05), step=0.01, label_visibility="collapsed")
        dynamic_label, dynamic_start_col, dynamic_giveback_col, dynamic_lock_col = st.columns(4)
        dynamic_label.write("动态波动")
        protection_start_dynamic = dynamic_start_col.number_input("动态启动", 0.01, 2.0, value=setting_float("volatility_default_profit_protection_start_dynamic", 0.15), step=0.01, label_visibility="collapsed")
        protection_giveback_dynamic = dynamic_giveback_col.number_input("动态回吐", 0.01, 1.0, value=setting_float("volatility_default_profit_protection_giveback_dynamic", 0.25), step=0.01, label_visibility="collapsed")
        locked_profit_dynamic = dynamic_lock_col.number_input("动态锁定", 0.0, 1.0, value=setting_float("volatility_default_minimum_locked_profit_dynamic", 0.08), step=0.01, label_visibility="collapsed")
        strong_label, strong_start_col, strong_giveback_col, strong_lock_col = st.columns(4)
        strong_label.write("强波动")
        protection_start_strong = strong_start_col.number_input("强波动启动", 0.01, 2.0, value=setting_float("volatility_default_profit_protection_start_strong", 0.20), step=0.01, label_visibility="collapsed")
        protection_giveback_strong = strong_giveback_col.number_input("强波动回吐", 0.01, 1.0, value=setting_float("volatility_default_profit_protection_giveback_strong", 0.30), step=0.01, label_visibility="collapsed")
        locked_profit_strong = strong_lock_col.number_input("强波动锁定", 0.0, 1.0, value=setting_float("volatility_default_minimum_locked_profit_strong", 0.06), step=0.01, label_visibility="collapsed")
    with st.expander("参数配置 · 分批止盈与冲高保护", expanded=False):
        st.caption(
            "2 张以上自动分批；1 张合约达到主动落袋线会先兑现原合约，"
            "后续由新信号/低成本表达继续做 runner。"
        )
        partial_enabled = st.toggle(
            "启用分批止盈",
            value=setting_bool("volatility_default_partial_profit_enabled", True),
        )
        partial_left, partial_middle, partial_right = st.columns(3)
        partial_tier1 = partial_left.number_input(
            "第一档盈利阈值", 0.05, 5.0,
            value=setting_float("volatility_default_partial_profit_tier1", 0.18),
            step=0.05,
            help="达到该浮盈后先卖出一部分，把利润落袋。",
        )
        partial_tier1_fraction = partial_middle.number_input(
            "第一档卖出比例", 0.05, 0.95,
            value=setting_float("volatility_default_partial_profit_tier1_fraction", 0.50),
            step=0.05,
            help="按当前持仓数量计算，且至少保留 1 张继续跟踪。",
        )
        partial_tier2 = partial_left.number_input(
            "第二档盈利阈值", 0.05, 10.0,
            value=setting_float("volatility_default_partial_profit_tier2", 0.35),
            step=0.05,
        )
        partial_tier2_fraction = partial_middle.number_input(
            "第二档卖出比例", 0.05, 0.95,
            value=setting_float("volatility_default_partial_profit_tier2_fraction", 0.25),
            step=0.05,
            help="第二档未必每次都执行；如果只剩 1 张，会自动交给盈利回吐保护。",
        )
        single_take_profit = partial_right.number_input(
            "单张主动落袋线", 0.05, 5.0,
            value=setting_float("volatility_default_single_contract_take_profit", 0.18),
            step=0.01,
            help="只有 1 张期权时不能分批，达到该盈利后先卖出原合约，避免冲高回落。",
        )
        tiered_lock_enabled = st.toggle(
            "启用阶梯锁盈",
            value=setting_bool("volatility_default_tiered_profit_lock_enabled", True),
            help="峰值越高，最低保留盈利越高：10%→6%、18%→12%、30%→18%、50%→30%、80%→50%。",
        )
        tighten_left, tighten_middle, tighten_right = st.columns(3)
        time_tighten_minutes = tighten_left.number_input(
            "持仓多久后收紧（分钟）", 5, 360,
            value=setting_int("volatility_default_profit_time_tighten_minutes", 60),
            step=5,
            help="持仓超过该时间且已有明显浮盈后，盈利回吐阈值自动收紧。",
        )
        time_tighten_giveback = tighten_middle.number_input(
            "时间收紧回吐", 0.01, 1.0,
            value=setting_float("volatility_default_profit_time_tighten_giveback", 0.12),
            step=0.01,
            help="超过持仓时间后，只允许从峰值回吐这么多比例。",
        )
        tighten_right.info("行业默认：18% 主动落袋，60 分钟后回吐阈值收紧到 12%。")
    with st.expander("按退出原因冷却与重入保护", expanded=False):
        cooldown_left, cooldown_middle, cooldown_right = st.columns(3)
        cooldown_take_profit = cooldown_left.number_input("止盈后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_take_profit_hours", 4))
        cooldown_profit_protection = cooldown_middle.number_input("盈利保护后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_profit_protection_hours", 8))
        cooldown_stop_loss = cooldown_right.number_input("硬止损后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_stop_loss_hours", 24))
        cooldown_expiry = cooldown_left.number_input("到期风险退出后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_expiry_hours", 0))
        cooldown_decay = cooldown_middle.number_input("IV/Gamma衰减后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_decay_hours", 8))
        cooldown_contraction = cooldown_right.number_input("波动收缩后冷却小时", 0, 168, value=setting_int("volatility_default_cooldown_contraction_hours", 6))
        cooldown_bypass = st.toggle(
            "允许强信号绕过冷却（不建议）",
            value=setting_bool("volatility_default_cooldown_bypass_enabled", False),
            help="成熟期权模式默认关闭。开启后极强信号可能在退出后提前重入，会增加过度交易和利润回吐风险。",
        )
    with st.expander("单笔止损限额（下单前强制校验）", expanded=True):
        stop_left, stop_right = st.columns(2)
        max_position_loss_ratio = stop_left.number_input(
            "相对止损（权利金亏损比例）", 0.01, 1.0,
            value=setting_float("volatility_default_max_position_loss_ratio", 0.25),
            step=0.05,
            help=(
                "主防线：持仓权利金相对亏损达到该比例即止损，随仓位规模自动缩放。"
                "与上方「硬止损比例」语义不同 —— 这里是**单笔最大亏损**的相对口径。"
            ),
        )
        max_position_loss_usd = stop_right.number_input(
            "绝对止损（单笔最大亏损，美元）", 1.0, 100_000.0,
            value=setting_float("volatility_default_max_position_loss_usd", 300.0),
            step=10.0,
            help=(
                "兜底：单笔最大允许亏损金额，且不超过账户净值的 0.5%。"
                "防止极端跳空下相对止损失守。"
            ),
        )
        st.caption(
            "**两层取较紧者先生效（OR）**：小仓位由相对止损决定（例：$294 权利金 → 约 $75 触发）；"
            "大仓位由绝对上限决定（例：$2,076 权利金 → $300 先触发）。不是「先相对、不够再绝对」。"
        )
        st.caption(
            "⚠️ 这两个限额会**显式写入每一条波动率策略**，并在**下单前强制校验**："
            "任一条策略缺省即记 ERROR 并**拒绝开仓**（不静默兜底）。"
            "改动会留痕到审计事件，可通过「运行审计」页追溯。"
        )
        same_contract_cooldown = st.number_input(
            "同一合约止盈后冷却小时", 1, 168,
            value=setting_int("volatility_default_same_contract_cooldown_hours", 24),
        )

    volatility_exit_parameters = {
        "max_hold_hours": int(max_hold_hours),
        "minimum_hold_minutes": int(minimum_hold_minutes),
        "iv_decline_exit_ratio": float(iv_decline_exit_ratio),
        "efficiency_decline_exit_ratio": float(efficiency_decline_exit_ratio),
        "contraction_confirmations_required": int(contraction_confirmations),
        "minimum_effective_delta": float(minimum_effective_delta),
        "adverse_signal_confirmations_required": int(adverse_confirmations),
        "adverse_exit_minimum_hold_minutes": int(adverse_exit_hold),
        "entry_confirmation_minutes": float(entry_confirmation_minutes),
        "entry_confirmation_min_volume_ratio": float(entry_confirmation_min_volume_ratio),
        "entry_confirmation_min_volume_retention": float(entry_confirmation_min_volume_retention),
        "entry_confirmation_require_active": bool(entry_confirmation_require_active),
        "profit_protection_start": float(profit_protection_start),
        "profit_protection_drawdown": float(profit_protection_drawdown),
        "minimum_locked_profit": float(minimum_locked_profit),
        "near_expiry_protection_days": int(near_days),
        "profit_protection_start_near_expiry": float(protection_start_near),
        "profit_protection_giveback_near_expiry": float(protection_giveback_near),
        "minimum_locked_profit_near_expiry": float(locked_profit_near),
        "profit_protection_start_normal": float(protection_start_normal),
        "profit_protection_giveback_normal": float(protection_giveback_normal),
        "minimum_locked_profit_normal": float(locked_profit_normal),
        "profit_protection_start_dynamic": float(protection_start_dynamic),
        "profit_protection_giveback_dynamic": float(protection_giveback_dynamic),
        "minimum_locked_profit_dynamic": float(locked_profit_dynamic),
        "profit_protection_start_strong": float(protection_start_strong),
        "profit_protection_giveback_strong": float(protection_giveback_strong),
        "minimum_locked_profit_strong": float(locked_profit_strong),
        "partial_profit_enabled": bool(partial_enabled),
        "partial_profit_tier1": float(partial_tier1),
        "partial_profit_tier1_fraction": float(partial_tier1_fraction),
        "partial_profit_tier2": float(partial_tier2),
        "partial_profit_tier2_fraction": float(partial_tier2_fraction),
        "single_contract_take_profit": float(single_take_profit),
        "tiered_profit_lock_enabled": bool(tiered_lock_enabled),
        "profit_time_tighten_minutes": int(time_tighten_minutes),
        "profit_time_tighten_giveback": float(time_tighten_giveback),
        "dynamic_time_stop_enabled": bool(dynamic_stop_enabled),
        "dynamic_time_stop_floor": float(dynamic_stop_floor),
        "dynamic_time_stop_start_minutes": int(dynamic_stop_start),
        "dynamic_time_stop_full_decay_minutes": int(dynamic_stop_full),
        "cooldown_take_profit_hours": int(cooldown_take_profit),
        "cooldown_profit_protection_hours": int(cooldown_profit_protection),
        "cooldown_stop_loss_hours": int(cooldown_stop_loss),
        "cooldown_expiry_hours": int(cooldown_expiry),
        "cooldown_decay_hours": int(cooldown_decay),
        "cooldown_contraction_hours": int(cooldown_contraction),
        "cooldown_bypass_enabled": bool(cooldown_bypass),
        "same_contract_cooldown_hours": int(same_contract_cooldown),
        # 单笔止损限额（双层口径）—— 必须显式落库，缺省即拒绝开仓
        "max_position_loss_ratio": float(max_position_loss_ratio),
        "max_position_loss_usd": float(max_position_loss_usd),
    }

    volatility_exit_default_settings = {
        "volatility_default_max_hold_hours": int(max_hold_hours),
        "volatility_default_minimum_hold_minutes": int(minimum_hold_minutes),
        "volatility_default_iv_decline_exit_ratio": float(iv_decline_exit_ratio),
        "volatility_default_efficiency_decline_exit_ratio": float(efficiency_decline_exit_ratio),
        "volatility_default_contraction_confirmations_required": int(contraction_confirmations),
        "volatility_default_minimum_effective_delta": float(minimum_effective_delta),
        "volatility_default_adverse_signal_confirmations_required": int(adverse_confirmations),
        "volatility_default_adverse_exit_minimum_hold_minutes": int(adverse_exit_hold),
        "volatility_default_entry_confirmation_minutes": float(entry_confirmation_minutes),
        "volatility_default_entry_confirmation_min_volume_ratio": float(entry_confirmation_min_volume_ratio),
        "volatility_default_entry_confirmation_min_volume_retention": float(entry_confirmation_min_volume_retention),
        "volatility_default_entry_confirmation_require_active": str(bool(entry_confirmation_require_active)).lower(),
        "volatility_default_profit_protection_start": float(profit_protection_start),
        "volatility_default_profit_protection_drawdown": float(profit_protection_drawdown),
        "volatility_default_minimum_locked_profit": float(minimum_locked_profit),
        "volatility_default_near_expiry_protection_days": int(near_days),
        "volatility_default_profit_protection_start_near_expiry": float(protection_start_near),
        "volatility_default_profit_protection_giveback_near_expiry": float(protection_giveback_near),
        "volatility_default_minimum_locked_profit_near_expiry": float(locked_profit_near),
        "volatility_default_profit_protection_start_normal": float(protection_start_normal),
        "volatility_default_profit_protection_giveback_normal": float(protection_giveback_normal),
        "volatility_default_minimum_locked_profit_normal": float(locked_profit_normal),
        "volatility_default_profit_protection_start_dynamic": float(protection_start_dynamic),
        "volatility_default_profit_protection_giveback_dynamic": float(protection_giveback_dynamic),
        "volatility_default_minimum_locked_profit_dynamic": float(locked_profit_dynamic),
        "volatility_default_profit_protection_start_strong": float(protection_start_strong),
        "volatility_default_profit_protection_giveback_strong": float(protection_giveback_strong),
        "volatility_default_minimum_locked_profit_strong": float(locked_profit_strong),
        "volatility_default_partial_profit_enabled": str(bool(partial_enabled)).lower(),
        "volatility_default_partial_profit_tier1": float(partial_tier1),
        "volatility_default_partial_profit_tier1_fraction": float(partial_tier1_fraction),
        "volatility_default_partial_profit_tier2": float(partial_tier2),
        "volatility_default_partial_profit_tier2_fraction": float(partial_tier2_fraction),
        "volatility_default_single_contract_take_profit": float(single_take_profit),
        "volatility_default_tiered_profit_lock_enabled": str(bool(tiered_lock_enabled)).lower(),
        "volatility_default_profit_time_tighten_minutes": int(time_tighten_minutes),
        "volatility_default_profit_time_tighten_giveback": float(time_tighten_giveback),
        "volatility_default_dynamic_time_stop_enabled": str(bool(dynamic_stop_enabled)).lower(),
        "volatility_default_dynamic_time_stop_floor": float(dynamic_stop_floor),
        "volatility_default_dynamic_time_stop_start_minutes": int(dynamic_stop_start),
        "volatility_default_dynamic_time_stop_full_decay_minutes": int(dynamic_stop_full),
        "volatility_default_cooldown_take_profit_hours": int(cooldown_take_profit),
        "volatility_default_cooldown_profit_protection_hours": int(cooldown_profit_protection),
        "volatility_default_cooldown_stop_loss_hours": int(cooldown_stop_loss),
        "volatility_default_cooldown_expiry_hours": int(cooldown_expiry),
        "volatility_default_cooldown_decay_hours": int(cooldown_decay),
        "volatility_default_cooldown_contraction_hours": int(cooldown_contraction),
        "volatility_default_maturity_min_dte": int(maturity_min_dte),
        "volatility_default_maturity_max_dte": int(max_days),
        "volatility_default_cooldown_bypass_enabled": str(bool(cooldown_bypass)).lower(),
        "volatility_default_same_contract_cooldown_hours": int(same_contract_cooldown),
        "volatility_default_max_position_loss_ratio": float(max_position_loss_ratio),
        "volatility_default_max_position_loss_usd": float(max_position_loss_usd),
    }
    with st.container(border=True):
        st.subheader("保存退出/保护参数")
        st.caption(
            "保存为默认值会影响以后新生成/新保存的波动率策略；"
            "应用到已保存策略会批量更新已经存在的自动策略。"
        )
        st.markdown(
            "当前会保存以下参数："
            "`独立平仓参数`、`入场稳定性确认`、`按信号强度分层保护`、"
            "`分批止盈参数`、`时间衰减动态止损`、`按退出原因冷却`、`单笔止损限额`。"
        )
        save_default_col, apply_existing_col = st.columns(2)
        if save_default_col.button(
            "保存为默认保护参数",
            disabled=not admin,
            icon=":material/save:",
            key="save_volatility_exit_parameters_as_defaults",
        ):
            previous_defaults = {
                key: store.get_setting(key) for key in volatility_exit_default_settings
            }
            for key, value in volatility_exit_default_settings.items():
                store.set_setting(key, str(value))
            store.audit(
                "VOLATILITY_EXIT_DEFAULTS_UPDATED",
                {
                    "parameters": volatility_exit_default_settings,
                    "previous": previous_defaults,
                    "changed_by": "streamlit",
                },
                "volatility_long_gamma",
             actor="streamlit_admin:panel")
            st.success("已保存为默认保护参数；以后新生成/新保存的波动率策略会使用这组默认值。")
        if apply_existing_col.button(
            "应用到所有已保存策略",
            disabled=not admin,
            icon=":material/published_with_changes:",
            key="save_volatility_exit_parameters_to_campaigns",
        ):
            existing_campaigns = load_volatility_campaigns(_page_tenant_id)
            previous_limits = {
                campaign.get("code"): {
                    key: campaign.get(key)
                    for key in ("max_position_loss_ratio", "max_position_loss_usd")
                }
                for campaign in existing_campaigns
            }
            updated_campaigns = [
                {**campaign, **volatility_exit_parameters}
                for campaign in existing_campaigns
            ]
            save_volatility_campaigns(updated_campaigns, tenant_id=_page_tenant_id)
            store.audit(
                "VOLATILITY_EXIT_PARAMETERS_UPDATED",
                {
                    "campaigns": len(updated_campaigns),
                    "parameters": volatility_exit_parameters,
                    "previous_stop_limits": previous_limits,
                    "changed_by": "streamlit",
                },
                "volatility_long_gamma",
             actor="streamlit_admin:panel")
            st.success(f"已更新 {len(updated_campaigns)} 条已保存波动率策略，下一轮服务循环生效。")
    minimum_move = st.number_input("自动方向最低当日涨跌幅", 0.0, 0.20, 0.020, step=0.001, format="%.3f")
    entry_zscore = st.number_input("动态波动Z值门槛", 0.5, 5.0, 2.0, step=0.1)
    cooldown_hours = st.number_input("退出后冷却小时", 1, 168, 24)

    if st.button(
        "生成波动率期权候选", type="primary",
        icon=":material/search:", key="generate_volatility_candidates",
    ):
        st.session_state.pop("volatility_candidates", None)
        st.session_state.pop("volatility_candidate_summary", None)
        try:
            with st.spinner(f"正在读取 {underlying} 的到期日、期权链和实时行情……"):
                expirations = broker.option_expirations(underlying).copy()
                latest = date.today() + timedelta(days=int(max_days))
                valid = expirations[
                    (pd.to_datetime(expirations["strike_time"]).dt.date >= date.today())
                    & (pd.to_datetime(expirations["strike_time"]).dt.date <= latest)
                ]
                if valid.empty:
                    raise ValueError("指定窗口内没有有效到期日，请增加“最远到期天数”。")
                expirations_to_scan = valid.sort_values("strike_time")[
                    "strike_time"
                ].astype(str).head(3).tolist()
                chain_frames = [
                    broker.option_chain(underlying, expiration)
                    for expiration in expirations_to_scan
                ]
                chain = pd.concat(
                    [frame for frame in chain_frames if not frame.empty],
                    ignore_index=True,
                )
                if chain.empty:
                    raise ValueError("富途返回的期权链为空。")
                underlying_quote = broker.snapshots([underlying]).iloc[0]
                spot = float(underlying_quote["last_price"])
                bars = broker.candles(underlying, count=120)
                regime = volatility_regime(underlying_quote, bars, float(entry_zscore))
                option_types = [direction_choice]
                direction_reason = "使用手动选择的方向"
                if direction_choice == "自动判断":
                    automatic_type, direction_reason, _ = automatic_option_direction(
                        underlying_quote, bars, "VOLATILITY",
                        float(minimum_move),
                    )
                    if automatic_type is None:
                        option_types = ["CALL", "PUT"]
                        direction_reason = (
                            f"{direction_reason}；当前先同时展示 Call 和 Put 候选，"
                            "等待方向信号形成后再自动入场。"
                        )
                    else:
                        option_types = [automatic_type]
                # Quote only the nearest usable slice; full chains can contain thousands of contracts.
                chain = chain.sort_values("strike_price").head(600)
                quotes = broker.snapshots_tolerant(chain["code"].astype(str).tolist())
                if quotes.empty:
                    raise ValueError("期权链存在，但没有取得任何可用实时行情。")
                screened_frames = [
                    screen_gamma_contracts(
                        chain, quotes, spot, option_type, int(min_oi), float(max_spread),
                        float(max_moneyness),
                    )
                    for option_type in option_types
                ]
                candidates = pd.concat(
                    [frame for frame in screened_frames if not frame.empty],
                    ignore_index=True,
                ) if any(not frame.empty for frame in screened_frames) else pd.DataFrame()
                screened_count = len(candidates)
                if not candidates.empty:
                    option_history = pd.DataFrame(
                        store.option_history_for_underlying(underlying)
                    )
                    candidates = add_iv_history_percentiles(
                        candidates, option_history, minimum_samples=30,
                    )
                    candidates = rank_long_option_candidates(
                        candidates, spot,
                        realized_volatility=realized_volatility(bars),
                        regime=regime,
                        holding_hours=float(st.session_state.get("volatility_expectancy_horizon_hours", 4)),
                        option_history=option_history,
                        profit_target=float(profit_target), stop_loss=float(stop_loss),
                    )
                    candidates = rank_volatility_contracts(
                        candidates, spot, realized_volatility(bars),
                        signal_active=bool(regime.get("active")),
                        strong_signal=bool(
                            regime.get("strong_signal_override")
                            and regime.get("trigger_confirmed")
                        ),
                        max_premium_per_stage=float(max_premium),
                        contracts_per_stage=int(contracts_per_stage),
                    )
                    structures = build_volatility_structure_candidates(
                        candidates, spot,
                        max_premium_per_stage=float(max_premium),
                        contracts_per_stage=int(contracts_per_stage),
                    )
                    st.session_state.volatility_structure_candidates = structures
                    delta = pd.to_numeric(
                        candidates.get("option_delta"), errors="coerce"
                    ).abs().fillna(0)
                    candidates["候选等级"] = "不合格"
                    watch = (
                        (pd.to_numeric(candidates["iv_rv"], errors="coerce") <= 1.80)
                        & delta.between(0.20, 0.85)
                    )
                    candidates.loc[watch, "候选等级"] = "观察"
                    candidates.loc[candidates["volatility_eligible"], "候选等级"] = "合格"
                st.session_state.volatility_candidates = candidates
                st.session_state.volatility_spot = spot
                st.session_state.volatility_direction_reason = direction_reason
                st.session_state.volatility_regime = regime
                st.session_state.volatility_realized = realized_volatility(bars)
                st.session_state.volatility_candidate_summary = {
                    "到期日数": len(expirations_to_scan),
                    "期权链合约数": len(chain),
                    "取得行情数": len(quotes),
                    "通过基础筛选数": screened_count,
                    "最终合格数": (
                        int(candidates["volatility_eligible"].sum())
                        if not candidates.empty else 0
                    ),
                    "观察候选数": (
                        int((candidates["候选等级"] == "观察").sum())
                        if not candidates.empty else 0
                    ),
                    "方向": " + ".join(option_types),
                }
            if candidates.empty:
                st.warning(
                    "候选生成已完成，但没有合约通过当前基础筛选。"
                    "可适当降低最低未平仓量、扩大最大价差或价内/价外距离。"
                )
            else:
                st.success(f"候选生成完成，共找到 {len(candidates)} 个合约。")
        except Exception as exc:
            st.session_state.volatility_candidate_error = str(exc)
            st.error(f"候选生成失败：{exc}")

    candidates = st.session_state.get("volatility_candidates", pd.DataFrame())
    summary = st.session_state.get("volatility_candidate_summary")
    if summary:
        st.caption(
            "本次生成："
            + " · ".join(f"{key} {value}" for key, value in summary.items())
        )
        regime = st.session_state.get("volatility_regime", {})
        if not regime.get("active", False):
            st.warning(
                f"候选仅供查看，当前动态波动信号尚未激活，自动策略不会入场："
                f"{regime.get('reason', '未满足触发条件')}"
            )
            st.dataframe(pd.DataFrame(regime.get("checks", [])), hide_index=True)
    if not candidates.empty:
        st.metric("正股最新价", f"${st.session_state.volatility_spot:.2f}")
        st.info(st.session_state.get("volatility_direction_reason", ""))
        regime = st.session_state.get("volatility_regime", {})
        with st.container(horizontal=True):
            st.metric("20日波动Z值", f"{regime.get('zscore_20d', 0):.2f}", border=True)
            st.metric("ATR倍数", f"{regime.get('atr_ratio', 0):.2f}", border=True)
            st.metric("量比", f"{regime.get('volume_ratio', 0):.2f}", border=True)
            st.metric("方向分", f"{regime.get('direction_score', 0):+.0f}", border=True)
        visible = [column for column in (
            "候选等级", "code", "model_expected_return", "estimated_profit_probability",
            "volatility_eligible", "volatility_quality_score", "volatility_filter_reason",
            "days_to_expiry", "iv_rv",
            "probability_confidence", "breakeven_price", "gamma_theta_efficiency",
            "option_type", "strike_price", "strike_time", "bid_price", "ask_price",
            "mid_price", "spread_pct", "moneyness_pct", "option_open_interest", "volume",
            "option_implied_volatility", "option_delta", "option_gamma", "option_theta",
            "option_vega", "option_rho", "option_contract_multiplier",
        ) if column in candidates]
        st.caption("优先按波动率合约质量排序：Delta、7–21天期限、Gamma/Theta、IV成本和流动性；末日期权保留但提高门槛。")
        candidate_view = chinese_columns(candidates[visible])
        st.dataframe(candidate_view, hide_index=True, column_config={
            "模型盈利概率": st.column_config.ProgressColumn(
                "模型盈利概率", min_value=0.0, max_value=1.0, format="percent"
            ),
            "模型期望收益": st.column_config.NumberColumn("模型期望收益", format="percent"),
            "盈亏平衡价": st.column_config.NumberColumn("盈亏平衡价", format="$%.2f"),
        })
        structures = st.session_state.get("volatility_structure_candidates", pd.DataFrame())
        if not structures.empty:
            st.subheader("最佳期权表达")
            structure_columns = [column for column in (
                "structure_label", "code", "legs", "structure_net_debit",
                "structure_max_loss", "structure_expected_return", "structure_eligible",
                "structure_filter_reason", "net_gamma", "iv_history_percentile",
            ) if column in structures]
            structure_view = structures[structure_columns].copy()
            if "legs" in structure_view.columns:
                structure_view["legs"] = structure_view["legs"].map(
                    lambda value: json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (list, dict, tuple)) else str(value)
                )
            st.dataframe(
                chinese_columns(structure_view),
                hide_index=True, height=260,
            )
        eligible_codes = candidates.loc[candidates["volatility_eligible"], "code"].tolist()
        selected_code = st.selectbox("选择合约", eligible_codes or candidates["code"].tolist())
        selected = candidates[candidates["code"] == selected_code].iloc[0]
        selected_option_type = str(selected["option_type"])
        cached_realized = float(
            st.session_state.get(
                "volatility_realized",
                selected.get("annual_realized_volatility", 0),
            )
            or 0
        )
        analysis = contract_analysis(
            selected, float(st.session_state.volatility_spot),
            cached_realized, store,
            peer_contracts=candidates,
        )
        with st.container(horizontal=True):
            st.metric(
                "IV 历史百分位",
                f"{analysis['IV历史百分位']:.1%}" if analysis["IV百分位可用"] else "样本不足",
                help=(
                    f"样本来源：{analysis['IV样本来源']}；"
                    f"样本数：{analysis['IV历史样本数']}；"
                    f"置信度：{analysis['IV样本置信度']}。"
                ),
                border=True,
            )
            st.metric("IV / 实现波动", f"{analysis['IV/RV']:.2f}", border=True)
            st.metric("预期波动", f"{analysis['预期波动比例']:.1%}", border=True)
        estimated = float(selected["ask_price"]) * float(
            selected["option_contract_multiplier"]
        ) * int(contracts_per_stage)
        st.metric("本批估算权利金", f"${estimated:,.2f}")
        if not bool(selected.get("volatility_eligible", False)):
            st.error(f"当前没有合格合约：{selected.get('volatility_filter_reason', '')}")
        elif analysis["IV/RV"] > 1.5 or analysis["IV历史百分位"] > .75:
            st.error("IV成本过高：要求 IV/RV ≤ 1.50 且 IV历史百分位 ≤ 75%。")
        elif estimated > max_premium:
            st.error("本批超过最大权利金，不能保存。")
        elif st.button("保存为波动率自动策略", disabled=not admin):
            campaigns = [
                item for item in load_volatility_campaigns(_page_tenant_id)
                if item.get("code") != selected_code
            ]
            campaigns.append({
                "code": selected_code, "underlying": underlying,
                "event_date": date.today().isoformat(), "event_session": "行情触发",
                "option_type": selected_option_type, "contracts_per_stage": int(contracts_per_stage),
                "max_stages": int(stages), "max_premium_per_stage": float(max_premium),
                "entry_hours": int(entry_hours), "profit_target": float(profit_target),
                "stop_loss": float(stop_loss), "trailing_stop": float(trailing_stop),
                "max_position_loss_ratio": float(max_position_loss_ratio),
                "max_position_loss_usd": float(max_position_loss_usd),
                **volatility_exit_parameters,
                "minimum_underlying_move": float(minimum_move),
                "entry_zscore": float(entry_zscore), "cooldown_hours": int(cooldown_hours),
                "regime_profile": st.session_state.get("volatility_regime", {}),
                "entry_iv": float(analysis["隐含波动率"]),
                "entry_gamma_theta_efficiency": float(selected.get("gamma_theta_efficiency", 0)),
                "optimizer_version": 2,
                "enabled": True, "trigger_mode": "VOLATILITY",
                # 显式带租户（P0-9）：否则身份退化成裸 {code}，与 ADOPT 产出的
                # {tenant}|{code} 同 code 并存两条。
                "tenant_id": _page_tenant_id,
                **{
                    column: selected.get(column) for column in (
                        "strike_price", "strike_time", "bid_price", "ask_price", "mid_price",
                        "spread_pct", "moneyness_pct", "option_open_interest", "volume",
                        "option_implied_volatility", "option_delta", "option_gamma",
                        "option_theta", "option_vega", "option_rho", "option_contract_multiplier",
                    ) if column in selected.index
                },
            })
            upsert_campaign(_page_campaigns_key, campaigns[-1])
            st.success("已保存到独立的 volatility_long_gamma 策略。")

    st.subheader("自动策略 · 已保存策略与当前持仓")
    # 列表、清理候选与删除都只认当前页租户（见 campaign_belongs_to_tenant）。
    campaigns = [
        item for item in load_volatility_campaigns(_page_tenant_id)
        if campaign_belongs_to_tenant(item, _page_tenant_id)
    ]
    if campaigns:
        campaign_codes = [item["code"] for item in campaigns]
        try:
            saved_orders = broker.orders()
            saved_positions = broker.positions()
            saved_broker_error = ""
        except BrokerError as exc:
            saved_orders = pd.DataFrame()
            saved_positions = pd.DataFrame()
            saved_broker_error = str(exc)
            st.warning(f"模拟盘持仓/待成交订单读取失败，仅显示 Shadow 持仓：{exc}")
        with st.container(horizontal=True):
            refresh_saved_quotes = st.button(
                "刷新已保存策略行情", icon=":material/refresh:",
                help="按需读取实时行情；不刷新时优先快速展示已保存配置。",
            )
            if st.button("清除行情缓存", icon=":material/delete_sweep:"):
                st.session_state.pop("saved_volatility_live_quotes", None)
                st.session_state.pop("saved_volatility_quotes_time", None)
                st.rerun()
        if refresh_saved_quotes:
            try:
                with st.spinner(f"正在批量读取 {len(campaign_codes)} 条期权行情……"):
                    quote_frame = broker.snapshots(campaign_codes)
                st.session_state.saved_volatility_live_quotes = (
                    quote_frame.set_index("code").to_dict("index")
                    if not quote_frame.empty else {}
                )
                st.session_state.saved_volatility_quotes_time = pd.Timestamp.now(
                    tz="Asia/Shanghai"
                ).strftime("%Y-%m-%d %H:%M:%S")
            except BrokerError as exc:
                st.warning(f"实时行情刷新失败，继续显示保存值：{exc}")
        live_quotes = st.session_state.get("saved_volatility_live_quotes", {})
        if st.session_state.get("saved_volatility_quotes_time"):
            st.caption(
                f"实时行情最近刷新（中国时间）："
                f"{st.session_state.saved_volatility_quotes_time}"
            )
        latest_decisions = {}
        for decision in store.decisions(limit=1000):
            if decision["strategy_id"] == "volatility_long_gamma":
                latest_decisions.setdefault(decision["symbol"], decision)
        rows = []
        strategy_state = load_state()
        shadow_positions = store.shadow_positions_for_strategy("volatility_long_gamma")
        pending_codes = set()
        if not saved_orders.empty:
            pending_rows = saved_orders[
                saved_orders["order_status"].map(broker.is_cancellable_status)
            ].copy()
            pending_codes = set(pending_rows["code"].astype(str)) if not pending_rows.empty else set()
        paper_position_map = {}
        if not saved_positions.empty and "code" in saved_positions.columns:
            for code, group in saved_positions.groupby(saved_positions["code"].astype(str)):
                quantity = float(pd.to_numeric(group.get("qty"), errors="coerce").fillna(0).sum())
                cost_series = pd.to_numeric(group.get("cost_price"), errors="coerce").dropna()
                paper_position_map[code] = {
                    "quantity": quantity,
                    "cost_price": float(cost_series.iloc[0]) if not cost_series.empty else 0.0,
                }
        expired_cleanup_candidates = []
        for campaign in campaigns:
            issues = diagnose_campaign({**campaign, "trigger_mode": "VOLATILITY"}, [])
            shadow_quantity, shadow_cost = shadow_positions.get(campaign["code"], (0.0, 0.0))
            paper_position = paper_position_map.get(campaign["code"], {})
            paper_quantity = float(paper_position.get("quantity", 0.0))
            paper_cost = float(paper_position.get("cost_price", 0.0))
            current_quantity = paper_quantity + float(shadow_quantity)
            open_price = paper_cost if paper_quantity > 0 else float(shadow_cost)
            latest = latest_decisions.get(campaign["code"], {})
            state_key = f"volatility_long_gamma:{campaign['code']}"
            expiry = pd.to_datetime(campaign.get("strike_time"), errors="coerce")
            expired = bool(not pd.isna(expiry) and expiry.date() < date.today())
            invalid_config = bool(hard_campaign_issues(campaign, issues))
            pending = campaign["code"] in pending_codes
            if (expired or invalid_config) and current_quantity <= 0 and not pending:
                expired_cleanup_candidates.append({
                    "code": campaign["code"],
                    # 删除必须用**完整身份**：裸 code 在跨租户同 code 时会命中多个租户，
                    # 被 delete_campaign 的"模糊即拒绝"抛异常（fail-closed），
                    # 结果是"看着有候选、点下去删不掉"。
                    "identity": _campaign_identity(campaign),
                    "tenant": str(campaign.get("tenant_id") or "").strip() or "未标记(legacy)",
                    "expired": expired,
                    "invalid_config": invalid_config,
                    "issues": issues,
                })
            rows.append({
                **campaign, **live_quotes.get(campaign["code"], {}),
                **campaign_time_display(campaign, strategy_state.get(state_key, {})),
                "当前持仓数": current_quantity,
                "开仓价": open_price,
                "模拟盘持仓": paper_quantity,
                "模拟盘成本价": paper_cost,
                "当前 Shadow 持仓": shadow_quantity, "Shadow 平均成本": shadow_cost,
                "租户": str(campaign.get("tenant_id") or "").strip() or "未标记(legacy)",
                "运行状态": latest.get("decision", ""), "最新原因": latest.get("reason", ""),
                "配置状态": "阻断" if hard_campaign_issues(campaign, issues) else "正常",
                "诊断": "；".join(issues),
            })
        saved = translate_common_values(chinese_columns(pd.DataFrame(rows)))
        saved.insert(0, "操作", ":material/delete: 删除")
        saved_columns = [column for column in (
            "操作", "代码", "正股代码", "期权类型", "行权价", "到期日",
            "最新价", "当前持仓数", "开仓价", "买一价", "卖一价",
            "中间价", "买卖价差比例(%)", "价内/价外距离(%)", "未平仓量", "成交量", "隐含波动率",
            "Delta", "Gamma", "Theta", "Vega", "Rho", "合约乘数", "每批合约数", "最大批数",
            "每批最大权利金", "入场窗口（小时）", "止盈比例", "硬止损比例", "峰值回撤退出比例",
            "动态波动Z值门槛", "退出冷却小时", "入场隐含波动率", "入场Gamma/Theta效率",
            "波动率合约质量分", "波动率合约筛选原因", "最近表达复核",
            "当前 Shadow 持仓", "Shadow 平均成本", "租户", "运行状态", "最新原因", "配置状态", "诊断",
            "入场开始时间（中国）", "入场截止时间（中国）",
        ) if column in saved.columns]

        with st.container(border=True):
            st.subheader("过期/失效策略清理")
            st.caption(
                "只会清理**当前页租户**下已经过期或配置失效，且没有模拟盘持仓、没有 Shadow 持仓、"
                "没有待成交订单的波动率策略。有任何风险暴露都会跳过；"
                "跨租户记录既不显示也不会被误删。"
            )
            st.metric("可安全清理数量", len(expired_cleanup_candidates), border=True)
            if saved_broker_error:
                st.warning("当前无法确认模拟盘持仓/待成交订单，已禁止批量清理。")
            if st.button(
                "清理过期/失效且无持仓策略",
                disabled=not admin or bool(saved_broker_error) or not expired_cleanup_candidates,
                icon=":material/cleaning_services:",
            ):
                for item in expired_cleanup_candidates:
                    delete_campaign(_page_campaigns_key, item["identity"])
                store.audit(
                    "VOLATILITY_EXPIRED_CAMPAIGNS_CLEANED",
                    {
                        "count": len(expired_cleanup_candidates),
                        "campaigns": expired_cleanup_candidates,
                        "rule": "expired_or_invalid_and_no_position_no_pending_order",
                    },
                    "volatility_long_gamma",
                 actor="streamlit_admin:panel")
                st.success(f"已清理 {len(expired_cleanup_candidates)} 条过期/失效且无持仓策略。")
                st.rerun()

        def remove_saved_volatility_campaign() -> None:
            if not admin:
                st.session_state["remove_volatility_campaign_error"] = "请先解锁管理员操作。"
                return
            click = st.session_state.get("remove_saved_volatility_campaign")
            if not click:
                return
            code = str(saved.iloc[int(click["row"])]["代码"])
            # 只允许删**当前页租户**的那一条：先按 code 找回本租户的记录，
            # 再用完整身份删除。找不到就拒绝 —— 绝不退化成"按 code 全局删"。
            match = next(
                (item for item in campaigns if str(item.get("code") or "") == code), None,
            )
            if match is None:
                st.session_state["remove_volatility_campaign_error"] = (
                    f"{code} 不属于当前页租户（{_page_tenant_id or '未标记'}），"
                    "已禁止删除 —— 跨租户记录不可在本页操作"
                )
                return
            identity = _campaign_identity(match)
            shadow_quantity, _ = store.shadow_position("volatility_long_gamma", code)
            if shadow_quantity > 0:
                st.session_state["remove_volatility_campaign_error"] = (
                    f"{code} 仍有 {shadow_quantity:g} 张 Shadow 持仓，请先完成退出。"
                )
                return
            try:
                orders = broker.orders()
                positions = broker.positions()
                pending = orders[
                    (orders["code"].astype(str) == code)
                    & orders["order_status"].map(broker.is_cancellable_status)
                ] if not orders.empty else orders
            except BrokerError as exc:
                st.session_state["remove_volatility_campaign_error"] = (
                    f"无法确认 {code} 是否存在待成交订单：{exc}"
                )
                return
            paper_rows = positions[positions["code"].astype(str) == code] if not positions.empty else positions
            paper_quantity = float(paper_rows["qty"].sum()) if not paper_rows.empty else 0
            if paper_quantity > 0:
                st.session_state["remove_volatility_campaign_error"] = (
                    f"{code} 仍有 {paper_quantity:g} 张模拟盘持仓，请先平仓后再删除策略。"
                )
                return
            if not pending.empty:
                st.session_state["remove_volatility_campaign_error"] = (
                    f"{code} 仍有 {len(pending)} 笔待成交订单，请先到订单管理撤单。"
                )
                return
            delete_campaign(_page_campaigns_key, identity)
            store.audit(
                "VOLATILITY_CAMPAIGN_DELETED",
                {"code": code, "identity": identity, "source": "saved_campaign_row"},
                "volatility_long_gamma",
             actor="streamlit_admin:panel")
            st.session_state["removed_volatility_campaign"] = code

        st.dataframe(saved[saved_columns], hide_index=True, height=520, column_config={
            "操作": st.column_config.ButtonColumn(
                "操作", pinned=True, type="tertiary",
                on_click=remove_saved_volatility_campaign,
                key="remove_saved_volatility_campaign",
            ),
        })
        removed_campaign = st.session_state.pop("removed_volatility_campaign", None)
        if removed_campaign:
            st.success(f"{removed_campaign} 已从已保存的波动率策略中删除。")
        remove_error = st.session_state.pop("remove_volatility_campaign_error", None)
        if remove_error:
            st.error(remove_error)

        st.subheader("清空并重启标的")
        if not admin:
            st.caption("解锁管理员后才能执行清空和重启。")
        elif not campaigns:
            st.info("尚未保存波动率期权策略，无法重启。")
        else:
            restart_targets = {
                f"{item['code']} · {item.get('underlying', '')}": item for item in campaigns
            }
            selected_restart_label = st.selectbox(
                "选择要清空并重启的标的", list(restart_targets.keys()),
            )
            selected_restart_campaign = restart_targets[selected_restart_label]
            restart_reason = st.text_input(
                "重启原因", placeholder="例如：重新按新追价规则运行"
            )

            def clear_and_restart_volatility_campaign() -> None:
                if not restart_reason.strip():
                    st.session_state["restart_volatility_error"] = "请先填写重启原因。"
                    return
                code = str(selected_restart_campaign["code"])
                underlying_code = str(selected_restart_campaign.get("underlying", ""))
                cleared = store.clear_symbol_activity(
                    "volatility_long_gamma",
                    code,
                    # 铁律：**写侧用哪种形态，清理侧就必须认得哪种。**
                    # 状态键现在有两种形态（带租户=新写入 / 不带租户=历史存量），
                    # 走共享助手一次性取全，不在页面里手拼键形态，否则漏一种就清不干净。
                    sorted(volatility_state_keys(code, _page_tenant_id, underlying_code)) + [
                        f"volatility_cooldown:{underlying_code}",
                        f"volatility_cooldown_hours:{underlying_code}",
                    ],
                )
                store.audit(
                    "VOLATILITY_CAMPAIGN_CLEARED_AND_RESTARTED",
                    {
                        "code": code, "underlying": underlying_code,
                        "reason": restart_reason, "cleared": cleared,
                    },
                    "volatility_long_gamma",
                 actor="streamlit_admin:panel")
                try:
                    run_volatility_once()
                except Exception as exc:
                    st.session_state["restart_volatility_error"] = (
                        f"{code} 已清空，但重启执行失败：{exc}"
                    )
                    return
                st.session_state["restart_volatility_success"] = (
                    f"{code} 已清空并重新运行一次。"
                )

            if st.button(
                "清空并重启该标的", type="primary",
                disabled=not admin or not restart_reason.strip(),
            ):
                clear_and_restart_volatility_campaign()

        restart_success = st.session_state.pop("restart_volatility_success", None)
        if restart_success:
            st.success(restart_success)
        restart_error = st.session_state.pop("restart_volatility_error", None)
        if restart_error:
            st.error(restart_error)
    else:
        st.info("尚未保存波动率期权策略。")

    st.subheader("执行诊断 · 独立运行与调试")
    mode = store.get_setting("strategy_execution_mode:volatility_long_gamma", "") or "INHERIT"
    enabled = store.get_setting("strategy_enabled:Volatility Long Gamma", "false") == "true"
    with st.container(horizontal=True):
        st.metric("执行模式", mode, border=True)
        st.metric("自动调度", "已启用" if enabled else "已关闭", border=True)
    st.page_link(
        "pages/15_strategy_control_center.py", label="前往策略与资金控制设置波动率策略",
        icon=":material/tune:",
    )
    if st.button("立即运行一次波动率策略", disabled=not admin or not enabled or not campaigns):
        try:
            run_volatility_once()
            st.success("单次运行完成。")
        except Exception as exc:
            st.error(f"运行失败：{exc}")

    decisions = pd.DataFrame([
        row for row in store.decisions(limit=300) if row["strategy_id"] == "volatility_long_gamma"
    ])
    if not decisions.empty:
        def decision_detail(value: str) -> pd.Series:
            payload = json.loads(value or "{}")
            return pd.Series({
                "当前持仓": payload.get("position_quantity", 0),
                "计划下单": payload.get("planned_order_quantity", 0),
                "持仓来源": {
                    "SHADOW_LEDGER": "Shadow 虚拟成交账本",
                    "FUTU_ACCOUNT": "富途模拟账户",
                }.get(payload.get("position_source"), "历史执行记录"),
                "盈亏比例": payload.get("pnl_ratio"),
                "峰值回撤": payload.get("drawdown"),
                "校验提醒": "；".join(payload.get("validation_warnings", [])),
                "自动方向依据": payload.get("direction_reason", "历史记录未保存"),
                "波动Z值": (payload.get("volatility_regime") or {}).get("zscore_20d"),
                "ATR倍数": (payload.get("volatility_regime") or {}).get("atr_ratio"),
                "量比": (payload.get("volatility_regime") or {}).get("volume_ratio"),
                "短期实现波动": (payload.get("volatility_regime") or {}).get("rv10"),
                "长期实现波动": (payload.get("volatility_regime") or {}).get("rv60"),
                "入场开始（中国）": pd.to_datetime(
                    payload.get("entry_start"), errors="coerce", utc=True
                ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("entry_start") else "",
                "入场截止（中国）": pd.to_datetime(
                    payload.get("event_time"), errors="coerce", utc=True
                ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("event_time") else "",
            })

        details = decisions["payload_json"].apply(decision_detail)
        decision_display = pd.concat([decisions.drop(columns=["payload_json"]), details], axis=1)
        localized_decisions = translate_common_values(china_time_columns(decision_display).rename(columns={
            "symbol": "期权代码", "decision": "决策", "reason": "原因",
            "created_at": "时间（中国）",
        }).drop(columns=["id", "strategy_id"], errors="ignore"))
        st.dataframe(localized_decisions, hide_index=True, height=520,
            column_config={
                "盈亏比例": st.column_config.NumberColumn(format="percent"),
                "峰值回撤": st.column_config.NumberColumn(format="percent"),
            })
