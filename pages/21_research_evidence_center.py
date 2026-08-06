from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.ui import (
    china_time_columns, chinese_columns, readable_payload, render_admin_login,
    render_sidebar,
)


st.set_page_config(page_title="研究与证据中心", page_icon="🧪", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

st.title("研究与证据中心")
st.caption("聚合期权覆盖、Shadow 账本、回测证据和运行告警，给策略调优提供统一入口。")


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_coverage() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().option_history_coverage())


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_alerts() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().alerts(limit=5000))


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_operational_events() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().operational_events(limit=5000))


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_intents() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().intents(limit=5000))


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_shadow_fills() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().shadow_fills(limit=10000))


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_validation_runs() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().validation_runs(limit=500))


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def load_evidence() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().acceptance_evidence())


def fmt_days(days: int) -> str:
    return f"{days} 天"


def strategy_name(value: str) -> str:
    labels = {
        "core_satellite": "Core Satellite",
        "earnings_gamma": "财报 Gamma",
        "earnings_gamma_manual": "财报 Gamma 手动单",
        "volatility_long_gamma": "波动率期权",
    }
    return labels.get(value, value)


def to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True)


def format_report_time(value: object) -> str:
    """Render persisted timestamps safely, including legacy string records."""
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return "时间未知"
    return f"{timestamp.tz_convert('Asia/Shanghai'):%Y-%m-%d %H:%M}"


def strategy_label(strategy_id: str) -> str:
    return strategy_name(strategy_id)


def build_strategy_report(
    strategy_id: str,
    window_days: int,
    coverage: pd.DataFrame,
    intents: pd.DataFrame,
    shadow_fills: pd.DataFrame,
    alerts: pd.DataFrame,
    evidence: pd.DataFrame,
    validation_runs: pd.DataFrame,
    incidents: pd.DataFrame,
) -> str:
    now = pd.Timestamp.now(tz="UTC").tz_convert("Asia/Shanghai")
    scope_name = "全部策略" if strategy_id == "ALL" else strategy_label(strategy_id)
    report_lines = [
        f"# {scope_name} 研究报告",
        "",
        f"- 生成时间：{now:%Y-%m-%d %H:%M:%S %Z}",
        f"- 观察窗口：最近 {window_days} 天",
    ]
    scoped_intents = intents.copy()
    scoped_shadow = shadow_fills.copy()
    scoped_alerts = alerts.copy()
    scoped_evidence = evidence.copy()
    scoped_validation = validation_runs.copy()
    if strategy_id != "ALL":
        scoped_intents = scoped_intents[scoped_intents["strategy_id"] == strategy_id].copy()
        scoped_shadow = scoped_shadow[scoped_shadow["strategy_id"] == strategy_id].copy()
        scoped_alerts = scoped_alerts[
            scoped_alerts["event_type"].astype(str).str.contains(strategy_id, case=False, na=False)
            | scoped_alerts["message"].astype(str).str.contains(strategy_id, case=False, na=False)
        ].copy()
        scoped_evidence = scoped_evidence[scoped_evidence["strategy_id"] == strategy_id].copy()
        scoped_validation = scoped_validation[scoped_validation["strategy_id"] == strategy_id].copy()
    if not scoped_intents.empty:
        counts = scoped_intents["status"].astype(str).value_counts().to_dict()
        filled = scoped_intents[scoped_intents["filled_quantity"].fillna(0).astype(float) > 0]
        rejects = scoped_intents[scoped_intents["status"].astype(str).eq("REJECTED")]
        report_lines.extend([
            "",
            "## 订单与执行",
            f"- 意图总数：{len(scoped_intents)}",
            f"- 已成交意图：{len(filled)}",
            f"- 被拒意图：{len(rejects)}",
            f"- 状态分布：{counts}",
        ])
        if not rejects.empty:
            recent_rejects = rejects.sort_values("created_at").tail(5)
            report_lines.append("- 最近拒单：")
            for _, row in recent_rejects.iterrows():
                report_lines.append(
                    f"  - {format_report_time(row.get('created_at'))} {row['symbol']} / {row['side']}："
                    f"{row.get('error', '') or row.get('status', '')}"
                )
    if not scoped_shadow.empty:
        open_shadow = scoped_shadow[scoped_shadow["quantity"].astype(float) > 0]
        report_lines.extend([
            "",
            "## Shadow 账本",
            f"- Shadow 成交记录：{len(scoped_shadow)}",
            f"- 当前仍有数量的 Shadow 记录：{len(open_shadow)}",
        ])
        if not open_shadow.empty:
            tail_shadow = open_shadow.sort_values("created_at").tail(5)
            report_lines.append("- 最近仍持有的 Shadow 记录：")
            for _, row in tail_shadow.iterrows():
                report_lines.append(
                    f"  - {format_report_time(row.get('created_at'))} {row['symbol']} / {row['side']} "
                    f"{float(row['quantity']):g} @ {float(row['limit_price']):g}"
                )
    if not coverage.empty:
        report_lines.extend([
            "",
            "## 历史覆盖",
            f"- 覆盖合约数：{len(coverage)}",
            f"- 累计快照数：{int(coverage['sample_count'].sum())}",
            f"- 可用快照数：{int(coverage['good_samples'].sum())}",
        ])
        weak = coverage.sort_values(["sample_count", "good_samples"], ascending=[True, True]).head(5)
        report_lines.append("- 最薄弱的合约：")
        for _, row in weak.iterrows():
            report_lines.append(
                f"  - {row['code']}：{int(row['sample_count'])} 条 / {int(row['good_samples'])} 条可用"
            )
    if not scoped_validation.empty or not scoped_evidence.empty:
        report_lines.extend(["", "## 回测与审批证据"])
        report_lines.append(f"- 回测验证记录：{len(scoped_validation)}")
        report_lines.append(f"- 审批证据记录：{len(scoped_evidence)}")
        if not scoped_validation.empty:
            last_validation = scoped_validation.sort_values("created_at").iloc[-1]
            report_lines.append(
                f"- 最近验证：{format_report_time(last_validation.get('created_at'))}，"
                f"结果={'通过' if bool(last_validation['passed']) else '未通过'}"
            )
    if not scoped_alerts.empty:
        error_rows = scoped_alerts[scoped_alerts["severity"].astype(str).eq("ERROR")]
        warn_rows = scoped_alerts[scoped_alerts["severity"].astype(str).eq("WARNING")]
        report_lines.extend([
            "",
            "## 告警",
            f"- 错误告警：{len(error_rows)}",
            f"- 警告告警：{len(warn_rows)}",
        ])
        if not error_rows.empty:
            tail_errors = error_rows.sort_values("created_at").tail(5)
            report_lines.append("- 最近错误：")
            for _, row in tail_errors.iterrows():
                report_lines.append(
                    f"  - {format_report_time(row.get('created_at'))} "
                    f"{row['event_type']}：{row['message']}"
                )
    if not incidents.empty:
        report_lines.extend(["", "## 开放事件", f"- 当前开放事件：{len(incidents)}"])
        tail_incidents = incidents.sort_values("last_seen").tail(5)
        for _, row in tail_incidents.iterrows():
            report_lines.append(
                f"  - {format_report_time(row.get('last_seen'))} {row['incident_key']}：{row['message']}"
            )
    return "\n".join(report_lines)


