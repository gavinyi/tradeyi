from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from futu_quant.attribution import attribution_report
from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.experiments import sync_strategy_experiments
from futu_quant.radar_control import (
    start as start_radar,
    status as radar_status,
    stop as stop_radar,
)
from futu_quant.service_control import restart, start, status as service_status, stop
from futu_quant.execution_mode import resolve_configured_execution_mode
from futu_quant.strategy_pause import is_strategy_entries_paused, set_strategy_entries_paused
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.strategy_budgeting import (
    budget_group_label,
    recommended_active_budget_rows,
)
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.strategy_toggle import resolve_strategy_enabled, set_strategy_enabled
from futu_quant.ui import chinese_columns, get_current_tenant, render_admin_login, render_sidebar


st.set_page_config(page_title="策略与资金控制", page_icon="🎛️", layout="wide")
# 本页能启停策略、改资金/暂停状态（set_strategy_enabled / set_strategy_entries_paused），
# 直接改变运行状态；settings 又参与租户判定。这类页面不吃缓存（2026-11-04 Chief 判据）。
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()
admin = render_admin_login()

def current_tenant_id() -> str:
    """当前租户（页面级单一口径）。

    必须在模块级先算一次：控制页的策略表格在**页面渲染时**就要按租户解析执行模式，
    只在 `load_control_snapshot()` 里定义局部变量会直接 `NameError`（页面打不开）。
    """
    try:
        return str(get_current_tenant(settings).get("tenant_id") or "")
    except Exception:
        return ""


tenant_id = current_tenant_id()

st.title("策略与资金控制")
st.caption(
    "统一管理策略启停、执行模式、资金配额和后台服务。策略页面只负责各自参数，组合风险页面只负责压力测试。"
)

def load_control_snapshot():
    """读取控制页快照。

    **刻意不加 `st.cache_data`**：快照里含「当前租户的暂停状态」，而侧边栏可切换租户。
    缓存会在切换租户后最多 30 秒内继续显示**上一个租户**的状态 ——
    那正是本次要修的「界面显示与真实作用域不符」的同一类病。
    """
    current_store = TradingStore()
    # 暂停状态按**当前租户**读（租户键优先、全局键兜底），不再无条件读全局键 ——
    # 否则界面会把「别的租户的暂停状态」显示成当前租户的，误导操作者。
    # 与模块级同口径，避免「一处算一次、另一处又算一次」漂移
    snapshot_tenant_id = tenant_id
    tenant_paused = (
        current_store.get_tenant_setting(snapshot_tenant_id, "strategies_paused", "")
        if snapshot_tenant_id else ""
    )
    return (
        current_store.heartbeats(),
        current_store.strategy_risk_budgets(),
        current_store.strategy_experiments(limit=300),
        tenant_paused or current_store.get_setting("strategies_paused", "false"),
        current_store.get_setting("execution_mode", os.getenv("EXECUTION_MODE", "PAPER")),
    )


heartbeats_rows, budgets_rows, experiments, strategies_paused, execution_mode_setting = load_control_snapshot()
heartbeats = {row["strategy_id"]: row for row in heartbeats_rows}
budgets = {row["strategy_id"]: row for row in budgets_rows}

active_versions = {
    row["strategy_id"]: row for row in experiments if row["is_active"]
}
latest_versions = {}
for row in experiments:
    latest_versions.setdefault(row["strategy_id"], row)

rows = []
for plugin in strategy_plugins():
    heartbeat = heartbeats.get(plugin.strategy_id, {})
    budget = budgets.get(plugin.strategy_id, {})
    active = active_versions.get(plugin.strategy_id, {})
    latest = latest_versions.get(plugin.strategy_id, {})
    rows.append({
        "strategy_id": plugin.strategy_id, "display_name": plugin.display_name,
        "service_name": plugin.service_name, "asset_class": plugin.asset_class,
        "enabled": resolve_strategy_enabled(
            store, plugin.strategy_id, tenant_id or None,
            default=not plugin.automatically_scheduled,
        ),
        # P1-6：读侧走单源，且必须带上本页租户 —— 只读旧无前缀键会把
        # 「实盘已切换 REAL」显示成 INHERIT。
        "execution_mode": resolve_configured_execution_mode(
            store, tenant_id, plugin.strategy_id,
        ) or "INHERIT",
        # P1-9：单策略暂停新开仓 (Reduce-Only 只减仓)
        "entries_paused": is_strategy_entries_paused(store, plugin.strategy_id, tenant_id),
        "heartbeat_status": heartbeat.get("status", "NOT_STARTED"),
        "heartbeat_message": heartbeat.get("message", "尚无心跳"),
        "capital_ratio": float(budget.get("capital_ratio", 0)),
        "active_version": str(active.get("code_version", "未登记")),
        "latest_version": str(latest.get("code_version", "未登记")),
        "version_drift": bool(active and latest and active["version_hash"] != latest["version_hash"]),
    })
