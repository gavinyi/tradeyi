"""告警投递中心（P0-2 S4）。

这个页面只回答一个问题：**告警有没有真的送出去**。
「库里有、用户看不到」正是本次修复要根治的形态 —— 所以可见性必须落在一个
**常驻页面**上，而不是靠再发一条告警去发现告警坏了（那是无限递归）。

三块内容：
① **按通道**的配置状态 —— 一个通道没配，另一个仍能送达；两个都没配才等于「只落库」；
② **未投递视图** —— `ERROR/CRITICAL AND delivered=0 AND muted=0`；
③ **静音清单管理** —— 显式事件名、禁通配、写批准人/原因（走 `mutate_runtime_config` 留痕）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.alerts import (
    ALERT_MUTE_LIST_KEY,
    ALERT_NEVER_MUTE,
    ALERT_NEVER_MUTE_PREFIXES,
    AlertManager,
    is_never_mutable,
    parse_mute_list,
)
from futu_quant.config import Settings
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="告警投递中心", page_icon="📣", layout="wide")
render_sidebar(Settings.load())
st.title("告警投递中心")
st.caption(
    "回答一个问题：**告警有没有真的送出去**。"
    "「库里有、用户看不到」是本次要根治的形态。"
)

store = TradingStore()
alerts = AlertManager(store)
admin = render_admin_login()


@st.cache_data(ttl=15, max_entries=2, show_spinner=False)
def load_undelivered() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().undelivered_alerts(limit=300))


@st.cache_data(ttl=15, max_entries=2, show_spinner=False)
def load_muted() -> pd.DataFrame:
    return pd.DataFrame(TradingStore().muted_alerts(limit=300))


def delivery_label(value) -> str:
    """三态显示：`-1` 未尝试 / `0` 失败 / `1` 成功。

    `-1` 与 `0` 必须**看得见区别** —— 「通道空着」是配置问题，「通道坏了」是故障。
    """
    try:
        state = int(value)
    except (TypeError, ValueError):
        state = -1
    if state == 1:
        return "✅ 成功"
    if state == 0:
        return "❌ 失败"
    return "⚠️ 未尝试"


# ---------------------------------------------------------------------------
# ① 通道状态（按通道，不是一个布尔）
# ---------------------------------------------------------------------------
channels = alerts.channel_status()
with st.container(border=True):
    st.subheader("投递通道")
    st.caption(
        "**按通道**看：只清空一个通道时，告警仍会经另一个通道送达；"
        "**两个都不可用**才等于「只落库、不推送」。"
    )
    with st.container(horizontal=True):
        st.metric("Webhook", "已配置" if channels["webhook"] else "未配置", border=True)
        st.metric("飞书群", "已配置" if channels["feishu"] else "未配置", border=True)
    if not channels["webhook"] and not channels["feishu"]:
        st.error(
            "**两个通道都未配置** —— 当前所有告警只会落库，不会送达任何人。"
            "请配置 `ALERT_WEBHOOK_URL`，或 `FEISHU_WEBHOOK_URL` / `FEISHU_GROUP_CHAT_ID`。",
            icon=":material/error:",
        )
    elif not channels["webhook"] or not channels["feishu"]:
        missing = "Webhook" if not channels["webhook"] else "飞书群"
        st.warning(
            f"{missing} 通道未配置；告警仍会经另一个通道送达。", icon=":material/warning:",
        )
    else:
        st.success("两个通道都已配置。", icon=":material/check_circle:")

# ---------------------------------------------------------------------------
# ② 未投递视图
# ---------------------------------------------------------------------------
undelivered = load_undelivered()
with st.container(border=True):
    st.subheader("未投递告警")
    st.caption(
        "口径：`severity IN ('ERROR','CRITICAL') AND delivered = 0 AND muted = 0`。"
        "**被静音的不在这里** —— 静音是人工决策、不是故障；但它仍在下方「被静音」里可见。"
    )
    if undelivered.empty:
        st.success("当前没有未投递的 ERROR 及以上告警。", icon=":material/check_circle:")
    else:
        view = undelivered.copy()
        view["webhook 通道"] = view["webhook_delivered"].map(delivery_label)
        view["飞书通道"] = view["feishu_delivered"].map(delivery_label)
        columns = [
            column for column in
            ("id", "severity", "event_type", "message", "webhook 通道", "飞书通道", "created_at")
            if column in view.columns
        ]
        st.dataframe(china_time_columns(view[columns]), hide_index=True)
        st.caption(f"共 {len(view)} 条。修复方向：先看通道状态，再确认是否被静音。")

# ---------------------------------------------------------------------------
# ③ 静音清单管理
# ---------------------------------------------------------------------------
current_raw = store.get_setting(ALERT_MUTE_LIST_KEY, "")
current, rejected_in_config = parse_mute_list(current_raw)
with st.container(border=True):
    st.subheader("静音清单")
    st.caption(
        "静音项必须是**完整事件名**，**禁止通配**（`*` / `?`）。"
        "被静音的事件**仍然落库**（`muted=1`），只是不推送。"
        "**不可静音清单是代码常量，永远覆盖静音配置。**"
    )
    st.code("\n".join(sorted(current)) if current else "（空）", language=None)
    if rejected_in_config:
        st.error(
            f"配置里含非法项，已忽略：{rejected_in_config} —— 请改成完整事件名。",
            icon=":material/error:",
        )

    with st.container():
        st.info(
            "静音清单的编辑入口已统一到「通知与运维」页（唯一写入点）。"
            "本页只做只读展示，避免同一配置两处可改。",
            icon=":material/info:",
        )
        st.page_link(
            "pages/47_ops_and_notifications.py",
            label="前往「通知与运维」修改静音清单",
            icon=":material/campaign:",
        )

    with st.expander("查看不可静音清单（代码常量，不可配置）", expanded=False):
        st.caption(
            "这些事件**任何配置都不能静音** —— 静音它们等于把安全系统变哑。"
        )
        st.code("\n".join(sorted(ALERT_NEVER_MUTE)), language=None)
        st.caption(
            "前缀匹配**仅在本清单内**允许："
            + "、".join(f"`{prefix}*`" for prefix in ALERT_NEVER_MUTE_PREFIXES)
        )
        st.caption(
            "判定：`is_never_mutable(event_type)` —— "
            "静音清单与不可静音清单取交集时，**不可静音胜出**。"
        )

muted = load_muted()
with st.container(border=True):
    st.subheader("被静音的告警")
    st.caption(
        "**静音不是抹除** —— 它们不出现在「未投递」视图，但必须仍然可见，"
        "否则「人工决策」和「从来没发生过」就分不清了。"
    )
    if muted.empty:
        st.info("当前没有被静音的告警。")
    else:
        muted_view = muted.copy()
        muted_view["webhook 通道"] = muted_view["webhook_delivered"].map(delivery_label)
        muted_view["飞书通道"] = muted_view["feishu_delivered"].map(delivery_label)
        columns = [
            column for column in
            ("id", "severity", "event_type", "message", "webhook 通道", "飞书通道", "created_at")
            if column in muted_view.columns
        ]
        st.dataframe(china_time_columns(muted_view[columns]), hide_index=True)

st.caption(
    "**递归防护**：探针自己投不出去时，不靠再发告警去发现告警故障 —— "
    "由本页面的「未投递」视图兜底。"
)
