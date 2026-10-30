from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import signal
import sys
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
from futu_quant.migrations import PendingMigrationsError
from futu_quant.service_control import status as trading_service_status
from futu_quant.tenant import active_tenants
from futu_quant.volatility_scanner import fan_out_to_tenant, scan_market_layer


NY = ZoneInfo("America/New_York")
LOCK_FILE = Path("data/volatility-radar.lock")
PID_FILE = Path("data/volatility-radar.pid")
STOP_FILE = Path("data/volatility-radar.stop")
STOP_EVENT = threading.Event()

#: 租户 broker 缓存：按 tenant_id 复用（握手昂贵，不能每次扫描新建）。
_TENANT_BROKERS: dict[str, object] = {}


def _active_tenant_profiles() -> list:
    """启用中的租户（按 tenant_id 排序，保证扫描顺序稳定）。

    走 **共享入口** ``tenant.active_tenants()`` —— 自己再过滤一遍 DEFAULT_TENANTS
    就是第二份"哪些租户算启用"的实现，两处漂移时扇出会静默漏租户。
    """
    return active_tenants()


def _tenant_broker(profile):
    """取该租户的 broker —— 长生命周期缓存，按租户构造。"""
    broker = _TENANT_BROKERS.get(profile.tenant_id)
    if broker is None:
        broker = FutuPaperBroker(
            Settings.load(refresh=True),
            trading_env_override=profile.trading_env,
            account_id_override=profile.account_id,
        )
        _TENANT_BROKERS[profile.tenant_id] = broker
    return broker


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


def _scan_all_tenants(store, symbols: tuple[str, ...]) -> tuple[list, list]:
    """按租户扇出扫描 —— **昂贵行情层只跑一次**，账户层按租户各跑一次。

    分层来自 `docs/radar_multi_tenant_fanout_implementation_design.md`：
    `scan_market_layer()` 做 K 线/regime/快照这些与账户无关的昂贵工作，**只调一次**；
    `fan_out_to_tenant()` 只碰本租户的 positions/orders/campaign。

    **失败隔离**：单租户失败不拖垮整轮（记 `VOLATILITY_TENANT_SCAN_FAILED`，
    其余租户照常）；全部租户失败才抛错，让主循环计一次错误并退避。

    返回 ``(results, failed_tenant_ids)``。
    """
    results: list = []
    failed: list[str] = []
    profiles = _active_tenant_profiles()
    if not profiles:
        return results, failed

    # 昂贵层：**整轮只跑一次**（K 线 / regime / 快照）—— 用第一个启用租户的 broker
    # 取行情（行情与账户无关）。注意签名是 (broker, store, symbols, progress=...)：
    # store 用于暂停开关、信号状态键与决策留痕，progress 用于扫描期心跳。
    market = scan_market_layer(
        _tenant_broker(profiles[0]), store, symbols,
        progress=lambda i, total, sym: store.heartbeat(
            "volatility_scanner", "SCANNING",
            f"正在扫描 {i}/{total}：{sym}", os.getpid(),
        ),
    )
    if not getattr(market, "symbols", ()):  # 未配置标的 / 空扫描
        store.heartbeat(
            "volatility_scanner", "HEALTHY",
            "未配置波动率策略标的，跳过雷达扫描", os.getpid(),
        )
        return results, failed

    for profile in profiles:
        # 该租户独立退避：某一租户连续失败时**只跳过它**，其他租户照常扫描。
        # 没有这一层的话，一个挂掉的账户会让整轮扇出每 5 分钟失败一次。
        backoff_key = f"volatility_tenant_scan_backoff:{profile.tenant_id}"
        # 解析必须 **fail-safe**：这条比较在 per-tenant 的 try **之外**，
        # 一旦键里存了坏值（naive 时间、空串、非日期），异常会**终止整轮雷达**。
        # 所以坏值一律当作"不在退避中" —— 宁可多扫一次，也不能整轮崩。
        in_backoff = False
        try:
            backoff_until = datetime.fromisoformat(
                str(store.get_setting(backoff_key, "") or "")
            )
            if backoff_until.tzinfo is None:
                backoff_until = backoff_until.replace(tzinfo=NY)
            in_backoff = datetime.now(NY) < backoff_until
        except (TypeError, ValueError):
            in_backoff = False
        if in_backoff:
            continue
        try:
            outcome = fan_out_to_tenant(
                store, _tenant_broker(profile), profile, market, symbols,
            )
            results.extend(getattr(outcome, "rows", []) or [])
        except Exception as tenant_exc:  # noqa: BLE001
            failed.append(profile.tenant_id)
            store.audit(
                "VOLATILITY_TENANT_SCAN_FAILED",
                {"tenant_id": profile.tenant_id, "error": str(tenant_exc)[:200]},
                "volatility_long_gamma",
             actor="service:radar")
            store.heartbeat(
                "volatility_scanner", "ERROR",
                f"租户 {profile.tenant_id} 扫描失败：{str(tenant_exc)[:120]}",
                os.getpid(),
            )
            # 失败退避：15 分钟内不再扫该租户（退避时长可后续按连续失败次数放大）
            store.set_setting(
                backoff_key,
                (datetime.now(NY) + timedelta(minutes=15)).isoformat(),
            )
    if len(failed) == len(profiles):
        raise RuntimeError(f"全部租户扫描失败：{failed}")
    return results, failed


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
        try:
            store = TradingStore()
        except PendingMigrationsError as exc:
            # 待迁移库 + 雷达进程无迁移授权（授权只在 Worker 进程内，P0 2026-10-26）：
            # 明确报中文原因并退出，不静默重试、不刷堆栈 —— 先重启 Worker 完成迁移。
            print(f"波动率雷达无法启动（数据库待迁移）：{exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        PID_FILE.write_text(str(os.getpid()))
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
                        profiles = _active_tenant_profiles()
                        symbols = tuple(dict.fromkeys(
                            symbol
                            for profile in profiles
                            for symbol in load_volatility_symbols(profile.tenant_id)
                        ))
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
                        # #2 多租户扇出：昂贵行情层只跑一次，账户层按租户各跑一次；
                        # 单租户失败只跳过该租户（独立退避），其余租户照常。
                        results, tenant_failures = _scan_all_tenants(store, symbols)
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
                    # 不能只按固定间隔睡：900 秒会**跨越开盘** —— 2026-10-30
                    # 21:24:56 睡下、21:30 开盘，直到 21:39:56 才醒来扫描，
                    # 开盘后 10 分钟的高波动完全没扫到（用户当场发现「10 分钟没动」）。
                    # 等待时长按「距下次开盘」截断，开盘当刻即醒。
                    next_wait_seconds = max(
                        5.0,
                        calendar.seconds_until_regular_open(
                            now, cap=float(next_wait_seconds)
                        ),
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