control = pd.DataFrame(rows)

service = service_status()
radar = radar_status()
with st.container(horizontal=True):
    service_label = "重启中" if service.restart_pending else ("运行中" if service.running else "未运行")
    st.metric("统一服务", service_label, border=True)
    st.metric("已注册策略", len(control), border=True)
    st.metric("启用自动策略", int(control["enabled"].sum()), border=True)
    st.metric("版本漂移", int(control["version_drift"].sum()), border=True)
    st.metric("全局策略状态", "已暂停" if strategies_paused == "true" else "运行", border=True)

service_tab, overview_tab, budget_tab, attribution_tab, experiment_tab, protocol_tab = st.tabs(
    ["服务控制", "策略调度", "策略资金", "收益归因", "实验版本", "插件协议"]
)

with service_tab:
    st.subheader("服务控制")
    with st.container(border=True):
        st.markdown("**统一交易服务**")
        if service.running:
            hours, remainder = divmod(int(service.uptime_seconds), 3600)
            minutes = remainder // 60
            st.success(
                f"{service.message} · PID {service.pid} · 已运行 {hours}小时{minutes}分钟"
            )
        else:
            st.error(service.message or "服务未运行")
        with st.container(horizontal=True):
            if st.button(
                "启动统一服务", disabled=service.running or not admin,
                icon=":material/play_arrow:", key="automation_start_service",
            ):
                result = start()
                store.audit(
                    "SERVICE_START",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                 actor="streamlit_admin:panel")
                st.toast(result.message)
                st.rerun()
            if st.button(
                "停止统一服务", disabled=not service.running or not admin,
                icon=":material/stop:", key="automation_stop_service",
            ):
                result = stop()
                store.audit(
                    "SERVICE_STOP",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                 actor="streamlit_admin:panel")
                st.toast(result.message)
                st.rerun()
            if st.button(
                "重启统一服务", disabled=not admin or service.restart_pending,
                icon=":material/restart_alt:", key="automation_restart_service",
            ):
                result = restart()
                store.audit(
                    "SERVICE_RESTART",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                 actor="streamlit_admin:panel")
                st.toast(result.message)
                st.rerun()

    with st.container(border=True):
        st.markdown("**独立波动率雷达**")
        if radar.running:
            if service.running and not service.restart_pending:
                st.success(f"{radar.message} · PID {radar.pid} · 扫描与交易执行链均在线")
            else:
                st.warning(
                    f"{radar.message} · PID {radar.pid} · 当前仅扫描，不会执行自动交易；"
                    "请先恢复统一交易服务"
                )
        else:
            st.warning(radar.message or "雷达未运行")
        st.caption("启动统一服务时会一并启动雷达；这里保留独立启停，用于只运行自选标的扫描。")
        with st.container(horizontal=True):
            if st.button(
                "启动雷达", disabled=radar.running or not admin,
                icon=":material/radar:", key="automation_start_radar",
            ):
                result = start_radar()
                store.audit(
                    "RADAR_START",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                 actor="streamlit_admin:panel")
                st.toast(result.message)
                st.rerun()
            if st.button(
                "停止雷达", disabled=not radar.running or not admin,
                icon=":material/stop_circle:", key="automation_stop_radar",
            ):
                result = stop_radar()
                store.audit(
                    "RADAR_STOP",
                    {"actor": "streamlit_admin", "source": "automation_control_center",
                     "result": result.message},
                 actor="streamlit_admin:panel")
                st.toast(result.message)
                st.rerun()

    with st.container(border=True):
        st.markdown("**租户级运行方式**")
        # 跨租户隔离（产品红线，10-23 实锤）：暂停键必须**按租户**写。
        # 此前写的是全局键 `strategies_paused` —— 而 run_service 读取时
        # **先看租户键、再回落全局键**，于是全局键一写，**所有租户**的策略循环
        # 一起被停：在实盘租户点「暂停全部策略」，模拟盘也被连坐。
        # N-Tenant 隔离会被一个全局开关整体绕过，那隔离就是假的。
        # 读：租户键优先，回落全局键（兼容历史设置，不让既有暂停悄悄失效）；
        # 写：只写租户键 —— 动不了别的租户。
        try:
            current_tenant = get_current_tenant(settings)
            pause_tenant_id = str(current_tenant.get("tenant_id") or "")
        except Exception:
            pause_tenant_id = ""
        tenant_paused = (
            store.get_tenant_setting(pause_tenant_id, "strategies_paused", "")
            if pause_tenant_id else ""
        )
        global_paused = store.get_setting("strategies_paused", "false")
        paused = (tenant_paused or global_paused).lower() == "true"
        mode = str(execution_mode_setting).upper()
        # 选项恒含三态：此前按 `settings.trading_env` 裁剪，而 settings 在面板进程内是
        # 启动时的快照（Settings.load 默认不覆盖已存在的环境变量）。改完 .env 未重启面板时，
        # REAL 不在选项里 → default 回落 PAPER → 下面「selected != mode」把 PAPER 写回库，
        # **仅打开页面就会把 REAL 静默降级成 PAPER**（10-29 13:59:46 实际发生过一次）。
        mode_options = ["SHADOW", "PAPER", "REAL"]
        default_mode = mode if mode in mode_options else "PAPER"

        def _persist_global_mode() -> None:
            """只在**用户真正改动**时落库（on_change 回调），渲染期不再自动写入。"""
            new_mode = str(st.session_state.get("automation_global_execution_mode") or "").upper()
            if not new_mode or new_mode not in mode_options:
                return
            current = str(store.get_setting("execution_mode", "PAPER")).upper()
            if new_mode == current:
                return
            store.set_setting("execution_mode", new_mode)
            store.audit(
                "EXECUTION_MODE_CHANGED",
                {"actor": "streamlit_admin", "source": "automation_control_center",
                 "before": current, "after": new_mode},
             actor="streamlit_admin:panel")

        st.segmented_control(
            "全局执行模式", mode_options, default=default_mode,
            help=(
                "SHADOW 不向富途提交订单；PAPER 提交到富途模拟盘；REAL 执行真金实盘交易。"
                "REAL 需要券商连接环境为 REAL（FUTU_TRADING_ENV），否则下单会被实盘闸门拒绝。"
            ),
            disabled=not admin, key="automation_global_execution_mode",
            on_change=_persist_global_mode,
        )
        if st.button(
            "恢复全部策略" if paused else "暂停全部策略",
            disabled=not admin, key="automation_pause_all",
        ):
            new_paused = not paused
            if pause_tenant_id:
                store.set_tenant_setting(pause_tenant_id, "strategies_paused", str(new_paused).lower())
                pause_scope = f"tenant:{pause_tenant_id}"
            else:
                # 拿不到租户上下文时不得静默写全局 —— 那正是本次修的漏洞
                store.set_setting("strategies_paused", str(new_paused).lower())
                pause_scope = "global(fallback:未取到租户上下文)"
            store.audit(
                "STRATEGIES_PAUSE_CHANGED",
                {"actor": "streamlit_admin", "source": "automation_control_center",
                 "paused": new_paused, "scope": pause_scope, "tenant_id": pause_tenant_id},
             actor="streamlit_admin:panel")
            st.rerun()
        st.caption(
            f"⚠️ 「暂停全部策略」= 停掉**当前租户（`{pause_tenant_id or '未识别'}`）**的**整个策略循环**："
            "不再评估任何持仓，因此**止盈、止损、到期退出、组合平仓管理会一并停止**，"
            "只剩订单对账与风控引擎仍在跑。**该开关只作用于当前租户，不影响其他租户。**"
            "若只想停开新仓、保留已有持仓的退出管理，请改用波动率策略页的「暂停新开仓」开关。"
        )

