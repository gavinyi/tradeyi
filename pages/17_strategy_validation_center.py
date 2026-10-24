from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.combo_analytics import combo_execution_quality
from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar, translate_common_values
from futu_quant.validation_loop import (
    apply_auto_downgrade, calibrate_capacity, calibrate_scores, capacity_observations,
    closed_combo_observations, collect_combo_market_snapshots, combo_walk_forward,
    execution_bias, regime_performance, run_validation_cycle, strategy_drift,
    validation_report,
)


st.set_page_config(page_title="策略验证中心", page_icon="✅", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()

st.title("策略验证与自动校准中心")
st.caption(
    "持续比较历史回测、Shadow 与富途模拟成交。样本不足不会判定为通过；"
    "自动治理只能把 PAPER 降为 SHADOW，不能自动升级。"
)

with st.container(horizontal=True):
    if st.button("采集组合行情快照", icon=":material/add_chart:"):
        try:
            count = collect_combo_market_snapshots(broker, store)
            st.toast(f"已采集 {count} 个组合")
            st.rerun()
        except BrokerError as exc:
            st.error(str(exc))
    if st.button("运行全部校准", type="primary", icon=":material/model_training:", disabled=not admin):
        try:
            result = run_validation_cycle(broker, store)
            st.success(f"校准完成：{result}")
            st.rerun()
        except BrokerError as exc:
            st.error(str(exc))

snapshots = pd.DataFrame(store.combo_market_snapshots(limit=10_000))
calibrations = pd.DataFrame(store.calibration_runs(limit=2_000))
drift = strategy_drift(store)
closed = closed_combo_observations(store)
auto_downgrade = store.get_setting("validation_auto_downgrade", "false") == "true"

with st.container(horizontal=True):
    st.metric("组合行情快照", len(snapshots), border=True)
    st.metric("平均数据质量", f"{snapshots['quality_score'].mean():.1f}/100" if not snapshots.empty else "尚无样本", border=True)
    st.metric("已关闭组合样本", len(closed), border=True)
    st.metric("走样策略", int((drift.get("状态") == "显著走样").sum()) if not drift.empty else 0, border=True)
    st.metric("校准运行", len(calibrations), border=True)

overview_tab, bias_tab, score_tab, capacity_tab, drift_tab, walk_tab, report_tab = st.tabs(
    ["数据质量与市场状态", "回测/Shadow/模拟偏差", "评分与状态分层", "容量校准", "参数走样与降级", "组合 Walk-forward", "日报与周报"]
)

with overview_tab:
    if snapshots.empty:
        st.info("尚无组合快照。创建组合后，常规交易时段每分钟自动采集。")
    else:
        latest = snapshots.sort_values("id").groupby("combo_id", as_index=False).tail(1)
        display = china_time_columns(latest).rename(columns={
            "combo_id": "组合编号", "underlying": "正股", "regime": "市场状态",
            "quality_score": "质量分", "quality_grade": "质量等级",
            "mid_net_debit": "中间净借记", "executable_net_debit": "立即成交净借记",
            "package_spread": "组合盘口成本", "capacity": "估算容量",
            "created_at": "采集时间（中国）",
        }).drop(columns=["id", "details_json"], errors="ignore")
        display["市场状态"] = display["市场状态"].map({
            "HIGH_VOLATILITY": "高波动", "TREND_UP": "上升趋势", "TREND_DOWN": "下降趋势",
            "RANGE": "震荡", "LIQUIDITY_STRESS": "流动性紧张", "UNKNOWN": "样本不足",
        }).fillna(display["市场状态"])
        st.dataframe(display, hide_index=True, column_config={
            "质量分": st.column_config.ProgressColumn(min_value=0, max_value=100),
            "中间净借记": st.column_config.NumberColumn(format="$%.2f"),
            "立即成交净借记": st.column_config.NumberColumn(format="$%.2f"),
        })
        quality_trend = snapshots.copy()
        quality_trend["created_at"] = pd.to_datetime(quality_trend["created_at"], utc=True).dt.tz_convert("Asia/Shanghai")
        st.line_chart(quality_trend, x="created_at", y="quality_score", color="combo_id")

with bias_tab:
    bias, summary = execution_bias(store)
    with st.container(horizontal=True):
        st.metric("配对样本", summary.get("sample_count", 0), border=True)
        st.metric("校准状态", "可用" if summary.get("status") == "READY" else "样本不足", border=True)
        st.metric("平均不利偏差", f"{summary.get('mean_adverse_bias', 0):.2%}", border=True)
    if bias.empty:
        st.info("需要相同策略、代码和方向同时具有 Shadow 与 PAPER 成交，至少 20 个配对样本后启用偏差修正。")
    else:
        st.dataframe(translate_common_values(bias), hide_index=True, column_config={
            "不利价格偏差": st.column_config.NumberColumn(format="percent"),
        })
    quality = combo_execution_quality(store)
    if not quality.empty:
        st.subheader("组合执行偏差")
        st.dataframe(translate_common_values(quality), hide_index=True)

with score_tab:
    score_table, score_summary = calibrate_scores(closed)
    regimes = regime_performance(closed)
    st.metric("候选评分校准", "可用" if score_summary["status"] == "READY" else "样本不足", border=True)
    st.caption(score_summary["recommendation"])
    if not score_table.empty:
        st.dataframe(score_table, hide_index=True, column_config={
            "胜率": st.column_config.NumberColumn(format="percent"),
            "平均收益": st.column_config.NumberColumn(format="percent"),
        })
    else:
        st.info("至少需要 30 个带入场质量评分的已关闭组合，才会调整候选评分权重。")
    st.subheader("市场状态分层表现")
    if regimes.empty:
        st.info("尚无已关闭组合，无法按趋势、震荡、高波动和流动性紧张状态分层。")
    else:
        st.dataframe(regimes, hide_index=True, column_config={
            "胜率": st.column_config.NumberColumn(format="percent"),
            "平均收益": st.column_config.NumberColumn(format="percent"),
            "最差收益": st.column_config.NumberColumn(format="percent"),
        })

with capacity_tab:
    observations = capacity_observations(store)
    capacity = calibrate_capacity(observations)
    with st.container(horizontal=True):
        st.metric("容量成交样本", capacity["sample_count"], border=True)
        st.metric("建议成交量参与率", f"{capacity['recommended_volume_participation']:.2%}", border=True)
        st.metric("建议未平仓量参与率", f"{capacity['recommended_oi_participation']:.2%}", border=True)
    if capacity["status"] != "READY":
        st.info("至少需要 20 条组合腿成交记录；当前继续使用保守默认值 5% / 2%。")
    elif not observations.empty:
        st.dataframe(observations, hide_index=True)

with drift_tab:
    selected = st.toggle(
        "允许验证系统自动把走样策略从 PAPER 降为 SHADOW",
        value=auto_downgrade, disabled=not admin,
    )
    if selected != auto_downgrade:
        store.set_setting("validation_auto_downgrade", str(selected).lower())
        store.audit("VALIDATION_AUTO_DOWNGRADE_SETTING", {"enabled": selected}, actor="streamlit_admin:panel")
        st.rerun()
    if drift.empty:
        st.info("还没有策略执行样本。")
    else:
        st.dataframe(translate_common_values(drift), hide_index=True, column_config={
            "基准成交率": st.column_config.NumberColumn(format="percent"),
            "近期成交率": st.column_config.NumberColumn(format="percent"),
            "基准失败率": st.column_config.NumberColumn(format="percent"),
            "近期失败率": st.column_config.NumberColumn(format="percent"),
            "基准滑点": st.column_config.NumberColumn(format="percent"),
            "近期滑点": st.column_config.NumberColumn(format="percent"),
        })
        if st.button("立即执行安全降级检查", disabled=not admin or not selected):
            changed = apply_auto_downgrade(store, drift)
            st.success("已降级：" + "、".join(changed) if changed else "没有策略满足降级条件。")

with walk_tab:
    combos = store.option_combos()
    if not combos:
        st.info("请先创建组合并积累各腿历史快照。")
    else:
        with st.form("combo_walk_forward", border=True):
            combo_id = st.selectbox("组合", [row["combo_id"] for row in combos])
            windows = st.number_input("滚动窗口数", min_value=2, max_value=8, value=4, step=1)
            run_walk = st.form_submit_button("运行组合 Walk-forward", type="primary")
        if run_walk:
            try:
                legs = store.option_combo_legs(combo_id)
                histories = {leg["symbol"]: pd.DataFrame(store.option_history(leg["symbol"])) for leg in legs}
                result, summary = combo_walk_forward(histories, legs, int(windows))
                st.dataframe(result, hide_index=True, column_config={
                    "收益率": st.column_config.NumberColumn(format="percent"),
                    "最大回撤": st.column_config.NumberColumn(format="percent"),
                })
                st.json(summary)
            except ValueError as exc:
                st.error(str(exc))

with report_tab:
    with st.container(horizontal=True):
        if st.button("生成今日验证报告", type="primary", disabled=not admin):
            validation_report(store, "DAILY")
            st.rerun()
        if st.button("生成本周验证报告", disabled=not admin):
            validation_report(store, "WEEKLY")
            st.rerun()
    reports = store.validation_reports()
    if not reports:
        st.info("尚无验证报告。统一服务会每日和每周自动生成。")
    else:
        selected_report = st.selectbox(
            "选择报告", range(len(reports)),
            format_func=lambda index: f"{reports[index]['period_type']} · {reports[index]['period_key']} · {reports[index]['status']}",
        )
        report = reports[selected_report]
        st.markdown(report["markdown"])
        st.download_button(
            "下载 Markdown 报告", report["markdown"],
            file_name=f"validation-{report['period_type'].lower()}-{report['period_key']}.md",
            mime="text/markdown", icon=":material/download:",
        )
        with st.expander("报告结构化数据"):
            st.json(json.loads(report["summary_json"]))
