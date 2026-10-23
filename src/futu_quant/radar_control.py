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
    # Cross-platform; never os.kill(pid, 0) on Windows (that terminates).
    # python_only=True：必须校验进程身份 —— 雷达被静默跳过的直接原因就是
    # 只判 PID 存活（PID 被 csrss.exe 复用）。
    return pid_alive(pid, python_only=True)


def _lock_owner_pid() -> int | None:
    """Recover a running radar whose pid file was removed or desynchronized."""
    try:
        from .store import TradingStore
        store = TradingStore()
        for h in store.heartbeats():
            if h.get("strategy_id") == "volatility_scanner":
                cand_pid = h.get("pid")
                if cand_pid and _pid_alive(int(cand_pid)):
                    return int(cand_pid)
    except Exception:
        pass

    if LOCK_FILE.exists():
        try:
            lines = LOCK_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line.isdigit() and _pid_alive(int(line)):
                    return int(line)
        except Exception:
            pass

    if sys.platform == "darwin" and LOCK_FILE.exists():
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
    env = os.environ.copy()
    env.setdefault("APPDATA", str(ROOT / "data" / "appdata"))
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if sys.platform == "win32" else 0
    radar_out = open(LOG_FILE, "a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_radar_service.py")],
        cwd=ROOT, stdout=radar_out, stderr=radar_out,
        env=env, creationflags=flags,
    )
    for _ in range(35):
        time.sleep(0.15)
        current = status()
        if current.running or process.poll() is not None:
            break
    return current if current.running else RadarStatus(True, process.pid, "雷达正在启动", 0)


def stop() -> RadarStatus:
    current = status()
    if current.running and current.pid:
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        # Windows cannot deliver SIGTERM; the radar polls STOP_FILE and exits
        # on its own, so graceful shutdown works the same on both platforms.
        request_stop(current.pid, force_on_windows=False)
        deadline = time.monotonic() + float(os.getenv("RADAR_STOP_TIMEOUT_SECONDS", "8"))
        while time.monotonic() < deadline and _pid_alive(current.pid):
            time.sleep(.1)
        if _pid_alive(current.pid):
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