with overview_tab:
    st.info(
        "策略启停 / 暂停新开仓的编辑入口已统一到「策略中心」页（唯一写入点）。"
        "本页只做只读展示，避免同一配置两处可改。",
        icon=":material/info:",
    )
    st.page_link(
        "pages/44_strategy_center.py",
        label="前往「策略中心」修改策略启停 / 暂停新开仓",
        icon=":material/tune:",
    )
    st.dataframe(
        control.drop(columns=["execution_mode", "entries_paused"], errors="ignore"),
        hide_index=True, width="stretch",
        column_config={
            "strategy_id": st.column_config.TextColumn("策略ID"),
            "display_name": st.column_config.TextColumn("策略"),
            "enabled": st.column_config.CheckboxColumn("启用自动调度"),
            "capital_ratio": st.column_config.NumberColumn("资金配额", format="percent"),
            "version_drift": st.column_config.CheckboxColumn("版本漂移"),
        },
    )

    with st.container(border=True):
        st.subheader("💡 交易控制三大开关机制对比")
        st.markdown(
            """
| 控制开关 | 作用范围 | 拦截行为 | 影响平仓与退出吗？ | 适用场景 |
| :--- | :--- | :--- | :---: | :--- |
| **⏸️ 暂停新开仓 (只减仓)** | 单个策略独立 | **只拦截 BUY (开仓/加仓)** | ❌ **绝对不影响** (止盈止损正常运行) | 标的波动剧烈，需暂停新开仓并继续管理既有持仓 |
| **🚨 账户急停 (Kill Switch)** | 当前账户全部策略 | **只拦截 BUY (开仓/加仓)** | ❌ **不影响** (平仓减仓不受限) | 账户整体风控熔断，需紧急按住全账户开仓 |
| **⏹️ 暂停策略调度** | 单个策略或全局调度 | **完全终止策略循环** | ⚠️ **连带平仓一起暂停** | 策略代码升级、紧急维护检修 |
            """
        )

