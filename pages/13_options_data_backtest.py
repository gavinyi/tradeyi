from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.option_event_backtest import run_option_event_backtest
from futu_quant.option_history import collect_option_snapshots, history_frame
from futu_quant.option_evidence import assess_option_evidence, option_research_buckets
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.volatility_validation import volatility_walk_forward
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="期权数据回测", page_icon="📊", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

st.title("历史期权数据与事件驱动回测")
st.caption(
    "后台在美股常规交易时段每分钟采集已配置合约和持仓合约。回测按卖一价开仓、"
    "买一价平仓，并在到期时按内在价值结算。"
)


@st.cache_data(ttl=15, max_entries=2, show_spinner=False)
def load_coverage() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().option_history_coverage())


if st.button("立即采集一次", icon=":material/download:", disabled=not admin):
    try:
        with st.spinner("正在读取富途模拟环境行情……"):
            result = collect_option_snapshots(broker, store)
        st.cache_data.clear()
        st.success(
            f"采集完成：目标 {result['contracts']} 个，写入 {result['stored']} 个，"
            f"可用 {result['good']} 个，降级 {result['degraded']} 个。"
        )
    except (BrokerError, ValueError) as exc:
        st.error(str(exc))

coverage = load_coverage()
with st.container(horizontal=True):
    st.metric("历史合约", len(coverage), border=True)
    st.metric("历史快照", int(coverage["sample_count"].sum()) if not coverage.empty else 0, border=True)
    st.metric("可用快照", int(coverage["good_samples"].sum()) if not coverage.empty else 0, border=True)
    st.metric(
        "最长覆盖交易日", int(coverage["trading_days"].max()) if not coverage.empty else 0,
        border=True,
    )

coverage_tab, backtest_tab, walk_forward_tab = st.tabs(
    ["数据覆盖", "事件驱动回测", "波动率走步验证"]
)
with coverage_tab:
    if coverage.empty:
        st.info("尚无历史期权快照。配置期权策略后，服务会在常规交易时段开始积累。")
    else:
        st.dataframe(chinese_columns(coverage), hide_index=True, width="stretch")
        st.caption(
            "GOOD 要求买一价大于 0、卖一价不低于买一价且 IV 有效。"
            "DEGRADED 数据会保存用于诊断，但默认不参与回测。"
        )

with backtest_tab:
    if coverage.empty:
        st.info("积累至少 5 条同一合约的可用快照后才能回测。")
    else:
        codes = coverage["code"].astype(str).tolist()
        with st.form("option_event_backtest_form", border=True):
            code = st.selectbox("期权合约", codes)
            initial_cash = st.number_input("初始资金（美元）", 100.0, 100_000_000.0, 100_000.0)
            contracts = st.number_input("合约数量", 1, 10_000, 1)
            commission = st.number_input("每张单边费用（美元）", 0.0, 100.0, 0.65, step=0.05)
            take_profit = st.number_input("止盈比例", 0.01, 10.0, 0.50, format="%.2f")
            stop_loss = st.number_input("止损比例", 0.01, 1.0, 0.35, format="%.2f")
            submitted = st.form_submit_button("开始事件驱动回测", type="primary")
        if submitted:
            try:
                snapshots = history_frame(store, code)
                evidence = assess_option_evidence(snapshots, min_samples=5, min_days=1)
                if evidence.status != "SUFFICIENT":
                    st.warning(evidence.reason)
                result = run_option_event_backtest(
                    snapshots, code, float(initial_cash), int(contracts),
                    float(commission), float(take_profit), float(stop_loss),
                )
                metrics = result.metrics
                with st.container(horizontal=True):
                    st.metric("期末权益", f"${metrics['final_equity']:,.2f}", border=True)
                    st.metric("总收益率", f"{metrics['total_return']:.2%}", border=True)
                    st.metric("最大回撤", f"{metrics['max_drawdown']:.2%}", border=True)
                    st.metric("有效样本", int(metrics["sample_count"]), border=True)
                    st.metric("盘口价差成本", f"${metrics['spread_cost']:,.2f}", border=True)
                    st.metric("有效交易日", int(metrics["evidence_trading_days"]), border=True)
                st.caption(
                    f"历史证据：{evidence.reason}；降级/不可成交占比 {evidence.degraded_ratio:.1%}。"
                )
                st.line_chart(result.equity.set_index("date")[["equity"]])
                equity_tab, trade_tab, raw_tab = st.tabs(["权益路径", "模拟成交", "历史快照"])
                equity_cn = chinese_columns(result.equity)
                trades_cn = chinese_columns(result.trades)
                raw_cn = chinese_columns(snapshots)
                equity_tab.dataframe(equity_cn, hide_index=True, width="stretch")
                trade_tab.dataframe(trades_cn, hide_index=True, width="stretch")
                raw_tab.dataframe(raw_cn, hide_index=True, width="stretch")
                buckets = option_research_buckets(snapshots)
                if not buckets.empty:
                    raw_tab.caption("研究分桶：用于比较相近 Delta、到期时间和流动性的合约。")
                    raw_tab.dataframe(buckets, hide_index=True, width="stretch")
                trade_tab.download_button(
                    "下载模拟成交 CSV", trades_cn.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"{code}_event_backtest.csv", mime="text/csv",
                )
                st.session_state["pending_option_backtest_evidence"] = {
                    "strategy_id": "volatility_long_gamma",
                    "symbol": code,
                    "passed": True,
                    "summary": metrics,
                    "windows": [],
                    "kind": "期权数据回测",
                }
            except ValueError as exc:
                st.error(str(exc))
                st.info("继续保持统一服务运行，系统会按分钟积累真实历史盘口。")

