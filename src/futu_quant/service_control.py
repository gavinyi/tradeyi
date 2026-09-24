from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import time

from .process_utils import pid_alive, request_stop


ROOT = Path(__file__).resolve().parents[2]
PID_FILE = ROOT / "data" / "trading-service.pid"
LOG_FILE = ROOT / "data" / "service.log"
STOP_FILE = ROOT / "data" / "trading-service.stop"
RESTART_FILE = ROOT / "data" / "trading-service.restart"
LAUNCHD_LABEL = "com.futu.paperquant"


@dataclass(frozen=True)
class ServiceStatus:
    running: bool
    pid: int | None = None
    message: str = ""
    uptime_seconds: float = 0
    restart_pending: bool = False


def _pid_alive(pid: int) -> bool:
    # Cross-platform; never os.kill(pid, 0) on Windows (that terminates).
    return pid_alive(pid)


def _launchd_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def _launchd_registered() -> bool:
    result = subprocess.run(  # noqa: S603
        ["launchctl", "print", _launchd_target()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return result.returncode == 0


def status() -> ServiceStatus:
    if not PID_FILE.exists():
        return ServiceStatus(False, message="服务未启动")
    try:
        pid = int(PID_FILE.read_text().strip())
        if not _pid_alive(pid):
            return ServiceStatus(False, message="服务未运行")
        uptime = max(0, datetime.now(timezone.utc).timestamp() - PID_FILE.stat().st_mtime)
        if RESTART_FILE.exists():
            message = "服务正在重启；旧进程退出后会自动接棒"
        elif STOP_FILE.exists():
            message = "服务正在停止；当前任务结束后会退出"
        else:
            message = "服务运行中"
        return ServiceStatus(True, pid, message, uptime, RESTART_FILE.exists())
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
    RESTART_FILE.unlink(missing_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin" and _launchd_registered():
        subprocess.run(  # noqa: S603
            ["launchctl", "kickstart", _launchd_target()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        for _ in range(30):
            time.sleep(0.1)
            current = status()
            if current.running:
                return current
    # Close the parent descriptor immediately; otherwise Windows keeps the live
    # service log locked across UI/service control operations.
    log = LOG_FILE.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "run_service.py")], cwd=ROOT,
            stdout=log, stderr=log, start_new_session=True,
        )
    finally:
        log.close()
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


def stop(*, stop_radar: bool = True) -> ServiceStatus:
    current = status()
    if not current.running or current.pid is None:
        PID_FILE.unlink(missing_ok=True)
        if stop_radar:
            try:
                from .radar_control import stop as stop_radar_service
                stop_radar_service()
            except Exception:
                pass
        return ServiceStatus(False, message="服务已经停止")
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()
    # Windows cannot deliver SIGTERM; the service polls STOP_FILE and exits on
    # its own, which keeps graceful shutdown identical on both platforms.
    request_stop(current.pid, force_on_windows=False)
    deadline = time.monotonic() + float(os.getenv("SERVICE_STOP_TIMEOUT_SECONDS", "8"))
    while time.monotonic() < deadline and _pid_alive(current.pid):
        time.sleep(0.1)
    if _pid_alive(current.pid):
        return ServiceStatus(
            True, current.pid, "服务正在停止；当前任务结束后会退出", current.uptime_seconds
        )
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    if stop_radar:
        try:
            from .radar_control import stop as stop_radar_service
            stop_radar_service()
        except Exception:
            pass
    return ServiceStatus(False, message="服务已停止")


def restart() -> ServiceStatus:
    current = status()
    if RESTART_FILE.exists():
        return ServiceStatus(
            current.running, current.pid, "重启请求已排队，请勿重复点击",
            current.uptime_seconds, True,
        )
    if not current.running or current.pid is None:
        return start()
    RESTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESTART_FILE.write_text(str(current.pid))
    stopped = stop(stop_radar=False)
    if stopped.running:
        log = LOG_FILE.open("a", encoding="utf-8")
        try:
            subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "futu_quant.service_control", "handoff", str(current.pid)],
                cwd=ROOT, stdout=log, stderr=log, start_new_session=True,
            )
        finally:
            log.close()
        return ServiceStatus(
            True, stopped.pid, "旧服务正在停止；退出后会自动启动新服务",
            stopped.uptime_seconds, True,
        )
    RESTART_FILE.unlink(missing_ok=True)
    return start()


def _handoff(previous_pid: int, timeout_seconds: float = 180) -> ServiceStatus:
    """Wait for the previous singleton to exit, then start exactly one successor."""
    deadline = time.monotonic() + max(10, timeout_seconds)
    while time.monotonic() < deadline and _pid_alive(previous_pid):
        time.sleep(0.25)
    if _pid_alive(previous_pid):
        RESTART_FILE.unlink(missing_ok=True)
        return ServiceStatus(
            True, previous_pid, "旧服务超时未退出，已取消自动接棒以避免双实例",
        )
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    RESTART_FILE.unlink(missing_ok=True)
    return start()


def _main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "handoff":
        _handoff(int(sys.argv[2]))


if __name__ == "__main__":
    _main()
