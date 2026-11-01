"""配置中心业务入口 3：风险与限额 —— **唯一可编辑入口**（P0-UI-2 第一刀）。

回答核心问题：我这个账户的额度是多少、能改哪几个、改完什么时候生效？

归属技术域：④风控边界 + ②交易安全（②只读展示 + 指路，不在本页编辑）。

口径（PRD §12 + §13 + Reviewer 2026-11-01 的写入层修正）：
- **A 限额全景（只读）**：本归属全部配置项列出，只做说明与指路；
- **B 可编辑（primary = 5）**：单笔开仓 / 日累计开仓 / 单笔减仓 / 日累计减仓 / 日亏损急停线，
  需管理员登录 + 表单内二次确认，写入**租户层 `risk_limits:{tenant}`**（风控真正读取的层），
  并落审计；
- **写不生效就禁用保存**：生效来源是账户级 `risk` 段时，改租户层无效，必须禁用并指路；
- **②实盘门禁项只读**：`REAL_MAX_*` 与「允许真金交易」来自门禁同源函数，只展示 + 指路；
- **薄视图**：元数据/校验/写路径在 `futu_quant.risk_limit_editing`，本页只渲染与提交。
"""

from __future__ import annotations

import streamlit as st

from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY
from futu_quant.real_trading import resolve_real_account_limits
from futu_quant.risk_limit_editing import (
    EDITABLE_ITEMS,
    build_changes,
    build_tenant_risk_payload,
    effective_values,
    save_tenant_risk_limits,
    source_is_writable_here,
)
from futu_quant.risk_limits import (
    RiskLimitsConfigError,
    resolve_risk_limits_detail,
    risk_limits_tenant_key,
)
from futu_quant.ui import (
    get_current_tenant,
    get_tenant_broker,
    open_trading_store,
    render_admin_login,
    render_sidebar,
)

st.set_page_config(page_title="风险与限额 - 统一配置", page_icon="🛡️", layout="wide")


def _render_panorama() -> None:
    """A 段：本归属全部限额项（只读）。"""
    specs = [s for s in GLOBAL_REGISTRY.all_specs() if s.user_area == "risk_limits"]
    primary = [s for s in specs if s.surface == "primary"]
    advanced = [s for s in specs if s.surface == "advanced"]

    st.subheader("📋 限额全景（只读）")
    st.caption(
        f"本页归属 {len(specs)} 项风控/限额配置：核心 {len(primary)} 项、高级 {len(advanced)} 项。"
        "这一块只做说明与指路，不在此处编辑。"
    )
    for spec in primary:
        st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
    with st.expander(f"高级风控阈值与细项（{len(advanced)} 项）", expanded=False):
        for spec in advanced:
            st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
            st.caption(
                f"归属：{spec.category} · 生效方式：{spec.activation} · 风险等级：{spec.risk_level}"
            )


def _render_real_gate_readonly(store, settings) -> None:
    """②实盘门禁额度：只读展示 + 指路（PRD §12 硬红线，本页不改）。"""
    tenant = get_current_tenant(settings)
    account_id = tenant.get("acc_id") if isinstance(tenant, dict) else None
    st.subheader("🔒 实盘门禁额度（只读 · 改动请走「账户与交易环境」）")
    if not account_id:
        st.info("当前租户没有绑定实盘账户，实盘门禁额度不适用。")
        return
    try:
        limits = resolve_real_account_limits(store, settings, account_id)
    except Exception as exc:  # noqa: BLE001 - 解析失败必须显式可见，不能静默
        st.warning(f"实盘门禁额度解析失败：{exc}")
        return
    if getattr(limits, "config_corrupt", False):
        st.error(
            f"该账户的账户级配置已损坏（{limits.corrupt_reason}）："
            "开仓买入已被禁止，卖出仍可执行。请先修复配置再回本页查看。"
        )
        return
    st.markdown(
        f"- 允许真金交易：**{'是' if limits.enabled else '否'}**\n"
        f"- 单笔上限：**${limits.max_order_notional:,.0f}** · "
        f"单日上限：**${limits.max_daily_notional:,.0f}**\n"
        f"- 人工二次确认：**{'需要' if limits.require_manual_confirm else '不需要（高风险）'}**"
    )
    st.caption("这几项属交易安全门禁（②类），只读展示；修改入口在「账户与交易环境」页。")


def _source_label(source: str, tenant_id: str) -> str:
    if source.startswith("real_account_config:"):
        return "账户级配置（risk 段）"
    if tenant_id and source == risk_limits_tenant_key(tenant_id):
        return f"租户级配置（risk_limits:{tenant_id}）"
    if source == "env":
        return "环境变量兜底（尚未写入数据库）"
    return "无前缀旧键（迁移期兜底）"


