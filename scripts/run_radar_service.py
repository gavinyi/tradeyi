from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import signal
import threading
import time
from zoneinfo import ZoneInfo

os.environ.setdefault("FUTU_LOG_ROLE", "radar")

from futu_quant.alerts import AlertManager
from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings, load_volatility_symbols
from futu_quant.filelock import FileLockError, lock_exclusive
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.radar_schedule import (
    RadarScheduleState,
    adaptive_interval_seconds,
    error_backoff_seconds,
    save_radar_schedule_state,
)
from futu_quant.store import TradingStore
from futu_quant.service_control import status as trading_service_status
from futu_quant.volatility_scanner import scan_volatility_watchlist


NY = ZoneInfo("America/New_York")
LOCK_FILE = Path("data/volatility-radar.lock")
PID_FILE = Path("data/volatility-radar.pid")
STOP_FILE = Path("data/volatility-radar.stop")
STOP_EVENT = threading.Event()


def _request_stop(_signum=None, _frame=None) -> None:
    STOP_EVENT.set()
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()


def _wait_or_stop(seconds: float) -> None:
    """Wait up to *seconds*, waking early when a graceful stop is requested.

    POSIX wakes immediately when ``SIGTERM`` sets ``STOP_EVENT``.  Windows
    cannot deliver SIGTERM to the process, so the wait also polls the stop
    flag file that the control layer touches on stop/restart requests.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while not STOP_EVENT.is_set():
        if STOP_FILE.exists():
            STOP_EVENT.set()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        STOP_EVENT.wait(min(1.0, remaining))


def main() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    with LOCK_FILE.open("a+b") as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except FileLockError as exc:
            raise RuntimeError("波动率雷达已经在运行，拒绝启动第二个实例") from exc
        lock.seek(0)
        lock.write(f"{os.getpid()}\n".encode())
        lock.flush()
        lock.truncate()
        PID_FILE.write_text(str(os.getpid()))
        store = TradingStore()
        calendar = USMarketCalendar()
        consecutive_errors = 0
        try:
            while not STOP_EVENT.is_set():
                now = datetime.now(NY)
                next_wait_seconds = 900
                if calendar.session(now).regular_open(now):
                    symbols: tuple[str, ...] = ()
                    scan_started = time.monotonic()
                    try:
                        symbols = load_volatility_symbols()
                        if not symbols:
                            consecutive_errors = 0
                            state = RadarScheduleState.build(
                                symbol_count=0,
                                scan_duration_seconds=0,
                                interval_seconds=900,
                                consecutive_errors=0,
                            )
                            save_radar_schedule_state(store, state)
                            store.heartbeat(
                                "volatility_scanner", "HEALTHY",
                                "未配置波动率策略标的；15 分钟后重新检查",
                                os.getpid(),
                            )
                            _wait_or_stop(900)
                            continue
                        store.heartbeat(
                            "volatility_scanner", "SCANNING",
                            f"正在扫描 {len(symbols)} 个波动率策略标的", os.getpid(),
                        )
                        broker = FutuPaperBroker(Settings.load(refresh=True))
                        results = scan_volatility_watchlist(broker, store, symbols)
                        scan_duration = time.monotonic() - scan_started
                        consecutive_errors = 0
                        next_wait_seconds = max(300, adaptive_interval_seconds(
                            len(symbols), scan_duration
                        ))
                        # Do not duplicate the unified service's execution scan;
                        # the radar is a lower-frequency discovery process.
                        state = RadarScheduleState.build(
                            symbol_count=len(symbols),
                            scan_duration_seconds=scan_duration,
                            interval_seconds=next_wait_seconds,
                            consecutive_errors=0,
                        )
                        save_radar_schedule_state(store, state)
                        active_count = sum(1 for item in results if item.get("active"))
                        created_count = sum(
                            1 for item in results if item.get("campaign_created")
                        )
                        trading_service = trading_service_status()
                        execution_state = (
                            "扫描与交易执行链在线"
                            if trading_service.running and not trading_service.restart_pending
                            else "仅扫描，不交易：统一交易服务未就绪"
                        )
                        store.heartbeat(
                            "volatility_scanner", "HEALTHY",
                            f"已扫描 {len(symbols)} 个自选标的，触发 {active_count} 个，"
                            f"新增自动期权 {created_count} 个；"
                            f"耗时 {scan_duration:.1f} 秒，下次间隔 {next_wait_seconds} 秒；"
                            f"{execution_state}",
                            os.getpid(),
                        )
                    except Exception as exc:
                        scan_duration = time.monotonic() - scan_started
                        consecutive_errors += 1
                        next_wait_seconds = error_backoff_seconds(consecutive_errors)
                        state = RadarScheduleState.build(
                            symbol_count=len(symbols),
                            scan_duration_seconds=scan_duration,
                            interval_seconds=next_wait_seconds,
                            consecutive_errors=consecutive_errors,
                        )
                        save_radar_schedule_state(store, state)
                        message = (
                            f"{exc}；连续错误 {consecutive_errors} 次，"
                            f"{next_wait_seconds} 秒后重试"
                        )
                        store.heartbeat(
                            "volatility_scanner", "ERROR", message, os.getpid()
                        )
                        AlertManager(store).send(
                            "ERROR", "VOLATILITY_SCANNER_ERROR", message
                        )
                else:
                    store.heartbeat(
                        "volatility_scanner", "HEALTHY", "等待美股常规交易时段", os.getpid()
                    )
                _wait_or_stop(next_wait_seconds)
        finally:
            PID_FILE.unlink(missing_ok=True)
            STOP_FILE.unlink(missing_ok=True)
            try:
                from futu_quant.broker import close_all_connections
                close_all_connections()
            except Exception:
                pass


if __name__ == "__main__":
    main()
