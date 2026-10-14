from __future__ import annotations

import os

import pandas as pd
import streamlit as st

os.environ.setdefault("FUTU_LOG_ROLE", "streamlit")

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.core_satellite import STRATEGY_NAME, build_rebalance_plan
from futu_quant.home_dashboard import render_home_dashboard
from futu_quant.option_lifecycle import parse_option_code
from futu_quant.performance import performance_report
from futu_quant.strategies import STRATEGIES
from futu_quant.store import TradingStore
from futu_quant.trade_diagnostics import (
    build_diagnostic_report, diagnostic_window, latest_us_session_date,
)
from futu_quant.ui import china_time_columns, chinese_columns, render_admin_login, render_sidebar


st.set_page_config(
    page_title="Futu Paper Quant",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="auto",
)


dashboard_page = st.Page(
    lambda: None,
    title="首页驾驶舱",
    icon=":material/space_dashboard:",
    default=True,
)
account_page = st.Page(
    lambda: None,
    title="账户总览",
    icon=":material/account_balance_wallet:",
    url_path="account-overview",
)
navigation = {
    "账户与交易": [
        dashboard_page,
        account_page,
        st.Page("pages/29_strategy_review_execution_quality.py", title="策略复盘与执行质量", icon=":material/summarize:"),
        st.Page("pages/26_trade_diagnostics_center.py", title="交易诊断中心", icon=":material/troubleshoot:"),
        st.Page("pages/4_order_management.py", title="订单管理", icon=":material/receipt_long:"),
        st.Page("pages/12_trade_lifecycle.py", title="交易生命周期", icon=":material/account_tree:"),
        st.Page("pages/30_performance_attribution.py", title="收益与交易成本", icon=":material/monitoring:"),
    ],
    "行情与资讯": [
        st.Page("pages/23_market_overview.py", title="市场与自选行情", icon=":material/monitoring:"),
        st.Page("pages/24_options_market.py", title="期权市场", icon=":material/travel_explore:"),
        st.Page("pages/25_events_announcements.py", title="事件与公告", icon=":material/event_note:"),
    ],
    "策略中心": [
        st.Page("pages/1_trading_config.py", title="交易配置", icon=":material/settings:"),
        st.Page("pages/15_strategy_control_center.py", title="策略与资金控制", icon=":material/tune:"),
        st.Page("pages/11_strategy_activity_center.py", title="策略活动中心", icon=":material/hub:"),
        st.Page("pages/3_stock_backtest.py", title="股票策略回测", icon=":material/query_stats:"),
    ],
    "期权中心": [
        st.Page("pages/30_volatility_strategy_overview.py", title="波动率策略总览", icon=":material/dashboard:"),
        st.Page("pages/39_universe_incubator_center.py", title="标的孵化与赛马中心", icon=":material/rocket_launch:"),
        st.Page("pages/2_options_config.py", title="期权配置", icon=":material/settings_suggest:"),
        st.Page("pages/5_earnings_gamma_strategy.py", title="财报 Gamma 策略", icon=":material/bolt:"),
        st.Page("pages/10_volatility_options_strategy.py", title="波动率策略高级配置", icon=":material/waves:"),
        st.Page("pages/28_options_pnl_ledger.py", title="期权盈亏台账", icon=":material/price_check:"),
        st.Page("pages/16_options_combo_execution.py", title="期权组合执行", icon=":material/schema:"),
        st.Page("pages/13_options_data_backtest.py", title="期权数据回测", icon=":material/analytics:"),
    ],
    "风控与审批": [
        st.Page("pages/7_risk_center.py", title="风控中心", icon=":material/shield:"),
        st.Page("pages/27_live_trading_control.py", title="实盘交易控制", icon=":material/lock_open:"),
        st.Page("pages/14_portfolio_risk.py", title="组合风险", icon=":material/donut_large:"),
        st.Page("pages/9_launch_approval.py", title="上线审批", icon=":material/approval:"),
        st.Page("pages/17_strategy_validation_center.py", title="策略验证中心", icon=":material/fact_check:"),
        st.Page("pages/20_long_term_validation.py", title="长期验证", icon=":material/timeline:"),
    ],
    "研究与系统": [
        st.Page("pages/21_research_evidence_center.py", title="研究与证据中心", icon=":material/lab_profile:"),
        st.Page("pages/22_data_research_center.py", title="数据研究中心", icon=":material/science:"),
        st.Page("pages/6_system_monitor.py", title="系统监控", icon=":material/monitoring:"),
        st.Page("pages/8_runtime_log.py", title="运行日志", icon=":material/article:"),
        st.Page("pages/19_stability_operations_center.py", title="稳定性运营中心", icon=":material/monitor_heart:"),
        st.Page("pages/18_infrastructure_disaster_recovery.py", title="基础设施与灾备", icon=":material/dns:"),
    ],
}
if os.getenv("ENABLE_CRYPTO_MODULE", "false").lower() == "true":
    navigation["币量化"] = [
        st.Page("pages/31_crypto_quant_overview.py", title="币量化总览", icon=":material/currency_bitcoin:"),
        st.Page("pages/34_okx_account_overview.py", title="欧易账户总览", icon=":material/account_balance_wallet:"),
        st.Page("pages/33_crypto_strategy_review.py", title="策略复盘与执行质量", icon=":material/summarize:"),
        st.Page("pages/32_crypto_strategy_config_backtest.py", title="币策略配置与回测", icon=":material/candlestick_chart:"),
        st.Page("pages/38_futures_quant_overview.py", title="合约量化总览", icon=":material/radar:"),
        st.Page("pages/35_futures_account_overview.py", title="合约账户总览", icon=":material/account_balance:"),
        st.Page("pages/36_perpetual_futures_strategy.py", title="永续合约策略", icon=":material/swap_vert:"),
        st.Page("pages/37_futures_strategy_review.py", title="合约策略复盘", icon=":material/assessment:"),
    ]