with st.container(border=True):
    left, middle, right = st.columns(3)
    window_days = left.selectbox("观察窗口", [1, 3, 5, 7, 14], index=1, format_func=fmt_days)
    strategy_scope = middle.selectbox(
        "策略范围",
        ["全部", "core_satellite", "earnings_gamma", "earnings_gamma_manual", "volatility_long_gamma"],
        format_func=lambda value: "全部策略" if value == "全部" else strategy_name(value),
    )
    refresh_note = right.selectbox(
        "刷新粒度",
        ["30 秒缓存", "仅当前页面"],
        index=0,
        help="页面数据来自短缓存，切换后会自动刷新；这里仅用于表达查看频率。",
    )
    st.caption(f"查看窗口：最近 {window_days} 天 · {refresh_note}")

strategy_choices = ["ALL"]
coverage = load_coverage()
alerts = load_alerts()
ops = load_operational_events()
intents = load_intents()
shadow_fills = load_shadow_fills()
validation_runs = load_validation_runs()
evidence = load_evidence()
incidents = pd.DataFrame(store.incidents("OPEN", limit=500))

now = pd.Timestamp.now(tz="UTC")
cutoff = now - pd.Timedelta(days=window_days)

if not alerts.empty:
    alerts["created_at"] = to_utc(alerts["created_at"])
    recent_alerts = alerts[alerts["created_at"] >= cutoff].copy()
else:
    recent_alerts = alerts

if not ops.empty:
    ops["created_at"] = to_utc(ops["created_at"])
    recent_ops = ops[ops["created_at"] >= cutoff].copy()
else:
    recent_ops = ops

if not intents.empty:
    intents["created_at"] = to_utc(intents["created_at"])
    recent_intents = intents[intents["created_at"] >= cutoff].copy()
else:
    recent_intents = intents

if not shadow_fills.empty:
    shadow_fills["created_at"] = to_utc(shadow_fills["created_at"])
    recent_shadow_fills = shadow_fills[shadow_fills["created_at"] >= cutoff].copy()
else:
    recent_shadow_fills = shadow_fills

