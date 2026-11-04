"""配置中心业务入口 1：今日总览（只读大屏聚合）。

回答核心问题：现在能不能交易？有没有要处理的事？
硬约束：
1. 3秒内明确回答「是否允许交易」「有无待处理风险」；
2. 纯聚合展示（双租户健康、待审批、告警、待重启变更），绝对零可写配置项；
3. 杜绝硬编码“0 项”与静默 hasattr 兜底：缺失即明确提示未统计/未就绪；
4. 待重启变更清晰区分两行：
   - A 行：代码/元数据变更（需 stop/start 重启）：基于进程启动基线比对净差异文件数；
   - B 行：动态配置已存库（无需重启）：提示 Worker 轮询自动生效。
"""

from __future__ import annotations

import os
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import streamlit as st

logger = logging.getLogger(__name__)

from futu_quant.config import Settings
from futu_quant.ui import (
    render_sidebar,
    get_current_tenant,
    get_tenant_broker,
    open_trading_store,
)
from futu_quant.real_trading import resolve_real_account_limits
from futu_quant.limit_profile import resolve_limit_chain
from futu_quant.limit_profile_store import load_limit_profile

st.set_page_config(page_title="今日总览 - 统一配置中心", page_icon="🧭", layout="wide")

settings = Settings.load()
render_sidebar(settings)

st.title("🧭 今日总览 (交易与风险状态)")

# 提示说明
st.caption(
    "修改『归属区域、显示名称』等配置元数据后，需在桌面执行 stop.bat → start.bat 才会生效。"
    "按 F5 刷新无效：元数据在进程启动时一次性载入内存。"
)

store = open_trading_store()
broker = get_tenant_broker(settings)

def render_limit_status_bar(store, settings):
    tenant = get_current_tenant(settings)
    tenant_id = str(tenant.get("tenant_id") or tenant.get("id") or st.session_state.get("current_tenant_id") or "")
    env = "REAL_" + str(tenant.get("acc_id")) if tenant.get("is_real") and tenant.get("acc_id") else "SIMULATE"
    rows = store.equity_history(limit=1, env=env)
    equity = float(rows[0].get("total_assets") or 0) if rows else 0.0
    if not tenant_id or equity <= 0:
        st.warning("限额状态：权益缺失或非正，派生限额未就绪（fail-closed）")
        return
    chain = resolve_limit_chain(store, tenant_id, equity, load_limit_profile(store, tenant_id).profile)
    actual = min(chain.per_order, chain.capacity_line) if chain.capacity_line is not None else chain.per_order
    layer = "容量线" if chain.capacity_line is not None and chain.capacity_line <= chain.per_order else "派生层"
    st.info(f"限额状态 · 权益 ${equity:,.2f} · 单笔 ${actual:,.2f} · 单日 ${chain.daily_entries:,.2f} · 日亏 ${chain.daily_loss:,.2f} · 实际生效层：{layer}（容量线 ${chain.capacity_line:,.2f}）")

render_limit_status_bar(store, settings)

def resolve_trading_env_status(
    tenant: dict | None,
    settings: Settings,
    store: Any = None,
) -> tuple[str, str, str]:
    """判定当前交易环境卡片的 (value, delta, delta_color)。

    单真值原则（Reviewer & Product & Ops 铁律）：
    - 允许实盘交易的唯一来源必须是门禁同源函数 `resolve_real_account_limits(store, settings, account_id).enabled`；
    - 严禁页面层自行组合 `is_real` + `settings.real_trading_enabled` 产生第二套真值；
    - 门禁解析异常或返回空时，直接报告「⚪ 未知环境 / 门禁解析失败」并 WARN 留痕，严禁回退 settings 兜底；
    - account_id 为空直接报「⚪ 未知环境 / 缺少账户ID」，不跨层猜测 settings.real_account_id；
    - 显式暴露高风险态：`require_manual_confirm=False` 时直接在 delta 标明「实盘·无二次确认」；
    - 未知状态严禁兜底为模拟绿标，统一中性/未知警示。
    """
    if not isinstance(tenant, dict) or "is_real" not in tenant:
        return "⚪ 未知环境", "租户配置缺失或未就绪", "off"

    is_real = tenant.get("is_real")
    if is_real is True:
        account_id = tenant.get("acc_id")
        if not account_id:
            logger.warning("交易环境判定失败: 实盘租户缺少 acc_id，拒绝猜测 settings.real_account_id")
            return "⚪ 未知环境", "实盘租户缺少账户ID", "off"

        if store is None:
            logger.warning("交易环境判定失败: 未注入 store，无法执行同源门禁解析")
            return "⚪ 未知环境", "存储未就绪，门禁未解析", "off"

        try:
            real_limits = resolve_real_account_limits(store, settings, account_id)
        except Exception as e:
            logger.warning(f"交易环境门禁解析异常 (acc_id={account_id}): {e}", exc_info=True)
            return "⚪ 未知环境", f"门禁解析失败: {e}", "off"

        if real_limits is None:
            logger.warning(f"交易环境门禁返回空 (acc_id={account_id})")
            return "⚪ 未知环境", "门禁未返回有效限额对象", "off"

        # 坏键（2026-11-01 拍板）：账户级配置损坏时门禁只拦 BUY，
        # 界面不能笼统显示「总闸已关闭」—— 那会把「配置坏了」伪装成「主动关闭」。
        if getattr(real_limits, "config_corrupt", False):
            logger.warning(
                f"实盘账户 {account_id} 账户级配置损坏：{real_limits.corrupt_reason}"
            )
            return "🔴 实盘 (REAL)", "账户配置损坏·开仓已禁 (SELL 仍可执行)", "inverse"

        real_allowed = real_limits.enabled
        requires_confirm = real_limits.require_manual_confirm

        if real_allowed:
            if not requires_confirm:
                return "🔴 实盘 (REAL)", "实盘·无二次确认 (高风险)", "inverse"
            return "🔴 实盘 (REAL)", "允许真金交易", "inverse"
        return "🔴 实盘 (REAL)", "实盘总闸已关闭", "off"

    if is_real is False:
        return "🟢 模拟 (SIMULATE)", "模拟仿真环境", "normal"

    return "⚪ 未知环境", "无法判定环境类型", "off"


