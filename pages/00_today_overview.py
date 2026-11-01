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
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import streamlit as st

from futu_quant.config import Settings
from futu_quant.ui import render_sidebar, get_current_tenant
from futu_quant.ui_store import ui_store
from futu_quant.broker import get_tenant_broker

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

def resolve_trading_env_status(tenant: dict | None, settings: Settings) -> tuple[str, str, str]:
    """判定当前交易环境卡片的 (value, delta, delta_color)。

    安全哲学：
    - “安全态（模拟仿真）”必须由确凿证据证明，绝不能由默认值兜底产生。
    - 取不到租户信息或缺失关键字段时，严禁显示绿色模拟，一律提示中性/未知警示。
    """
    if not isinstance(tenant, dict) or "is_real" not in tenant:
        return "⚪ 未知环境", "租户配置缺失或未就绪", "off"

    is_real = tenant.get("is_real")
    if is_real is True:
        real_enabled = getattr(settings, "real_trading_enabled", False)
        if real_enabled:
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
env_val, env_delta, env_color = resolve_trading_env_status(current_tenant, settings)

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
