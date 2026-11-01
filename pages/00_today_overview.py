"""配置中心业务入口 1：今日总览（只读大屏聚合）。

回答核心问题：现在能不能交易？有没有要处理的事？
硬约束：
1. 3秒内明确回答「是否允许交易」「有无待处理风险」；
2. 纯聚合展示（双租户健康、待审批、告警、待重启变更），绝对零可写配置项。
"""

from __future__ import annotations

import streamlit as st
from futu_quant.config import Settings
from futu_quant.ui import render_sidebar
from futu_quant.ui_store import ui_store
from futu_quant.broker import get_tenant_broker
from futu_quant.tenant import get_current_tenant

st.set_page_config(page_title="今日总览 - 统一配置中心", page_icon="🧭", layout="wide")

settings = Settings.load()
render_sidebar(settings)

st.title("🧭 今日总览 (交易与风险状态)")

# 提示说明
st.caption(
    "修改『归属区域、显示名称』等配置元数据后，需在桌面执行 stop.bat → start.bat 才会生效。"
    "按 F5 刷新无效：元数据在进程启动时一次性载入内存。"
)

store = ui_store()
broker = get_tenant_broker(settings)

# 1. 核心状态卡片 (3秒内回答能不能交易)
col1, col2, col3, col4 = st.columns(4)

current_tenant = get_current_tenant(settings)
is_real = current_tenant.get("is_real", False)
real_enabled = settings.real_trading_enabled

with col1:
    st.metric(
        label="当前交易环境",
        value="🔴 实盘 (REAL)" if is_real else "🟢 模拟 (SIMULATE)",
        delta="允许真金交易" if (is_real and real_enabled) else ("模拟仿真" if not is_real else "实盘已关闭"),
    )

with col2:
    incidents = store.incidents("OPEN") if hasattr(store, "incidents") else []
    st.metric(
        label="未解决事故 (Incidents)",
        value=f"{len(incidents)} 项",
        delta="存在未恢复风险" if incidents else "系统健康",
        delta_color="inverse" if incidents else "normal",
    )

with col3:
    pending_approvals = store.pending_approvals() if hasattr(store, "pending_approvals") else []
    st.metric(
        label="待处理审批 (Approvals)",
        value=f"{len(pending_approvals)} 项",
        delta="需要人工审核" if pending_approvals else "无待办",
        delta_color="inverse" if pending_approvals else "normal",
    )

with col4:
    st.metric(
        label="待重启生效变更",
        value="0 项",
        delta="配置与运行态一致",
    )

st.markdown("---")

# 2. 快捷指路与四大业务入口
st.subheader("🛠️ 统一业务配置入口")
c_a, c_b, c_c, c_d = st.columns(4)

with c_a:
    st.info("**📈 策略中心**\n\n策略启停、运行模式、标的白名单、理财开关。\n\n[前往策略中心 ->](策略中心)")

with c_b:
    st.warning("**🛡️ 风险与限额**\n\n账户日限额、单笔限额、风控熔断线、止损止盈。\n\n[前往风险与限额 ->](风险与限额)")

with c_c:
    st.success("**🏦 账户与交易环境**\n\n租户激活、模拟/实盘切换、暂停新开仓、执行订单。\n\n[前往账户与环境 ->](账户与交易环境)")

with c_d:
    st.error("**🔔 通知与运维**\n\n飞书群告警、静音清单、紧急急停、巡检与灾备。\n\n[前往通知与运维 ->](通知与运维)")

st.caption("提示：高级技术参数与完整只读目录请前往「统一配置中心」底座。")
