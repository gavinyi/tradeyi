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
    effective_values,
    source_is_writable_here,
)
from futu_quant.risk_limits import (
    RiskLimitsConfigError,
    resolve_risk_limits_detail,
    risk_limits_tenant_key,
)
from futu_quant.limit_profile import LimitChain, LimitProfile
from futu_quant.limit_profile_store import load_limit_profile, publish_limit_profile
from futu_quant.limit_display import resolve_display_limits
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


def _latest_equity(store, tenant: dict) -> float | None:
    env = "REAL_" + str(tenant.get("acc_id")) if tenant.get("is_real") and tenant.get("acc_id") else "SIMULATE"
    rows = store.equity_history(limit=1, env=env)
    if not rows:
        return None
    value = rows[0].get("total_assets")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _legacy_limit_chain(store, settings, tenant_id: str, equity: float) -> LimitChain:
    """构造未启用派生层时的真实旧键链，仅用于页面只读对照。"""
    resolution = resolve_risk_limits_detail(store, get_tenant_broker(settings))
    limits = resolution.limits
    return LimitChain(
        tenant_id=tenant_id,
        equity=equity,
        per_order=limits.max_order_notional,
        batch=limits.max_order_notional,
        daily_entries=limits.max_daily_notional,
        daily_loss=limits.max_daily_loss,
        capacity_line=None,
        source=resolution.source,
    )


def _render_limit_profile_card(store, settings, tenant: dict) -> None:
    tenant_id = str(tenant.get("tenant_id") or tenant.get("id") or st.session_state.get("current_tenant_id") or "")
    equity = _latest_equity(store, tenant)
    st.subheader("💰 限额配置（派生层 · 按租户）")
    st.caption("输入仅接受占权益百分比；金额为已应用地板/硬顶后的实际生效值。旧美元编辑区已废弃，仅展示历史值。")
    if not tenant_id or equity is None:
        st.error(f"权益缺失或非正（当前：{equity!r}），拒绝生成/保存限额；不会回落硬编码金额。")
        return
    resolution = load_limit_profile(store, tenant_id)
    profile = resolution.profile
    legacy = _legacy_limit_chain(store, settings, tenant_id, equity)
    # 页面金额与运行时同源：都走 resolve_display_limits，绝不在页面里再算一遍。
    display = resolve_display_limits(store, tenant_id, equity, legacy)
    ratios = {
        "单笔开仓": ("per_order_ratio", "per_order"),
        "日累计开仓": ("daily_entries_ratio", "daily_entries"),
        "日亏损急停线": ("daily_loss_ratio", "daily_loss"),
    }
    with st.container(border=True):
        if display.derived_enabled:
            st.caption(
                f"派生层已启用 · 权益基数：${equity:,.2f} · 来源：{display.source} · "
                f"最近修改：{resolution.actor or '未记录'} {resolution.updated_at or ''} {resolution.reason or ''}"
            )
        else:
            st.warning(
                f"派生层未启用（当前来源：{display.source}）。"
                f"现在真正生效的是旧键金额：单笔 ${display.per_order:,.2f} · 单日 ${display.daily_entries:,.2f} · "
                f"日亏 ${display.daily_loss:,.2f}。下方百分比为待发布草案，保存发布后才生效。"
            )
        values = {}
        columns = st.columns(len(ratios))
        for column, (label, (field, output)) in zip(columns, ratios.items()):
            with column:
                values[field] = st.number_input(
                    f"{label}（权益 %）", min_value=0.0, max_value=100.0,
                    value=float(getattr(profile, field) * 100), step=0.1,
                    key=f"derived_{tenant_id}_{field}",
                ) / 100
                amount = getattr(display, output)
                st.metric(
                    f"{'实际生效' if display.derived_enabled else '当前生效（旧键）'}：{label}",
                    f"${amount:,.2f}",
                    f"{amount / equity:.2%} · 生效层：{display.layer}",
                )
        if display.derived_enabled:
            capacity = display.capacity_line
            st.caption(
                f"容量线：{('${:,.2f}'.format(capacity)) if capacity is not None else '未设置'}"
                f" · 单笔实际可下（min(派生单笔, 容量线)）：${display.effective_per_order:,.2f}"
            )
        else:
            st.caption("容量线：派生层启用后按 capacity_ratio 独立计算；当前旧键口径无容量线。")
        reason = st.text_input("变更理由（变松必填）", key=f"derived_reason_{tenant_id}")
        changed = any(abs(values[k] - getattr(profile, k)) > 1e-12 for k in values)
        confirm = st.checkbox("确认本次变更影响实际交易限额", key=f"derived_confirm_{tenant_id}")
        approval = st.text_input("审批指纹（仅变松必填）", key=f"derived_approval_{tenant_id}")
        if st.button("保存派生限额", type="primary", key=f"save_derived_{tenant_id}"):
            if not changed:
                st.info("没有检测到变更。")
            elif not reason.strip() or not confirm:
                st.error("保存派生限额必须二次确认并填写变更理由。")
            else:
                try:
                    result = publish_limit_profile(
                        store, tenant_id, LimitProfile(**{**profile.__dict__, **values}),
                        "streamlit_admin:panel", reason,
                        approval_fingerprint=approval,
                        # 首启用基线必须是旧键实际生效金额，不能是预览链。
                        legacy_limits=legacy,
                        equity=equity, confirm=confirm,
                    )
                    st.success("已发布：下一轮重读，≤2 分钟生效，无需 stop/start。")
                    st.json(result["chain"])
                except (ValueError, PermissionError, RuntimeError) as exc:
                    st.error(str(exc))


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

    _render_limit_profile_card(store, settings, get_current_tenant(settings))
    _render_panorama()

    st.subheader("📜 旧金额配置（只读 · 已废弃）")
    st.info("已废弃：改由派生层（权益 × 百分比）决定，此处仅展示历史值。请在顶部限额配置卡修改。")
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
        value = current.get(item.key)
        submitted_values[item.key] = float(value) if value is not None else float(item.minimum)
        st.metric(f"{item.label}（历史值，只读）", "未统计" if value is None else f"${float(value):,.2f}")
        st.caption(f"{item.impact} · 已废弃：改由派生层（权益 × 百分比）决定，此处仅展示历史值")

    st.info("旧金额编辑区已只读，金额生效路径已切换到顶部派生卡；页面不会再写入旧 risk_limits 金额键。")
    _render_real_gate_readonly(store, settings)


main()