with walk_forward_tab:
    st.subheader("与实际持有周期一致的逐笔验证")
    st.caption(
        "按 Ask 开仓、未来 Bid 平仓，使用 90 分钟至 24 小时的真实持有窗口；"
        "结果按时间切成多个窗口，避免只看单次首尾收益。"
    )
    with st.form("volatility_walk_forward_form", border=True):
        holding_hours = st.number_input(
            "验证持有小时", min_value=1.5, max_value=24.0,
            value=4.0, step=0.5,
        )
        windows_count = st.number_input(
            "走步窗口数", min_value=2, max_value=8, value=3,
        )
        minimum_returns = st.number_input(
            "最低可配对样本", min_value=20, max_value=5000, value=30,
        )
        run_walk_forward = st.form_submit_button(
            "运行波动率逐笔走步验证", type="primary"
        )
    if run_walk_forward:
        histories = {
            str(row["code"]): history_frame(store, str(row["code"]))
            for _, row in coverage.iterrows()
        }
        windows_result, validation_summary = volatility_walk_forward(
            histories, holding_hours=float(holding_hours),
            windows=int(windows_count), minimum_returns=int(minimum_returns),
        )
        st.session_state["volatility_walk_forward_result"] = windows_result
        st.session_state["volatility_walk_forward_summary"] = validation_summary
    validation_summary = st.session_state.get("volatility_walk_forward_summary")
    windows_result = st.session_state.get("volatility_walk_forward_result")
    if validation_summary:
        with st.container(horizontal=True):
            st.metric("状态", validation_summary["status"], border=True)
            st.metric("可配对样本", validation_summary["sample_count"], border=True)
            st.metric("验证窗口", validation_summary["window_count"], border=True)
            st.metric(
                "平均净收益", f"{validation_summary.get('mean_return', 0):.2%}",
                border=True,
            )
            st.metric(
                "胜率", f"{validation_summary.get('win_rate', 0):.1%}",
                border=True,
            )
        if validation_summary["passed"]:
            st.success(validation_summary["reason"])
        else:
            st.warning(validation_summary["reason"])
        if isinstance(windows_result, pd.DataFrame) and not windows_result.empty:
            st.dataframe(
                chinese_columns(windows_result), hide_index=True, width="stretch",
            )
        st.session_state["pending_option_backtest_evidence"] = {
            "strategy_id": "volatility_long_gamma",
            "symbol": "VOLATILITY_PORTFOLIO",
            "passed": bool(validation_summary["passed"]),
            "summary": validation_summary,
            "windows": (
                windows_result.to_dict("records")
                if isinstance(windows_result, pd.DataFrame) else []
            ),
            "kind": "波动率短周期逐笔走步验证",
        }

pending_evidence = st.session_state.get("pending_option_backtest_evidence")
if pending_evidence:
    with st.container(border=True):
        st.subheader("上线审批证据")
        st.write(
            f"{pending_evidence['strategy_id']} · {pending_evidence['kind']} · "
            f"结果：{'通过' if pending_evidence['passed'] else '未通过'}"
        )
        if st.button("保存本次结果为上线审批证据", type="primary", disabled=not admin):
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
            st.session_state.pop("pending_option_backtest_evidence", None)
            st.success("已保存。上线审批页的“回测验收”会立即更新。")