def resolve_pending_restart_status(
    pid_path: Path,
    repo_root: Path,
) -> tuple[str, str, str]:
    """判定代码与元数据待重启生效状态的 (value, delta, delta_color)。

    口径（Ops 推荐的文件级净差异算法）：
    - 5 个分支：无 pid / git 异常 / diff=0 / diff=N / 异常；
    - value 与 delta 同源计算返回，彻底杜绝各自独立判定导致的假正常。
    """
    if not pid_path.exists():
        return "服务未启动", "需桌面 start.bat 启动", "off"

    try:
        pid_mtime = datetime.fromtimestamp(pid_path.stat().st_mtime, tz=timezone.utc)
        res_base = subprocess.run(
            ["git", "log", "-1", f"--before={pid_mtime.isoformat()}", "--format=%h", "master"],
            cwd=str(repo_root), capture_output=True, text=True, check=False,
        )
        base_commit = res_base.stdout.strip()
        if not base_commit:
            return "未统计", "未能获取进程启动基线 commit", "off"

        res_diff = subprocess.run(
            ["git", "diff", "--name-only", f"{base_commit}..HEAD", "--", "src/", "pages/", "app.py"],
            cwd=str(repo_root), capture_output=True, text=True, check=False,
        )
        if res_diff.returncode != 0:
            return "未统计", "git diff 执行失败", "off"

        diff_files = [f for f in res_diff.stdout.strip().splitlines() if f]
        diff_count = len(diff_files)
        if diff_count > 0:
            return f"{diff_count} 个文件待重启", "需桌面 stop.bat → start.bat", "inverse"
        return "无，无需重启", "运行中代码与磁盘一致", "normal"
    except Exception as e:
        return "未统计", f"计算异常: {e}", "off"


# 1. 核心状态卡片 (3秒内回答能不能交易)
col1, col2, col3, col4 = st.columns(4)

current_tenant = get_current_tenant(settings)
env_val, env_delta, env_color = resolve_trading_env_status(current_tenant, settings, store)

with col1:
    st.metric(
        label="当前交易环境",
        value=env_val,
        delta=env_delta,
        delta_color=env_color,
    )

with col2:
    if not hasattr(store, "incidents"):
        st.metric(label="未解决事故 (Incidents)", value="未统计", delta="存储未就绪/无此接口", delta_color="inverse")
    else:
        incidents = store.incidents("OPEN")
        st.metric(
            label="未解决事故 (Incidents)",
            value=f"{len(incidents)} 项",
            delta="存在未恢复风险" if incidents else "系统健康",
            delta_color="inverse" if incidents else "normal",
        )

with col3:
    if not hasattr(store, "pending_approvals"):
        st.metric(label="待处理审批 (Approvals)", value="未统计", delta="存储未就绪/无此接口", delta_color="inverse")
    else:
        pending_approvals = store.pending_approvals()
        st.metric(
            label="待处理审批 (Approvals)",
            value=f"{len(pending_approvals)} 项",
            delta="需要人工审核" if pending_approvals else "无待办",
            delta_color="inverse" if pending_approvals else "normal",
        )

with col4:
    # 待重启变更：采用绝对路径与同源判定函数，杜绝相对路径失效与硬编码假正常
    repo_root = Path(__file__).resolve().parents[1]
    pid_file = repo_root / "data" / "trading-service.pid"
    status_label, restart_delta, restart_color = resolve_pending_restart_status(pid_file, repo_root)

    st.metric(
        label="代码与元数据生效状态",
        value=status_label,
        delta=restart_delta,
        delta_color=restart_color,
    )

st.info("💡 **动态配置更新机制**：数据库内日限额、静音清单、策略启停等业务参数已存库，由 Worker 轮询秒级自动重读（≤2分钟），无需停服重启。")

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
