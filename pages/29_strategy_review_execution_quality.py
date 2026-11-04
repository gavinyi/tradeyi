from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.strategy_review import build_strategy_review, review_markdown
from futu_quant.trade_diagnostics import diagnostic_window, latest_us_session_date
from futu_quant.ui import china_time_columns, chinese_columns, render_sidebar, get_current_tenant


st.set_page_config(page_title="策略复盘与执行质量", page_icon="📋", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
current_tenant = get_current_tenant(settings)
is_real = current_tenant["is_real"]

st.title(f"策略复盘与执行质量 · {'🔴 实盘核心' if is_real else '🟢 美股模拟'}")
if is_real:
    st.error(f"🔴 当前复盘口径：实盘真实账户 ({current_tenant['label']}) · 真金白银已实现收益")
else:
    st.caption(f"🟢 当前复盘口径：美股模拟账户 ({current_tenant['label']}) · 100万沙盒实验收益")


def display_percent_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce").map(
                lambda value: "" if pd.isna(value) else f"{value:.2%}"
            )
    return result


def option_pnl_display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for source, target in {
        "首次成交": "首次成交（中国）",
        "最后成交": "最后成交（中国）",
    }.items():
        if source in result:
            result[target] = pd.to_datetime(
                result[source], utc=True, errors="coerce",
            ).dt.tz_convert("Asia/Shanghai").dt.strftime("%m-%d %H:%M")
            result = result.drop(columns=[source])
    preferred = [
        "代码", "结果", "净盈亏", "毛盈亏", "费用",
        "买入均价", "卖出均价", "平仓数量", "剩余持仓",
        "买入数量", "卖出数量", "成交次数", "买入金额", "卖出金额",
        "首次成交（中国）", "最后成交（中国）",
    ]
    ordered = [column for column in preferred if column in result.columns]
    rest = [column for column in result.columns if column not in ordered]
    return result[ordered + rest]


default_session = latest_us_session_date()
left, middle, right = st.columns(3)
session_date = left.date_input("复盘交易日（美东）", value=default_session)
lookback_days = middle.number_input("快速切换天数", 0, 14, 0)
if lookback_days:
    session_date = default_session - timedelta(days=int(lookback_days))
right.metric("实际复盘日", str(session_date), border=True)

if st.button("生成复盘", type="primary", icon=":material/summarize:"):
    st.session_state.strategy_review_session_date = str(session_date)

if "strategy_review_session_date" not in st.session_state:
    st.info("选择交易日后，点击“生成复盘”即可汇总整体报告和执行质量面板。")
    st.stop()

active_date = st.session_state.strategy_review_session_date
window = diagnostic_window(pd.to_datetime(active_date).date())

with st.spinner("正在汇总策略日志、订单、成交和执行质量……"):
    review = build_strategy_review(TradingStore(), window)

st.subheader("整体报告")
with st.container(horizontal=True):
    st.metric("策略决策", review.metrics["策略决策"], border=True)
    st.metric("订单数", review.metrics["订单数"], border=True)
    st.metric("成交订单", review.metrics["成交订单"], border=True)
    st.metric("阻断次数", review.metrics["阻断次数"], border=True)
    st.metric("期权已实现盈亏", f"${float(review.metrics['期权已实现盈亏']):,.2f}", border=True)
    st.metric("盈利期权合约", review.metrics["盈利期权合约"], border=True)
    st.metric("亏损期权合约", review.metrics["亏损期权合约"], border=True)
    st.metric("期权未平持仓", review.metrics["期权未平持仓"], border=True)
    st.metric("平均浮盈捕获率", f"{float(review.metrics['平均浮盈捕获率']):.0%}", border=True)
    st.metric("低捕获合约", review.metrics["低捕获合约数"], border=True)
    st.metric("高优先级行动项", review.metrics["高优先级行动项"], border=True)
    st.metric("最低质量评分", review.metrics["最低质量评分"], border=True)
    st.metric("运行退化/超时", review.metrics["退化任务"], border=True)
    st.metric("运行异常", review.metrics["异常任务"], border=True)
    st.metric("策略中断事件", review.metrics["策略中断事件"], border=True)
    st.metric("持仓保护恢复", review.metrics["持仓保护恢复"], border=True)

