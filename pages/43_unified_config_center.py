"""统一配置中心只读目录与元数据审计看板 (Phase 2)。

本页面为纯只读展示页面：
1. 完整展示不可变 ConfigRegistry 登记的所有配置元数据；
2. 提供多维度分类、存储层、风险分级过滤与全文搜索；
3. 严格禁止引入任何直接写入、草稿创建或配置修改入口 (通过 CI AST 守卫强制保证)。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.config_center import GLOBAL_REGISTRY
from futu_quant.ui import render_sidebar

st.set_page_config(page_title="统一配置中心", page_icon="🧭", layout="wide")
settings = Settings.load()
render_sidebar(settings)

st.title("🧭 统一配置中心 (只读目录 & 元数据审计)")
st.caption("全系统配置单一事实来源 (Single Source of Truth) · 规范键、落库键、存储层与风险等级全景视图")

specs = GLOBAL_REGISTRY.all_specs()

# 顶部核心指标看板
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    st.metric("已登记配置总数", len(specs))
with col2:
    critical_count = sum(1 for s in specs if s.risk_level == "CRITICAL")
    st.metric("CRITICAL 极高风险", critical_count)
with col3:
    runtime_count = sum(1 for s in specs if s.storage == "runtime_config")
    st.metric("动态配置 (runtime_config)", runtime_count)
with col4:
    settings_count = sum(1 for s in specs if s.storage == "system_settings")
    st.metric("系统设置 (system_settings)", settings_count)
with col5:
    env_count = sum(1 for s in specs if s.storage == "env")
    st.metric("环境变量 (env)", env_count)

st.divider()

# 多维筛选工具栏
f_col1, f_col2, f_col3, f_col4 = st.columns(4)
with f_col1:
    all_categories = sorted({s.category for s in specs})
    selected_category = st.selectbox("业务分类筛选", options=["全部"] + all_categories, index=0)
with f_col2:
    all_risks = ["全部", "CRITICAL", "HIGH", "MEDIUM", "LOW"]
    selected_risk = st.selectbox("风险等级筛选", options=all_risks, index=0)
with f_col3:
    all_storages = ["全部", "runtime_config", "system_settings", "env"]
    selected_storage = st.selectbox("存储层筛选", options=all_storages, index=0)
with f_col4:
    search_keyword = st.text_input("全文检索 (键名/说明/别名/组件)", value="").strip().lower()

filtered_specs = specs
if selected_category != "全部":
    filtered_specs = [s for s in filtered_specs if s.category == selected_category]
if selected_risk != "全部":
    filtered_specs = [s for s in filtered_specs if s.risk_level == selected_risk]
if selected_storage != "全部":
    filtered_specs = [s for s in filtered_specs if s.storage == selected_storage]
if search_keyword:
    filtered_specs = [
        s for s in filtered_specs
        if search_keyword in s.key.lower()
        or search_keyword in s.runtime_key.lower()
        or search_keyword in s.title.lower()
        or search_keyword in s.description.lower()
        or any(search_keyword in a.lower() for a in s.aliases)
        or any(search_keyword in c.lower() for c in s.consumers)
    ]

st.markdown(f"**检索结果：匹配到 `{len(filtered_specs)}` 项配置**")

# 结构化表格数据构造
table_rows = []
for s in filtered_specs:
    table_rows.append({
        "规范键 (Canonical)": s.key,
        "落库键 (Runtime Key)": s.runtime_key,
        "配置名称": s.title,
        "风险等级": s.risk_level,
        "存储层": s.storage,
        "值类型": s.value_type,
        "代码默认值": str(s.default) if s.default is not None else "(None)",
        "可编辑": "是" if s.editable else "否 (只读/硬锁定)",
        "生效方式": s.activation,
        "业务分类": s.category,
        "消费者组件": ", ".join(s.consumers),
        "业务说明": s.description,
    })

if table_rows:
    df = pd.DataFrame(table_rows)
    st.dataframe(df, width="stretch", hide_index=True)
else:
    st.info("未检索到符合条件的配置项。")

st.divider()

# 配置项明细与反射探查器
st.subheader("🔍 配置项元数据深度探查")
spec_keys = [s.key for s in filtered_specs]
if spec_keys:
    inspect_key = st.selectbox("选择要深入探查元数据的配置规范键", options=spec_keys, index=0)
    inspect_spec = GLOBAL_REGISTRY.get(inspect_key)
    if inspect_spec:
        st.json({
            "key": inspect_spec.key,
            "runtime_key": inspect_spec.runtime_key,
            "aliases": list(inspect_spec.aliases),
            "title": inspect_spec.title,
            "description": inspect_spec.description,
            "category": inspect_spec.category,
            "scope": inspect_spec.scope,
            "storage": inspect_spec.storage,
            "risk_level": inspect_spec.risk_level,
            "value_type": inspect_spec.value_type,
            "default": inspect_spec.default,
            "editable": inspect_spec.editable,
            "requires_admin": inspect_spec.requires_admin,
            "requires_confirmation": inspect_spec.requires_confirmation,
            "activation": inspect_spec.activation,
            "consumers": list(inspect_spec.consumers),
            "tags": list(inspect_spec.tags),
            "dependencies": list(inspect_spec.dependencies),
            "secret": inspect_spec.secret,
            "deprecated": inspect_spec.deprecated,
        })