current_page = st.navigation(navigation, position="sidebar")
current_page.run()
if current_page not in (dashboard_page, account_page):
    st.stop()

try:
    settings = Settings.load()
    broker = FutuPaperBroker(settings)
    store = TradingStore()
except Exception as exc:
    st.error(str(exc))
    st.stop()

render_sidebar(settings)
admin = render_admin_login() if current_page == account_page else False

if current_page == dashboard_page:
    render_home_dashboard(settings, broker, store, account_page=account_page)
    st.stop()


@st.cache_data(ttl=30, max_entries=3, show_spinner=False)
def load_home_diagnostics():
    return build_diagnostic_report(
        TradingStore(), diagnostic_window(latest_us_session_date()),
    )


st.title("Futu 美股模拟量化")
latest_diagnostics = load_home_diagnostics()
diagnostic_summary = latest_diagnostics["summary"]
diagnostic_problems = 0 if diagnostic_summary.empty else int(
    diagnostic_summary["最终结果"].isin({
        "已拒绝", "提交失败", "未形成订单", "候选被拒绝", "被阻断",
    }).sum()
)
with st.container(border=True):
    st.subheader("昨夜运行结果")
    with st.container(horizontal=True):
        st.metric("触发标的", latest_diagnostics["metrics"]["触发标的"], border=True)
        st.metric("订单意图", latest_diagnostics["metrics"]["订单意图"], border=True)
        st.metric("异常链路", diagnostic_problems, border=True)
        st.metric("Shadow 成交", latest_diagnostics["metrics"]["Shadow成交"], border=True)
    if diagnostic_problems:
        st.warning(
            f"发现 {diagnostic_problems} 条未下单、被拒绝或失败记录，点击下方入口查看最终原因。",
            icon=":material/warning:",
        )
    else:
        st.caption("当前交易日没有发现阻断或失败记录。")
    st.page_link(
        "pages/26_trade_diagnostics_center.py", label="打开交易诊断中心",
        icon=":material/troubleshoot:",
    )
