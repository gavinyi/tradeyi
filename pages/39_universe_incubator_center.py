"""
标的孵化与赛马中心 (Dynamic Universe Expansion & Sandbox Incubator Dashboard)
"""
from __future__ import annotations

from datetime import datetime, timezone
import pandas as pd
import streamlit as st

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS, Settings
from futu_quant.store import TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar
from futu_quant.universe.lifecycle_manager import LifecycleManager
from futu_quant.universe.recall_scanner import RecallScanner

st.set_page_config(page_title="标的孵化与赛马中心", page_icon="🚀", layout="wide")
settings = Settings.load()
render_sidebar(settings)
admin = render_admin_login()

st.title("标的孵化与赛马中心")
st.caption("全市场期权高流动性召回 (Top 50) $\\to$ 隔离沙盒影子演练 (10日) $\\to$ ML 截面精排 $\\to$ 多样性打散晋级实盘交易池。")

store = TradingStore()
broker = FutuPaperBroker(settings)

# KPI 统计行
candidates = store.candidate_symbols()
incubator_rows = store.all_incubator_symbols()
promoted_rows = store.active_promoted_symbols()
core_symbols = list(DEFAULT_VOLATILITY_SYMBOLS)

with st.container(horizontal=True):
    st.metric("核心底仓 (永久常驻)", f"{len(core_symbols)} 只", help="底仓 13 只标的坚如磐石，永不淘汰")
    st.metric("生效外援 (动态晋级)", f"{len(promoted_rows)} / 5~10 只", help="通过沙盒实测与 ML 精排授予实盘资格的外援标的（行情火爆时自适应扩容至最多10只）")
    st.metric("沙盒影子孵化中", f"{len(incubator_rows)} 只", help="在后台静默记录微观滑点与突破胜率的候选标的")
    eligible_count = sum(1 for r in incubator_rows if r.get("promotion_eligible", 0) == 1)
    st.metric("满足晋级硬门槛", f"{eligible_count} 只", help="双轨准入：⚡ 暴风快轨 (3日极速转正) 或 🐢 常规慢轨 (7~10日稳健考察)")
    st.metric("全市场召回池", f"{len(candidates)} 只", help="最新一周美股期权成交量前 50 名优质流动性标的")

# 四大视图 Tab
tab_active, tab_incubator, tab_recall, tab_events = st.tabs([
    "🎯 当前实盘交易池",
    "🧪 沙盒影子赛马榜",
    "🌐 全市场 Top 50 召回池",
    "📜 生命周期流转审计",
])

# ---------------- Tab 1: 当前实盘交易池 ----------------
with tab_active:
    st.subheader("当前实盘交易池结构 (Core ∪ Promoted)")
    st.caption("实际交易标的池 = 永久核心底仓 (13只) ∪ 当前有效晋级外援 (最多 5 只)。")

    col_core, col_satellite = st.columns([1, 1])

    with col_core:
        with st.container(border=True):
            st.markdown("#### 🏛️ 永久核心底仓 (13只)")
            st.caption("系统的压舱石，不参与任何全市场淘汰机制，盘中高频持续监控。")
            core_df = pd.DataFrame([
                {
                    "标的代码": sym,
                    "分类定位": "指数压舱石" if sym in {"US.SPY", "US.QQQ", "US.GLD"} else
                                "高弹性先锋" if sym == "US.COIN" else
                                "硬科技算力" if sym in {"US.NVDA", "US.AMD", "US.AVGO", "US.TSM"} else "科技现金牛",
                    "历史实盘贡献": "+$1,496 (最佳)" if sym == "US.COIN" else
                                    "+$814 (稳健)" if sym == "US.AMD" else
                                    "+$604 (单笔+140%)" if sym == "US.QQQ" else "正收益稳健",
                    "状态": "永久有效",
                }
                for sym in core_symbols
            ])
            st.dataframe(core_df, hide_index=True, width="stretch")

    with col_satellite:
        with st.container(border=True):
            st.markdown("#### 🚀 动态晋级外援 (当前生效)")
            st.caption("通过沙盒 10 日实战演练并在 ML 积分榜脱颖而出的特邀黑马标的。")
            if not promoted_rows:
                st.info("当前暂无生效外援。沙盒正在对候选标的进行微观演练，达标后将自动晋级。")
            else:
                sat_data = []
                for p in promoted_rows:
                    sat_data.append({
                        "外援标的": p["symbol"],
                        "行业板块": p.get("industry", "OTHER"),
                        "晋级打分": f"{float(p.get('promotion_score', 0)):.2f}",
                        "晋级时间": str(p.get("promoted_at", ""))[:16],
                        "授权到期": str(p.get("expires_at", ""))[:16],
                        "状态": p.get("status", "ACTIVE"),
                    })
                st.dataframe(pd.DataFrame(sat_data), hide_index=True, width="stretch")

            if admin and promoted_rows:
                st.markdown("##### 外援应急下架操作 (管理员)")
                target_evict = st.selectbox("选择要手动清退的外援标的", [p["symbol"] for p in promoted_rows], key="select_evict")
                evict_reason = st.text_input("清退理由", value="管理员手动移出外援", key="input_evict_reason")
                if st.button("确认清退该外援", type="secondary", icon=":material/delete_forever:"):
                    mgr = LifecycleManager(store=store)
                    store.evict_promoted_symbol(target_evict, evict_reason)
                    mgr.sync_to_runtime_universe()
                    st.success(f"外援标的 {target_evict} 已从实盘池安全清退！")
                    st.rerun()

