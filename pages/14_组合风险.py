from __future__ import annotations

import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.portfolio_allocator import ensure_default_budgets, strategy_budget_usage
from futu_quant.store import TradingStore
from futu_quant.stress import joint_stress_test, portfolio_market_snapshot
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="组合风险", page_icon="🧭", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
ensure_default_budgets(store)
admin = render_admin_login()

st.title("组合资金分配与联合压力测试")
st.caption("统一管理策略资金、最大可损失和期权权利金预算，并模拟价格、IV、时间与流动性联合冲击。")


@st.cache_data(ttl=30, max_entries=2, show_spinner=False)
def load_portfolio():
    current_store = TradingStore()
    current_broker = FutuPaperBroker(Settings.load())
    usage, equity = strategy_budget_usage(current_store, current_broker)
    positions = current_broker.positions()
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

budget_tab, stress_tab, holdings_tab = st.tabs(["策略风险预算", "联合压力矩阵", "组合持仓快照"])

with budget_tab:
    st.caption(
        "这里是策略预算的唯一配置入口。配额合计不能超过 100%；未分配部分作为现金和风险缓冲。"
        "Core Satellite 的目标仓位上限与新增风险检查都会立即采用这里的资金配额。"
    )
    edit_columns = [
        "strategy_id", "capital_ratio", "max_loss_ratio", "option_premium_ratio", "enabled",
    ]
    budget_editor = st.data_editor(
        usage[edit_columns], hide_index=True, disabled=["strategy_id"], width="stretch",
        column_config={
            "strategy_id": st.column_config.TextColumn("策略"),
            "capital_ratio": st.column_config.NumberColumn(
                "资金配额", min_value=0.0, max_value=1.0, format="percent"
            ),
            "max_loss_ratio": st.column_config.NumberColumn(
                "最大损失预算", min_value=0.0, max_value=1.0, format="percent"
            ),
            "option_premium_ratio": st.column_config.NumberColumn(
                "期权权利金配额", min_value=0.0, max_value=1.0, format="percent"
            ),
            "enabled": st.column_config.CheckboxColumn("启用"),
        },
        key="portfolio_budget_editor",
    )
    if st.button("保存三维策略预算", type="primary", disabled=not admin):
        try:
            store.save_strategy_risk_budgets(budget_editor.to_dict("records"))
            st.cache_data.clear()
            st.success("策略风险预算已保存，统一下单风控立即生效。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    st.subheader("当前使用情况")
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