with st.container(border=True):
    st.subheader("推荐工作入口")
    st.caption("如果你今天主要是调策略、看证据、查问题，先打开这里。")
    st.page_link(
        "pages/21_research_evidence_center.py",
        label="打开研究与证据中心",
        icon=":material/lab_profile:",
    )
if st.button("刷新账户", icon=":material/refresh:"):
    st.cache_data.clear()


@st.cache_data(ttl=30, max_entries=2, show_spinner=False)
def load_account(include_history: bool = False):
    # Keep the common account page responsive: historical orders are opt-in.
    current = (broker.accounts(), broker.account_info(), broker.positions(), broker.orders())
    history = broker.orders(True) if include_history else pd.DataFrame()
    return (*current, history)


@st.cache_data(ttl=30, show_spinner=False)
def load_strategy_pnl():
    return performance_report(TradingStore(), FutuPaperBroker(Settings.load()))


def is_option_symbol(symbol: object) -> bool:
    return parse_option_code(str(symbol)) is not None


def numeric_sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def position_amount(frame: pd.DataFrame) -> float:
    for column in ("market_val", "val", "amount"):
        if column in frame.columns:
            return numeric_sum(frame, column)
    if {"qty", "nominal_price"}.issubset(frame.columns):
        qty = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
        price = pd.to_numeric(frame["nominal_price"], errors="coerce").fillna(0.0)
        return float((qty * price).sum())
    return 0.0


