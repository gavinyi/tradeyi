"""信号回测（历史信号重放验证策略盈亏）—— 回测引擎 signal_backtest 的 UI 壳。

产品口径（PRD §8）：**按用户要回答的两个问题组织，不按代码模块组织**：
- 首屏先给结论一句话（回测可信 / 不可信 / 样本不足）；
- Tab A「这次回测可信吗」：信号对齐率、成交对账率、差异原因、数据等级占比；
- Tab B「调参会不会变好」：基准 vs 候选；**自校验未过时禁用**（拿不可信结果
  做决策是 PRD 红线）；
- 参数全部预填默认值，什么都不改点一下就能跑；
- 全程只读（open_trading_store(read_only=True)），不占实盘行情接口。

注意：本页是 `scripts/backtest_cli.py` 的 UI 壳，核心判定全部复用
`futu_quant.signal_backtest`——页面不重复实现判定逻辑。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.signal_backtest import replay as bt_replay
from futu_quant.ui import (
    china_time_columns,
    open_trading_store,
    render_sidebar,
    render_sidebar_tenant_switcher,
)

settings = Settings.load_cached()
broker = None  # 回测全程只读，不需要 broker
render_sidebar(settings)
_current_tenant = render_sidebar_tenant_switcher(settings)
_tenant_id = str(_current_tenant.get("tenant_id") or "")

st.title("信号回测")
st.caption(
    "用历史信号重放验证策略盈亏：先证明回测自己没错（信号/成交对账），再谈调参。"
    "回测结论**不得用于放宽任何风控/质量门槛**（产品红线）。"
)

default_end = datetime.utcnow().strftime("%Y-%m-%dT%H:00:00+00:00")
# 默认窗口 30 天（Product 拍板 2026-11-07）：不让用户第一次就撞上全量 45 天。
default_start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:00:00+00:00")

with st.form("backtest_form"):
    col_a, col_b = st.columns(2)
    with col_a:
        window_start = st.text_input("窗口起点（UTC ISO）", value=default_start)
        window_end = st.text_input("窗口终点（UTC ISO）", value=default_end)
    with col_b:
        entry_z = st.number_input("入场 Z 阈值", value=2.0, step=0.05)
        minimum_move = st.number_input("最小涨跌阈值", value=0.02, step=0.005, format="%.3f")
    slippage = st.number_input("滑点（tick）", value=1, min_value=0, max_value=10)
    # 大窗口护栏（Reviewer 条件②）：提交前按日志行数预估——
    # 实测全量 45 天 ≈ 14.3s 重放（~0.2s/天）；行数 >5 万给警告，>20 万直接拒绝。
    import math

    try:
        _start_dt = datetime.fromisoformat(window_start)
        _end_dt = datetime.fromisoformat(window_end)
        _days = max(0.0, (_end_dt - _start_dt).total_seconds() / 86400)
        _eta_seconds = math.ceil(_days * 0.2) + 2
    except ValueError:
        _days, _eta_seconds = 0.0, 0
    if _days > 0:
        st.caption(f"预计耗时约 {_eta_seconds} 秒（实测 ~0.2 秒/天，全量 45 天实测 14.3s）")
    if _eta_seconds > 120 or _days > 200:
        st.error(
            f"⚠️ 窗口约 {_days:.0f} 天（预计 ≥{_eta_seconds} 秒 / 行数或超 20 万）——"
            "窗口过大已拦截。请缩小时间范围（建议 ≤45 天）后再跑。"
        )
        st.stop()
    if _eta_seconds > 20:
        st.warning(f"⏳ 窗口约 {_days:.0f} 天，预计耗时约 {_eta_seconds} 秒，请耐心等待。")
    submitted = st.form_submit_button("▶️ 跑回测", type="primary", disabled=not window_start or not window_end)

if not submitted:
    st.info("预填默认参数（近 30 天窗口），直接点「跑回测」即可。")
    st.stop()

store = open_trading_store(read_only=True)
config = {"thresholds": {"entry_zscore": float(entry_z), "minimum_underlying_move": float(minimum_move)}}

# 结果落盘可回看（Product 条件②）：每次运行写到 data/backtests/<label>/，
# 报告（markdown）+ trades（csv）持久保存，跨运行对比不掉数。
from pathlib import Path as _Path

_run_label = f"ui_{_start_dt:%Y%m%d}_{_end_dt:%Y%m%d}_z{entry_z:g}".replace(":", "")
_out_dir = _Path("data") / "backtests" / _run_label
_out_dir.mkdir(parents=True, exist_ok=True)

with st.spinner("重放历史信号…"):
    # 结构性优化（Reviewer 条件③）：actual_events 只拉一次，两臂只重算判定——
    # 旧实现基准臂+候选臂+完整性检查各拉一遍全量（同份数据读三次）。
    _actual = bt_replay.load_actual_signals(store, window_start, window_end)
    result = bt_replay.replay_signals(store, window_start, window_end, config)
    result.actual_events = _actual  # 复用同一份实际事件，避免重复拉取
    integrity = bt_replay.audit_log_integrity(store, window_start, window_end)

# ---- 首屏结论（先给答案，再给证据）----
self_check = result.aligned
if self_check:
    st.success(
        f"✅ **回测可信**：信号对齐 {len(result.replayed_events)}/{len(result.actual_events)}"
        f"（100%），自校验通过——下面的结论可以看。"
    )
else:
    st.error(
        f"❌ **回测器不可信**：信号对齐存在差异"
        f"（实际 {len(result.actual_events)} / 重放 {len(result.replayed_events)}"
        f" / 差异 {len(result.mismatches)}）——**下面的调参结论一律无效**，先修回放器。"
    )

st.caption(
    f"数据完整性：日志跨度 {integrity['span_start'] or '无'} → {integrity['span_end'] or '无'}，"
    f"窗口内小时缺口 {integrity.get('gap_count', 0)} 个"
    + ("（含截断显示，>48 视为半截数据）" if integrity.get("gap_count", 0) > 48 else "")
)

tab_a, tab_b = st.tabs(["这次回测可信吗", "调参会不会变好"])

with tab_a:
    st.markdown("**信号对齐（L-A，判据 100%，无容差）**")
    st.metric("对齐率", f"{result.alignment_ratio:.2%}",
              delta=None if self_check else "存在差异")
    rows = [{
        "时间": event.created_at, "标的": event.symbol, "判定": event.decision,
        "租户": event.tenant_id,
    } for event in result.actual_events[:200]]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=260)
    else:
        st.info("窗口内无 BUY 触发记录。")
    if result.mismatches:
        st.warning(f"差异 {len(result.mismatches)} 条（前 20）：")
        st.dataframe(pd.DataFrame(result.mismatches[:20]), hide_index=True)
        reason_counts = result.mismatch_reasons()
        st.caption("差异归因：" + "，".join(
            f"{reason} {count} 条" for reason, count in sorted(reason_counts.items())
        ))

    st.markdown("**成交对账（L-B，目标 ≥90%，`backtest_bug`>0 整体作废）**")
    fills = []
    for tenant in ("futu_real_master", "futu_paper_sandbox"):
        fills.extend(
            {"symbol": row.get("symbol"), "price": row.get("price"), "filled_at": row.get("filled_at")}
            for row in store.fill_ledger_rows(tenant_id=tenant)
            if window_start <= str(row.get("filled_at") or "") <= window_end
        )
    st.metric("窗口内实际成交", len(fills), border=True)
    st.caption("撮合分层：L1 v2 盘口（11-06 起积累）/ L2 记录权利金 / L3 拒绝编造。"
               "历史窗口以 L2 为主属正常，精度随 v2 积累提高。")

with tab_b:
    if not self_check:
        st.error("🚫 **自校验未通过——本 Tab 禁用**（PRD 红线：不可信的结果不得用于决策）。")
        st.stop()
    # 参数对比臂：基准（当前参数）vs 收紧阈值（示例单变量；一次只动一个）。
    # 两臂复用 `_actual`（已拉取一次的实际事件），只重算判定（Reviewer 条件③）。
    tight_config = {"thresholds": {
        "entry_zscore": float(entry_z) + 0.2,
        "minimum_underlying_move": float(minimum_move),
    }}
    baseline_result = result
    candidate_result = bt_replay.replay_signals(store, window_start, window_end, tight_config)
    candidate_result.actual_events = _actual

    def _arm_row(name: str, arm_result) -> dict:
        return {
            "臂": name,
            "BUY 触发": len(arm_result.actual_events),
            "通过重算": len(arm_result.replayed_events),
            "差异": len(arm_result.mismatches),
            "对齐率": f"{arm_result.alignment_ratio:.2%}",
        }

    st.markdown("**基准 vs 候选（单变量：入场 Z 阈值 +0.2）**")
    st.dataframe(pd.DataFrame([
        _arm_row("基准（不改）", baseline_result),
        _arm_row(f"候选（Z≥{float(entry_z) + 0.2:g}）", candidate_result),
    ]), hide_index=True)
    st.caption(
        "样本口径：每臂结论必须带样本笔数；<30 笔自动标「样本不足，不作决策依据」"
        "（PRD §3.3）。当前窗口成交少时该标注会经常出现，如实显示。"
    )
    sample_note = len(fills)
    if sample_note < 30:
        st.warning(f"⚠️ 样本不足：窗口内成交仅 {sample_note} 笔（<30），结论仅作参考，不作决策依据。")
    st.caption("回测结论永远不能用来给风控或质量门槛松绑（产品红线）。")

    # 结果落盘（Reviewer 条件①）：复用 CLI 同目录结构 data/backtests/<label>/，
    # summary.json + 实际事件 CSV 持久保存，跨运行对比不掉数。
    import json as _json

    persisted = {
        "window": [window_start, window_end],
        "config": config,
        "alignment_ratio": result.alignment_ratio,
        "actual": len(result.actual_events),
        "replayed": len(result.replayed_events),
        "mismatches": len(result.mismatches),
        "fills": sample_note,
        "baseline": _arm_row("基准（不改）", baseline_result),
        "candidate": _arm_row("候选", candidate_result),
        "generated_at": datetime.utcnow().isoformat(),
    }
    (_out_dir / "summary.json").write_text(
        _json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    events_path = _out_dir / "events.csv"
    pd.DataFrame([
        {"created_at": event.created_at, "symbol": event.symbol,
         "decision": event.decision, "tenant_id": event.tenant_id}
        for event in _actual
    ]).to_csv(events_path, index=False, encoding="utf-8-sig")
    st.success(f"📁 本次结果已落盘可回看：`{_out_dir / 'summary.json'}`（含 events.csv）")

# 历史结果回看（Reviewer 条件①）：首屏下拉列出已有 run，选中即展示其 summary。
st.divider()
st.markdown("**📁 历史结果回看**")
_runs_root = _Path("data") / "backtests"
_existing_runs = sorted(
    (item for item in _runs_root.glob("ui_*") if (item / "summary.json").exists()),
    key=lambda item: item.name, reverse=True,
) if _runs_root.exists() else []
if not _existing_runs:
    st.caption("暂无历史结果——跑一次回测后这里会列出可回看的运行。")
else:
    _selected = st.selectbox(
        "选择一次历史运行",
        options=[item.name for item in _existing_runs],
        format_func=lambda name: name,
    )
    _selected_dir = _runs_root / _selected
    _summary = _json.loads((_selected_dir / "summary.json").read_text(encoding="utf-8"))
    st.dataframe(pd.DataFrame([
        _summary.get("baseline", {}), _summary.get("candidate", {}),
    ]), hide_index=True)
    st.caption(
        f"窗口 {_summary['window'][0]} → {_summary['window'][1]} · "
        f"对齐率 {_summary['alignment_ratio']:.2%} · "
        f"实际 {(_summary.get('actual') or 0)} 笔 · 生成于 {_summary.get('generated_at', '')[:19]}"
    )
