from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.long_term_validation import (
    calibration_summary, equity_horizon_metrics, long_term_validation_cycle,
    manually_restore_paper, stability_scores, stored_greeks_attribution,
    stratified_execution_quality, strategy_horizon_metrics,
)
from futu_quant.store import TradingStore
from futu_quant.ui import render_admin_login, render_sidebar


st.set_page_config(page_title="长期验证", page_icon="📐", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("长期模拟验证与数据驱动校准")
st.caption("按 7、30、90 天观察收益、稳定性、交易成本、Greeks 和容量。样本不足不会被当作通过；系统只允许 PAPER 自动降级到 SHADOW。")

overview_tab, strategy_tab, execution_tab, greeks_tab, calibration_tab, governance_tab = st.tabs(
    ["长期总览", "策略贡献", "执行偏差", "Greeks 风险", "评分与容量", "降级治理"]
)

with overview_tab:
    equity = equity_horizon_metrics(store)
    stability = stability_scores(store)
    if equity.empty:
        st.info("尚无账户权益快照。统一服务运行后会逐步积累。")
    else:
        st.dataframe(equity, hide_index=True, column_config={
            "收益率": st.column_config.NumberColumn(format="percent"),
            "最大回撤": st.column_config.NumberColumn(format="percent"),
            "年化波动": st.column_config.NumberColumn(format="percent"),
        })
    if not stability.empty:
        with st.container(horizontal=True):
            st.metric("策略数", len(stability), border=True)
            st.metric("A/B 级策略", int(stability["等级"].isin(["A", "B"]).sum()), border=True)
            st.metric("发生参数走样", int(stability["参数走样"].eq("是").sum()), border=True)
            st.metric("平均稳定性", f"{stability['稳定性评分'].mean():.1f}", border=True)
        st.dataframe(stability, hide_index=True, column_config={
            "成交率": st.column_config.NumberColumn(format="percent"),
            "失败拒绝率": st.column_config.NumberColumn(format="percent"),
            "稳定性评分": st.column_config.ProgressColumn(min_value=0, max_value=100),
        })

with strategy_tab:
    strategy_metrics = strategy_horizon_metrics(store)
    if strategy_metrics.empty:
        st.info("尚无策略成交样本。")
    else:
        st.dataframe(strategy_metrics, hide_index=True, column_config={
            "胜率": st.column_config.NumberColumn(format="percent"),
            "已实现净盈亏": st.column_config.NumberColumn(format="$%.2f"),
            "手续费": st.column_config.NumberColumn(format="$%.2f"),
            "不利滑点成本": st.column_config.NumberColumn(format="$%.2f"),
        })
        chart = strategy_metrics[strategy_metrics["周期"] == "30天"]
        if not chart.empty:
            st.bar_chart(chart, x="策略", y="已实现净盈亏")

with execution_tab:
    execution = stratified_execution_quality(store)
    if execution.empty:
        st.info("尚无已成交样本，暂时不能按标的和时段校准滑点。")
    else:
        st.dataframe(execution, hide_index=True, column_config={
            "平均不利滑点": st.column_config.NumberColumn(format="percent"),
            "最大不利滑点": st.column_config.NumberColumn(format="percent"),
        })
        st.caption("分别按策略、代码、纽约交易时段和 SHADOW/PAPER 模式统计，避免用一个全局滑点覆盖所有期权。")

with greeks_tab:
    greeks = stored_greeks_attribution(store)
    if greeks.empty:
        st.info("当前没有带行情快照的活动期权组合。")
    else:
        totals = greeks[["Delta", "Gamma", "Theta", "Vega"]].sum()
        with st.container(horizontal=True):
            for name in ["Delta", "Gamma", "Theta", "Vega"]:
                st.metric(f"组合净 {name}", f"{totals[name]:.2f}", border=True)
        st.dataframe(greeks, hide_index=True)

with calibration_tab:
    calibration = calibration_summary(store)
    score = calibration["candidate_score"]
    capacity = calibration["capacity"]
    with st.container(horizontal=True):
        st.metric("候选评分状态", score["status"], border=True)
        st.metric("候选收益配对样本", score["sample_count"], border=True)
        st.metric("容量校准状态", capacity["status"], border=True)
        st.metric("容量样本", capacity["sample_count"], border=True)
    if calibration["candidate_buckets"]:
        st.dataframe(pd.DataFrame(calibration["candidate_buckets"]), hide_index=True)
    if calibration["regimes"]:
        st.dataframe(pd.DataFrame(calibration["regimes"]), hide_index=True)
    st.json(capacity)

with governance_tab:
    enabled = store.get_setting("validation_auto_downgrade", "false") == "true"
    selected = st.toggle("启用 PAPER → SHADOW 自动降级", value=enabled, disabled=not admin)
    if selected != enabled and admin:
        store.set_setting("validation_auto_downgrade", str(selected).lower())
        store.audit("VALIDATION_AUTO_DOWNGRADE_SETTING", {"enabled": selected}, actor="streamlit_admin:panel")
        st.rerun()
    st.warning("自动治理永远不会把 SHADOW 升级为 PAPER。恢复必须管理员选择策略并填写原因。")
    shadow = [row["strategy_id"] for row in store.strategy_risk_budgets()
              if store.get_setting(f"strategy_execution_mode:{row['strategy_id']}", "").upper() == "SHADOW"]
    if shadow:
        with st.form("manual_restore", border=True):
            strategy = st.selectbox("需要恢复的策略", shadow)
            reason = st.text_input("人工复核与恢复原因")
            submitted = st.form_submit_button("人工恢复为 PAPER", type="primary", disabled=not admin)
        if submitted:
            try:
                manually_restore_paper(store, strategy, "streamlit_admin", reason)
                st.success(f"{strategy} 已人工恢复为 PAPER。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    else:
        st.info("当前没有被降级且等待人工恢复的策略。")
    if st.button("立即运行长期验证", disabled=not admin, icon=":material/play_arrow:"):
        result = long_term_validation_cycle(store)
        st.success(f"长期验证完成；自动降级 {len(result['downgraded'])} 个策略。")
