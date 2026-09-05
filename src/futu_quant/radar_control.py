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
PID_FILE = ROOT / "data" / "volatility-radar.pid"
LOCK_FILE = ROOT / "data" / "volatility-radar.lock"
LOG_FILE = ROOT / "data" / "volatility-radar.log"
STOP_FILE = ROOT / "data" / "volatility-radar.stop"


@dataclass(frozen=True)
class RadarStatus:
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
        # Sandboxed dashboards may not be allowed to signal a healthy sibling process.
        return True


def _lock_owner_pid() -> int | None:
    """Recover a running radar whose pid file was removed during a long scan."""
    if not LOCK_FILE.exists():
        return None
    try:
        result = subprocess.run(
            ["lsof", "-t", str(LOCK_FILE)], capture_output=True, text=True,
            timeout=2, check=False,
        )
        for value in result.stdout.splitlines():
            pid = int(value.strip())
            if _pid_alive(pid):
                return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def status() -> RadarStatus:
    pid = None
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
    except ValueError:
        pid = None
    if pid is None or not _pid_alive(pid):
        pid = _lock_owner_pid()
        if pid is not None:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(pid))
    if pid is None:
        return RadarStatus(False, message="雷达未运行")
    try:
        uptime = datetime.now(timezone.utc).timestamp() - PID_FILE.stat().st_mtime
        message = "雷达正在停止；当前扫描结束后会退出" if STOP_FILE.exists() else "雷达运行中"
        return RadarStatus(True, pid, message, max(0, uptime))
    except (OSError, ValueError):
        return RadarStatus(False, message="雷达未运行")


def start() -> RadarStatus:
    current = status()
    if current.running:
        return current
    STOP_FILE.unlink(missing_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_radar_service.py")],
        cwd=ROOT, stdout=log, stderr=log, start_new_session=True,
    )
    time.sleep(.5)
    current = status()
    return current if current.running else RadarStatus(True, process.pid, "雷达正在启动", 0)


def stop() -> RadarStatus:
    current = status()
    if current.running and current.pid:
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        try:
            os.kill(current.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + float(os.getenv("RADAR_STOP_TIMEOUT_SECONDS", "8"))
        while time.monotonic() < deadline:
            try:
                os.kill(current.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        try:
            os.kill(current.pid, 0)
        except ProcessLookupError:
            pass
        else:
            return RadarStatus(
                True, current.pid, "雷达正在停止；当前扫描结束后会退出", current.uptime_seconds
            )
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    return RadarStatus(False, message="雷达已停止")


def restart() -> RadarStatus:
    stopped = stop()
    if stopped.running:
        return RadarStatus(
            True, stopped.pid,
            "雷达正在停止；当前扫描结束后会自动重启需要稍后重试",
            stopped.uptime_seconds,
        )
    return start()
