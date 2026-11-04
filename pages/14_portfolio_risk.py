from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.portfolio_allocator import ensure_default_budgets, strategy_budget_usage
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.stress import joint_stress_test, portfolio_market_snapshot
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="组合风险", page_icon="🧭", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
store = TradingStore()
ensure_default_budgets(store)
admin = render_admin_login()

st.title("组合资金分配与联合压力测试")
st.caption("查看策略资金占用，并模拟价格、IV、时间与流动性联合冲击。资金配置统一到“策略与资金控制”。")


@st.cache_data(ttl=30, max_entries=2, show_spinner=False)
def load_portfolio():
    current_store = TradingStore()
    current_broker = FutuPaperBroker(Settings.load())
    usage, equity = strategy_budget_usage(current_store, current_broker)
    positions_snapshot = current_store.broker_snapshot("positions")
    positions = current_broker.positions() if positions_snapshot is None else pd.DataFrame(positions_snapshot["payload"])
    quotes = portfolio_market_snapshot(current_broker, positions)
    return usage, equity, positions, quotes


try:
    usage, equity, positions, quotes = load_portfolio()
except BrokerError as exc:
    st.error(f"组合数据读取失败：{exc}")
    st.stop()

with st.container(horizontal=True):
    allocated = float(usage[usage["enabled"] == 1]["capital_ratio"].sum()) if not usage.empty else 0
    used = float(usage["capital_used"].sum()) if not usage.empty else 0
    option_used = float(usage["option_premium_used"].sum()) if not usage.empty else 0
    st.metric("账户权益", f"${equity:,.2f}", border=True)
    st.metric("已分配资金比例", f"{allocated:.1%}", border=True)
    st.metric("策略归属资金占用", f"${used:,.2f}", border=True)
    st.metric("策略期权权利金", f"${option_used:,.2f}", border=True)

budget_tab, stress_tab, holdings_tab = st.tabs(["策略预算使用", "联合压力矩阵", "组合持仓快照"])

with budget_tab:
    st.info(
        "资金配额、策略启停和 SHADOW/PAPER 模式统一在“策略与资金控制”调整；本页只看风险占用和压力结果。",
        icon=":material/info:",
    )
    st.page_link(
        "pages/15_strategy_control_center.py",
        label="前往策略与资金控制",
        icon=":material/tune:",
    )
    st.subheader("当前预算与使用情况")
    display = chinese_columns(usage.drop(columns=["updated_at"], errors="ignore"))
    st.dataframe(
        display, hide_index=True, width="stretch",
        column_config={
            "资金使用率": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent"),
            "损失预算使用率": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent"),
        },
    )

with stress_tab:
    if positions.empty:
        st.info("当前模拟账户没有持仓，无法生成压力矩阵。")
    else:
        stress = joint_stress_test(positions, quotes, equity)
        if st.button("保存本次压力测试快照", disabled=not admin):
            store.record_portfolio_stress(equity, stress.to_dict("records"))
            st.success("压力测试快照已保存。")
        worst = stress.iloc[0]
        with st.container(horizontal=True):
            st.metric("最差场景", str(worst["scenario"]), border=True)
            st.metric("最差估算损益", f"${float(worst['estimated_pnl']):,.2f}", border=True)
            st.metric("最差权益损失", f"{float(worst['equity_loss_ratio']):.2%}", border=True)
            st.metric("5% 警戒线", "突破" if bool(worst["breach"]) else "未突破", border=True)
        chart = stress[["scenario", "estimated_pnl"]].set_index("scenario")
        st.bar_chart(chart, horizontal=True)
        st.dataframe(
            chinese_columns(stress), hide_index=True, width="stretch",
            column_config={
                "价格冲击": st.column_config.NumberColumn(format="percent"),
                "流动性折价": st.column_config.NumberColumn(format="percent"),
                "权益损失比例": st.column_config.NumberColumn(format="percent"),
            },
        )
        st.caption("Greeks 压力结果为局部近似；极端行情下实际损失可能更高，应结合盘口和保证金复核。")

with holdings_tab:
    st.dataframe(chinese_columns(positions), hide_index=True, width="stretch")
    with st.expander("风险计算使用的行情快照"):
        st.dataframe(chinese_columns(quotes), hide_index=True, width="stretch")
