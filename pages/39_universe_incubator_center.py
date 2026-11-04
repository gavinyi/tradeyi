"""
标的孵化与赛马中心 (Dynamic Universe Expansion & Sandbox Incubator Dashboard)
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pandas as pd
import streamlit as st

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS, Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar
from futu_quant.universe.lifecycle_manager import LifecycleManager
from futu_quant.universe.recall_scanner import RecallScanner

st.set_page_config(page_title="标的孵化与赛马中心", page_icon="🚀", layout="wide")
settings = Settings.load_cached()
render_sidebar(settings)
admin = render_admin_login()

st.title("标的孵化与赛马中心")
st.caption("全市场期权高流动性召回 (Top 50) $\\to$ 隔离沙盒影子演练 (10日) $\\to$ ML 截面精排 $\\to$ 多样性打散晋级实盘交易池。")

store = TradingStore()
broker = FutuPaperBroker(settings)

# KPI 统计行
candidates = store.candidate_symbols()
incubator_rows = store.all_incubator_symbols()
delivery_tenant_id = "futu_paper_sandbox"
promoted_rows = store.active_promoted_symbols(delivery_tenant_id)
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
                    mgr = LifecycleManager(store=store, tenant_id=delivery_tenant_id)
                    store.evict_promoted_symbol(delivery_tenant_id, target_evict, evict_reason)
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

def _safe_float(v: object, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _format_lifecycle_event(event: dict) -> dict:
    created_at = event.get("created_at", "")
    event_type = event.get("event_type", "")
    symbol = event.get("symbol", "")
    raw_details = event.get("details_json", "{}")
    try:
        details = json.loads(raw_details) if isinstance(raw_details, str) else (raw_details or {})
    except Exception:
        details = {}

    display_symbol = "全部 / 系统" if symbol in ("ALL", "", None) else symbol

    ACTION_MAP = {
        "PROMOTION": ("🚀 晋级实盘池", "PROMOTION"),
        "FAST_EVICTION": ("🚨 紧急秒踢清退", "FAST_EVICTION"),
        "DEMOTION": ("📉 自然淘汰/降级", "DEMOTION"),
        "PROMOTION_REJECTED_OVER_EXTENDED": ("⛔ 动量耗竭否决", "OVER_EXTENDED"),
        "SANDBOX_SIGNAL_CAPTURED": ("🎯 沙盒突破信号", "SIGNAL"),
        "SYNC_RUNTIME": ("🔄 热生效同步", "SYNC"),
        "RECALL_COMPLETED": ("📥 周度召回完成", "RECALL"),
        "FEISHU_REVIEW_DELIVERED": ("📢 飞书复盘投递", "NOTIFICATION"),
        "PROFIT_SWEEPER_SETTLED": ("💰 收益收割归集", "SETTLEMENT"),
    }
    action_label, category = ACTION_MAP.get(event_type, (f"ℹ️ {event_type}", "OTHER"))

    reason = ""
    params = ""

    if event_type == "PROMOTION":
        score = _safe_float(details.get("score"), 0.0)
        track = details.get("track_mode", "常规")
        industry = details.get("industry", "其他")
        replaced = details.get("replaced_symbol")
        if replaced:
            reason = f"评分 {score:.2f} 达成迟滞替换门槛，替换现任标的 {replaced} 晋级外援实盘池"
            params = f"综合评分: {score:.2f} | 模式: {track} | 替换: {replaced}"
        else:
            reason = f"综合评分 {score:.2f}，通过沙盒孵化({track})晋级外援实盘池（{industry}板块）"
            expires = str(details.get("expires_at", ""))[:10]
            params = f"综合评分: {score:.2f} | 模式: {track} | 授权至: {expires}"

    elif event_type == "FAST_EVICTION":
        raw_reason = details.get("reason", "触发应急风控红线")
        reason = f"触发硬风控：{raw_reason}"
        params = "风控状态: 触发应急秒踢并移出实盘池"

    elif event_type == "DEMOTION":
        raw_reason = details.get("reason", "授权到期自然降级")
        reason = f"生命周期调整：{raw_reason}"
        params = "流转状态: 自然淘汰出池"

    elif event_type == "PROMOTION_REJECTED_OVER_EXTENDED":
        reason = "近期涨幅过大进入动量耗竭期，触发防追高门禁一票否决"
        params = "门禁裁定: 一票否决禁止高位接盘"

    elif event_type == "SANDBOX_SIGNAL_CAPTURED":
        intraday = details.get("intraday_move", "")
        spread = details.get("spread_pct", "")
        price = _safe_float(details.get("last_price"), 0.0)
        price_str = f"${price:,.2f}" if price > 0 else "—"
        reason = f"沙盒捕获突破：日内异动 {intraday}，买卖价差 {spread}，现价 {price_str}"
        params = f"异动: {intraday} | 价差: {spread} | 现价: {price_str}"

    elif event_type == "SYNC_RUNTIME":
        core = details.get("core_count", 0)
        sat = details.get("satellite_count", 0)
        tot = len(details.get("total_symbols", []))
        reason = f"实盘标的池已热生效同步：底仓 {core} 只 + 晋级外援 {sat} 只 = 共 {tot} 只"
        params = f"底仓: {core} | 外援: {sat} | 总计: {tot}"

    elif event_type == "RECALL_COMPLETED":
        count = details.get("recalled_count", 0)
        week = details.get("week_start", "")
        reason = f"全市场周度初筛完成：筛选出 {count} 只高流动性标的 (周期: {week})"
        params = f"初筛入选: {count} 只"

    elif event_type == "FEISHU_REVIEW_DELIVERED":
        date_str = details.get("session_date", "")
        reason = f"日度量化复盘报告已成功推送到飞书群 ({date_str})"
        params = f"复盘日期: {date_str} | 状态: 投递成功"

    elif event_type == "PROFIT_SWEEPER_SETTLED":
        reason = details.get("reason", "完成日度超额收益锁定与利润归集")
        params = "归集状态: 已对齐完成"

    else:
        reason = details.get("reason") or details.get("message") or "系统自动审计事件"
        params = ", ".join(f"{k}: {v}" for k, v in list(details.items())[:3]) or "——"

    return {
        "created_at": created_at,
        "标的代码": display_symbol,
        "生命周期动作": action_label,
        "决策原因与业务影响": reason,
        "审计核验 / 关键参数": params,
        "_raw_symbol": symbol,
        "_event_type": event_type,
        "_category": category,
        "_details_json": raw_details if isinstance(raw_details, str) else json.dumps(raw_details, ensure_ascii=False),
    }


# ---------------- Tab 4: 生命周期流转审计 ----------------
with tab_events:
    st.subheader("标的池生命周期流转审计流水")
    st.caption("基于机构级风控标准，完整记录高流动性召回、沙盒影子评估、晋级实盘、风控秒踢与全系统热生效审计。")

    raw_events = store.universe_lifecycle_events(limit=500)
    if not raw_events:
        st.info("暂无生命周期事件记录。")
    else:
        today_utc_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if hasattr(store, "universe_lifecycle_summary"):
            stats = store.universe_lifecycle_summary(today_prefix=today_utc_prefix)
        else:
            try:
                with store.connection() as connection:
                    rows = connection.execute(
                        "SELECT event_type, count(*) FROM universe_lifecycle_events GROUP BY event_type"
                    ).fetchall()
                    stats = {str(r[0]): int(r[1]) for r in rows}
                    row = connection.execute(
                        "SELECT count(*) FROM universe_lifecycle_events WHERE created_at LIKE ?",
                        (f"{today_utc_prefix}%",),
                    ).fetchone()
                    stats["_today"] = int(row[0]) if row and row[0] is not None else 0
            except Exception:
                stats = {
                    "_today": sum(1 for e in raw_events if str(e.get("created_at", "")).startswith(today_utc_prefix)),
                    "PROMOTION": sum(1 for e in raw_events if e.get("event_type") == "PROMOTION"),
                    "FAST_EVICTION": sum(1 for e in raw_events if e.get("event_type") == "FAST_EVICTION"),
                    "DEMOTION": sum(1 for e in raw_events if e.get("event_type") == "DEMOTION"),
                    "SANDBOX_SIGNAL_CAPTURED": sum(1 for e in raw_events if e.get("event_type") == "SANDBOX_SIGNAL_CAPTURED"),
                }
        today_count = stats.get("_today", 0)
        promotion_count = stats.get("PROMOTION", 0)
        eviction_count = stats.get("FAST_EVICTION", 0) + stats.get("DEMOTION", 0)
        signal_count = stats.get("SANDBOX_SIGNAL_CAPTURED", 0)

        kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
        kpi_col1.metric("今日事件总数", f"{today_count} 项", help="今日(UTC自然日)全系统触发的全部生命周期与审计动作")
        kpi_col2.metric("外援实盘晋级", f"{promotion_count} 次", help="历史全量真实累计通过沙盒孵化与ML精排晋级外援实盘池的次数")
        kpi_col3.metric("风控秒踢 / 清退", f"{eviction_count} 次", help="历史全量真实累计触发价差恶化、连续硬止损或授权到期的移出次数")
        kpi_col4.metric("沙盒异动捕获", f"{signal_count} 次", help="沙盒在全市场高流动性标的中实时捕获的突破信号全量真实累计")

        st.markdown("---")

        parsed_list = []
        for e in raw_events:
            try:
                parsed_list.append(_format_lifecycle_event(e))
            except Exception as exc:
                parsed_list.append({
                    "created_at": e.get("created_at", ""),
                    "标的代码": e.get("symbol", "全部 / 系统"),
                    "生命周期动作": f"⚠️ {e.get('event_type', 'UNKNOWN')}",
                    "决策原因与业务影响": f"原始数据解析异常已隔离: {exc}",
                    "审计核验 / 关键参数": "——",
                    "_raw_symbol": e.get("symbol", ""),
                    "_event_type": e.get("event_type", ""),
                    "_category": "ERROR",
                    "_details_json": str(e.get("details_json", "")),
                })
        df_all = pd.DataFrame(parsed_list)
        if "created_at" in df_all.columns:
            df_all = china_time_columns(df_all)
            df_all["时间（中国）"] = pd.to_datetime(df_all["created_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S").fillna(df_all["created_at"])
        else:
            df_all["时间（中国）"] = ""

        f_col1, f_col2 = st.columns(2)

        unique_symbols = sorted([s for s in df_all["_raw_symbol"].dropna().unique() if s])
        symbol_options = ["全部标的"] + [s for s in unique_symbols if s != "ALL"] + (["全部 / 系统 (ALL)"] if "ALL" in unique_symbols else [])
        selected_sym = f_col1.selectbox("🎯 标的筛选", options=symbol_options, key="filter_universe_sym")

        CATEGORY_OPTIONS = {
            "全部事件": None,
            "🚀 晋级实盘 (PROMOTION)": ["PROMOTION"],
            "🚨 风控秒踢 (FAST_EVICTION)": ["FAST_EVICTION"],
            "📉 淘汰降级 (DEMOTION)": ["DEMOTION"],
            "⛔ 动量耗竭否决 (OVER_EXTENDED)": ["PROMOTION_REJECTED_OVER_EXTENDED"],
            "🎯 沙盒突破信号 (SIGNAL)": ["SANDBOX_SIGNAL_CAPTURED"],
            "🔄 池同步与召回 (SYNC/RECALL)": ["SYNC_RUNTIME", "RECALL_COMPLETED"],
        }
        selected_cat_label = f_col2.selectbox("🏷️ 事件分类筛选", options=list(CATEGORY_OPTIONS.keys()), key="filter_universe_cat")
        target_event_types = CATEGORY_OPTIONS[selected_cat_label]

        df_filtered = df_all.copy()
        if selected_sym != "全部标的":
            if selected_sym == "全部 / 系统 (ALL)":
                df_filtered = df_filtered[df_filtered["_raw_symbol"] == "ALL"]
            else:
                df_filtered = df_filtered[df_filtered["_raw_symbol"] == selected_sym]

        if target_event_types is not None:
            df_filtered = df_filtered[df_filtered["_event_type"].isin(target_event_types)]

        st.caption(f"当前筛选共呈现 **{len(df_filtered)}** 条结构化生命周期审计记录。")

        display_columns = ["时间（中国）", "标的代码", "生命周期动作", "决策原因与业务影响", "审计核验 / 关键参数"]
        st.dataframe(
            df_filtered[display_columns],
            hide_index=True,
            width="stretch",
            column_config={
                "时间（中国）": st.column_config.TextColumn("时间（中国）", width="medium"),
                "标的代码": st.column_config.TextColumn("标的代码", width="small"),
                "生命周期动作": st.column_config.TextColumn("生命周期动作", width="small"),
                "决策原因与业务影响": st.column_config.TextColumn("决策原因与业务影响", width="large"),
                "审计核验 / 关键参数": st.column_config.TextColumn("审计核验 / 关键参数", width="medium"),
            },
        )

        with st.expander("🔍 原始技术负载排障抽检 (研发与运维专用)"):
            st.caption("展示最新筛选事件的技术负载与 Raw JSON，用于底层微观数据对账。")
            tech_view = df_filtered[["时间（中国）", "标的代码", "_event_type", "_details_json"]].rename(
                columns={"_event_type": "原始事件码", "_details_json": "原始JSON负载"}
            )
            st.dataframe(tech_view.head(30), hide_index=True, width="stretch")

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