if strategy_scope != "全部" and not recent_intents.empty:
    recent_intents = recent_intents[recent_intents["strategy_id"] == strategy_scope].copy()
if strategy_scope != "全部" and not evidence.empty:
    evidence = evidence[evidence["strategy_id"] == strategy_scope].copy()
if strategy_scope != "全部" and not validation_runs.empty:
    validation_runs = validation_runs[validation_runs["strategy_id"] == strategy_scope].copy()
if strategy_scope != "全部" and not recent_shadow_fills.empty:
    recent_shadow_fills = recent_shadow_fills[recent_shadow_fills["strategy_id"] == strategy_scope].copy()

shadow_count = int((recent_intents["status"].astype(str).str.startswith("SHADOW").sum()) if not recent_intents.empty else 0)
rejected_count = int((recent_intents["status"].astype(str).eq("REJECTED").sum()) if not recent_intents.empty else 0)
fill_count = int((recent_intents["filled_quantity"].fillna(0).astype(float) > 0).sum()) if not recent_intents.empty else 0
error_count = int((recent_alerts["severity"].astype(str).eq("ERROR").sum()) if not recent_alerts.empty else 0)
open_incidents = len(incidents)

if not intents.empty:
    strategy_choices.extend(sorted(set(intents["strategy_id"].astype(str))))
else:
    strategy_choices.extend(["core_satellite", "earnings_gamma", "earnings_gamma_manual", "volatility_long_gamma"])
report_strategy = st.selectbox(
    "生成研究报告的策略",
    strategy_choices,
    format_func=lambda value: "全部策略" if value == "ALL" else strategy_label(value),
)
if st.button("生成策略研究报告", type="primary", icon=":material/description:"):
    report = build_strategy_report(
        report_strategy, window_days, coverage, recent_intents, recent_shadow_fills,
        recent_alerts, evidence, validation_runs, incidents,
    )
    st.session_state["strategy_research_report"] = {
        "strategy_id": report_strategy,
        "report": report,
        "generated_at": pd.Timestamp.now(tz="UTC"),
    }
    st.success("已生成研究报告。")

generated_report = st.session_state.get("strategy_research_report")
if isinstance(generated_report, dict) and generated_report.get("report"):
    st.subheader("策略研究报告")
    st.code(generated_report["report"], language="markdown")
    st.download_button(
        "下载当前研究报告 Markdown",
        generated_report["report"].encode("utf-8"),
        file_name=f"{generated_report['strategy_id']}_research_report.md",
        mime="text/markdown",
    )

with st.container(horizontal=True):
    st.metric("期权覆盖合约", int(len(coverage)), border=True)
    st.metric("有效快照", int(coverage["good_samples"].sum()) if not coverage.empty else 0, border=True)
    st.metric("最近 Shadow 意图", shadow_count, border=True)
    st.metric("最近错误告警", error_count, border=True)
    st.metric("当前开放事件", open_incidents, border=True)
    st.metric("最近已成交意图", fill_count, border=True)

overview_left, overview_right = st.columns([1.1, 0.9])
with overview_left:
    st.subheader("最近诊断摘要")
    summary_lines = [
        f"- 观察窗口：最近 {window_days} 天",
        f"- 策略范围：{'全部策略' if strategy_scope == '全部' else strategy_name(strategy_scope)}",
        f"- 最近 Shadow 意图：{shadow_count}",
        f"- 最近已成交意图：{fill_count}",
        f"- 最近错误告警：{error_count}",
        f"- 当前开放事件：{open_incidents}",
    ]
    if not coverage.empty:
        low_cov = coverage.sort_values(["sample_count", "good_samples"], ascending=[True, True]).head(3)
        low_cov_lines = [
            f"  - {row['code']}：{int(row['sample_count'])} 条样本，{int(row['good_samples'])} 条可用，{int(row['trading_days'])} 个交易日"
            for _, row in low_cov.iterrows()
        ]
        summary_lines.append("- 最薄弱覆盖标的：")
        summary_lines.extend(low_cov_lines)
    if not recent_alerts.empty:
        top_errors = recent_alerts[recent_alerts["severity"].astype(str).eq("ERROR")].tail(3)
        if not top_errors.empty:
            summary_lines.append("- 最近错误：")
            for _, row in top_errors.iterrows():
                summary_lines.append(f"  - {row['event_type']}：{row['message']}")
    summary_md = "\n".join(summary_lines)
    st.markdown(summary_md)
    st.download_button(
        "下载研究摘要 Markdown",
        summary_md.encode("utf-8"),
        file_name="research_evidence_summary.md",
        mime="text/markdown",
    )

