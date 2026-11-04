from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from futu_quant.backtest import run_backtest, run_core_satellite_backtest
from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.core_satellite import STRATEGY_NAME
from futu_quant.core_satellite import PROFILES
from futu_quant.strategies import STRATEGIES
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar
from futu_quant.walk_forward import walk_forward_validate
from futu_quant.ui import open_trading_store as TradingStore


st.set_page_config(page_title="策略回测", page_icon="🧪", layout="wide")
render_sidebar(Settings.load_cached())
st.title("股票 / ETF 策略回测")
st.caption("信号在当日收盘生成，订单在下一交易日开盘成交，避免使用未来数据。")

settings = Settings.load_cached()
broker = FutuPaperBroker(settings)
store = TradingStore()
admin = render_admin_login()

with st.form("backtest_parameters"):
    left, middle, right = st.columns(3)
    symbol = left.selectbox("标的", settings.watchlist)
    strategy = middle.selectbox("策略", [*STRATEGIES, STRATEGY_NAME])
    quantity = right.number_input(
        "每次交易数量", min_value=1, max_value=100_000,
        value=settings.quantity_for(symbol), step=1,
    )
    start_default = date.today() - timedelta(days=365 * 3)
    start = left.date_input("开始日期", start_default)
    end = middle.date_input("结束日期", date.today())
    initial_cash = right.number_input("初始资金（美元）", 100.0, 100_000_000.0, 100_000.0)
    commission = left.number_input("每笔固定佣金（美元）", 0.0, 1000.0, 0.0, step=0.1)
    slippage = middle.number_input("滑点（基点）", 0.0, 1000.0, 2.0, step=0.5)
    submitted = st.form_submit_button("开始回测", type="primary")
    walk_forward = st.checkbox("同时运行 Walk-forward 样本外验证", value=True)

if strategy == STRATEGY_NAME:
    st.info(
        "Core Satellite 是组合级回测：使用交易配置中的全部标的，并自动补充 SPY；"
        "上方单个标的和固定交易数量不会参与其仓位计算。"
    )
    profile = st.selectbox(
        "Core Satellite 参数档案",
        list(PROFILES),
        index=list(PROFILES).index(settings.core_satellite_profile),
        format_func=lambda key: PROFILES[key]["label"],
    )
else:
    profile = settings.core_satellite_profile

