"""进程失联巡检（P1-13 Layer 1）：**PID 身份 + 心跳时效** 双校验。

为什么必须双校验：
- 只看「PID 存活」会被 Windows PID 复用骗到（我们自己的 `start.bat :pid_alive`
  就踩过：旧 PID 被 csrss 复用，服务被静默跳过）；
- 只看「心跳状态」会发现不了进程暴毙 —— 状态会永远停在最后一次写的 HEALTHY。

判据（任一成立即判失联）：
1. PID 文件缺失 / PID 对应进程不在 / **命令行与期望脚本不符**（说明 PID 被复用）；
2. 进程在，但心跳 `updated_at` 超过该组件的超时阈值。

本模块只放**纯判定逻辑**，不直接读 Windows 进程表、不直接发告警 —— 这样判据可以被
单测钉住；真正的 CIM 查询与告警发送在 `scripts/scheduled_log_audit.py` 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 维护窗口标记：`stop.bat` 杀进程前写、`start.bat` 启动前删。
MAINTENANCE_MARKER = Path("data/.maintenance")
# 标记的有效期：一次失败的停止可能留下标记，超时后必须恢复巡检（否则永久静默）。
MAINTENANCE_MAX_AGE = timedelta(hours=2)

# Worker 心跳超时 = 最长单任务软超时(300s) + 退避缓冲(120s)。
DEFAULT_WORKER_HEARTBEAT_TIMEOUT = 420
# 雷达：max(扫描间隔 × 2, 600)。
RADAR_HEARTBEAT_TIMEOUT_FLOOR = 600

EVENT_TERMINATED = "PROCESS_TERMINATED"
EVENT_HEARTBEAT_TIMEOUT = "PROCESS_HEARTBEAT_TIMEOUT"


@dataclass(frozen=True)
class Target:
    """一个被巡检的进程。"""

    name: str
    tenant_id: str
    pid_file: str
    cmd_markers: tuple[str, ...]
    heartbeat_component: str


TARGETS: tuple[Target, ...] = (
    Target(
        name="实盘 Worker",
        tenant_id="futu_real_master",
        pid_file="data/trading-service-futu_real_master.pid",
        cmd_markers=("run_service.py", "--tenant futu_real_master"),
        heartbeat_component="trading_service:futu_real_master",
    ),
    Target(
        name="模拟 Worker",
        tenant_id="futu_paper_sandbox",
        pid_file="data/trading-service-futu_paper_sandbox.pid",
        cmd_markers=("run_service.py", "--tenant futu_paper_sandbox"),
        heartbeat_component="trading_service:futu_paper_sandbox",
    ),
    Target(
        name="波动率雷达",
        tenant_id="",
        pid_file="data/volatility-radar.pid",
        cmd_markers=("run_radar_service.py",),
        heartbeat_component="volatility_scanner",
    ),
)


def maintenance_active(
    marker_path: Path | str = MAINTENANCE_MARKER,
    now: datetime | None = None,
    max_age: timedelta = MAINTENANCE_MAX_AGE,
) -> bool:
    """是否处于人工维护窗口（`stop.bat`/`start.bat` 之间）。

    只看「文件在且 mtime 在有效期内」：一次失败的停止可能留下标记，
    过期后必须恢复巡检 —— 否则守护会永久静默，比不巡检更糟。
    """
    path = Path(marker_path)
    if not path.exists():
        return False
    now = now or datetime.now(timezone.utc)
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return now - modified < max_age


def heartbeat_timeout_seconds(component: str, interval_seconds: float | None = None) -> int:
    """各组件的超时阈值（可配置来源在调用方读 `runtime_config`）。"""
    if component == "volatility_scanner":
        interval = float(interval_seconds or 300.0)
        return int(max(interval * 2, RADAR_HEARTBEAT_TIMEOUT_FLOOR))
    return int(DEFAULT_WORKER_HEARTBEAT_TIMEOUT)


def cmdline_matches(cmdline: str | None, markers: tuple[str, ...]) -> bool:
    """命令行是否属于我们期望的那个脚本（防 PID 复用）。"""
    if not cmdline:
        return False
    text = str(cmdline).replace("\\", "/")
    return all(marker.replace("\\", "/") in text for marker in markers)


def evaluate_heartbeat(
    *,
    target: Target,
    pid_present: bool,
    pid_alive: bool,
    cmdline: str | None,
    heartbeat_updated_at: datetime | None,
    now: datetime | None = None,
    timeout_override: int | None = None,
) -> tuple[str, dict] | None:
    """返回 `(事件名, payload)`；健康则返回 `None`。

    事件级别由调用方固定为 CRITICAL（这是不可静音清单里的事件）。
    """
    now = now or datetime.now(timezone.utc)
    base = {
        "target": target.name,
        "tenant_id": target.tenant_id,
        "pid_file": target.pid_file,
    }

    # ① 进程级：文件缺失 / 进程不在 / 命令行不符（PID 复用）
    if not pid_present:
        return EVENT_TERMINATED, {**base, "reason": "PID 文件不存在"}
    if not pid_alive:
        return EVENT_TERMINATED, {**base, "reason": "PID 对应进程已退出"}
    if not cmdline_matches(cmdline, target.cmd_markers):
        return EVENT_TERMINATED, {
            **base,
            "reason": "PID 已被其他进程复用（命令行与期望脚本不符）",
            "expected": list(target.cmd_markers),
            "actual": (cmdline or "")[:200],
        }

    # ② 心跳级：进程在但心跳过期（僵死/卡死）
    timeout = int(
        timeout_override or heartbeat_timeout_seconds(target.heartbeat_component)
    )
    if heartbeat_updated_at is None:
        return EVENT_HEARTBEAT_TIMEOUT, {
            **base,
            "reason": "进程存活但没有任何心跳记录",
            "timeout_seconds": timeout,
        }
    age = (now - heartbeat_updated_at).total_seconds()
    if age > timeout:
        return EVENT_HEARTBEAT_TIMEOUT, {
            **base,
            "reason": f"心跳停滞 {age:.0f}s，超过阈值 {timeout}s",
            "age_seconds": round(age, 1),
            "timeout_seconds": timeout,
        }
    return None