for item in review.narrative:
    if "中断" in item or "保护" in item:
        st.error(item)
    else:
        st.info(item)

report_markdown = review_markdown(review)
st.download_button(
    "下载本次复盘报告",
    data=report_markdown,
    file_name=f"策略复盘_{active_date}.md",
    mime="text/markdown",
    icon=":material/download:",
)

action_tab, quality_tab, lifecycle_tab, pnl_tab, orders_tab, option_tab, core_tab, runtime_tab, interruption_tab, blockers_tab, evidence_tab, raw_tab = st.tabs([
    "行动项", "质量评分", "期权生命周期", "期权盈亏拆解", "订单执行质量",
    "波动率期权质量", "Core Satellite 调仓", "运行健康", "中断追踪", "阻塞原因", "回测证据", "原始报告",
])

with action_tab:
    st.subheader("复盘行动项")
    st.caption("按交易生命周期、运行异常和阻塞原因生成，优先处理高优先级项。")
    if review.action_items.empty:
        st.success("本窗口没有需要立即处理的行动项。")
    else:
        st.dataframe(
            review.action_items,
            hide_index=True,
            column_config={
                "优先级": st.column_config.TextColumn("优先级"),
                "建议动作": st.column_config.TextColumn("建议动作", width="large"),
            },
        )

with quality_tab:
    st.subheader("策略质量评分")
    st.caption("按入场、退出、执行、风控四个维度打分，最低分排在最前面。")
    if review.quality_scores.empty:
        st.info("本窗口没有足够数据生成质量评分。")
    else:
        st.dataframe(
            review.quality_scores,
            hide_index=True,
            column_config={
                "总评分": st.column_config.ProgressColumn("总评分", min_value=0, max_value=100),
                "入场质量": st.column_config.ProgressColumn("入场质量", min_value=0, max_value=100),
                "退出质量": st.column_config.ProgressColumn("退出质量", min_value=0, max_value=100),
                "执行质量": st.column_config.ProgressColumn("执行质量", min_value=0, max_value=100),
                "风控质量": st.column_config.ProgressColumn("风控质量", min_value=0, max_value=100),
                "建议": st.column_config.TextColumn("建议", width="large"),
            },
        )

with lifecycle_tab:
    st.subheader("期权交易生命周期")
    st.caption("每个合约从入场、持仓、峰值浮盈、退出到最终盈亏串成一行；MAE 无历史采集时会明确标记。")
    if review.option_lifecycle.empty:
        st.info("本窗口没有可展示的期权生命周期。")
    else:
        view = option_pnl_display_frame(review.option_lifecycle)
        view = display_percent_columns(view, ["当前盈亏比例", "最大浮盈MFE", "浮盈捕获率"])
        st.dataframe(
            view,
            hide_index=True,
            column_config={
                "买入均价": st.column_config.NumberColumn("买入均价", format="$%.2f"),
                "卖出均价": st.column_config.NumberColumn("卖出均价", format="$%.2f"),
                "毛盈亏": st.column_config.NumberColumn("毛盈亏", format="$%.2f"),
                "净盈亏": st.column_config.NumberColumn("净盈亏", format="$%.2f"),
                "最新原因": st.column_config.TextColumn("最新原因", width="large"),
            },
        )

with pnl_tab:
    st.subheader("期权盈亏拆解")
    st.caption("按昨晚窗口内的实际成交聚合，每个合约展示买入/卖出均价、平仓数量、剩余持仓和估算盈亏。")
    if review.option_pnl_breakdown.empty:
        st.info("本窗口没有可计算的期权成交盈亏。")
    else:
        pnl_values = pd.to_numeric(review.option_pnl_breakdown["净盈亏"], errors="coerce").fillna(0)
        with st.container(horizontal=True):
            st.metric("净盈亏", f"${pnl_values.sum():,.2f}", border=True)
            st.metric("盈利合约", int(pnl_values.gt(0).sum()), border=True)
            st.metric("亏损合约", int(pnl_values.lt(0).sum()), border=True)
            closed = pd.to_numeric(review.option_pnl_breakdown["平仓数量"], errors="coerce").fillna(0).gt(0)
            wins = pnl_values[closed].gt(0).sum()
            losses = pnl_values[closed].lt(0).sum()
            st.metric("已平胜率", f"{wins / max(1, wins + losses):.0%}", border=True)
            st.metric("毛盈利", f"${float(review.metrics['期权毛盈利']):,.2f}", border=True)
            st.metric("毛亏损", f"${float(review.metrics['期权毛亏损']):,.2f}", border=True)
        view = option_pnl_display_frame(review.option_pnl_breakdown)
        st.dataframe(
            view,
            hide_index=True,
            column_config={
                "买入均价": st.column_config.NumberColumn("买入均价", format="$%.2f"),
                "卖出均价": st.column_config.NumberColumn("卖出均价", format="$%.2f"),
                "买入金额": st.column_config.NumberColumn("买入金额", format="$%.2f"),
                "卖出金额": st.column_config.NumberColumn("卖出金额", format="$%.2f"),
                "毛盈亏": st.column_config.NumberColumn("毛盈亏", format="$%.2f"),
                "费用": st.column_config.NumberColumn("费用", format="$%.2f"),
                "净盈亏": st.column_config.NumberColumn("净盈亏", format="$%.2f"),
            },
        )