if submitted:
    if start >= end:
        st.error("开始日期必须早于结束日期。")
        st.stop()
    try:
        with st.spinner("正在从富途下载历史行情并执行回测……"):
            if strategy == STRATEGY_NAME:
                symbols = tuple(dict.fromkeys(settings.core_satellite_symbols))
                required = tuple(dict.fromkeys(("US.SPY", *symbols)))
                frames = {
                    code: broker.candles_range(code, start.isoformat(), end.isoformat())
                    for code in required
                }
                result = run_core_satellite_backtest(
                    frames, symbols, float(initial_cash),
                    settings.core_satellite_max_stock_weight,
                    settings.core_satellite_min_trade_usd,
                    float(commission), float(slippage),
                    profile,
                    settings.core_satellite_rebalance_frequency,
                    settings.core_satellite_weight_tolerance,
                )
                bars = frames["US.SPY"]
            else:
                bars = broker.candles_range(symbol, start.isoformat(), end.isoformat())
                result = run_backtest(
                    bars, strategy, float(initial_cash), int(quantity),
                    float(commission), float(slippage),
                )
                validation = walk_forward_validate(
                    bars, strategy, float(initial_cash), int(quantity),
                    float(commission), float(slippage),
                ) if walk_forward else None
        if strategy == STRATEGY_NAME:
            validation = None
            evidence_strategy = "core_satellite"
            evidence_passed = (
                result.metrics["total_return"] > 0
                and result.metrics["max_drawdown"] >= -0.30
                and result.metrics["trade_count"] > 0
            )
            evidence_summary = dict(result.metrics)
            evidence_windows = []
        else:
            evidence_strategy = strategy
            evidence_passed = bool(validation and validation["passed"])
            evidence_summary = validation["summary"] if validation else dict(result.metrics)
            evidence_windows = validation["windows"] if validation else []
        st.session_state["pending_backtest_evidence"] = {
            "strategy_id": evidence_strategy, "display_strategy": strategy,
            "symbol": symbol, "passed": evidence_passed,
            "summary": evidence_summary, "windows": evidence_windows,
            "kind": "Walk-forward" if validation else "组合回测",
        }
        metrics = result.metrics
        columns = st.columns(6)
        columns[0].metric("期末权益", f"${metrics['final_equity']:,.2f}")
        columns[1].metric("总收益率", f"{metrics['total_return']:.2%}")
        columns[2].metric("最大回撤", f"{metrics['max_drawdown']:.2%}")
        columns[3].metric("委托成交次数", f"{int(metrics['trade_count'])}")
        columns[4].metric("完整交易", f"{int(metrics['closed_trades'])}")
        columns[5].metric("胜率", f"{metrics['win_rate']:.2%}")
        st.subheader("权益曲线")
        st.line_chart(result.equity.set_index("date")[["equity"]])
        equity_tab, trades_tab, bars_tab = st.tabs(["每日权益", "交易记录", "历史行情"])
        equity_cn = chinese_columns(result.equity)
        trades_cn = chinese_columns(result.trades)
        bars_cn = chinese_columns(bars)
        equity_tab.dataframe(equity_cn, hide_index=True)
        trades_tab.dataframe(trades_cn, hide_index=True)
        bars_tab.dataframe(bars_cn, hide_index=True)
        equity_tab.download_button(
            "下载权益 CSV", equity_cn.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{symbol}_{strategy}_equity.csv", mime="text/csv",
        )
        trades_tab.download_button(
            "下载交易 CSV", trades_cn.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{symbol}_{strategy}_trades.csv", mime="text/csv",
        )
        if strategy != STRATEGY_NAME and validation is not None:
            st.subheader("Walk-forward 样本外验证")
            summary = validation["summary"]
            with st.container(horizontal=True):
                st.metric("验证结果", "通过" if validation["passed"] else "未通过", border=True)
                st.metric("盈利窗口比例", f"{summary['profitable_window_ratio']:.0%}", border=True)
                st.metric("收益稳定度", f"{summary['return_stability']:.2%}", border=True)
                st.metric("最差回撤", f"{summary['worst_drawdown']:.2%}", border=True)
                st.metric("Bootstrap 正收益概率", f"{summary['bootstrap_positive_probability']:.0%}", border=True)
            st.dataframe(pd.DataFrame(validation["windows"]), hide_index=True)
            st.caption("执行参数敏感性（滑点压力）")
            st.dataframe(pd.DataFrame(validation["sensitivity"]), hide_index=True,
                         column_config={"总收益率": st.column_config.NumberColumn(format="percent"),
                                        "最大回撤": st.column_config.NumberColumn(format="percent")})
    except (BrokerError, ValueError) as exc:
        st.error(str(exc))

pending_evidence = st.session_state.get("pending_backtest_evidence")
if pending_evidence:
    with st.container(border=True):
        st.subheader("上线审批证据")
        st.write(
            f"{pending_evidence['display_strategy']} · {pending_evidence['kind']} · "
            f"结果：{'通过' if pending_evidence['passed'] else '未通过'}"
        )
        if st.button(
            "保存本次结果为上线审批证据", type="primary", disabled=not admin
        ):
            store.record_validation(
                pending_evidence["strategy_id"], pending_evidence["symbol"],
                pending_evidence["passed"], pending_evidence["summary"],
                pending_evidence["windows"],
            )
            store.record_acceptance_evidence(
                pending_evidence["strategy_id"], "BACKTEST", pending_evidence["passed"],
                value=float(pending_evidence["summary"].get(
                    "median_return", pending_evidence["summary"].get("total_return", 0)
                )), details=pending_evidence["summary"],
            )
            st.session_state.pop("pending_backtest_evidence", None)
            st.success("已保存。上线审批页的“回测验收”会立即更新。")
