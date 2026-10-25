"""ML 机器学习模型监控、参数配置与 A/B 对照回测检验中心。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ml_freshness import (
    FRESHNESS_LABELS,
    FRESHNESS_OK,
    FRESHNESS_STALE,
    FRESHNESS_WARN,
    SCORING_DISABLED,
    SCORING_LABELS,
    SCORING_ML,
    SCORING_RULE_FALLBACK,
    freshness_summary_line,
    ml_model_health,
)
from futu_quant.ml_config import (
    FACTOR_SPECS,
    MODE_LABELS,
    MODE_ML,
    MODE_RULE,
    resolve_ml_factor,
    resolve_scoring_mode,
    set_ml_factor,
    set_scoring_mode,
)
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import get_current_tenant, render_admin_login, render_sidebar

st.set_page_config(page_title="ML 模型与回测验证中心", page_icon="🧠", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("🧠 机器学习 (ML) 监控与回测验证中心")
st.caption(
    "提供模型真实年龄与降级观测、多租户打分模式与因子配置，以及纯规则 vs ML 模型的 A/B 对照回测与生产准入单调性检验。"
)

# 生效租户：ML 是否启用按租户配置解析；拿不到就诚实显示"未识别"，不猜。
try:
    page_tenant_id = str(get_current_tenant(settings).get("tenant_id") or "")
except Exception:
    page_tenant_id = ""

health = ml_model_health()

freshness = str(health.get("freshness") or "")
_freshness_tone = {
    FRESHNESS_OK: "🟢",
    FRESHNESS_WARN: "🟠",
    FRESHNESS_STALE: "🔴",
}.get(freshness, "⚪")
age = health.get("age_days")
age_text = f"{age:.1f} 天" if isinstance(age, (int, float)) else "未知"
scoring_mode = str(health.get("scoring_mode") or "")

# ===========================================================================
# 顶部全局导航页签：将状态监控、回测大屏与参数配置清晰物理分流
# ===========================================================================
tab_status, tab_backtest, tab_factors = st.tabs(
    ["📌 模型状态与重训监控", "📊 规则 vs ML 对照回测大屏", "⚙️ 因子与租户参数配置"]
)

# ---------------------------------------------------------------------------
# Tab 1: 模型状态与重训监控
# ---------------------------------------------------------------------------
with tab_status:
    with st.container(horizontal=True, border=True):
        st.metric("新鲜度", f"{_freshness_tone} {FRESHNESS_LABELS.get(freshness, '未知')}", border=True)
        st.metric("模型年龄", age_text, border=True)
        st.metric("打分方式", SCORING_LABELS.get(scoring_mode, scoring_mode or "未知"), border=True)
        st.metric("sklearn", "可用" if health.get("sklearn_available") else "未安装", border=True)

    if scoring_mode == SCORING_RULE_FALLBACK:
        st.error("ML 评分未生效，当前为**规则降级**打分。降级不等于报错，但评分质量会下降。")
    elif scoring_mode == SCORING_DISABLED:
        st.info("ML 评分处于**显式关闭**状态（配置选择，不是故障）。")
    elif scoring_mode == SCORING_ML:
        st.success("ML 模型已加载并生效。")

    st.subheader("ML 打分开关（按租户）")
    st.caption(
        "**默认 `rule`（不显式开启就不上 ML）**：先跑通规则，再按需接入模型。"
        f"当前租户：`{page_tenant_id or '未识别'}`。切换只影响本租户，不影响其他租户。"
    )
    _configured_mode = resolve_scoring_mode(store, page_tenant_id)
    _selected_mode = st.radio(
        "打分方式",
        options=[MODE_RULE, MODE_ML],
        index=0 if _configured_mode == MODE_RULE else 1,
        format_func=lambda value: MODE_LABELS.get(value, value),
        horizontal=True,
        disabled=not admin,
        key="ml_scoring_mode_switch",
    )
    if _selected_mode != _configured_mode:
        if not admin:
            st.warning("需要管理员权限才能修改打分开关。")
        elif not page_tenant_id:
            st.error("无法解析当前租户，已拒绝写全局开关（会连带切换其他租户）。")
        else:
            try:
                written_key = set_scoring_mode(store, page_tenant_id, _selected_mode)
            except ValueError as exc:
                st.error(str(exc))
            else:
                store.audit(
                    "ML_SCORING_MODE_CHANGED",
                    {
                        "tenant_id": page_tenant_id,
                        "before": _configured_mode,
                        "after": _selected_mode,
                        "key": written_key,
                    },
                    actor="streamlit_admin:panel",
                )
                st.rerun()

    st.divider()

    st.subheader("重训控制")
    st.caption(
        "手动触发会在**后台子进程**中执行（不卡界面、不占服务主循环），并发触发由文件锁安全防护。"
    )
    _confirm = st.checkbox("我确认要触发一次重训（训练满意后将自动覆盖生产模型）", disabled=not admin)
    if st.button("手动触发重训", type="primary", disabled=not admin or not _confirm):
        import subprocess as _sp
        import sys as _sys
        from pathlib import Path as _Path

        _wrapper = _Path(__file__).resolve().parents[1] / "scripts" / "run_ml_retrain_wrapper.py"
        try:
            _sp.Popen(
                [_sys.executable, str(_wrapper), "--triggered-by", "panel", "--kind", "manual"],
                cwd=str(_wrapper.parents[1]),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            st.success("已提交后台重训，结果会实时刷新在下方历史表中。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"触发失败：{exc}")

    st.subheader("重训历史记录")
    _runs = store.ml_retrain_runs(limit=20)
    if _runs:
        st.dataframe(pd.DataFrame(_runs), hide_index=True, width="stretch")
    else:
        st.info("还没有重训记录。SKIP（缺依赖/已在跑）也会忠实记录在此。")

    with st.expander("🔍 为什么是这个状态（元数据底账与判据来源）"):
        detail_rows = [
            ["模型路径", health.get("model_path") or "—"],
            ["模型是否存在于磁盘", "是" if health.get("model_exists") else "否"],
            ["训练时间（pkl 内）", health.get("trained_at") or "未记录"],
            [
                "年龄来源",
                {
                    "pkl.trained_at": "pkl 内 trained_at（可信训练时间）",
                    "pkl.mtime": "pkl 文件修改时间（回落）",
                    "unknown": "未知 —— 读不到模型",
                }.get(str(health.get("age_source") or "unknown"), str(health.get("age_source"))),
            ],
            ["新鲜度档位", FRESHNESS_LABELS.get(freshness, freshness or "未知")],
            ["打分方式", SCORING_LABELS.get(scoring_mode, scoring_mode or "未知")],
            ["判定原因", health.get("scoring_reason") or "—"],
            ["降级原因", health.get("degradation_cause") or "—"],
            ["元数据 json 的 trained_at", health.get("metadata_json_trained_at") or "无"],
        ]
        st.dataframe(
            pd.DataFrame(detail_rows, columns=["字段", "值"]),
            hide_index=True, width="stretch",
        )
        st.caption(
            "⚠️ **元数据 json 的 `trained_at` 仅是「元数据刷新时间」，不是真实训练时间** —— "
            f"本系统年龄一律严格取自 pkl 内部时间戳：{freshness_summary_line(health)}"
        )


# ---------------------------------------------------------------------------
# Tab 2: 规则 vs ML 对照回测大屏
# ---------------------------------------------------------------------------
with tab_backtest:
    st.subheader("📊 规则 vs ML 对照回测与满足度检验 (A/B Alpha 验证)")
    st.caption(
        "**唯一控制变量**：同等初始资金、同等仓位、同等退出止损线（-25% / $300），"
        "唯一变量是候选标的打分排序口径（规则分 vs ML分）。量化检验 ML 模型是否带来稳定正向 Alpha。"
    )

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 2, 2])
    with col_ctrl1:
        window_choice = st.selectbox(
            "回测历史窗口",
            ["全部历史样本", "最近 90 天", "最近 60 天", "最近 30 天"],
            index=0,
            key="ml_backtest_window",
        )
    with col_ctrl2:
        init_cap = st.number_input(
            "初始本金 ($)",
            min_value=1000.0,
            max_value=1000000.0,
            value=10000.0,
            step=1000.0,
            key="ml_backtest_capital",
        )
    with col_ctrl3:
        top_k_val = st.number_input(
            "每日入选标的数 (Top K)",
            min_value=1,
            max_value=10,
            value=3,
            step=1,
            key="ml_backtest_topk",
        )

    if st.button("▶️ 开始对照回测与模型满足度检验", type="primary", key="btn_run_ml_comparison"):
        from datetime import datetime as _dt, timedelta as _td
        from futu_quant.ml_backtest_comparator import run_ml_vs_rule_comparison

        start_date_filter = None
        if window_choice == "最近 30 天":
            start_date_filter = (_dt.now() - _td(days=30)).strftime("%Y-%m-%d")
        elif window_choice == "最近 60 天":
            start_date_filter = (_dt.now() - _td(days=60)).strftime("%Y-%m-%d")
        elif window_choice == "最近 90 天":
            start_date_filter = (_dt.now() - _td(days=90)).strftime("%Y-%m-%d")

        with st.spinner("正在并行回放规则组与 ML 组历史盘口选品逻辑..."):
            try:
                res = run_ml_vs_rule_comparison(
                    store=store,
                    start_date=start_date_filter,
                    initial_capital=float(init_cap),
                    top_k=int(top_k_val),
                )
                st.session_state["ml_comparison_result"] = res
            except Exception as e:
                st.error(f"回测比对执行失败: {e}")

    res = st.session_state.get("ml_comparison_result")
    if res is not None:
        # 1. 本次回测结果（**不是**准入卡）：中性展示，不得用绿勾/红叉替页面背书，
        # 也不得写成「可实盘接入」这类准入结论（Chief 2026-10-25 裁定）。
        # 守卫 `tests/test_readiness_wording.py` 咬住「verdict 不得用 st.success/st.error」。
        rep = res.readiness_report
        st.info(f"### {rep['verdict_title']}\n{rep['verdict_desc']}")
        if rep.get("sample_trades") is not None:
            st.caption(
                f"样本量：{rep['sample_trades']} 笔 / {rep['sample_days']} 个交易日"
                "（交易日=去重后的日期数）；"
                f"准入参考：{rep.get('admission_reference')}"
            )

        # 数据质量必须可见（P0-B：行级缺值不抛，但丢弃数/原因不许静默）
        dq = getattr(res, "data_quality", {}) or {}
        if dq.get("unusable_rows"):
            detail = "、".join(
                f"{name}:{count}" for name, count in dq.get("missing_by_column", {}).items()
            )
            st.warning(
                f"⚠️ 数据质量：共 {dq['total_rows']} 条，行级缺值丢弃 "
                f"{dq['unusable_rows']} 条（可用 {dq['usable_rows']} 条）；缺值列：{detail}"
            )
        elif dq:
            st.caption(f"数据质量：{dq['total_rows']} 条全部可用（无行级缺值）")

        # 兜底 rule 的 tie-break 落地质量（Chief 2026-10-25 裁定 ②）：
        # 模型加载失败时由它接管选品，realised 分位必须可见；但它是**单次抽样**，
        # 只作监控、不作「模型是否可用」的判据，更不许据此调盐（见 data/qa 报告）。
        tq = getattr(res, "tiebreak_quality", {}) or {}
        if tq.get("status") == "ok":
            segments = tq.get("segments") or []
            ok_segments = [s for s in segments if s.get("status") == "ok"]
            segment_text = ""
            if ok_segments:
                fused_values = [s["fused_percentile"] for s in ok_segments]
                segment_text = (
                    f"；按交易日 5 段实测 {len(ok_segments)}/{len(segments)} 段可用，"
                    f"融合分位 {min(fused_values):.0f}%~{max(fused_values):.0f}%"
                )
            st.caption(
                f"兜底 rule tie-break（K={tq.get('fusion_k')} 盐融合）："
                f"realised 分位 {tq.get('fused_percentile'):.1f}%"
                f"（单盐 {tq.get('single_salt_percentile'):.1f}%，"
                f"{tq.get('groups')} 组 / {tq.get('pairs')} 对）"
                f"{segment_text} —— 单份数据抽样值，仅监控不作判据"
            )
        elif tq:
            st.caption(
                f"兜底 rule tie-break 监控不可用：{tq.get('reason')}"
                f"（缺失列 {tq.get('missing')}）"
                " —— 已显式记录，不静默"
            )

        # 滚动统计（Chief 2026-10-25 裁定 ②）：口径必须是「最近 N 条采样」而非
        # 「最近 30 个自然日」。数据来源二：本页手动点按钮 + 收盘后调度审计
        # （`scripts/ml_tiebreak_audit.py`，两者同一 event_type，此处自动合并）。
        # Reviewer 口径：**采样率 = 训练 CSV 导出频率，不是每日**——CSV 不重导时每天只
        # 会 skipped_duplicate，不产生新行，所以不许按「每天 1 条」核查。
        try:
            import json as _json

            from futu_quant.ml_scorer import summarise_tiebreak_history

            history_payloads = []
            for row in reversed(store.audit_events(500) or []):
                if row.get("event_type") != "RULE_TIEBREAK_REALISED_QUALITY":
                    continue
                try:
                    history_payloads.append(_json.loads(row.get("payload_json") or "{}"))
                except (TypeError, ValueError):
                    continue
            summary = summarise_tiebreak_history(history_payloads)
            if summary["status"] == "ok":
                st.caption(
                    f"滚动统计（口径=最近 {summary['last_n']} 条采样，**不是**最近 30 个自然日）："
                    f"融合分位均值 {summary['fused_mean_percentile']:.1f}%"
                    f"（区间 {summary['fused_min_percentile']:.0f}%~"
                    f"{summary['fused_max_percentile']:.0f}%）；"
                    f"单盐均值 {summary['single_mean_percentile']:.1f}%；"
                    f"样本 {summary['samples']} 条。"
                    "来源：**手动运行**对照回测 + 收盘后调度审计；"
                    "**采样率与训练 CSV 导出频率一致**（CSV 不重导时每日只记 skipped_duplicate，"
                    "不产生新行，故不能按「每天 1 条」核查）。"
                )
            else:
                st.caption(
                    "滚动统计：暂无已入库的 realised 分位记录 —— "
                    "来源为**手动运行**对照回测与收盘后调度审计；"
                    "**采样率与训练 CSV 导出频率一致**，未重导训练数据就不会有新行。"
                )
        except Exception as exc:  # noqa: BLE001
            st.caption(f"滚动统计读取失败（已显式显示，不静默）：{type(exc).__name__}: {exc}")

        with st.expander("🔍 生产准入硬指标核验清单 (Checklist)", expanded=True):
            st.dataframe(pd.DataFrame(rep["checklist"]), hide_index=True, width="stretch")

        # 2. 核心绩效对决矩阵
        st.markdown("#### 🏆 核心绩效正面对决 (A/B Benchmark Matrix)")
        r_m = res.rule_metrics
        m_m = res.ml_metrics

        metric_rows = [
            ["总累计收益率", f"{r_m['total_return_pct']:+.2f}%", f"{m_m['total_return_pct']:+.2f}%", f"{m_m['total_return_pct'] - r_m['total_return_pct']:+.2f}%"],
            ["交易胜率", f"{r_m['win_rate']:.1f}%", f"{m_m['win_rate']:.1f}%", f"{m_m['win_rate'] - r_m['win_rate']:+.1f}%"],
            ["盈亏比", f"{r_m['profit_loss_ratio']:.2f}", f"{m_m['profit_loss_ratio']:.2f}", f"{m_m['profit_loss_ratio'] - r_m['profit_loss_ratio']:+.2f}"],
            ["最大回撤", f"{r_m['max_drawdown_pct']:.2f}%", f"{m_m['max_drawdown_pct']:.2f}%", f"{m_m['max_drawdown_pct'] - r_m['max_drawdown_pct']:+.2f}%"],
            ["夏普比率", f"{r_m['sharpe']:.2f}", f"{m_m['sharpe']:.2f}", f"{m_m['sharpe'] - r_m['sharpe']:+.2f}"],
            ["平均单笔盈亏", f"${r_m['avg_pnl']:+,.2f}", f"${m_m['avg_pnl']:+,.2f}", f"${m_m['avg_pnl'] - r_m['avg_pnl']:+,.2f}"],
            ["有效交易笔数", f"{r_m['trades_count']} 笔", f"{m_m['trades_count']} 笔", f"{m_m['trades_count'] - r_m['trades_count']:+d} 笔"],
            ["评分相关系数 (Rank IC)", f"{res.rule_ic:.4f}", f"{res.rank_ic:.4f}", f"{res.rank_ic - res.rule_ic:+.4f}"],
        ]
        st.dataframe(
            pd.DataFrame(metric_rows, columns=["检验维度", "基准组 (纯规则打分)", "实验组 (ML模型打分)", "ML 净超额 (Alpha)"]),
            hide_index=True, width="stretch",
        )

        # 3. 净值走势图
        st.markdown("#### 📈 资金净值走势叠加对比")
        st.line_chart(res.equity_series.set_index("日期"), height=320)

        # 4. 五档单调性检验
        st.markdown("#### 🎯 ML 分数五档收益单调性检验 (Quintile Monotonicity)")
        st.caption("将候选样本按 ML 分数切分为 5 档（Q1最高 ➔ Q5最低）。理想模型要求实际收益必须呈现单调递减（高分组赚钱、低分组亏钱），杜绝反向失效与过拟合。")
        if not res.quintile_returns.empty:
            q_plot_df = res.quintile_returns.copy()
            q_plot_df["收益率($)"] = q_plot_df["avg_pnl"] * 100.0
            st.bar_chart(q_plot_df.set_index("quintile")["收益率($)"], height=260)
            st.dataframe(
                res.quintile_returns.rename(columns={"quintile": "分档", "avg_pnl": "平均单笔期权盈亏($)", "win_rate": "分档胜率", "count": "样本数"}),
                hide_index=True, width="stretch",
            )

        # 5. 选品分歧归因账本
        st.markdown("#### 🔍 选品分歧与归因账本 (Divergence Ledger)")
        st.caption("清晰呈现规则与 ML 判定相左的具体标的案例：ML 避雷（规则看好但被 ML 过滤）与 ML 淘金（规则落选但被 ML 提拔）。")
        if res.divergence_records:
            st.dataframe(pd.DataFrame(res.divergence_records), hide_index=True, width="stretch")
        else:
            st.info("所选窗口内两套模型选品一致性极高，未出现显著分歧标的。")
    else:
        st.info("点击上方「▶️ 开始对照回测与模型满足度检验」按钮，即刻在当前历史数据上并行回放两套选品逻辑。")


# ---------------------------------------------------------------------------
# Tab 3: 因子与租户参数配置
# ---------------------------------------------------------------------------
with tab_factors:
    st.subheader("ML 因子参数配置（按租户）")
    st.caption(
        "这些值全部走**租户级三层解析**（租户键 → 全局键 → 默认），改动只影响当前租户，"
        "并记录审计事件 `ML_FACTOR_CHANGED`。"
    )
    with st.form("ml_factor_form", border=True):
        _current_values = {
            name: resolve_ml_factor(store, page_tenant_id, name) for name in FACTOR_SPECS
        }
        _inputs = {}
        for _name, _spec in FACTOR_SPECS.items():
            if _spec["kind"] == "str":
                _inputs[_name] = st.text_input(
                    _spec["label"], value=str(_current_values[_name]), disabled=not admin,
                )
            else:
                _inputs[_name] = st.number_input(
                    _spec["label"],
                    value=float(_current_values[_name]),
                    min_value=float(_spec["min"]), max_value=float(_spec["max"]),
                    step=1.0 if _spec["kind"] == "int" else 0.01,
                    disabled=not admin,
                )
        _saved = st.form_submit_button("保存 ML 配置", type="primary", disabled=not admin)
    if _saved:
        if not page_tenant_id:
            st.error("无法解析当前租户，已拒绝写全局配置。")
        else:
            _changed: list[dict] = []
            for _name, _value in _inputs.items():
                try:
                    _key = set_ml_factor(store, page_tenant_id, _name, _value)
                except (ValueError, KeyError) as exc:
                    st.error(f"{_name}: {exc}")
                    continue
                _changed.append({"factor": _name, "value": _value, "key": _key})
            if _changed:
                store.audit(
                    "ML_FACTOR_CHANGED",
                    {"tenant_id": page_tenant_id, "changes": _changed},
                    actor="streamlit_admin:panel",
                )
                st.success(f"已保存 {len(_changed)} 项（仅影响租户 {page_tenant_id}）。")

    st.divider()

    st.subheader("租户 ML 运行模式总览")
    st.caption("直接查看所有注册租户当前分配的打分模式、模型路径与入选阈值。")
    from futu_quant.tenant import active_tenants as _active_tenants

    _rows = []
    for _profile in _active_tenants():
        _mode = resolve_scoring_mode(store, _profile.tenant_id)
        _rows.append(
            {
                "租户": _profile.tenant_id,
                "环境": getattr(_profile, "trading_env", "?"),
                "打分方式": MODE_LABELS.get(_mode, _mode),
                "模型路径": resolve_ml_factor(store, _profile.tenant_id, "model_path"),
                "打分阈值": resolve_ml_factor(store, _profile.tenant_id, "ml_threshold"),
            }
        )
    if _rows:
        st.dataframe(pd.DataFrame(_rows), hide_index=True, width="stretch")