with budget_tab:
    st.subheader("策略资金")
    st.caption(
        "这是策略资金配额的主配置入口。资金配额合计不能超过 100%；未分配部分就是现金和风险缓冲。"
    )
    budget_rows = budgets_rows
    if not budget_rows:
        st.info("策略预算尚未初始化，请先进入组合风险页面生成默认预算。")
    else:
        budget_frame = pd.DataFrame(budget_rows)
        budget_frame["strategy_group"] = budget_frame["strategy_id"].map(budget_group_label)
        enabled_total = float(
            budget_frame[budget_frame["enabled"].astype(bool)]["capital_ratio"].sum()
        )
        with st.container(horizontal=True):
            st.metric("启用资金配额", f"{enabled_total:.0%}", border=True)
            st.metric("现金/未分配缓冲", f"{max(0.0, 1 - enabled_total):.0%}", border=True)
            st.metric(
                "波动率期权配额",
                f"{float(budget_frame.loc[budget_frame['strategy_id'] == 'volatility_long_gamma', 'capital_ratio'].sum()):.0%}",
                border=True,
            )
            st.metric(
                "财报策略配额",
                f"{float(budget_frame[budget_frame['strategy_id'].isin(['earnings_gamma', 'earnings_gamma_manual'])]['capital_ratio'].sum()):.0%}",
                border=True,
            )
        if st.button(
            "应用当前推荐：Core 65% + 波动率期权 25%",
            type="primary",
            disabled=not admin,
            icon=":material/auto_fix_high:",
        ):
            rows_to_save = recommended_active_budget_rows(budget_rows)
            store.save_strategy_risk_budgets(rows_to_save)
            store.audit(
                "STRATEGY_BUDGET_PRESET_APPLIED",
                {"actor": "streamlit_admin", "preset": "core65_volatility25"},
             actor="streamlit_admin:panel")
            st.success("推荐资金配置已应用：财报自动/手工策略资金已转给波动率期权。")
            st.rerun()
        edit_columns = [
            "strategy_group", "strategy_id", "capital_ratio", "max_loss_ratio",
            "option_premium_ratio", "enabled",
        ]
        edited_budget = st.data_editor(
            budget_frame[edit_columns],
            hide_index=True,
            width="stretch",
            disabled=["strategy_group", "strategy_id"],
            column_config={
                "strategy_group": st.column_config.TextColumn("策略分组"),
                "strategy_id": st.column_config.TextColumn("策略ID", pinned=True),
                "capital_ratio": st.column_config.NumberColumn(
                    "资金配额", min_value=0.0, max_value=1.0, format="percent"
                ),
                "max_loss_ratio": st.column_config.NumberColumn(
                    "最大损失预算", min_value=0.0, max_value=1.0, format="percent"
                ),
                "option_premium_ratio": st.column_config.NumberColumn(
                    "期权权利金配额", min_value=0.0, max_value=1.0, format="percent"
                ),
                "enabled": st.column_config.CheckboxColumn("参与风控预算"),
            },
            key="strategy_budget_control_editor",
        )
        if st.button("保存策略资金配额", disabled=not admin):
            rows_to_save = edited_budget.drop(columns=["strategy_group"]).to_dict("records")
            try:
                store.save_strategy_risk_budgets(rows_to_save)
                st.success("策略资金配额已保存，统一下单风控立即生效。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        st.caption(
            "建议当前阶段：Core Satellite 是股票主策略；volatility_long_gamma 是期权主策略；"
            "earnings_gamma / earnings_gamma_manual 暂停资金配额，只保留页面和历史记录。"
        )

