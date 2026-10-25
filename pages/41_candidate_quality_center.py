"""候选质量中心（P0-5 §8 验收 4/5/11）。

**薄视图**：所有查询走 `store` / `candidate_quality`，页面不做 SQL、不放业务逻辑。

回答三个问题：
① 当前租户生效的候选质量阈值是什么（含**来源**）；
② 被硬排除 / 软降权的候选各有多少、为什么（**无候选时也必须能看到原因**，
   不能只显示"没机会"）；
③ `quote_age_at_submit` 的 P50/P95 —— **让 300s 的代价可见，而不是靠断言**。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.candidate_quality import (
    PHASE_HARD_EXCLUDE,
    PHASE_SOFT_DEMOTE,
    CandidateQualityError,
    resolve_candidate_quality,
)
from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import china_time_columns, get_current_tenant, render_sidebar


st.set_page_config(page_title="候选质量中心", page_icon="🎯", layout="wide")
render_sidebar(Settings.load())
st.title("候选质量中心")
st.caption(
    "回答「**为什么这轮没有候选**」：被硬排除多少、被软降权多少、各自什么原因。"
    "静默丢行是今晚反复出事的形态，这里把它显式化。"
)

store = TradingStore()
settings = Settings.load()

try:
    tenant = get_current_tenant(settings)
    tenant_id = str(tenant.get("tenant_id") or "")
    is_real = bool(tenant.get("is_real"))
except Exception:  # noqa: BLE001
    tenant_id, is_real = "", False


@st.cache_data(ttl=15, max_entries=4, show_spinner=False)
def load_summary(tenant: str) -> pd.DataFrame:
    return pd.DataFrame(TradingStore().candidate_exclusion_summary(tenant, hours=24))


@st.cache_data(ttl=15, max_entries=4, show_spinner=False)
def load_detail(tenant: str) -> pd.DataFrame:
    return pd.DataFrame(TradingStore().recent_candidate_exclusions(tenant, limit=300))


@st.cache_data(ttl=15, max_entries=4, show_spinner=False)
def load_age_stats(tenant: str) -> dict:
    return TradingStore().candidate_quote_age_percentiles(tenant, hours=24)


# ---------------------------------------------------------------------------
# ① 生效阈值（含来源）
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("生效阈值")
    try:
        config = resolve_candidate_quality(store, tenant_id, is_real=is_real)
    except CandidateQualityError as exc:
        # fail-closed：配置非法就直说，**不静默用默认值**
        st.error(f"阈值解析失败（fail-closed，未回落默认值）：{exc}", icon=":material/error:")
        config = None
    if config is not None:
        st.caption(f"来源：`{config.source}` · 租户：`{tenant_id or '未识别'}`")
        with st.container(horizontal=True):
            st.metric("软降权价差", f"{config.soft_spread_pct:.1f}%", border=True)
            st.metric("硬排除价差", f"{config.hard_spread_pct:.1f}%", border=True)
            st.metric("入场上限价差", f"{config.entry_max_spread_pct:.1f}%", border=True)
        with st.container(horizontal=True):
            st.metric("报价停滞硬线", f"{config.hard_stale_seconds}s", border=True)
            st.metric("入场降级上限", f"{config.entry_max_stale_seconds}s", border=True)
            st.metric("未平仓量下限", f"{config.hard_oi_min}", border=True)
        st.caption(
            "链式校验（fail-closed）：**软线 ≤ 硬线 ≤ 入场上限** —— 违反即拒绝解析，"
            "否则会出现「候选层放行、入场层拦住」的不可解释 BLOCK。"
        )

# ---------------------------------------------------------------------------
# ② 24 小时聚合 + 明细
# ---------------------------------------------------------------------------
summary = load_summary(tenant_id)
with st.container(border=True):
    st.subheader("近 24 小时：候选被排除 / 被降权")
    if summary.empty:
        st.info("当前窗口内没有排除或降权记录。")
    else:
        hard = int(summary[summary["phase"] == PHASE_HARD_EXCLUDE]["count"].sum())
        soft = int(summary[summary["phase"] == PHASE_SOFT_DEMOTE]["count"].sum())
        with st.container(horizontal=True):
            st.metric("硬排除", hard, border=True)
            st.metric("软降权（仍在候选池）", soft, border=True)
        st.dataframe(summary, hide_index=True)
        st.caption(
            "**软降权 ≠ 硬排除**：被降权的合约仍在候选池，只是排序靠后；"
            "被硬排除的才不进池。"
        )

detail = load_detail(tenant_id)
with st.container(border=True):
    st.subheader("排除明细")
    st.caption("留痕带**实测值**（报价年龄 / 价差 / 量能）—— 否则「被排除」和「猜被排除」分不清。")
    if detail.empty:
        st.info("没有排除明细。")
    else:
        st.dataframe(china_time_columns(detail), hide_index=True)

# ---------------------------------------------------------------------------
# ③ 300s 的代价（P50/P95）
# ---------------------------------------------------------------------------
stats = load_age_stats(tenant_id)
with st.container(border=True):
    st.subheader("下单时报价年龄（quote_age_at_submit）")
    st.caption(
        "300s 是「先上线、带埋点、**一周后按数据复审**」——"
        "**若 P95 逼近上限，说明拦单过多，应回退**。这里让代价可见，而不是靠断言。"
    )
    if not stats.get("count"):
        st.info("暂无埋点数据（门禁尚未放行过任何一笔）。")
    else:
        with st.container(horizontal=True):
            st.metric("样本数", stats["count"], border=True)
            st.metric("P50", f"{stats['p50']}s", border=True)
            st.metric("P95", f"{stats['p95']}s", border=True)
            st.metric("P99", f"{stats['p99']}s", border=True)