def option_position_display(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep the option P/L and cost fields adjacent in the account overview."""
    display = chinese_columns(frame)
    if display.empty:
        return display
    columns = display.columns.tolist()
    if "盈亏比例" not in columns:
        return display
    insert_at = columns.index("盈亏比例") + 1
    for column in ("盈亏金额", "成本价"):
        if column in columns:
            columns.remove(column)
            columns.insert(insert_at, column)
            insert_at += 1
    return display[columns]


def shadow_positions_frame(store: TradingStore) -> pd.DataFrame:
    frame = pd.DataFrame(store.shadow_open_positions())
    if frame.empty:
        return frame
    frame["asset_class"] = frame["symbol"].map(
        lambda value: "期权" if is_option_symbol(value) else "股票/ETF"
    )
    frame["notional"] = (
        pd.to_numeric(frame["quantity"], errors="coerce").fillna(0.0)
        * pd.to_numeric(frame["average_cost"], errors="coerce").fillna(0.0)
        * frame["symbol"].map(lambda value: 100 if is_option_symbol(value) else 1)
    )
    return frame


try:
    accounts, info, positions, orders, history = load_account(False)
    if info.empty or positions.empty:
        cached_accounts = store.broker_snapshot("accounts")
        cached_orders = store.broker_snapshot("orders")
        if accounts.empty and cached_accounts:
            accounts = pd.DataFrame(cached_accounts["payload"])
        if orders.empty and cached_orders:
            orders = pd.DataFrame(cached_orders["payload"])
    if info.empty or positions.empty:
        cached_info = store.broker_snapshot("account_info")
        cached_positions = store.broker_snapshot("positions")
        if info.empty and cached_info:
            info = pd.DataFrame(cached_info["payload"])
        if positions.empty and cached_positions:
            positions = pd.DataFrame(cached_positions["payload"])
        if info.empty or positions.empty:
            st.warning("OpenD 当前不可用，暂无账户快照；请稍后重试。")
    st.success(f"已连接富途 OpenD；发现 {len(accounts)} 个美股模拟账户")
    if not positions.empty and "code" in positions.columns:
        option_mask = positions["code"].map(is_option_symbol)
        option_positions = positions[option_mask].copy()
        stock_positions = positions[~option_mask].copy()
    else:
        option_positions = pd.DataFrame()
        stock_positions = positions.copy()
    shadow_positions = shadow_positions_frame(store)
    shadow_options = (
        shadow_positions[shadow_positions["asset_class"] == "期权"].copy()
        if not shadow_positions.empty else pd.DataFrame()
    )
    shadow_stocks = (
        shadow_positions[shadow_positions["asset_class"] == "股票/ETF"].copy()
        if not shadow_positions.empty else pd.DataFrame()
    )
    if not info.empty:
        row = info.iloc[0]

        # 仅将 qty > 0 的记录视为当前在仓；券商当天已平仓的零数量记录不计入持仓数
        active_opt_mask = (
            pd.to_numeric(option_positions.get("qty", 0), errors="coerce").fillna(0) > 0
            if not option_positions.empty else pd.Series(dtype=bool)
        )
        active_option_positions = (
            option_positions[active_opt_mask].copy()
            if not option_positions.empty else pd.DataFrame()
        )
        active_stock_mask = (
            pd.to_numeric(stock_positions.get("qty", 0), errors="coerce").fillna(0) > 0
            if not stock_positions.empty else pd.Series(dtype=bool)
        )
        active_stock_positions = (
            stock_positions[active_stock_mask].copy()
            if not stock_positions.empty else pd.DataFrame()
        )

        stock_today_pnl = numeric_sum(stock_positions, "today_pl_val")
        stock_total_pnl = numeric_sum(stock_positions, "pl_val")
        stock_mv = position_amount(active_stock_positions)
        opt_today_pnl = numeric_sum(option_positions, "today_pl_val")
        opt_total_pnl = numeric_sum(option_positions, "pl_val")
        opt_mv = position_amount(active_option_positions)
        total_today_pnl = stock_today_pnl + opt_today_pnl
        total_pos_pnl = stock_total_pnl + opt_total_pnl

        # 账户总览：只放真正的账户级指标
        with st.container(border=True):
            st.markdown("### 量化系统驾驶舱")
            st.caption("账户资产、股票底仓与期权战区分开展示；所有数值均来自富途模拟账户快照。")
            with st.container(horizontal=True):
                st.metric("总资产权益", f"${float(row.get('total_assets', 0)):,.2f}", border=True)
                st.metric("现金余额", f"${float(row.get('cash', 0)):,.2f}", border=True)
                st.metric("证券总市值", f"${float(row.get('market_val', 0)):,.2f}", border=True)
                st.metric("可用购买力", f"${float(row.get('power', 0)):,.2f}", border=True)
            with st.container(horizontal=True):
                st.metric(
                    "账户当日浮动盈亏", f"${total_today_pnl:+,.2f}",
                    f"当前持仓累计 ${total_pos_pnl:+,.2f}",
                    delta_color="normal", border=True,
                )
                st.metric(
                    "股票 + 期权市值", f"${stock_mv + opt_mv:,.2f}",
                    f"股票 ${stock_mv:,.2f} · 期权 ${opt_mv:,.2f}",
                    border=True,
                )
            st.caption("当日浮动盈亏按当前持仓明细汇总，不包含已平仓兑现收益，也不包含 Shadow 虚拟成交。")

        # 两个独立战区：相同结构、相同层级，避免数字混排
        stock_zone, option_zone = st.columns(2)
        with stock_zone:
            with st.container(border=True):
                st.markdown("#### 股票 / ETF 战区")
                st.caption("Core Satellite 现货底仓")
                with st.container(horizontal=True):
                    st.metric("在仓标的", f"{len(active_stock_positions)} 只", border=True)
                    st.metric("总市值", f"${stock_mv:,.2f}", border=True)
                st.metric("当日浮动盈亏", f"${stock_today_pnl:+,.2f}", border=True)
                st.metric("当前持仓累计浮盈", f"${stock_total_pnl:+,.2f}", border=True)
                st.page_link(
                    "pages/4_core_satellite_strategy.py",
                    label="查看股票调仓与策略状态",
                    icon=":material/pie_chart:",
                )
        with option_zone:
            with st.container(border=True):
                st.markdown("#### 期权战区")
                st.caption("波动率策略与主动风控")
                with st.container(horizontal=True):
                    st.metric("在仓合约", f"{len(active_option_positions)} 张", border=True)
                    st.metric("权利金市值", f"${opt_mv:,.2f}", border=True)
                st.metric("当日浮动盈亏", f"${opt_today_pnl:+,.2f}", border=True)
                st.metric("当前持仓累计浮盈", f"${opt_total_pnl:+,.2f}", border=True)
                st.page_link(
                    "pages/28_options_pnl_ledger.py",
                    label="查看期权逐笔台账与平仓历史",
                    icon=":material/receipt_long:",
                )

    # 详细数据按用途分组，避免重复嵌套 tabs
    tabs = st.tabs([
        f"股票/ETF 持仓 ({len(active_stock_positions)})",
        f"期权持仓 ({len(active_option_positions)})",
        "待成交订单",
        "今日订单",
        "历史订单",
        "策略盈亏归因",
        "策略信号",
        "执行审计",
        "账户原始数据",
    ])

    with tabs[0]:
        if stock_positions.empty:
            st.info("当前富途模拟账户没有股票/ETF 持仓。")
        else:
            st.dataframe(chinese_columns(stock_positions), hide_index=True)

    with tabs[1]:
        if option_positions.empty:
            st.info("当前富途模拟账户没有期权持仓。")
        else:
            show_all = st.checkbox("包含今日已平仓清零合约", value=False, key="show_all_zero_qty_options")
            display_df = option_positions if show_all else active_option_positions
            if display_df.empty:
                st.info("当前无有效期权持仓（今日持仓已全部平仓锁定）。")
            else:
                st.dataframe(option_position_display(display_df), hide_index=True)
            st.page_link(
                "pages/28_options_pnl_ledger.py",
                label="查看期权逐笔盈亏台账与平仓收益",
                icon=":material/price_check:",
            )

    pending_orders = (
        orders[orders["order_status"].map(broker.is_cancellable_status)].copy()
        if not orders.empty else orders
    )
    with tabs[2]:
        st.dataframe(chinese_columns(pending_orders), hide_index=True)
        cancel_result = st.session_state.pop("main_cancel_result", None)
        if cancel_result is not None:
            succeeded = int((cancel_result["result"] == "SUCCESS").sum()) if not cancel_result.empty else 0
            st.success(f"撤单处理完成：成功 {succeeded} 笔，共处理 {len(cancel_result)} 笔。")
            if not cancel_result.empty:
                st.dataframe(chinese_columns(cancel_result), hide_index=True)
        if pending_orders.empty:
            st.info("当前没有待成交或可撤销订单。")
        else:
            selected_order = st.selectbox(
                "选择待撤订单", pending_orders.index.tolist(),
                format_func=lambda index: (
                    f"{pending_orders.loc[index, 'order_id']} · "
                    f"{pending_orders.loc[index, 'code']} · "
                    f"{pending_orders.loc[index, 'trd_side']}"
                ),
            )
            cancel_one, cancel_all = st.columns(2)
            if cancel_one.button("撤销选中订单", type="primary", disabled=not admin):
                st.session_state.main_cancel_result = broker.cancel_orders(pending_orders.loc[[selected_order]])
                st.cache_data.clear()
                st.rerun()
            with cancel_all.form("main_cancel_all_pending"):
                confirmed = st.checkbox("确认撤销全部待成交订单")
                submitted = st.form_submit_button("一键撤销全部待成交订单", disabled=not admin)
                if submitted:
                    if not confirmed:
                        st.error("请先勾选确认。")
                    else:
                        st.session_state.main_cancel_result = broker.cancel_orders(pending_orders)
                        st.cache_data.clear()
                        st.rerun()

    with tabs[3]:
        st.dataframe(chinese_columns(orders), hide_index=True)

    with tabs[4]:
        st.caption("历史订单查询可能较慢，仅在明确点击后访问 OpenD。")
        if st.button("加载历史订单", key="load_main_history"):
            _, _, _, _, history = load_account(True)
            st.session_state.main_history = history
        history = st.session_state.get("main_history", pd.DataFrame())
        if history.empty:
            st.info("尚未加载历史订单。")
        else:
            st.dataframe(chinese_columns(history), hide_index=True)

    with tabs[5]:
        try:
            strategy_summary, strategy_detail, _ = load_strategy_pnl()
            if strategy_summary.empty:
                st.info("当前暂无策略归因明细数据。")
            else:
                with st.container(horizontal=True):
                    st.metric("已实现盈亏", f"${float(strategy_summary['已实现盈亏'].sum()):,.2f}", border=True)
                    st.metric("未实现盈亏", f"${float(strategy_summary['未实现盈亏'].sum()):,.2f}", border=True)
                    st.metric("策略总盈亏", f"${float(strategy_summary['总盈亏'].sum()):,.2f}", border=True)
                st.dataframe(chinese_columns(strategy_summary), hide_index=True)
                if not strategy_detail.empty:
                    st.dataframe(chinese_columns(strategy_detail), hide_index=True)
                st.caption("这里按统一成交账本和策略虚拟持仓归因，包含 Shadow 成交；与富途账户盈亏不是同一口径。")
        except BrokerError as exc:
            st.warning(f"策略盈亏暂不可用：{exc}")

    with tabs[6]:
        symbol = st.selectbox("标的", settings.watchlist)
        strategy_name = st.selectbox("策略", [*STRATEGIES, STRATEGY_NAME])
        if st.button("计算最新信号"):
            if strategy_name == STRATEGY_NAME:
                symbols = tuple(dict.fromkeys(settings.core_satellite_symbols))
                required = tuple(dict.fromkeys(("US.SPY", *symbols)))
                frames = {code: broker.candles(code, count=260) for code in required}
                account_value = float(info.iloc[0]["total_assets"])
                plan, risk_on = build_rebalance_plan(
                    frames, positions, account_value, symbols,
                    settings.core_satellite_max_stock_weight,
                    settings.core_satellite_min_trade_usd,
                    settings.core_satellite_profile,
                    settings.core_satellite_weight_tolerance,
                )
                st.metric("市场状态", "RISK-ON" if risk_on else "RISK-OFF")
                st.dataframe([
                    {"代码": order.symbol, "方向": order.side, "股数": order.quantity,
                     "参考价": order.price, "当前仓位": order.current_weight,
                     "目标仓位": order.target_weight, "原因": order.reason}
                    for order in plan
                ], hide_index=True)
                if not plan:
                    st.info("当前没有达到最小交易金额的调仓指令。")
            else:
                candles = broker.candles(symbol)
                result = STRATEGIES[strategy_name](candles)
                st.metric("信号", result.signal.value, f"最新价 ${result.price:.2f}")
                st.caption(result.reason)
                china_candles = china_time_columns(candles)
                st.line_chart(china_candles.set_index("time_key")["close"])
                st.info(f"{symbol} 自动执行数量：{settings.quantity_for(symbol)} 股；实际下单由策略进程负责。")

    with tabs[7]:
        intent_rows = store.intents()
        if not intent_rows:
            st.info("暂无由统一执行层发出的订单。")
        else:
            intent_table = chinese_columns(pd.DataFrame(intent_rows)).rename(columns={
                "strategy_id": "策略", "intent_key": "交易意图键", "symbol": "代码",
                "side": "方向", "quantity": "数量", "limit_price": "限价",
                "status": "执行状态", "broker_order_id": "券商订单号", "error": "错误信息",
                "created_at": "创建时间", "updated_at": "更新时间", "metadata_json": "附加信息",
            })
            st.dataframe(intent_table.drop(columns=["id"], errors="ignore"), hide_index=True)
            st.caption("同一策略与交易意图键只能提交一次；REJECTED 表示被本地风控拦截。")

    with tabs[8]:
        st.dataframe(chinese_columns(info), hide_index=True)
        st.caption("账户快照由统一交易服务周期采集；页面优先显示服务最新结果。")
except BrokerError as exc:
    st.error(f"连接失败：{exc}")
    st.info("请启动并登录 Futu OpenD，确认端口与 .env 一致，然后刷新页面。")
