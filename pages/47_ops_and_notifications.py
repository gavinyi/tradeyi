"""配置中心业务入口 5：通知与运维。

回答核心问题：告警发给谁？服务活着吗？改完要不要重启？
归属技术域：⑪通知与可观测性 + ⑩行情与数据 + ⑫运维与高级 + ③急停与运行控制
规范：首屏 <= 2 屏，默认渲染项 <= 30，高级详情折叠。正文使用中文业务描述，底层键名收进高级详情。
"""

from __future__ import annotations

import streamlit as st
from futu_quant.config import Settings
from futu_quant.ui import render_sidebar
from futu_quant.config_center import GLOBAL_REGISTRY

st.set_page_config(page_title="通知与运维 - 统一配置", page_icon="🔔", layout="wide")

settings = Settings.load()
render_sidebar(settings)

st.title("🔔 通知与运维")

specs = [s for s in GLOBAL_REGISTRY.all_specs() if s.user_area == "ops_notify"]
primary_specs = [s for s in specs if s.surface == "primary"]
advanced_specs = [s for s in specs if s.surface == "advanced"]

editable_primary_count = sum(1 for s in primary_specs if s.editable)
if editable_primary_count == 0:
    st.info("ℹ️ 分批建设中：当前为只读骨架展示，可编辑控制流将于后续批次接入。")

st.caption(
    "修改『归属区域、显示名称』等配置元数据后，需在桌面执行 stop.bat → start.bat 才会生效。"
    "按 F5 刷新无效：元数据在进程启动时一次性载入内存。"
)

st.write(f"当前通知与运维共包含 **{len(specs)}** 项配置（核心主干 **{len(primary_specs)}** 项，高级参数 **{len(advanced_specs)}** 项折叠）。")

# 核心通知与急停主干
st.subheader("核心告警通知与急停控制")
for s in primary_specs:
    with st.container():
        st.markdown(f"### {s.title}")
        st.write(s.description or "暂无业务说明")
        st.text(f"控制对象: {s.category} | 风险等级: {s.risk_level} | 生效方式: {s.activation}")
        st.divider()

# 高级详情折叠
with st.expander(f"高级调度监控与运维参数 ({len(advanced_specs)} 项)"):
    for s in advanced_specs:
        st.markdown(f"- **{s.title}** (`{s.runtime_key}` / `{s.key}`) : {s.description}")
