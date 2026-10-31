from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.disaster_recovery import create_recovery_point, latest_recovery_drill, verify_recovery_point
from futu_quant.migrations import migration_status
from futu_quant.parquet_store import archive_market_data, warehouse_files
from futu_quant.postgres_migration import postgres_readiness
from futu_quant.rbac import PERMISSIONS, role_for_session
from futu_quant.secret_store import keychain_available, secret_status, set_secret
from futu_quant.storage_monitor import storage_report
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import china_time_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="基础设施与灾备", page_icon="🗄️", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()
role = role_for_session(admin)

st.title("基础设施、数据仓库与灾备")
st.caption("当前 SQLite 继续作为默认权威库；PostgreSQL 只提供显式离线迁移路径，不会自动切换运行后端。")

migration = migration_status(store)
postgres = postgres_readiness()
storage = storage_report()
files = warehouse_files()

with st.container(horizontal=True):
    st.metric("数据库迁移版本", f"{migration['current']} / {migration['latest']}", border=True)
    st.metric("PostgreSQL", "迁移就绪" if postgres["safe_to_migrate"] else "未启用", border=True)
    st.metric("Parquet 文件", len(files), border=True)
    st.metric("恢复点", len(store.recovery_points()), border=True)
    st.metric("磁盘可用", f"{storage['free_ratio']:.1%}", border=True)

database_tab, warehouse_tab, recovery_tab, config_tab, security_tab, capacity_tab, deploy_tab = st.tabs(
    ["数据库迁移", "Parquet 与血缘", "恢复点与演练", "配置发布", "权限与密钥", "容量监控", "容器部署"]
)

with database_tab:
    st.subheader("版本化迁移")
    st.dataframe(china_time_columns(pd.DataFrame(store.schema_migrations())), hide_index=True)
    (st.success if migration["healthy"] else st.warning)(
        "数据库结构已是最新版本。" if migration["healthy"] else f"待执行迁移：{migration['pending']}"
    )
    st.subheader("PostgreSQL 渐进迁移")
    st.json(postgres)
    st.code("uv sync --extra postgres\nuv run python scripts/migrate_to_postgres.py --dry-run\nuv run python scripts/migrate_to_postgres.py --execute", language="bash")
    st.warning("执行迁移只复制数据，不会自动修改 DATABASE_BACKEND；完成行数核对和恢复演练后才能人工切换。")

with warehouse_tab:
    if st.button("立即增量归档到 Parquet", type="primary", disabled=not admin):
        records = archive_market_data(store)
        st.success(f"完成 {len(records)} 个日期/标的分区归档。")
        st.rerun()
    lineage = pd.DataFrame(store.lineage())
    if lineage.empty:
        st.info("尚无行情归档。后台每天自动增量归档。")
    else:
        st.dataframe(china_time_columns(lineage).drop(columns=["id"], errors="ignore"), hide_index=True)
        st.caption("内容哈希、来源、质量等级与分区路径共同构成回测数据血缘。")

with recovery_tab:
    with st.container(horizontal=True):
        if st.button("创建增量恢复点", type="primary", disabled=not admin):
            point = create_recovery_point(store)
            st.success(f"恢复点 {point['recovery_id']} 已创建。")
            st.rerun()
        if st.button("运行隔离恢复演练", disabled=not admin):
            result = latest_recovery_drill(store)
            (st.success if result["passed"] else st.error)(f"灾备演练结果：{result}")
            st.rerun()
    points = pd.DataFrame(store.recovery_points())
    if not points.empty:
        st.dataframe(china_time_columns(points), hide_index=True)
        selected = st.selectbox("选择恢复点复核", points["recovery_id"].tolist())
        if st.button("重新验证选中恢复点", disabled=not admin):
            st.json(verify_recovery_point(store, selected))
    st.caption("恢复演练只在临时隔离目录重建数据库并执行完整性检查，不覆盖当前运行数据。")

