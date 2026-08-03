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
PID_FILE = ROOT / "data" / "okx-crypto-swap-service.pid"
LOG_FILE = ROOT / "data" / "okx-crypto-swap-service.log"
CODE_PATHS = (
    ROOT / "scripts" / "run_crypto_swap_strategy.py",
    ROOT / "src" / "futu_quant" / "crypto_swap.py",
    ROOT / "src" / "futu_quant" / "crypto_swap_managed_futures.py",
    ROOT / "src" / "futu_quant" / "crypto_swap_execution.py",
    ROOT / "src" / "futu_quant" / "crypto_swap_risk.py",
    ROOT / "src" / "futu_quant" / "crypto_swap_radar.py",
    ROOT / "src" / "futu_quant" / "crypto_swap_watchlist.py",
    ROOT / "src" / "futu_quant" / "okx_broker.py",
)


@dataclass(frozen=True)
class CryptoSwapServiceStatus:
    running: bool
    pid: int | None = None
    message: str = ""
    uptime_seconds: float = 0
    restart_required: bool = False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def status() -> CryptoSwapServiceStatus:
    if not PID_FILE.exists():
        return CryptoSwapServiceStatus(False, message="合约服务未启动")
    try:
        pid = int(PID_FILE.read_text())
    except (OSError, ValueError):
        return CryptoSwapServiceStatus(False, message="合约服务状态无效")
    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return CryptoSwapServiceStatus(False, message="合约服务未运行")
    started = PID_FILE.stat().st_mtime
    stale = any(path.exists() and path.stat().st_mtime > started for path in CODE_PATHS)
    return CryptoSwapServiceStatus(
        True, pid,
        "代码已更新，需要重启合约服务" if stale else "合约服务运行中",
        max(0, datetime.now(timezone.utc).timestamp() - started),
        stale,
    )


def start(interval_seconds: int = 300) -> CryptoSwapServiceStatus:
    current = status()
    if current.running:
        return current
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a")
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable, str(ROOT / "scripts" / "run_crypto_swap_strategy.py"),
            "--loop", "--interval", str(max(60, int(interval_seconds))),
        ],
        cwd=ROOT, stdout=log, stderr=log, start_new_session=True,
    )
    PID_FILE.write_text(str(process.pid))
    time.sleep(.2)
    return status()


def stop() -> CryptoSwapServiceStatus:
    current = status()
    if current.running and current.pid:
        try:
            os.kill(current.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(current.pid):
            time.sleep(.1)
    PID_FILE.unlink(missing_ok=True)
    return CryptoSwapServiceStatus(False, message="合约服务已停止")
