from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

from .process_utils import pid_alive, request_stop


ROOT = Path(__file__).resolve().parents[2]
PID_FILE = ROOT / "data" / "okx-crypto-service.pid"
LOG_FILE = ROOT / "data" / "okx-crypto-service.log"
SERVICE_CODE_PATHS = (
    ROOT / "scripts" / "run_crypto_strategy.py",
    ROOT / "src" / "futu_quant" / "crypto_radar.py",
    ROOT / "src" / "futu_quant" / "crypto_watchlist.py",
    ROOT / "src" / "futu_quant" / "crypto_execution.py",
    ROOT / "src" / "futu_quant" / "crypto_volatility.py",
    ROOT / "src" / "futu_quant" / "okx_broker.py",
)


@dataclass(frozen=True)
class CryptoServiceStatus:
    running: bool
    pid: int | None = None
    message: str = ""
    uptime_seconds: float = 0
    restart_required: bool = False


def code_restart_required(started_at: float, paths=SERVICE_CODE_PATHS) -> bool:
    return any(path.exists() and path.stat().st_mtime > started_at for path in paths)


def _pid_alive(pid: int) -> bool:
    # Cross-platform; never os.kill(pid, 0) on Windows (that terminates).
    return pid_alive(pid)


def status() -> CryptoServiceStatus:
    if not PID_FILE.exists():
        return CryptoServiceStatus(False, message="持续服务未启动")
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return CryptoServiceStatus(False, message="持续服务状态无效")
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return CryptoServiceStatus(False, message="持续服务未运行")
    started_at = PID_FILE.stat().st_mtime
    uptime = max(0, datetime.now(timezone.utc).timestamp() - started_at)
    stale = code_restart_required(started_at)
    message = "代码已更新，需要重启持续服务" if stale else "持续服务运行中"
    return CryptoServiceStatus(True, pid, message, uptime, stale)


def start(interval_seconds: int = 300) -> CryptoServiceStatus:
    current = status()
    if current.running:
        return current
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a")
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "run_crypto_strategy.py"),
            "--loop",
            "--interval",
            str(max(60, int(interval_seconds))),
        ],
        cwd=ROOT,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    PID_FILE.write_text(str(process.pid))
    time.sleep(.2)
    return status()


def stop() -> CryptoServiceStatus:
    current = status()
    if current.running and current.pid:
        request_stop(current.pid)  # force-kills on Windows (no signal delivery)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _pid_alive(current.pid):
            time.sleep(.1)
    PID_FILE.unlink(missing_ok=True)
    return CryptoServiceStatus(False, message="持续服务已停止")
