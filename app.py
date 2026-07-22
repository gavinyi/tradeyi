from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.account_metrics import account_pnl_metrics
from futu_quant.config import Settings
from futu_quant.core_satellite import STRATEGY_NAME, build_rebalance_plan
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


home_page = st.Page(
    lambda: None,
    title="账户总览",
    icon=":material/account_balance_wallet:",
    default=True,
)
navigation = {
    "账户与交易": [
        home_page,
        st.Page("pages/26_交易诊断中心.py", title="交易诊断中心", icon=":material/troubleshoot:"),
        st.Page("pages/4_订单管理.py", title="订单管理", icon=":material/receipt_long:"),
        st.Page("pages/12_交易生命周期.py", title="交易生命周期", icon=":material/account_tree:"),
    ],
    "行情与资讯": [
        st.Page("pages/23_市场总览.py", title="市场与自选行情", icon=":material/monitoring:"),
        st.Page("pages/24_期权市场.py", title="期权市场", icon=":material/travel_explore:"),
        st.Page("pages/25_事件与公告.py", title="事件与公告", icon=":material/event_note:"),
    ],
    "策略中心": [
        st.Page("pages/1_交易配置.py", title="交易配置", icon=":material/settings:"),
        st.Page("pages/15_策略控制台.py", title="自动化控制中心", icon=":material/tune:"),
        st.Page("pages/11_策略活动中心.py", title="策略活动中心", icon=":material/hub:"),
        st.Page("pages/3_策略回测.py", title="股票策略回测", icon=":material/query_stats:"),
    ],
    "期权中心": [
        st.Page("pages/2_期权配置.py", title="期权配置", icon=":material/settings_suggest:"),
        st.Page("pages/5_财报Gamma策略.py", title="财报 Gamma 策略", icon=":material/bolt:"),
        st.Page("pages/10_波动率期权策略.py", title="波动率期权策略", icon=":material/waves:"),
        st.Page("pages/16_期权组合执行.py", title="期权组合执行", icon=":material/schema:"),
        st.Page("pages/13_期权数据回测.py", title="期权数据回测", icon=":material/analytics:"),
    ],
    "风控与审批": [
        st.Page("pages/7_风控中心.py", title="风控中心", icon=":material/shield:"),
        st.Page("pages/27_实盘交易控制.py", title="实盘交易控制", icon=":material/lock_open:"),
        st.Page("pages/14_组合风险.py", title="组合风险", icon=":material/donut_large:"),
        st.Page("pages/9_上线审批.py", title="上线审批", icon=":material/approval:"),
        st.Page("pages/17_策略验证中心.py", title="策略验证中心", icon=":material/fact_check:"),
        st.Page("pages/20_长期验证.py", title="长期验证", icon=":material/timeline:"),
    ],
    "研究与系统": [
        st.Page("pages/21_研究与证据中心.py", title="研究与证据中心", icon=":material/lab_profile:"),
        st.Page("pages/22_数据研究中心.py", title="数据研究中心", icon=":material/science:"),
        st.Page("pages/6_系统监控.py", title="系统监控", icon=":material/monitoring:"),
        st.Page("pages/8_运行日志.py", title="运行日志", icon=":material/article:"),
        st.Page("pages/19_稳定性运营中心.py", title="稳定性运营中心", icon=":material/monitor_heart:"),
        st.Page("pages/18_基础设施与灾备.py", title="基础设施与灾备", icon=":material/dns:"),
    ],
}
current_page = st.navigation(navigation, position="sidebar")
current_page.run()
if current_page != home_page:
    st.stop()

try:
    settings = Settings.load()
    broker = FutuPaperBroker(settings)
    store = TradingStore()
except Exception as exc:
    st.error(str(exc))
    st.stop()

render_sidebar(settings)
admin = render_admin_login()


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
        "pages/26_交易诊断中心.py", label="打开交易诊断中心",
        icon=":material/troubleshoot:",
    )
with st.container(border=True):
    st.subheader("推荐工作入口")
    st.caption("如果你今天主要是调策略、看证据、查问题，先打开这里。")
    st.page_link(
        "pages/21_研究与证据中心.py",
        label="打开研究与证据中心",
        icon=":material/lab_profile:",
    )
if st.button("刷新账户", icon=":material/refresh:"):
    st.cache_data.clear()


@st.cache_data(ttl=15, show_spinner=False)
def load_account():
    return broker.accounts(), broker.account_info(), broker.positions(), broker.orders(), broker.orders(True)


@st.cache_data(ttl=30, show_spinner=False)
def load_strategy_pnl():
    return performance_report(TradingStore(), FutuPaperBroker(Settings.load()))


