from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
PID_FILE = ROOT / "data" / "trading-service.pid"
LOG_FILE = ROOT / "data" / "service.log"
STOP_FILE = ROOT / "data" / "trading-service.stop"


@dataclass(frozen=True)
class ServiceStatus:
    running: bool
    pid: int | None = None
    message: str = ""
    uptime_seconds: float = 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live process owned by another user is still live. The PID file is only
        # created by this application, so status must remain a cheap local check.
        return True


def status() -> ServiceStatus:
    if not PID_FILE.exists():
        return ServiceStatus(False, message="服务未启动")
    try:
        pid = int(PID_FILE.read_text().strip())
        if not _pid_alive(pid):
            return ServiceStatus(False, message="服务未运行")
        uptime = max(0, datetime.now(timezone.utc).timestamp() - PID_FILE.stat().st_mtime)
        message = "服务正在停止；当前任务结束后会退出" if STOP_FILE.exists() else "服务运行中"
        return ServiceStatus(True, pid, message, uptime)
    except ValueError:
        return ServiceStatus(False, message="服务未运行")


def start() -> ServiceStatus:
    current = status()
    if current.running:
        try:
            from .radar_control import start as start_radar
            start_radar()
        except Exception:
            pass
        return current
    STOP_FILE.unlink(missing_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a")
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "run_service.py")], cwd=ROOT,
        stdout=log, stderr=log, start_new_session=True,
    )
    # run_service writes its PID only after acquiring the singleton lock. Writing it
    # here could overwrite the real owner's PID when a second start is rejected.
    for _ in range(20):
        time.sleep(0.1)
        current = status()
        if current.running or process.poll() is not None:
            break
    try:
        from .radar_control import start as start_radar
        start_radar()
    except Exception:
        pass
    if not current.running and process.poll() is None:
        return ServiceStatus(True, process.pid, "服务正在启动", 0)
    return current


def stop() -> ServiceStatus:
    current = status()
    if not current.running or current.pid is None:
        PID_FILE.unlink(missing_ok=True)
        try:
            from .radar_control import stop as stop_radar
            stop_radar()
        except Exception:
            pass
        return ServiceStatus(False, message="服务已经停止")
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()
    try:
        os.kill(current.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + float(os.getenv("SERVICE_STOP_TIMEOUT_SECONDS", "8"))
    while time.monotonic() < deadline and _pid_alive(current.pid):
        time.sleep(0.1)
    if _pid_alive(current.pid):
        return ServiceStatus(
            True, current.pid, "服务正在停止；当前任务结束后会退出", current.uptime_seconds
        )
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    try:
        from .radar_control import stop as stop_radar
        stop_radar()
    except Exception:
        pass
    return ServiceStatus(False, message="服务已停止")


def restart() -> ServiceStatus:
    stopped = stop()
    if stopped.running:
        return ServiceStatus(
            True, stopped.pid, "旧服务仍在停止中；为防止双实例，暂未启动新服务",
            stopped.uptime_seconds,
        )
    return start()