with config_tab:
    st.subheader("配置草稿、差异、审批与发布")
    from futu_quant.config_center import get_draftable_config_keys

    # 统一配置中心 Phase 1 改造：受限表驱动选择，严禁自由文本输入未知配置键
    # 严格门禁：通过统一助手函数剔除 CRITICAL 极高风险与需二次确认项
    candidate_keys = get_draftable_config_keys()

    with st.form("config_release_draft", border=True):
        config_key = st.selectbox(
            "配置键 (受限目录驱动，已剔除 CRITICAL 极高风险)",
            options=candidate_keys,
            index=0 if candidate_keys else None,
        )
        draft_text = st.text_area("草稿 JSON", value="{}", height=140)
        reason = st.text_input("变更原因")
        create_draft = st.form_submit_button("创建配置草稿", disabled=not admin)
    if create_draft:
        try:
            release_id = store.create_config_release(config_key, json.loads(draft_text), "streamlit_admin", reason)
            st.success(f"草稿 #{release_id} 已创建，尚未发布。")
            st.rerun()
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(str(exc))
    releases = pd.DataFrame(store.config_releases())
    if not releases.empty:
        st.dataframe(china_time_columns(releases).drop(columns=["draft_json"], errors="ignore"), hide_index=True)
        drafts = releases[releases["status"] == "DRAFT"]
        if not drafts.empty:
            release_id = st.selectbox("待审批草稿", drafts["id"].astype(int).tolist())
            release = drafts[drafts["id"] == release_id].iloc[0]
            st.json(json.loads(release["diff_json"]))
            with st.container(horizontal=True):
                if st.button("审批并发布", type="primary", disabled=not admin):
                    store.publish_config_release(int(release_id), "streamlit_admin")
                    st.success("配置已原子发布；服务下一循环读取。")
                    st.rerun()
                if st.button("拒绝草稿", disabled=not admin):
                    store.reject_config_release(int(release_id), "streamlit_admin")
                    st.rerun()
        published = releases[releases["status"] == "PUBLISHED"]
        if not published.empty:
            rollback_id = st.selectbox("选择已发布版本回滚", published["id"].astype(int).tolist())
            if st.button("回滚选中配置", disabled=not admin):
                store.rollback_config_release(int(rollback_id), "streamlit_admin")
                st.success("配置已回滚并生成新的运行版本。")
                st.rerun()

with security_tab:
    st.subheader("角色权限")
    st.info(f"当前会话角色：{role}")
    permission_rows = [{"角色": name, "权限": "、".join(sorted(values))} for name, values in PERMISSIONS.items()]
    st.dataframe(pd.DataFrame(permission_rows), hide_index=True)
    st.subheader("系统钥匙串")
    st.caption("页面只显示是否已配置，绝不回显密钥值。")
    secrets = ["ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_SECRET", "DATABASE_URL"]
    st.dataframe(pd.DataFrame(secret_status(secrets)), hide_index=True)
    with st.form("keychain_secret", border=True):
        secret_name = st.selectbox("密钥名称", secrets)
        secret_value = st.text_input("密钥值", type="password")
        save_secret = st.form_submit_button(
            "保存到 macOS 钥匙串", disabled=not admin or not keychain_available(),
        )
    if save_secret:
        try:
            set_secret(secret_name, secret_value)
            st.success("已写入系统钥匙串。")
        except RuntimeError as exc:
            st.error(str(exc))

with capacity_tab:
    components = pd.DataFrame([
        {"组件": key, "占用字节": value, "占用GB": value / 1024**3}
        for key, value in storage["components"].items()
    ])
    (st.success if storage["status"] == "HEALTHY" else st.error)(
        f"磁盘状态 {storage['status']}；可用 {storage['free_bytes'] / 1024**3:.2f} GB。"
    )
    st.bar_chart(components, x="组件", y="占用GB")
    st.dataframe(components, hide_index=True)

with deploy_tab:
    st.markdown(
        """
        Docker Compose 提供三个隔离服务：仪表盘、统一交易服务和波动率雷达；PostgreSQL 使用独立 profile。

        默认仪表盘容器强制 `ENABLE_PAPER_ORDERS=false`。自动模拟交易必须显式启用
        `paper-automation` profile，并在 `.env.docker` 中确认模拟盘配置。
        """
    )
    st.code(
        "cp .env.docker.example .env.docker\n"
        "docker compose up -d dashboard\n"
        "docker compose --profile paper-automation up -d trading-service volatility-radar\n"
        "docker compose --profile postgres up -d postgres",
        language="bash",
    )
    st.caption(f"项目部署文件：{Path('docker-compose.yml').resolve()}")