# ---------------- Tab 2: 沙盒影子赛马榜 ----------------
with tab_incubator:
    st.subheader("隔离沙盒影子演练赛马榜 (Sandbox Leaderboard)")
    st.caption("在后台静默捕获真实突破信号与微观滑点，不花一分真钱验证真实胜率与回撤。")

    if not incubator_rows:
        st.info("沙盒当前暂无记录。点击下方按钮或等待周度任务导入候选标的。")
    else:
        inc_data = []
        for r in incubator_rows:
            wins = int(r.get("simulated_wins", 0))
            losses = int(r.get("simulated_losses", 0))
            total = wins + losses
            win_rate_str = f"{wins / total:.1%}" if total > 0 else "—"
            pnl = float(r.get("simulated_pnl_usd", 0.0))
            max_dd = float(r.get("simulated_max_dd", 0.0))
            eligible = bool(r.get("promotion_eligible", 0))

            inc_data.append({
                "标的代码": r["symbol"],
                "观察天数": f"{r.get('observation_days', 0)} 天",
                "赛马轨道": "⚡ 暴风快轨 (3日)" if r.get("track_mode") == "FAST_TRACK" else "🐢 常规慢轨 (7~10日)" if r.get("track_mode") == "STANDARD" else "⏳ 孵化中",
                "突破信号数": r.get("signal_triggers", 0),
                "虚拟胜率": win_rate_str,
                "虚拟净收益": f"{pnl:+.2f} 美元",
                "虚拟最大回撤": f"{max_dd:.1%} (门槛<25%)",
                "平均滑点": f"{float(r.get('avg_slippage_pct', 0)):.2%}",
                "量能留存率": f"{float(r.get('volume_retention_ratio', 0)):.1%}",
                "最新 ML 得分": f"{float(r.get('latest_ml_score', 0)):.4f}",
                "晋级硬门槛": "⚡ 快轨达标" if r.get("track_mode") == "FAST_TRACK" else "✅ 慢轨达标" if eligible else "⏳ 孵化中",
            })

        inc_df = pd.DataFrame(inc_data).sort_values("最新 ML 得分", ascending=False)
        st.dataframe(inc_df, hide_index=True, width="stretch")

# ---------------- Tab 3: 全市场 Top 50 召回池 ----------------
with tab_recall:
    st.subheader("全市场期权流动性 Top 50 粗排召回池")
    st.caption("每周日晚从全美股期权总成交量榜抓取，筛选平值价差 <= 3.0%、成交额 > $5亿的流动性白马。")

    if not candidates:
        st.info("当前暂无周度召回数据。")
    else:
        rec_data = []
        for c in candidates:
            rec_data.append({
                "交易周": c.get("week_start", ""),
                "标的代码": c["symbol"],
                "公司名称": c.get("underlying_name", ""),
                "所属板块": c.get("industry", "OTHER"),
                "期权日均量": f"{float(c.get('option_daily_volume', 0)):,.0f}",
                "期权持仓量": f"{float(c.get('open_interest', 0)):,.0f}",
                "正股成交额": f"${float(c.get('stock_turnover_usd', 0)) / 1e8:.2f} 亿",
                "平值价差": f"{float(c.get('median_spread_pct', 0)):.2%}",
                "状态": c.get("status", "ACTIVE"),
            })
        st.dataframe(pd.DataFrame(rec_data), hide_index=True, width="stretch")

# ---------------- Tab 4: 生命周期流转审计 ----------------
with tab_events:
    st.subheader("标的池生命周期流转审计流水")
    st.caption("完整记录召回、沙盒评估、晋级实盘、秒踢清退与运行时同步的全部审计事件。")

    events = store.universe_lifecycle_events(limit=200)
    if not events:
        st.info("暂无生命周期事件记录。")
    else:
        evt_data = []
        for e in events:
            evt_data.append({
                "时间": e.get("created_at", ""),
                "事件类型": e.get("event_type", ""),
                "涉及标的": e.get("symbol", ""),
                "详情负载": e.get("details_json", "{}"),
            })
        st.dataframe(china_time_columns(pd.DataFrame(evt_data)).rename(columns={"时间": "时间（中国）"}), hide_index=True, width="stretch")

# ---------------- 快捷运维操作区 ----------------
st.divider()
with st.expander("🛠️ 离线批处理与手动触发运维 (管理员)"):
    st.caption("支持手动提前触发周度召回、日度 ML 精排打散或同步运行时标的池。")
    col1, col2, col3 = st.columns(3)

    if col1.button("执行周度全市场 Top 50 召回", icon=":material/download:", disabled=not admin):
        with st.spinner("正在拉取全市场期权成交前 50 标的..."):
            scanner = RecallScanner(broker=broker, store=store)
            week_str = datetime.now(timezone.utc).date().isoformat()
            recalled = scanner.run_weekly_recall(week_str)
            st.success(f"周度召回完成！共筛选出 {len(recalled)} 只高流动性标的。")
            st.rerun()

    if col2.button("执行盘后 ML 精排与轮换仲裁", icon=":material/psychology:", disabled=not admin):
        with st.spinner("正在对沙盒候选打分并执行 MMR 行业打散..."):
            from futu_quant.scheduled_tasks import run_daily_universe_ranking
            msg = run_daily_universe_ranking(store=store)
            st.success(msg)
            st.rerun()

    if col3.button("一键同步标的池至运行时", icon=":material/sync:", disabled=not admin):
        mgr = LifecycleManager(store=store)
        merged_pool = mgr.sync_to_runtime_universe()
        st.success(f"标的池已实时生效！当前共 {len(merged_pool)} 只。")
        st.rerun()