try:
    accounts, info, positions, orders, history = load_account()
    st.success(f"已连接富途 OpenD；发现 {len(accounts)} 个美股模拟账户")
    if not info.empty:
        row = info.iloc[0]
        pnl = account_pnl_metrics(positions)
        with st.container(horizontal=True):
            st.metric("总权益", f"${float(row.get('total_assets', 0)):,.2f}", border=True)
            st.metric("现金", f"${float(row.get('cash', 0)):,.2f}", border=True)
            st.metric("证券市值", f"${float(row.get('market_val', 0)):,.2f}", border=True)
            st.metric("可用购买力", f"${float(row.get('power', 0)):,.2f}", border=True)
            st.metric(
                "当日持仓盈亏", f"${pnl['today_position_pnl']:,.2f}",
                delta_color="normal", border=True,
            )
            st.metric(
                "总持仓浮动盈亏", f"${pnl['total_position_pnl']:,.2f}",
                delta_color="normal", border=True,
            )
        st.caption(
            "损益按富途模拟账户持仓明细汇总；当日持仓盈亏不等于含已平仓交易的完整账户日收益，"
            "且不包含 SHADOW 虚拟成交。"
        )
        try:
            strategy_summary, strategy_detail, _ = load_strategy_pnl()
            if not strategy_summary.empty:
                with st.container(horizontal=True):
                    st.metric(
                        "策略已实现盈亏", f"${float(strategy_summary['已实现盈亏'].sum()):,.2f}",
                        border=True,
                    )
                    st.metric(
                        "策略未实现盈亏", f"${float(strategy_summary['未实现盈亏'].sum()):,.2f}",
                        border=True,
                    )
                    st.metric(
                        "策略总盈亏", f"${float(strategy_summary['总盈亏'].sum()):,.2f}",
                        border=True,
                    )
                with st.expander("查看策略盈亏明细", expanded=False):
                    st.dataframe(chinese_columns(strategy_summary), hide_index=True)
                    st.caption(
                        "这里按统一成交账本和策略虚拟持仓归因，包含 Shadow 成交；"
                        "它和富途账户盈亏不是同一个口径。"
                    )
                    if not strategy_detail.empty:
                        st.dataframe(chinese_columns(strategy_detail), hide_index=True)
        except BrokerError as exc:
            st.warning(f"策略盈亏暂不可用：{exc}")
    tabs = st.tabs(
        ["持仓", "待成交订单", "今日订单", "历史订单", "执行审计", "策略信号", "账户原始数据"]
    )
    tabs[0].dataframe(chinese_columns(positions), hide_index=True)
    pending_orders = (
        orders[orders["order_status"].map(broker.is_cancellable_status)].copy()
        if not orders.empty else orders
    )
    with tabs[1]:
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
                "选择待撤订单",
                pending_orders.index.tolist(),
                format_func=lambda index: (
                    f"{pending_orders.loc[index, 'order_id']} · "
                    f"{pending_orders.loc[index, 'code']} · "
                    f"{pending_orders.loc[index, 'trd_side']}"
                ),
            )
            cancel_one, cancel_all = st.columns(2)
            if cancel_one.button("撤销选中订单", type="primary", disabled=not admin):
                st.session_state.main_cancel_result = broker.cancel_orders(
                    pending_orders.loc[[selected_order]]
                )
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
    tabs[2].dataframe(chinese_columns(orders), hide_index=True)
    tabs[3].dataframe(chinese_columns(history), hide_index=True)
    with tabs[4]:
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
    tabs[6].dataframe(chinese_columns(info), hide_index=True)
    with tabs[5]:
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
                st.dataframe(
                    [
                        {
                            "代码": order.symbol, "方向": order.side, "股数": order.quantity,
                            "参考价": order.price, "当前仓位": order.current_weight,
                            "目标仓位": order.target_weight, "原因": order.reason,
                        }
                        for order in plan
                    ],
                    hide_index=True,
                )
                if not plan:
                    st.info("当前没有达到最小交易金额的调仓指令。")
            else:
                candles = broker.candles(symbol)
                result = STRATEGIES[strategy_name](candles)
                st.metric("信号", result.signal.value, f"最新价 ${result.price:.2f}")
                st.caption(result.reason)
                china_candles = china_time_columns(candles)
                st.line_chart(china_candles.set_index("time_key")["close"])
                st.info(
                    f"{symbol} 自动执行数量：{settings.quantity_for(symbol)} 股；"
                    "实际下单由策略进程负责。"
                )
except BrokerError as exc:
    st.error(f"连接失败：{exc}")
    st.info("请启动并登录 Futu OpenD，确认端口与 .env 一致，然后刷新页面。")