def main() -> None:
    settings = Settings.load()
    render_sidebar(settings)

    st.title("🛡️ 风险与限额")
    st.caption(
        "这 5 项写入数据库的**租户层**（风控真正读取的那一层），Worker 每轮自动重读，"
        "**≤2 分钟生效，无需重启**。"
    )

    store = open_trading_store()
    broker = get_tenant_broker(settings)

    try:
        resolution = resolve_risk_limits_detail(store, broker)
        current = effective_values(resolution.limits)
        limits_error = ""
    except RiskLimitsConfigError as exc:
        resolution = None
        current = {}
        limits_error = str(exc)

    _render_panorama()

    st.subheader("✏️ 可编辑（5 项 · 需二次确认）")

    writable = False
    if resolution is not None:
        source = resolution.source
        tenant_id = resolution.tenant_id
        writable = source_is_writable_here(source, tenant_id)
        st.caption(f"当前生效来源：**{_source_label(source, tenant_id)}**（改这里就是改这一层）")
        if not writable:
            st.warning(
                "该账户的额度已被**账户级配置**接管（账户级优先级高于租户级）："
                "在本页保存不会生效，因此已禁用保存。请到「账户与交易环境」页修改账户级 risk 段。"
            )

    if limits_error:
        st.error(
            f"当前生效值无法解析：{limits_error} —— 这是 fail-closed 提示，不是页面故障；"
            "先修复配置，再回到本页修改。"
        )
    if current:
        st.caption(
            "当前生效值："
            + " · ".join(
                f"{item.label} ${current[item.key]:,.0f}"
                for item in EDITABLE_ITEMS
                if item.key in current
            )
        )
    else:
        st.caption("当前生效值：**未统计**（解析失败或字段缺失），不显示 0 冒充正常。")

    if not render_admin_login():
        st.info("修改风险与资金限额需要管理员登录；未登录时本页只读。")
        _render_real_gate_readonly(store, settings)
        return

    submitted_values: dict[str, float] = {}
    for item in EDITABLE_ITEMS:
        st.caption(item.impact)
        submitted_values[item.key] = st.number_input(
            f"{item.label}（美元）",
            min_value=float(item.minimum),
            max_value=float(item.maximum),
            value=float(current.get(item.key, item.minimum)),
            step=float(item.step),
            key=f"risk_edit_{item.key}",
        )

    # 保存前把**实际将写入的完整 10 项**摆出来（Reviewer 要求）：
    # 保存会把当前生效的 10 项一起固化进租户层，原本来自 env 兜底的项从此"钉住"，
    # 用户不知道就会踩 —— 必须能看到「哪些是本次改动、哪些是沿用当前值」。
    preview_changes: dict[str, str] = {}
    preview_payload: dict[str, float] | None = None
    preview_error = ""
    if resolution is not None:
        try:
            preview_changes = build_changes(submitted_values, current)
            preview_payload = build_tenant_risk_payload(resolution.limits, preview_changes)
        except ValueError as exc:
            preview_error = str(exc)

    st.markdown("**保存后将写入租户层的完整 risk 段（10 项）**")
    if preview_error:
        st.warning(f"暂无法生成写入预览：{preview_error}")
    elif preview_payload is not None:
        rows = []
        for field_name, value in preview_payload.items():
            mark = "★ 本次修改" if field_name in preview_changes else "沿用当前值"
            rows.append(f"- {'★' if field_name in preview_changes else '·'} "
                        f"`{field_name}` = {value} —— {mark}")
        st.markdown("\n".join(rows))
        st.caption(
            "★ 项是本次改动；其余为**沿用当前生效值**。写入后这 10 项都固定在租户层："
            "原本由环境变量兜底的项从此以库为准，改 `.env` 不再生效。"
            "（这是有意为之：风控对「部分配置」是 fail-closed 拒绝，只写 5 项会让风控不可用。）"
        )

    confirmed = st.checkbox(
        "我确认修改的是真实生效的风控/资金参数（影响下单额度与急停线），"
        "并已将上面列出的 10 项一起固化进租户层",
    )
    submitted = st.button("保存这 5 项", type="primary", disabled=not writable)

    if submitted and resolution is not None:
        if not writable:
            st.error("当前生效层不可在本页修改（见上方提示），未保存。")
        elif not confirmed:
            st.warning("未勾选确认框，本次不保存。")
        else:
            try:
                changes = build_changes(submitted_values, current)
            except ValueError as exc:
                st.error(str(exc))
            else:
                if not changes:
                    st.info("输入与当前生效值相同，未做任何修改。")
                else:
                    try:
                        payload = build_tenant_risk_payload(resolution.limits, changes)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        before = {key: current.get(key) for key in changes}
                        written_key = save_tenant_risk_limits(
                            store, resolution.tenant_id, payload,
                        )
                        store.audit(
                            "RISK_LIMITS_UPDATED",
                            {
                                "actor": "streamlit_admin",
                                "layer": written_key,
                                "tenant_id": resolution.tenant_id,
                                "before": before,
                                # 改动集：便于人读「这次改了什么」
                                "after": changes,
                                # 实际写进库的完整 10 项：事后要能回答「当时固化成了什么」
                                "written_payload": dict(payload),
                                "source": "pages/45_risk_and_limits.py",
                            },
                            actor="streamlit_admin:panel",
                        )
                        st.success(
                            f"已写入 {written_key}："
                            + " · ".join(f"{key} → {value}" for key, value in changes.items())
                            + "（Worker 下一轮自动重读，≤2 分钟生效）"
                        )
                        st.caption(
                            "本次写入的是**完整** risk 段（未改动字段沿用当前生效值），"
                            "因此不会因为「部分配置」被风控 fail-closed 拦下。"
                        )

    _render_real_gate_readonly(store, settings)


main()