with overview_right:
    st.subheader("证据链状态")
    if evidence.empty:
        st.info("暂无上线审批证据。")
    else:
        evidence_view = evidence.rename(columns={
            "strategy_id": "策略", "evidence_type": "证据类型", "passed": "是否通过",
            "value": "数值", "details_json": "详情", "created_at": "创建时间",
        }).copy()
        evidence_view["详情"] = evidence_view["详情"].map(readable_payload)
        evidence_view["是否通过"] = evidence_view["是否通过"].map(lambda value: "通过" if bool(value) else "未通过")
        st.dataframe(chinese_columns(china_time_columns(evidence_view)), width="stretch", hide_index=True)

tab_coverage, tab_backtest, tab_shadow, tab_ops = st.tabs(
    ["覆盖率", "回测证据", "Shadow 诊断", "告警与运维"]
)

with tab_coverage:
    st.subheader("期权历史覆盖")
    if coverage.empty:
        st.info("当前还没有期权历史快照。")
    else:
        coverage_view = chinese_columns(china_time_columns(coverage))
        st.dataframe(coverage_view, width="stretch", hide_index=True)
        st.caption("样本数偏低的合约，后续回测和候选排序都会更容易失真。")

with tab_backtest:
    st.subheader("回测证据")
    if validation_runs.empty:
        st.info("暂无回测验证记录。")
    else:
        validation_view = chinese_columns(china_time_columns(validation_runs))
        if "metrics_json" in validation_view.columns:
            validation_view["指标"] = validation_view["metrics_json"].map(readable_payload)
        if "windows_json" in validation_view.columns:
            validation_view["窗口"] = validation_view["windows_json"].map(readable_payload)
        st.dataframe(validation_view.drop(columns=["metrics_json", "windows_json"], errors="ignore"),
                     width="stretch", hide_index=True)

with tab_shadow:
    st.subheader("Shadow 订单与成交")
    if recent_intents.empty:
        st.info("当前窗口内没有 Shadow 或相关订单意图。")
    else:
        shadow_view = recent_intents.copy()
        shadow_view = shadow_view.rename(columns={
            "strategy_id": "策略", "intent_key": "交易意图键", "symbol": "代码",
            "side": "方向", "quantity": "数量", "limit_price": "限价", "status": "状态",
            "filled_quantity": "已成交数量", "average_fill_price": "成交均价",
            "error": "错误信息", "reprice_count": "追价次数",
            "created_at": "创建时间", "updated_at": "更新时间",
            "last_reprice_at": "最后追价时间", "metadata_json": "附加信息",
        })
        shadow_view["附加信息"] = shadow_view["附加信息"].map(readable_payload)
        st.dataframe(chinese_columns(china_time_columns(shadow_view)), width="stretch", hide_index=True)
    st.subheader("Shadow 成交记录")
    if recent_shadow_fills.empty:
        st.info("当前窗口内没有 Shadow 成交记录。")
    else:
        shadow_fill_view = chinese_columns(china_time_columns(recent_shadow_fills)).copy()
        if "quote_json" in shadow_fill_view.columns:
            shadow_fill_view["行情快照"] = shadow_fill_view["quote_json"].map(readable_payload)
        st.dataframe(shadow_fill_view.drop(columns=["quote_json"], errors="ignore"),
                     width="stretch", hide_index=True)

with tab_ops:
    st.subheader("运行告警")
    if recent_alerts.empty:
        st.info("当前窗口内没有告警。")
    else:
        alert_view = recent_alerts.rename(columns={
            "severity": "级别", "event_type": "事件", "message": "说明", "delivered": "已送达",
            "created_at": "创建时间",
        }).copy()
        alert_view["已送达"] = alert_view["已送达"].map(lambda value: "是" if bool(value) else "否")
        st.dataframe(chinese_columns(china_time_columns(alert_view)), width="stretch", hide_index=True)
    st.subheader("开放事件")
    if incidents.empty:
        st.info("当前没有开放事件。")
    else:
        incident_view = incidents.rename(columns={
            "incident_key": "事件键", "severity": "级别", "status": "状态",
            "message": "说明", "count": "次数", "last_seen": "最近时间",
            "recovered_at": "恢复时间", "created_at": "创建时间",
        }).copy()
        st.dataframe(chinese_columns(china_time_columns(incident_view)), width="stretch", hide_index=True)
    st.subheader("运维事件")
    if recent_ops.empty:
        st.info("当前窗口内没有运维事件。")
    else:
        ops_view = recent_ops.rename(columns={
            "component": "组件", "event_type": "事件", "status": "状态",
            "payload_json": "内容", "created_at": "创建时间",
        }).copy()
        ops_view["内容"] = ops_view["内容"].map(readable_payload)
        st.dataframe(chinese_columns(china_time_columns(ops_view)), width="stretch", hide_index=True)