with orders_tab:
    st.subheader("订单执行质量")
    if review.orders.empty:
        st.info("本窗口没有订单。")
    else:
        view = china_time_columns(review.orders)
        st.dataframe(chinese_columns(view), hide_index=True)

with option_tab:
    st.subheader("波动率期权质量面板")
    st.caption("重点看当前盈亏、峰值盈利、盈利回吐、保护档和为什么没有卖。")
    if review.option_quality.empty:
        st.info("本窗口没有波动率期权记录。")
    else:
        view = display_percent_columns(
            review.option_quality,
            [
                "当前盈亏", "最大浮盈MFE", "峰值盈利", "浮盈捕获率",
                "盈利回吐", "阶梯锁盈线", "保护启动", "回吐阈值", "锁定盈利",
            ],
        )
        st.dataframe(view, hide_index=True)

with core_tab:
    st.subheader("Core Satellite 调仓质量")
    if review.core_quality.empty:
        st.info("本窗口没有 Core Satellite 调仓订单。")
    else:
        view = display_percent_columns(review.core_quality, ["当前权重", "目标权重"])
        st.dataframe(view, hide_index=True)

with runtime_tab:
    st.subheader("运行健康与断点定位")
    st.caption("用于区分：昨晚没交易到底是策略条件不满足，还是 OpenD/对账/期权历史采集等运行层退化。")
    if review.runtime_health.empty:
        st.info("本窗口没有服务任务记录。")
    else:
        view = review.runtime_health.copy()
        if "最后时间" in view:
            view["最后时间（中国）"] = pd.to_datetime(
                view["最后时间"], utc=True, errors="coerce",
            ).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M:%S")
            view = view.drop(columns=["最后时间"])
        st.dataframe(view, hide_index=True)

with interruption_tab:
    st.subheader("OpenD 中断与持仓保护追踪")
    st.caption("红色事件说明策略执行链路发生过中断；恢复确认会显示持仓保护何时完成恢复。")
    if review.interruptions.empty:
        st.success("本窗口没有 OpenD 断连或持仓保护事件。")
    else:
        st.error("本窗口出现过 OpenD/持仓保护事件，请确认恢复确认、订单对账和持仓对账都已完成。")
        st.dataframe(
            review.interruptions,
            hide_index=True,
            column_config={
                "说明": st.column_config.TextColumn("说明", width="large"),
            },
        )

with blockers_tab:
    st.subheader("阻塞原因")
    if review.blockers.empty:
        st.success("本窗口没有阻塞记录。")
    else:
        st.dataframe(review.blockers, hide_index=True)

with evidence_tab:
    st.subheader("回测/前测证据对照")
    st.caption("把复盘交易和现有实验、验证、审批证据放在同一处，避免只看夜盘结果调参数。")
    if review.evidence_alignment.empty:
        st.info("没有找到验证或证据记录。")
    else:
        view = china_time_columns(review.evidence_alignment)
        st.dataframe(
            view,
            hide_index=True,
            column_config={
                "关键指标": st.column_config.TextColumn("关键指标", width="large"),
                "结论": st.column_config.TextColumn("结论", width="large"),
            },
        )

with raw_tab:
    st.subheader("Markdown 原始报告")
    st.code(report_markdown, language="markdown")
