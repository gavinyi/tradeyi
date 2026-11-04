"""配置中心业务入口 5：通知与运维 —— **静音清单唯一可编辑入口**（P0-UI-2 第二刀）。

回答核心问题：告警发给谁？哪些被静音了？服务活着吗？改完要不要重启？

归属技术域：⑪通知与可观测性 + ⑩行情与数据 + ⑫运维与高级 + ③急停与运行控制。

本页这一批真正能改的只有**静音清单**，其余为只读状态 + 指路：

- **静音清单**：写 `alert_mute_list`，读它的是 `AlertManager._mute_list()`
  —— **写读同层**：`update_mute_list()` 走 `mutate_runtime_config`（`runtime_config` 表），
  而 `_mute_list()` 优先读 `get_runtime_config()`（同表），两层一致，改完下一轮告警即生效；
- **不可静音清单**：代码常量，只读；写入侧 `update_mute_list` 会直接拒绝这些事件；
- **飞书渠道**：只显示「是否已配置」与最近投递三态（-1 未配置 / 0 失败 / 1 成功），
  **不提供密钥编辑**（webhook URL / secret 属密钥，只在 `.env` 或密钥卡片里维护）；
- 其余 `ops_notify` 归属项：只读展示，底层键名收进高级详情。
"""

from __future__ import annotations

import streamlit as st

from futu_quant.alerts import (
    ALERT_MUTE_LIST_KEY,
    ALERT_NEVER_MUTE,
    ALERT_NEVER_MUTE_PREFIXES,
    AlertManager,
    feishu_configured,
    is_never_mutable,
    parse_mute_list,
    update_mute_list,
)
from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY
from futu_quant.ui import open_trading_store, render_admin_login, render_sidebar

st.set_page_config(page_title="通知与运维 - 统一配置", page_icon="🔔", layout="wide")


def current_mute_list(store) -> tuple[set[str], list[str]]:
    """读当前静音清单（**与 AlertManager 同一读取链**：runtime_config 优先，回落 system_setting）。"""
    try:
        cfg = store.get_runtime_config(ALERT_MUTE_LIST_KEY)
        raw = cfg[0] if cfg else store.get_setting(ALERT_MUTE_LIST_KEY, "")
    except Exception:  # noqa: BLE001 - 读失败按空清单 + 让用户从界面看到「未统计」
        return set(), []
    return parse_mute_list(raw)


def _render_panorama() -> None:
    specs = [s for s in GLOBAL_REGISTRY.all_specs() if s.user_area == "ops_notify"]
    primary = [s for s in specs if s.surface == "primary"]
    advanced = [s for s in specs if s.surface == "advanced"]

    st.subheader("📋 归属本页的配置全景（只读）")
    st.caption(
        f"本页归属 {len(specs)} 项：核心 {len(primary)} 项、高级 {len(advanced)} 项。"
        "本批只有「静音清单」可编辑，其余只做说明与指路。"
    )
    for spec in primary:
        st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
    with st.expander(f"高级调度监控与运维参数（{len(advanced)} 项）", expanded=False):
        for spec in advanced:
            st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
            st.caption(
                f"归属：{spec.category} · 生效方式：{spec.activation} · 风险等级：{spec.risk_level}"
            )


def _render_channel_status(store) -> None:
    st.subheader("📣 告警通道状态（只读）")
    configured = feishu_configured(store)
    st.markdown(
        f"- 飞书通道：**{'已配置' if configured else '未配置'}**"
        + ("" if configured else "（未配置时告警只落库，不推群）")
    )
    st.caption(
        "webhook / secret 属密钥，**不在页面里编辑** —— 请在 `.env` 或密钥卡片维护，"
        "避免密钥出现在页面输入框与日志里。"
    )


def _render_never_mute() -> None:
    with st.expander(
        f"不可静音清单（{len(ALERT_NEVER_MUTE)} 项，代码常量 · 任何配置都不能静音）",
        expanded=False,
    ):
        st.caption("静音它们等于把安全系统变哑，因此写入侧会直接拒绝。")
        st.code("\n".join(sorted(ALERT_NEVER_MUTE)), language=None)
        st.caption(
            "前缀匹配**仅在本清单内**允许：" + "、".join(f"`{p}*`" for p in ALERT_NEVER_MUTE_PREFIXES)
        )


def main() -> None:
    settings = Settings.load_cached()
    render_sidebar(settings)

    st.title("🔔 通知与运维")
    st.caption(
        "静音清单写入后由告警模块**下一轮即生效**（无需重启）；"
        "被静音的事件仍会落库，只是不推送。"
    )

    store = open_trading_store()

    _render_panorama()
    _render_channel_status(store)
    _render_never_mute()

    st.subheader("✏️ 可编辑：静音清单")
    current, rejected_in_config = current_mute_list(store)
    st.caption(
        "静音项必须是**完整事件名**，禁止通配（`*` / `?`）。"
        "被静音的事件仍然落库（`muted=1`），只是不推送。"
        "不可静音清单是代码常量，永远覆盖静音配置。"
    )
    st.markdown(
        "当前静音：" + ("、".join(sorted(current)) if current else "**（空）**")
    )
    if rejected_in_config:
        st.error(f"配置里含非法项，已忽略：{rejected_in_config} —— 请改成完整事件名。")

    if not render_admin_login():
        st.info("修改静音清单需要管理员登录；未登录时本页只读。")
        return

    raw_entries = st.text_area(
        "静音事件名（每行一个）",
        value="\n".join(sorted(current)),
        height=140,
        key="ops_notify_mute_entries",
        help="例如 PORTFOLIO_STRESS_BREACH。禁止 RECONCILIATION_* 这类通配。",
    )
    approver = st.text_input("批准人", value="", key="ops_notify_mute_approver")
    reason = st.text_input("原因", value="", key="ops_notify_mute_reason")
    submitted = st.button("保存静音清单", type="primary")

    if submitted:
        entries = [line.strip() for line in raw_entries.splitlines() if line.strip()]
        blocked = [item for item in entries if is_never_mutable(item)]
        if blocked:
            st.error(f"这些事件不可静音，请从清单里去掉：{blocked}")
        elif not approver.strip():
            st.error("必须填写批准人 —— 静音是人工决策，必须能追溯到人。")
        else:
            accepted, rejected = update_mute_list(
                store, entries, approver.strip(), reason.strip(),
            )
            if rejected:
                st.error(f"已拒绝，未写入：{rejected}（必须是完整事件名，禁止通配）")
            else:
                st.success(
                    f"已保存 {len(accepted)} 项静音事件，下一轮告警即按新清单判定。"
                )
                st.caption("写入位置：`alert_mute_list`（`runtime_config`），读取方 `AlertManager._mute_list()`。")


main()
