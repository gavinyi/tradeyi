"""配置中心业务入口 2：策略中心 —— **策略启停 / 暂停新开仓 唯一可编辑入口**。

回答核心问题：每个策略开着吗？要不要暂停新开仓？跑什么模式？

归属技术域：⑤策略总览 ⑥Core Satellite ⑦期权与Gamma ⑧波动率策略。

本页这一批真正能改的两项（其余只读 + 指路）：

- **策略启停**：写 `strategy_enabled:{tenant}:{strategy}`，读它的是
  `resolve_strategy_enabled()`（同用 `get_setting` / `set_setting`，**同表同层**）；
  调度器下一个周期按新值决定是否运行该策略。
- **暂停新开仓（只减仓）**：写 `strategy_entries_paused:{tenant}:{strategy}`，
  读它的是 `is_strategy_entries_paused()`（同上）；只拦 BUY，已有持仓继续止盈止损退出。

**执行模式（SHADOW/PAPER/REAL）不在本页编辑**——它直通实盘门禁与审批，属红区，
本页只读展示当前生效值并指路到「实盘交易控制」页（Reviewer 判据 4）。

租户解析失败时**拒绝保存**，绝不回落旧全局键（旧键会被租户键盖过 = 写了不生效）。
"""

from __future__ import annotations

import streamlit as st

from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY
from futu_quant.strategy_pause import is_strategy_entries_paused, set_strategy_entries_paused
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.strategy_toggle import resolve_strategy_enabled, set_strategy_enabled
from futu_quant.ui import get_current_tenant, open_trading_store, render_admin_login, render_sidebar

st.set_page_config(page_title="策略中心 - 统一配置", page_icon="📈", layout="wide")


def strategy_rows(store, tenant_id: str) -> list[dict]:
    """当前租户下每个策略的启停 / 暂停状态（读的是调度器读的同一批键）。"""
    rows: list[dict] = []
    for plugin in strategy_plugins():
        rows.append({
            "strategy_id": plugin.strategy_id,
            "display_name": getattr(plugin, "display_name", plugin.strategy_id),
            "enabled": resolve_strategy_enabled(store, plugin.strategy_id, tenant_id),
            "entries_paused": is_strategy_entries_paused(store, plugin.strategy_id, tenant_id),
        })
    return rows


def _render_panorama() -> None:
    specs = [s for s in GLOBAL_REGISTRY.all_specs() if s.user_area == "strategy_center"]
    primary = [s for s in specs if s.surface == "primary"]
    advanced = [s for s in specs if s.surface == "advanced"]

    st.subheader("📋 归属本页的配置全景（只读）")
    st.caption(
        f"本页归属 {len(specs)} 项：核心 {len(primary)} 项、高级 {len(advanced)} 项。"
        "本批只有「策略启停 / 暂停新开仓」可编辑，其余只做说明与指路。"
    )
    for spec in primary:
        st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
    with st.expander(f"高级技术参数与策略细项（{len(advanced)} 项）", expanded=False):
        for spec in advanced:
            st.markdown(f"- **{spec.title}**：{spec.description or '暂无业务说明'}")
            st.caption(
                f"归属：{spec.category} · 生效方式：{spec.activation} · 风险等级：{spec.risk_level}"
            )


def main() -> None:
    settings = Settings.load()
    render_sidebar(settings)

    st.title("📈 策略中心")
    st.caption(
        "策略启停与暂停新开仓写入数据库后，由调度器**下一个周期**生效（无需重启）。"
        "暂停新开仓只拦买入；已有持仓继续由策略止盈、止损与退出管理。"
    )

    store = open_trading_store()
    tenant = get_current_tenant(settings)
    tenant_id = str(tenant.get("tenant_id") or tenant.get("id") or "") if isinstance(tenant, dict) else ""

    _render_panorama()

    st.subheader("✏️ 可编辑：策略启停 / 暂停新开仓")
    if not tenant_id:
        st.error(
            "无法解析当前租户，已拒绝在本页保存：写全局旧键会被租户键盖过，"
            "等于「保存成功但实际没生效」。请先在侧边栏选中租户。"
        )
        return
    st.caption(f"当前租户：**{tenant_id}** · 写入 `strategy_enabled:{tenant_id}:*`")

    rows = strategy_rows(store, tenant_id)
    st.markdown(
        "\n".join(
            f"- **{row['display_name']}**（`{row['strategy_id']}`）："
            f"{'运行中' if row['enabled'] else '已停止'}"
            f"{' · 已暂停新开仓' if row['entries_paused'] else ''}"
            for row in rows
        )
    )

    if not render_admin_login():
        st.info("修改策略启停需要管理员登录；未登录时本页只读。")
        return

    wanted_enabled: dict[str, bool] = {}
    wanted_paused: dict[str, bool] = {}
    for row in rows:
        st.divider()
        st.markdown(f"**{row['display_name']}**（`{row['strategy_id']}`）")
        wanted_enabled[row["strategy_id"]] = st.checkbox(
            "启用自动调度",
            value=bool(row["enabled"]),
            key=f"strategy_enabled_{row['strategy_id']}",
        )
        wanted_paused[row["strategy_id"]] = st.checkbox(
            "暂停新开仓（只减仓）",
            value=bool(row["entries_paused"]),
            key=f"strategy_paused_{row['strategy_id']}",
            help="开启后该策略坚决不建新仓；已有持仓继续执行止盈、止损与退出管理。",
        )
        st.caption(
            "执行模式（SHADOW / PAPER / REAL）属实盘门禁，红区，本页不提供编辑；"
            "请在「实盘交易控制」页查看与操作。"
        )

    confirmed = st.checkbox("我确认以上改动会立即影响策略是否运行与新开仓行为")
    submitted = st.button("保存策略启停", type="primary")

    if submitted:
        if not confirmed:
            st.warning("未勾选确认框，本次不保存。")
            return
        changed: list[dict] = []
        for row in rows:
            new_enabled = wanted_enabled[row["strategy_id"]]
            new_paused = wanted_paused[row["strategy_id"]]
            if new_enabled == bool(row["enabled"]) and new_paused == bool(row["entries_paused"]):
                continue
            set_strategy_enabled(store, row["strategy_id"], new_enabled, tenant_id=tenant_id)
            set_strategy_entries_paused(store, row["strategy_id"], new_paused, tenant_id=tenant_id)
            changed.append({
                "strategy_id": row["strategy_id"],
                "enabled": {"before": bool(row["enabled"]), "after": new_enabled},
                "entries_paused": {"before": bool(row["entries_paused"]), "after": new_paused},
            })
        if not changed:
            st.info("与当前状态相同，未做任何修改。")
            return
        store.audit(
            "STRATEGY_CONTROLS_UPDATED",
            {
                "actor": "streamlit_admin",
                "tenant_id": tenant_id,
                "changes": changed,
                "source": "pages/44_strategy_center.py",
            },
            actor="streamlit_admin:panel",
        )
        st.success(
            f"已保存 {len(changed)} 个策略的启停/暂停状态，调度器下一个周期生效。"
        )
        st.caption(
            f"写入位置：`strategy_enabled:{tenant_id}:*`、`strategy_entries_paused:{tenant_id}:*`"
            "（`system_settings`），读取方为调度器同一批解析函数。"
        )


main()