with attribution_tab:
    @st.cache_data(ttl=60, max_entries=2, show_spinner=False)
    def load_attribution():
        return attribution_report(TradingStore(), FutuPaperBroker(Settings.load()))

    try:
        summary, detail = load_attribution()
        if summary.empty:
            st.info("当前统一成交账本和虚拟持仓中没有可归因数据。")
        else:
            with st.container(horizontal=True):
                st.metric("已实现盈亏", f"${summary['realized_pnl'].sum():,.2f}", border=True)
                st.metric("未实现盈亏", f"${summary['unrealized_pnl'].sum():,.2f}", border=True)
                st.metric("累计费用", f"${summary['fees'].sum():,.2f}", border=True)
                st.metric("不利滑点", f"${summary['slippage_cost'].sum():,.2f}", border=True)
                st.metric("归因总盈亏", f"${summary['total_pnl'].sum():,.2f}", border=True)
            st.bar_chart(summary.set_index("strategy_id")[["realized_pnl", "unrealized_pnl"]])
            summary_tab, detail_tab = st.tabs(["按策略", "按标的"])
            summary_tab.dataframe(chinese_columns(summary), hide_index=True, width="stretch")
            detail_tab.dataframe(chinese_columns(detail), hide_index=True, width="stretch")
            detail_tab.download_button(
                "下载归因明细 CSV", chinese_columns(detail).to_csv(index=False).encode("utf-8-sig"),
                file_name="strategy_attribution.csv", mime="text/csv",
            )
            st.caption("已实现盈亏采用 FIFO；未实现盈亏使用策略虚拟持仓成本和当前行情。Greeks 列表示当前风险敞口，不是已实现收益。")
    except BrokerError as exc:
        st.error(f"归因行情读取失败：{exc}")

with experiment_tab:
    if st.button("登记当前代码与配置版本", disabled=not admin):
        with st.spinner("正在计算策略版本指纹……"):
            sync_strategy_experiments(store, settings)
        st.success("当前策略版本已登记。")
        st.rerun()
    experiment_frame = pd.DataFrame(experiments)
    if experiment_frame.empty:
        st.info("尚无实验版本。统一服务下次启动时会自动登记。")
    else:
        st.dataframe(
            chinese_columns(experiment_frame.drop(columns=["version_hash"], errors="ignore")),
            hide_index=True, width="stretch",
        )
        selected = st.selectbox(
            "选择要设为当前运行版本的实验", experiment_frame["id"].tolist(),
            format_func=lambda value: (
                f"#{value} · {experiment_frame[experiment_frame['id'] == value].iloc[0]['strategy_id']} · "
                f"{experiment_frame[experiment_frame['id'] == value].iloc[0]['code_version']}"
            ),
        )
        confirmed = st.checkbox("确认将此实验标记为当前运行版本")
        if st.button("切换当前版本标记", disabled=not admin or not confirmed):
            store.promote_experiment(int(selected))
            st.success("当前运行版本标记已更新。代码部署仍需通过文件发布流程完成。")
            st.rerun()

with protocol_tab:
    protocol = pd.DataFrame([plugin.as_record() for plugin in strategy_plugins()])
    st.dataframe(protocol, hide_index=True, width="stretch")
    st.caption(
        "所有策略现在都声明标的提供器、信号提供器、仓位模型、退出模型、风险档案、"
        "回测适配器和统一执行适配器。现有算法通过适配器接入，后续新策略必须满足同一协议。"
    )
