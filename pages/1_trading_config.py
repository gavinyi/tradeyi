from __future__ import annotations

from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError
from futu_quant.config import (
    Settings, normalize_us_symbol, save_core_satellite_settings, save_core_satellite_symbols,
    save_gamma_symbols, save_trading_preferences, save_volatility_symbols,
)
from futu_quant.core_satellite import PROFILES
from futu_quant.core_validation import (
    disable_core_daily_validation,
    enable_core_daily_validation,
    load_core_daily_validation,
)
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import (
    chinese_columns, render_admin_login, render_sidebar, get_current_tenant, get_tenant_broker,
)


st.set_page_config(page_title="交易配置", page_icon="⚙️", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
current_tenant = get_current_tenant(settings)
broker = get_tenant_broker(settings)
store = TradingStore()
admin = render_admin_login()

st.title(f"{current_tenant.get('short_name', '租户')}交易配置")
st.caption(f"当前操作租户：**{current_tenant.get('label')}** · 管理标的池与每次委托参数。")


@st.cache_data(ttl=3600, show_spinner="正在从富途载入美股证券目录……")
def load_catalog() -> pd.DataFrame:
    return broker.security_catalog()


def display_name(row: pd.Series) -> str:
    name = row.get("name", "")
    return f"{row['code']} · {name}" if name else str(row["code"])


def fuzzy_search(catalog: pd.DataFrame, query: str, limit: int = 30) -> pd.DataFrame:
    needle = query.strip().upper()
    if not needle:
        return catalog.head(0)

    def score(row: pd.Series) -> float:
        code = str(row.get("code", "")).upper()
        short_code = code.removeprefix("US.")
        name = str(row.get("name", "")).upper()
        searchable = f"{code} {short_code} {name}"
        if needle == short_code or needle == code:
            return 1000
        if short_code.startswith(needle):
            return 800 - len(short_code)
        if needle in searchable:
            return 600 - searchable.index(needle)
        return max(
            SequenceMatcher(None, needle, short_code).ratio(),
            SequenceMatcher(None, needle, name).ratio(),
        ) * 100

    result = catalog.copy()
    result["_score"] = result.apply(score, axis=1)
    return result[result["_score"] >= 35].nlargest(limit, "_score").drop(columns="_score")


current_tab, search_tab = st.tabs(
    ["当前配置", "查找并添加"], default="当前配置",
    key="trading_config_tab", on_change="rerun",
)

st.caption("使用建议：日常查看和调整 Core Satellite 在“当前配置”；新增标的时再进入“查找并添加”。各策略标的列表相互独立。")

catalog = pd.DataFrame()
if search_tab.open:
    try:
        catalog = load_catalog()
    except BrokerError as exc:
        with search_tab:
            st.error(f"无法载入证券目录：{exc}")

with search_tab:
    mode = st.radio("查询方式", ["模糊查询", "代码精确查询"], horizontal=True)
    destination = st.radio(
        "添加到", ["普通策略", "Core Satellite", "财报 Gamma", "波动率期权"], horizontal=True,
        help="各策略标的列表相互独立。",
    )
    query = st.text_input(
        "证券代码或名称",
        placeholder="例如 AAPL、苹果、Tesla、NVDA",
        help="模糊查询支持代码片段和证券名称；精确查询支持 AAPL 或 US.AAPL。",
    )
    results = pd.DataFrame()
    if query and search_tab.open and not catalog.empty:
        if mode == "代码精确查询":
            try:
                exact_code = normalize_us_symbol(query)
                results = catalog[catalog["code"].str.upper() == exact_code]
                if results.empty:
                    st.warning(f"富途证券目录中没有找到 {exact_code}")
            except ValueError as exc:
                st.warning(str(exc))
        else:
            results = fuzzy_search(catalog, query)
            if results.empty:
                st.warning("没有匹配结果，请换一个代码片段或名称。")

    if not results.empty:
        options = results.index.tolist()
        selected_index = st.selectbox(
            f"匹配结果（{len(results)}）",
            options,
            format_func=lambda index: display_name(results.loc[index]),
        )
        visible_columns = [
            column for column in ["code", "name", "stock_type", "lot_size", "listing_date"]
            if column in results.columns
        ]
        st.dataframe(chinese_columns(results[visible_columns]), hide_index=True)
        selected_code = str(results.loc[selected_index, "code"])
        if st.button(f"添加 {selected_code} 到 {destination}", type="primary", disabled=not admin):
            if destination == "Core Satellite":
                updated = tuple(dict.fromkeys((*settings.core_satellite_symbols, selected_code)))
                save_core_satellite_symbols(updated)
            elif destination == "财报 Gamma":
                updated = tuple(dict.fromkeys((*settings.gamma_symbols, selected_code)))
                save_gamma_symbols(updated)
            elif destination == "波动率期权":
                updated = tuple(dict.fromkeys((*settings.volatility_symbols, selected_code)))
                save_volatility_symbols(updated)
            else:
                updated = tuple(dict.fromkeys((*settings.watchlist, selected_code)))
                quantities = dict(settings.order_quantities)
                quantities.setdefault(selected_code, settings.order_quantity)
                save_trading_preferences(updated, quantities)
            st.success(f"已添加 {selected_code} 到 {destination}")
            st.cache_data.clear()
            st.rerun()

with current_tab:
    st.subheader("Core Satellite 风险档案")
    core_profile = st.selectbox(
        "参数档案", list(PROFILES), index=list(PROFILES).index(settings.core_satellite_profile),
        format_func=lambda key: (
            f"{PROFILES[key]['label']} · 股票上限 {PROFILES[key]['max_stock_weight']:.0%} · "
            f"基础仓位因子 {PROFILES[key]['base_factor']:.0%}"
        ),
    )
    frequency = st.selectbox(
        "正常调仓频率", ["daily", "weekly", "monthly"],
        index=["daily", "weekly", "monthly"].index(settings.core_satellite_rebalance_frequency),
        format_func={"daily": "每日", "weekly": "每周", "monthly": "每月"}.get,
    )
    tolerance = st.number_input(
        "绝对仓位容忍带", 0.0, 0.20, settings.core_satellite_weight_tolerance,
        step=0.005, format="%.3f",
    )
    min_trade = st.number_input(
        "最低交易金额（美元）", 0.0, 1_000_000.0,
        settings.core_satellite_min_trade_usd, step=100.0,
    )
    with st.container(border=True):
        st.subheader("Core / Satellite / Cash 预算")
        st.caption("Core 默认识别 SPY、VOO、QQQ；其他 Core Satellite 标的归为 Satellite。")
        core_budget = st.number_input(
            "Core 长期底仓预算比例", 0.0, 0.85,
            settings.core_satellite_core_budget, step=0.01, format="%.2f",
        )
        satellite_budget = st.number_input(
            "Satellite 增强仓预算比例", 0.0, 0.50,
            settings.core_satellite_satellite_budget, step=0.01, format="%.2f",
        )
        cash_buffer = st.number_input(
            "现金缓冲比例", 0.0, 0.50, settings.core_satellite_cash_buffer,
            step=0.01, format="%.2f",
        )
        total_budget = core_budget + satellite_budget + cash_buffer
        with st.container(horizontal=True):
            st.metric("Core预算", f"{core_budget:.0%}", border=True)
            st.metric("Satellite预算", f"{satellite_budget:.0%}", border=True)
            st.metric("现金缓冲", f"{cash_buffer:.0%}", border=True)
            st.metric("合计", f"{total_budget:.0%}", border=True)
        if total_budget > 0.95:
            st.error("Core + Satellite + 现金缓冲合计不能超过 95%。")

    with st.container(border=True):
        st.subheader("Core Satellite 执行节奏")
        st.caption("这些参数只控制调仓速度，不改变预算和标的列表。")
        max_single_rebalance_pct = st.number_input(
            "单次最多完成目标差额比例", 0.05, 1.0,
            settings.core_satellite_max_single_rebalance_pct,
            step=0.05, format="%.2f",
        )
        max_target_change = st.number_input(
            "单次目标权重变化上限", 0.01, 0.20,
            settings.core_satellite_max_target_change,
            step=0.01, format="%.2f",
            help="限制单个标的目标权重每次最多变化多少，降低一次性追涨或急速减仓。",
        )
        max_order_notional = st.number_input(
            "单笔计划最大金额（美元，0 表示不限制）", 0.0, 1_000_000.0,
            settings.core_satellite_max_order_notional, step=1000.0,
        )
        high_vol_threshold = st.number_input(
            "高波动阈值（SPY 20日收益标准差）", 0.0, 0.20,
            settings.core_satellite_high_volatility_threshold,
            step=0.0025, format="%.4f",
        )
        high_vol_factor = st.number_input(
            "高波动时单次调仓比例", 0.05, 1.0,
            settings.core_satellite_high_volatility_trade_factor,
            step=0.05, format="%.2f",
        )
        satellite_daily_scan = st.toggle(
            "Satellite 每日机会扫描",
            value=settings.core_satellite_satellite_daily_scan,
            help="正式调仓频率未到时，只扫描 Satellite 标的；Core 底仓不参与交易。",
        )
        satellite_min_score = st.number_input(
            "Satellite 每日机会最低评分", 0.0, 6.5,
            settings.core_satellite_satellite_min_score,
            step=0.5, format="%.1f",
            help="分数越高越严格。建议正式模拟阶段使用 4.0，避免弱信号频繁换仓。",
        )
    if st.button("保存 Core Satellite 配置", disabled=not admin):
        save_core_satellite_settings(
            core_profile, frequency, tolerance, min_trade,
            core_budget, satellite_budget, cash_buffer,
            max_single_rebalance_pct, max_order_notional,
            high_vol_threshold, high_vol_factor,
            satellite_daily_scan, satellite_min_score, max_target_change,
        )
        st.success("档案已保存；请重启正在运行的策略进程。")
        st.rerun()

    validation = load_core_daily_validation(store)
    with st.container(border=True):
        st.subheader("每日加速验证模式")
        st.caption(
            "独立覆盖每日调仓、验证容忍带和最低交易金额；不会改写上方正式参数。"
            "完成指定交易日或达到安全期限后，系统会自动恢复正式频率。"
        )
        status_col, progress_col, orders_col = st.columns(3)
        status_col.metric(
            "当前状态", "运行中" if validation.active else "未运行",
            border=True,
        )
        progress_col.metric(
            "验证进度",
            f"{len(validation.completed_sessions)}/{validation.target_sessions} 个交易日",
            border=True,
        )
        orders_col.metric("计划指令累计", validation.total_orders, border=True)
        if validation.active:
            expires = (
                validation.expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m-%d %H:%M"
                )
                if validation.expires_at else "—"
            )
            st.warning(
                f"验证模式生效中：每日检查，容忍带 {validation.weight_tolerance:.2%}，"
                f"最低交易金额 ${validation.min_trade_usd:,.0f}；"
                f"安全截止（中国时间）{expires}。"
                f"已保存的正式频率仍为 {settings.core_satellite_rebalance_frequency}。"
            )
        else:
            st.info(validation.reason)

        with st.form("core_daily_validation_form", border=True):
            validation_days = st.number_input(
                "验证交易日数", min_value=1, max_value=30,
                value=validation.target_sessions, step=1,
                help="同一美股交易日重复执行只计一次。建议先运行 5–10 个交易日。",
            )
            validation_tolerance = st.number_input(
                "验证容忍带", min_value=0.0, max_value=0.20,
                value=float(validation.weight_tolerance), step=0.0025, format="%.4f",
            )
            validation_min_trade = st.number_input(
                "验证最低交易金额（美元）", min_value=0.0, max_value=1_000_000.0,
                value=float(validation.min_trade_usd), step=50.0,
            )
            start_validation = st.form_submit_button(
                "启动新的每日验证批次", type="primary",
                icon=":material/speed:", disabled=not admin,
            )
        if start_validation:
            try:
                enable_core_daily_validation(
                    store,
                    target_sessions=int(validation_days),
                    weight_tolerance=float(validation_tolerance),
                    min_trade_usd=float(validation_min_trade),
                )
                st.success("每日加速验证已启动；统一服务下一个 Core 调度窗口自动生效。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

        if validation.enabled and st.button(
            "停止验证并立即恢复正式参数",
            icon=":material/stop_circle:", disabled=not admin,
        ):
            disable_core_daily_validation(store)
            st.success("验证已停止，Core Satellite 已恢复正式参数。")
            st.rerun()
        st.page_link(
            "pages/15_strategy_control_center.py",
            label="前往策略与资金控制检查 Core 执行模式",
            icon=":material/tune:",
        )

    st.subheader("Core Satellite 组合标的")
    st.caption("SPY 风险基准会自动加载；只有下列标的参与仓位分配。")
    core_symbols = st.multiselect(
        "取消选择后保存即可删除", settings.core_satellite_symbols,
        default=settings.core_satellite_symbols,
    )
    if st.button("保存 Core Satellite 标的", disabled=not admin):
        try:
            save_core_satellite_symbols(tuple(core_symbols))
            st.cache_data.clear()
            st.success("Core Satellite 标的已保存；请重启策略进程。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.subheader("财报 Gamma 策略标的")
    st.caption("这套列表只用于财报 Gamma 期权策略，不影响普通策略和 Core Satellite。")
    gamma_symbols = st.multiselect(
        "取消选择后保存即可删除", settings.gamma_symbols,
        default=settings.gamma_symbols, key="gamma_symbols",
    )
    if st.button("保存财报 Gamma 标的", disabled=not admin):
        try:
            save_gamma_symbols(tuple(gamma_symbols))
            st.cache_data.clear()
            st.success("财报 Gamma 标的已保存。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.subheader("波动率期权策略标的")
    st.caption("独立用于 Volatility Long Gamma，不依赖财报日历。")
    volatility_symbols = st.multiselect(
        "取消选择后保存即可删除", settings.volatility_symbols,
        default=settings.volatility_symbols, key="volatility_symbols",
    )
    if st.button("保存波动率期权标的", disabled=not admin):
        try:
            save_volatility_symbols(tuple(volatility_symbols))
            st.cache_data.clear()
            st.success("波动率期权标的已保存。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.subheader("各标的下单数量")
    st.caption("每行独立配置。取消“启用”后保存，即可从策略标的中删除。")
    configuration = pd.DataFrame(
        {
            "启用": [True] * len(settings.watchlist),
            "代码": list(settings.watchlist),
            "下单数量": [settings.quantity_for(symbol) for symbol in settings.watchlist],
        }
    )
    edited = st.data_editor(
        configuration,
        hide_index=True,
        disabled=["代码"],
        column_config={
            "启用": st.column_config.CheckboxColumn("启用"),
            "代码": st.column_config.TextColumn("代码"),
            "下单数量": st.column_config.NumberColumn(
                "下单数量（股）", min_value=1, max_value=10_000, step=1, required=True
            ),
        },
    )
    if st.button("保存配置", type="primary", disabled=not admin):
        try:
            enabled = edited[edited["启用"]]
            symbols = tuple(enabled["代码"].astype(str))
            quantities = {
                str(row["代码"]): int(row["下单数量"]) for _, row in enabled.iterrows()
            }
            save_trading_preferences(symbols, quantities)
            st.success("配置已保存。请重启正在运行的策略进程使其生效。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

st.divider()
