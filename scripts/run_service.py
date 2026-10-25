from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import contextvars
import json
import os
from pathlib import Path
from typing import Any

import platform
import shlex
import signal
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

os.environ.setdefault("FUTU_LOG_ROLE", "service")
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
if sys.platform == "win32":
    _local_appdata = Path(__file__).resolve().parents[1] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.alerts import ALERT_PROBE_INTERVAL_DEFAULT, AlertManager
from futu_quant.ml_freshness import check_ml_model_health
from futu_quant.approval import publish_approval_runtime_manifest
from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.volatility_orphans import volatility_write_state_key
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.health import maintenance
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.tenant import tenant_config_key_candidates
from futu_quant.shadow_matching import match_shadow_orders
from futu_quant.earnings_calendar import refresh_earnings_events, sync_campaign_events
from futu_quant.order_push import open_order_push
from futu_quant.recovery import daily_operations_report, startup_recovery
from futu_quant.correlation_risk import update_correlation_clusters
from futu_quant.core_validation import load_core_daily_validation
from futu_quant.config import load_gamma_campaigns, load_volatility_campaigns, upsert_campaign
from futu_quant.campaign_cleanup import cleanup_expired_campaigns
from futu_quant.connectivity_protection import active_protected_option_positions
from futu_quant.account_metrics import account_pnl_metrics
from futu_quant.risk import RiskLimits, ny_trading_day_start_utc
from futu_quant.option_lifecycle import execute_disposition_plans, monitor_option_lifecycle
from futu_quant.option_history import collect_option_snapshots
from futu_quant.portfolio_allocator import ensure_default_budgets
from futu_quant.stress import monitor_portfolio_stress
from futu_quant.stress_alerts import (
    LEGACY_INCIDENT_KEY as PORTFOLIO_STRESS_LEGACY_KEY,
    STATE_ALERTING,
    STATE_OK,
    legacy_incident_closed,
    mark_legacy_incident_closed,
    next_stress_action,
    portfolio_stress_incident_key,
    read_hysteresis_state,
    resolve_portfolio_stress_thresholds,
    write_hysteresis_state,
)
from futu_quant.strategy_plugins import BY_SERVICE_NAME, strategy_plugins
from futu_quant.experiments import sync_strategy_experiments
from futu_quant.combo_execution import ComboExecutionManager
from futu_quant.volatility_structures import automatic_combo_id
from futu_quant.validation_loop import (
    collect_combo_market_snapshots, run_validation_cycle, validation_report,
)
from futu_quant.parquet_store import archive_market_data
from futu_quant.disaster_recovery import create_recovery_point, latest_recovery_drill
from futu_quant.storage_monitor import storage_report
from futu_quant.operational_readiness import (
    audit_streamlit_pages, automatic_recovery_cycle, dynamic_allocation_plan,
    idempotency_audit, observability_snapshot, operations_report, paper_e2e_check,
)
from futu_quant.long_term_validation import long_term_validation_cycle
from futu_quant.market_data import FutuMarketDataProvider
from futu_quant.research_backfill import backfill_research_history
from run_gamma_strategy import run_once as run_gamma_once
from run_volatility_strategy import run_once as run_volatility_once
from run_strategies import run_once as run_strategy_once
from ml_tiebreak_audit import run_ml_tiebreak_audit
from futu_quant.filelock import FileLockError, lock_exclusive
from futu_quant.scheduled_tasks import (
    DAILY_STRATEGY_REVIEW_TASK, SHUTDOWN_TASK, DAILY_UNIVERSE_RANKING_TASK,
    WEEKLY_UNIVERSE_RECALL_TASK,
    run_daily_strategy_review, shutdown_computer, run_daily_universe_ranking,
    run_weekly_universe_recall,
    timer_default_enabled, timer_enabled_setting_key, _timer_due,
)


NY = ZoneInfo("America/New_York")
LOCK_FILE = Path("data/trading-service.lock")
PID_FILE = Path("data/trading-service.pid")
STOP_FILE = Path("data/trading-service.stop")
RESTART_FILE = Path("data/trading-service.restart")
LOG_FILE = Path("data/service.log")


def service_files(tenant_id: str | None = None) -> dict[str, Path]:
    """根据租户生成独立的单例锁、PID 与状态文件路径"""
    prefix = f"-{tenant_id}" if tenant_id else ""
    return {
        "lock": Path(f"data/trading-service{prefix}.lock"),
        "pid": Path(f"data/trading-service{prefix}.pid"),
        "stop": Path(f"data/trading-service{prefix}.stop"),
        "restart": Path(f"data/trading-service{prefix}.restart"),
        "log": Path(f"data/service{prefix}.log"),
    }


def resolve_configured_strategies(
    store, tenant_id: str | None, configured_env: set[str],
) -> set[str]:
    """解析本租户实际要调度的策略集合（显示名）。

    租户启用键有两种历史写法：显示名（``Core Satellite``）与小写策略 ID
    （``core_satellite``）。对账的 disable/recover 路径写的是**策略 ID** 形式，
    若这里只按 service_name 查就会查空、静默回落到全局键（全局
    ``strategy_enabled:Core Satellite`` 为 false）—— 租户的启用声明被完全忽略，
    实盘 core_satellite 永远不会被调度。两种形式都要认。
    """
    plugins = {
        plugin.service_name: plugin.strategy_id
        for plugin in strategy_plugins(scheduled_only=True)
    }
    configured: set[str] = set()
    for name, strategy_id in plugins.items():
        tenant_value = ""
        if tenant_id:
            for key in (
                f"strategy_enabled:{tenant_id}:{name}",
                f"strategy_enabled:{tenant_id}:{strategy_id}",
            ):
                candidate = store.get_setting(key, "")
                if candidate:
                    tenant_value = candidate
                    break
        if tenant_id and tenant_value:
            enabled = tenant_value == "true"
        else:
            enabled = store.get_setting(
                f"strategy_enabled:{name}", "true" if name in configured_env else "false"
            ) == "true"
        if enabled:
            configured.add(name)
    return configured


MARKET_CALENDAR = USMarketCalendar()
STOP_EVENT = threading.Event()

TASK_TIMEOUTS = {
    "shadow_matching": 30,
    "order_reconciliation": 60,
    "option_combo_monitor": 60,
    "combo_market_snapshot": 90,
    "validation_calibration": 180,
    "validation_report": 60,
    "parquet_archive": 180,
    "recovery_point": 180,
    "disaster_recovery_drill": 180,
    "storage_monitor": 30,
    "page_compatibility": 60,
    "paper_e2e": 180,
    "observability": 30,
    "alert_delivery_probe": 60,
    "ml_model_health": 60,
    "ml_auto_retrain": 60,
    "ml_tiebreak_audit": 120,
    "candidate_exclusions_prune": 60,
    "reconciliation_prune": 60,
    "automatic_recovery": 30,
    "idempotency_audit": 30,
    "operations_report": 60,
    "dynamic_allocation": 30,
    "long_term_validation": 60,
    "option_lifecycle": 45,
    "option_disposition": 60,
    "option_history": 45,
    "portfolio_stress": 90,
    "ledger_reconciliation": 60,
    "earnings_gamma": 120,
    "volatility_long_gamma": 120,
    "core_satellite": 120,
    "campaign_cleanup": 45,
    "system_health": 90,
    "earnings_calendar": 180,
    "daily_report": 60,
    "daily_strategy_review": 60,
    "computer_shutdown": 30,
    "correlation_risk": 120,
    "research_data_backfill": 300,
}

BROKER_CRITICAL_TASKS = {
    "trading_service",
    "order_reconciliation",
    "option_combo_monitor",
    "option_lifecycle",
    "option_disposition",
    "volatility_long_gamma",
}


def futu_sdk_preflight(
    store: TradingStore, alerts: AlertManager, settings: Settings,
    *, force: bool = False, heartbeat_name: str = "trading_service",
) -> bool:
    now = datetime.now(NY)
    cache_seconds = max(5, int(os.getenv("FUTU_SDK_PREFLIGHT_CACHE_SECONDS", "30")))
    if not force and _seconds_since(
        store.get_setting("opend_last_preflight_success_at", ""), now,
    ) < cache_seconds:
        return True
    timeout = max(3.0, float(os.getenv("FUTU_SDK_PREFLIGHT_TIMEOUT_SECONDS", "12")))
    env = os.environ.copy()
    if sys.platform == "win32":
        env.setdefault("APPDATA", str(Path(__file__).resolve().parents[1] / "data" / "appdata"))
    try:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "scripts/check_connection.py"],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        message = f"富途 OpenD 预检超时超过 {timeout:.0f} 秒，网关通信受阻"
        # P1-21：心跳名必须与成功路径一致（带租户），否则全局 trading_service 行
        # 永远停在 DEGRADED —— 恢复后也不会变回 HEALTHY 的假故障。
        store.heartbeat(heartbeat_name, "DEGRADED", message, os.getpid())
        record_broker_failure(store, alerts, "trading_service", message)
        alerts.send("CRITICAL", "OPEND_DISCONNECTED", message, incident_key="OPEND_DISCONNECTED")
        return False
    if result.returncode != 0:
        message = f"富途 OpenD 网关掉线或未启动 (预检退出码 {result.returncode})，系统已自动暂停新开仓并保持持仓保护状态"
        store.heartbeat(heartbeat_name, "DEGRADED", message, os.getpid())
        record_broker_failure(store, alerts, "trading_service", message)
        alerts.send("CRITICAL", "OPEND_DISCONNECTED", message, incident_key="OPEND_DISCONNECTED")
        return False
    store.set_setting("opend_last_preflight_success_at", now.isoformat())
    store.set_setting("opend_consecutive_failures", "0")
    alerts.recover("OPEND_DISCONNECTED", "富途 OpenD 网关连接已恢复正常，交易与行情调度链路恢复就绪", incident_key="OPEND_DISCONNECTED")
    if store.get_setting("strategy_interrupted", "false").lower() == "true":
        store.set_setting("strategy_interrupted", "false")
        store.set_setting("strategy_recovered_at", now.isoformat())
        store.operational_event(
            "protective_mode", "PROTECTIVE_RECOVERY_CONFIRMED", "HEALTHY",
            {"message": "OpenD SDK 预检恢复，持仓保护恢复链路结束"},
        )
        alerts.recover(
            "STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",
            "OpenD/券商连接已恢复，持仓保护模式解除",
            incident_key="STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",
        )
    return True


def opend_failure_message(message: str) -> bool:
    text = str(message).lower()
    return any(
        marker in text
        for marker in (
            "opend",
            "connect timeout",
            "connect fail",
            "etimedout",
            "snapshots failed",
            "history for",
            "get accounts",
        )
    )


def _seconds_since(raw: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(raw)).total_seconds() if raw else 10**9
    except ValueError:
        return 10**9


def request_service_self_restart(
    store: TradingStore, alerts: AlertManager, reason: str,
) -> None:
    if RESTART_FILE.exists() or STOP_EVENT.is_set():
        return
    RESTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESTART_FILE.write_text(str(os.getpid()))
    STOP_FILE.touch()
    STOP_EVENT.set()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if sys.platform == "win32":
        env.setdefault("APPDATA", str(Path(__file__).resolve().parents[1] / "data" / "appdata"))
    subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "futu_quant.service_control", "handoff", str(os.getpid())],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    store.operational_event(
        "trading_service", "SELF_RESTART_REQUESTED", "DEGRADED",
        {"pid": os.getpid(), "reason": reason},
    )
    alerts.send(
        "ERROR", "TRADING_SERVICE_SELF_RESTART",
        f"OpenD/SDK 连续异常，统一交易服务已请求自重启：{reason}",
        incident_key="TRADING_SERVICE_SELF_RESTART",
    )


def maybe_restart_opend(
    store: TradingStore, alerts: AlertManager, reason: str, now: datetime,
) -> bool:
    store_enabled = store.get_setting("futu_opend_auto_restart", "false").strip().lower() == "true"
    env_allowed = os.getenv("FUTU_OPEND_AUTO_RESTART", "false").strip().lower() == "true"
    enabled = store_enabled and env_allowed
    if not enabled:
        return False
    min_interval = max(60, int(os.getenv("FUTU_OPEND_RESTART_MIN_INTERVAL_SECONDS", "300")))
    if _seconds_since(store.get_setting("opend_last_restart_at", ""), now) < min_interval:
        return False
    command = os.getenv("FUTU_OPEND_RESTART_COMMAND", "").strip()
    try:
        if command:
            subprocess.run(  # noqa: S603
                shlex.split(command), cwd=Path(__file__).resolve().parents[1],
                timeout=float(os.getenv("FUTU_OPEND_RESTART_TIMEOUT_SECONDS", "20")),
                check=False,
            )
        elif platform.system() == "Darwin":
            app_name = os.getenv("FUTU_OPEND_APP_NAME", "Futu OpenD")
            subprocess.run(  # noqa: S603
                ["osascript", "-e", f'tell application "{app_name}" to quit'],
                timeout=10, check=False,
            )
            time.sleep(float(os.getenv("FUTU_OPEND_RESTART_WAIT_SECONDS", "2")))
            subprocess.run(["open", "-a", app_name], timeout=10, check=False)  # noqa: S603
        else:
            return False
        store.set_setting("opend_last_restart_at", now.isoformat())
        store.operational_event(
            "opend", "AUTO_RESTART_REQUESTED", "DEGRADED",
            {"reason": reason, "command": command or "darwin_app_restart"},
        )
        alerts.send(
            "ERROR", "OPEND_AUTO_RESTART",
            f"OpenD 连续连接异常，已尝试自动重启 OpenD：{reason}",
            incident_key="OPEND_AUTO_RESTART",
        )
        return True
    except Exception as exc:
        store.operational_event(
            "opend", "AUTO_RESTART_FAILED", "ERROR",
            {"reason": reason, "error": str(exc)},
        )
        alerts.send(
            "ERROR", "OPEND_AUTO_RESTART_FAILED",
            f"OpenD 自动重启失败：{exc}",
            incident_key="OPEND_AUTO_RESTART_FAILED",
        )
        return False


def request_protective_radar_restart(
    store: TradingStore, alerts: AlertManager, reason: str, now: datetime,
) -> bool:
    min_interval = max(30, int(os.getenv("PROTECTIVE_RADAR_RESTART_MIN_INTERVAL_SECONDS", "120")))
    if _seconds_since(store.get_setting("protective_radar_last_restart_at", ""), now) < min_interval:
        return False
    try:
        from futu_quant.radar_control import restart as restart_radar
        result = restart_radar()
        store.set_setting("protective_radar_last_restart_at", now.isoformat())
        store.operational_event(
            "volatility_scanner", "PROTECTIVE_RADAR_RESTART_REQUESTED", "DEGRADED",
            {
                "reason": reason,
                "running": result.running,
                "pid": result.pid,
                "message": result.message,
            },
        )
        alerts.send(
            "ERROR", "PROTECTIVE_RADAR_RESTART",
            f"持仓保护模式已尝试重启独立雷达：{reason}",
            incident_key="PROTECTIVE_RADAR_RESTART",
        )
        return True
    except Exception as exc:
        store.operational_event(
            "volatility_scanner", "PROTECTIVE_RADAR_RESTART_FAILED", "ERROR",
            {"reason": reason, "error": str(exc)},
        )
        alerts.send(
            "ERROR", "PROTECTIVE_RADAR_RESTART_FAILED",
            f"持仓保护模式重启雷达失败：{exc}",
            incident_key="PROTECTIVE_RADAR_RESTART_FAILED",
        )
        return False


def activate_protective_recovery(
    store: TradingStore, alerts: AlertManager, strategy_id: str,
    message: str, failures: int, now: datetime,
) -> None:
    session = MARKET_CALENDAR.session(now)
    if not (session.is_session and session.extended_open(now)):
        return
    positions = active_protected_option_positions(store)
    if not positions:
        return
    threshold = max(1, int(os.getenv("PROTECTIVE_RESTART_ON_FAILURES", "3")))
    if failures < threshold:
        store.operational_event(
            "protective_mode", "PROTECTIVE_MODE_ACTIVE", "DEGRADED",
            {
                "trigger_task": strategy_id,
                "failures": failures,
                "open_option_count": len(positions),
                "positions": positions[:20],
                "message": message,
                "waiting_for_failures": threshold,
            },
        )
        return
    min_interval = max(60, int(os.getenv("PROTECTIVE_SERVICE_RESTART_MIN_INTERVAL_SECONDS", "180")))
    if _seconds_since(store.get_setting("protective_service_last_restart_at", ""), now) < min_interval:
        return
    store.set_setting("strategy_interrupted", "true")
    store.set_setting("strategy_interrupted_at", now.isoformat())
    store.set_setting("strategy_interrupted_message", message[:500])
    store.set_setting("protective_service_last_restart_at", now.isoformat())
    store.operational_event(
        "protective_mode", "PROTECTIVE_RESTART_REQUESTED", "ERROR",
        {
            "trigger_task": strategy_id,
            "failures": failures,
            "open_option_count": len(positions),
            "positions": positions[:20],
            "message": message,
            "action": "restart_radar_and_trading_service",
        },
    )
    alerts.send(
        "ERROR", "STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",
        f"检测到未平期权 {len(positions)} 张时券商/行情连接连续失败，已进入持仓保护恢复：{message}",
        incident_key="STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",
    )
    request_protective_radar_restart(store, alerts, message, now)
    request_service_self_restart(store, alerts, f"持仓保护模式：{message}")


def record_broker_failure(
    store: TradingStore, alerts: AlertManager, strategy_id: str, message: str,
) -> None:
    if not opend_failure_message(message):
        return
    now = datetime.now(NY)
    failures = int(store.get_setting("opend_consecutive_failures", "0") or 0) + 1
    store.set_setting("opend_consecutive_failures", str(failures))
    store.set_setting("opend_last_failure_at", now.isoformat())
    store.set_setting("opend_last_failure_task", strategy_id)
    store.set_setting("opend_last_failure_message", message[:500])
    opend_threshold = max(
        1,
        int(os.getenv("FUTU_OPEND_RESTART_ON_FAILURES", "4")),
    )
    store.operational_event(
        "opend", "BROKER_CONNECTIVITY_FAILURE", "DEGRADED",
        {"task": strategy_id, "failures": failures, "message": message},
    )
    activate_protective_recovery(store, alerts, strategy_id, message, failures, now)
    session = MARKET_CALENDAR.session(now)
    if failures >= opend_threshold and session.is_session and session.extended_open(now):
        maybe_restart_opend(store, alerts, message, now)


def _request_stop(_signum=None, _frame=None) -> None:
    STOP_EVENT.set()
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()


# ----------------------------------------------------------------------------
# P0-8：调度键租户化
#
# 事故形态（10-23 定位）：两个租户 Worker 共用同一批 `last_run:*` /
# `task_backoff_until:*` / `task_failure_count:*` 键 —— A 跑完 `mark_run`，
# B 的 `due()` 当场返回 False → **B 的整个任务被静默跳过**；退避键同理，
# 一个租户失败压住另一个。**看起来在跑，实际没跑，而且没有任何日志能证明。**
#
# 修法：键带租户维度。每个 Worker 进程只服务一个租户，
# 因此在 `service_cycle` 入口 `bind_tenant()` **绑定一次**，
# 由 `due` / `mark_run` / `run_task` 共用 —— 不把 tenant_id 穿到 50 个调用点
# （那是把一个作用域事实扩散成参数噪音），也不手拼键（走 tenant.py 共享入口）。
#
# 用 `contextvars` 而非模块全局：同一进程内的回调/线程能继承，且不存在
# 「忘记清理导致下一个租户读到上一个」的风险（每次 service_cycle 都重绑）。
# ----------------------------------------------------------------------------
_ACTIVE_TENANT_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_tenant_id", default="",
)


def bind_tenant(tenant_id) -> None:
    """在 `service_cycle` 入口绑定本进程服务的租户（P0-8）。"""
    _ACTIVE_TENANT_ID.set(str(tenant_id or ""))


def _sched_read_key(store: TradingStore, base_key: str) -> str:
    """调度键读侧：**租户键优先、无前缀旧键兜底**（迁移前的存量状态）。"""
    tenant_key, legacy = tenant_config_key_candidates(_ACTIVE_TENANT_ID.get(), base_key)
    if tenant_key and store.get_setting(tenant_key):
        return tenant_key
    return legacy


def _sched_write_key(base_key: str) -> str:
    """调度键写侧：**只写租户键**（迁移后无前缀键必须零写入方）。"""
    tenant_key, legacy = tenant_config_key_candidates(_ACTIVE_TENANT_ID.get(), base_key)
    return tenant_key or legacy


def due(store: TradingStore, key: str, interval: int, now: datetime) -> bool:
    raw = store.get_setting(_sched_read_key(store, key))
    if not raw:
        return True
    return (now - datetime.fromisoformat(raw)).total_seconds() >= interval


def mark_run(store: TradingStore, key: str, now: datetime) -> None:
    store.set_setting(_sched_write_key(key), now.isoformat())


def core_rebalance_window(store: TradingStore, session, now: datetime) -> tuple[bool, str]:
    """Select the normal pre-close run or a missed daily-validation catch-up."""
    key = "last_run:core_satellite"
    if session.pre_close(now, 15):
        return due(store, key, 20 * 60 * 60, now), "收盘前 15 分钟调仓窗口"
    validation = load_core_daily_validation(store, now)
    catchup_delay = max(5, int(os.getenv("CORE_CATCHUP_AFTER_OPEN_MINUTES", "30")))
    if not validation.active or not session.regular_open(now):
        return False, "等待收盘前 15 分钟调仓窗口"
    if now < session.market_open + timedelta(minutes=catchup_delay):
        return False, f"每日验证补偿将在开盘 {catchup_delay} 分钟后启用"
    raw = store.get_setting(key, "")
    try:
        last_run = datetime.fromisoformat(raw).astimezone(NY) if raw else None
    except ValueError:
        last_run = None
    if last_run is None or last_run.date() < now.date():
        return True, "检测到每日加速验证漏跑，本交易日执行一次补偿调仓"
    return False, "本交易日每日加速验证已执行"


def run_task(
    store: TradingStore, alerts: AlertManager, strategy_id: str, callback,
    timeout_seconds: float | None = None,
) -> bool:
    backoff_raw = store.get_setting(_sched_read_key(store, f"task_backoff_until:{strategy_id}"))
    if backoff_raw and datetime.now(NY) < datetime.fromisoformat(backoff_raw):
        store.heartbeat(strategy_id, "DEGRADED", "故障退避中，等待自动重试", os.getpid())
        return False
    timeout = float(timeout_seconds or TASK_TIMEOUTS.get(strategy_id, 120))
    run_id = store.start_task_run(strategy_id, timeout, os.getpid())
    started = time.monotonic()
    store.heartbeat(strategy_id, "RUNNING", "正在执行", os.getpid())
    try:
        callback()
        duration = time.monotonic() - started
        store.set_setting(_sched_write_key(f"task_failure_count:{strategy_id}"), "0")
        store.set_setting(_sched_write_key(f"task_backoff_until:{strategy_id}"), "")
        if strategy_id in BROKER_CRITICAL_TASKS:
            store.set_setting("opend_consecutive_failures", "0")
            store.set_setting("opend_last_recovery_at", datetime.now(NY).isoformat())
        if duration > timeout:
            message = f"任务耗时 {duration:.1f} 秒，超过软超时 {timeout:.0f} 秒"
            store.finish_task_run(run_id, "SLOW", duration, message)
            store.heartbeat(strategy_id, "DEGRADED", message, os.getpid())
            alerts.send(
                "WARNING", "TASK_TIMEOUT", f"{strategy_id}：{message}",
                incident_key=f"TASK_TIMEOUT:{strategy_id}",
            )
            return True
        store.finish_task_run(run_id, "SUCCESS", duration)
        store.heartbeat(strategy_id, "HEALTHY", "最近一次执行成功", os.getpid())
        alerts.recover(
            "TASK_TIMEOUT", f"{strategy_id} 耗时已恢复正常",
            incident_key=f"TASK_TIMEOUT:{strategy_id}",
        )
        alerts.recover(
            "STRATEGY_ERROR", f"{strategy_id} 已恢复正常",
            incident_key=f"STRATEGY_ERROR:{strategy_id}",
        )
        return True
    except BrokerError as exc:
        duration = time.monotonic() - started
        message = str(exc)
        if strategy_id == "core_satellite" and (
            "持仓不足" in message or "可用持仓" in message or "可卖" in message
        ):
            store.finish_task_run(run_id, "DEGRADED", duration, message)
            until = datetime.now(NY) + timedelta(hours=20)
            store.set_setting(_sched_write_key(f"task_backoff_until:{strategy_id}"), until.isoformat())
            store.decision(
                strategy_id, None, "HOLD",
                f"Core Satellite 提交前持仓校验未通过，本轮不再重复提交：{message}",
                {"backoff_until": until.isoformat(), "non_retryable": True},
                tenant_id=_ACTIVE_TENANT_ID.get(),
            )
            store.heartbeat(strategy_id, "DEGRADED", "可卖数量不足，本轮已停止重试", os.getpid())
            alerts.send(
                "WARNING", "CORE_SELLABLE_GUARD",
                f"Core Satellite 可卖数量不足，本轮已停止重复提交：{message}",
                incident_key="CORE_SELLABLE_GUARD",
            )
            return True
        store.finish_task_run(run_id, "DEGRADED", duration, message)
        record_broker_failure(store, alerts, strategy_id, message)
        store.set_setting(
            _sched_write_key(f"task_backoff_until:{strategy_id}"),
            (datetime.now(NY) + timedelta(seconds=15)).isoformat(),
        )
        store.heartbeat(strategy_id, "DEGRADED", message, os.getpid())
        alerts.send(
            "WARNING", "OPEND_UNAVAILABLE",
            "富途 OpenD 暂不可用，相关券商任务已统一进入退避并会自动恢复",
        )
        return False
    except Exception as exc:
        duration = time.monotonic() - started
        store.finish_task_run(run_id, "ERROR", duration, str(exc))
        failures = int(store.get_setting(_sched_read_key(store, f"task_failure_count:{strategy_id}"), "0") or 0) + 1
        delay = min(300, 5 * (2 ** min(failures - 1, 6)))
        store.set_setting(_sched_write_key(f"task_failure_count:{strategy_id}"), str(failures))
        store.set_setting(
            f"task_backoff_until:{strategy_id}",
            (datetime.now(NY) + timedelta(seconds=delay)).isoformat(),
        )
        store.heartbeat(strategy_id, "ERROR", str(exc), os.getpid())
        alerts.send(
            "ERROR", "STRATEGY_ERROR", f"{strategy_id}: {exc}",
            incident_key=f"STRATEGY_ERROR:{strategy_id}",
        )
        return False


def active_volatility_combo_ids(store: TradingStore) -> set[str]:
    """Combo ids still owned by currently enabled volatility campaigns.

    #1 键级分区：只读本进程绑定的那个租户的清单（``bind_tenant`` 在 cycle 入口绑定）。
    """
    states = store.campaign_states()
    ids: set[str] = set()
    for campaign in load_volatility_campaigns(_ACTIVE_TENANT_ID.get()):
        if not campaign.get("enabled", True):
            continue
        legs = campaign.get("legs") or []
        if len(legs) <= 1:
            continue
        code = str(campaign.get("code") or "")
        tenant = _ACTIVE_TENANT_ID.get()
        # #1 键级分区：候选键从**共享助手**取（写形态 → 迁移前形态 → 裸 code），
        # 不再手拼带租户的 f-string —— 手拼就是"同一个概念两份实现"的起点。
        state = {}
        for _candidate in (
            volatility_write_state_key(code, tenant),
            volatility_write_state_key(code),
            code,
        ):
            if _candidate in states:
                state = states[_candidate] or {}
                break
        try:
            cycle = int(state.get("entry_cycle", 0) or 0)
        except (TypeError, ValueError):
            cycle = 0
        for candidate_cycle in {0, cycle}:
            ids.add(automatic_combo_id("volatility_long_gamma", campaign, candidate_cycle))
    return ids


def portfolio_stress_check(broker, store, alerts, tenant_id) -> None:
    """组合压力告警的**接线层**（P1-8）。

    刻意提成模块级函数而不是 `service_cycle` 里的闭包：闭包没法被测试直接调用，
    于是「模块函数测了、接线没测」这种形态会一直漏 —— 而接线里一个未导入的名字
    就能让 recover 分支每轮抛错、滞回状态永远停在 ALERTING。
    """
    result = monitor_portfolio_stress(broker, store)
    if result.empty:
        return
    # P1-8：阈值/事件键/滞回全部走单源；缓冲带内不再「告警-恢复」来回刷。
    breach, recover = resolve_portfolio_stress_thresholds(store, tenant_id)
    incident_key = portfolio_stress_incident_key(tenant_id)
    # 旧全局 incident 一次性收口：改租户键后老事件会永久悬空。
    if not legacy_incident_closed(store, tenant_id):
        alerts.recover(
            PORTFOLIO_STRESS_LEGACY_KEY,
            "组合压力告警已迁移为租户级事件，旧全局事件收口",
            incident_key=PORTFOLIO_STRESS_LEGACY_KEY,
        )
        mark_legacy_incident_closed(store, tenant_id)
    worst = result.iloc[0]
    ratio = float(worst["equity_loss_ratio"])
    state = read_hysteresis_state(store, tenant_id)
    action = next_stress_action(ratio, state, breach, recover)
    if action == "alert":
        alerts.send(
            "ERROR", "PORTFOLIO_STRESS_BREACH",
            f"[{tenant_id or 'global'}] 组合压力场景“{worst['scenario']}”估算损失 "
            f"{ratio:.1%}，超过 {breach:.1%}",
            incident_key=incident_key,
        )
        write_hysteresis_state(store, tenant_id, STATE_ALERTING)
    elif action == "recover":
        alerts.recover(
            incident_key, "组合压力损失已回到恢复线以内",
            incident_key=incident_key,
        )
        write_hysteresis_state(store, tenant_id, STATE_OK)
    # action == "none"：缓冲带内或状态未变 —— 不重发、不误消警


def maybe_trigger_auto_retrain(store) -> bool:
    """P2-4：自动月度重训（**默认关闭**，需用户明确开启后才生效）。

    设计要点：
    - 交给**独立子进程**（wrapper）执行，绝不阻塞交易主循环 —— 训练可能跑几十分钟；
    - 默认 `ml_auto_retrain_enabled=false`：这一步会改变策略行为，必须用户拍板；
    - 间隔按天配置（`ml_auto_retrain_interval_days`，默认 30）。
    """
    enabled = str(store.get_setting("ml_auto_retrain_enabled", "false")).strip().lower() == "true"
    if not enabled:
        return False
    wrapper = Path(__file__).resolve().parent / "run_ml_retrain_wrapper.py"
    if not wrapper.exists():
        store.operational_event(
            "ml_retrain", "ML_AUTO_RETRAIN_WRAPPER_MISSING", "ERROR",
            {"wrapper": str(wrapper)},
        )
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(wrapper), "--triggered-by", "scheduler", "--kind", "auto"],
            cwd=str(wrapper.parents[1]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - 触发失败要可见，但不能带崩调度循环
        store.operational_event(
            "ml_retrain", "ML_AUTO_RETRAIN_SPAWN_FAILED", "ERROR", {"error": str(exc)},
        )
        return False
    store.audit(
        "ML_AUTO_RETRAIN_TRIGGERED",
        {"triggered_by": "scheduler", "wrapper": str(wrapper)},
        actor="scheduler:main",
    )
    return True


def service_cycle(settings: Settings, store: TradingStore, tenant: Any | None = None) -> None:
    # P0-8：绑定本进程服务的租户 —— 调度键（last_run:* / task_backoff_until:* /
    # task_failure_count:*）据此带租户维度。**必须在任何 due/mark_run/run_task 之前**，
    # 否则本周期仍会写无前缀键、被另一个租户的 Worker 读到。
    bind_tenant(getattr(tenant, "tenant_id", "") if tenant else "")
    # P1-13 修复：心跳名必须带租户。两个 Worker 共用 `trading_service` 这一行时，
    # 一方僵死、另一方仍每分钟刷新 -> 僵死永远判不出来（Reviewer 实测该行 PID 只剩模拟盘）。
    service_heartbeat_name = (
        f"trading_service:{tenant.tenant_id}" if tenant else "trading_service"
    )
    now = datetime.now(NY)
    session = MARKET_CALENDAR.session(now)
    alerts = AlertManager(store)
    # Board timers are operational tasks, not trading tasks: run them before
    # broker preflight so a disconnected OpenD cannot suppress the 05:15
    # review or the 06:00 shutdown.
    china_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if store.get_setting(
        timer_enabled_setting_key(DAILY_STRATEGY_REVIEW_TASK),
        str(timer_default_enabled(DAILY_STRATEGY_REVIEW_TASK)).lower(),
    ) == "true" and _timer_due(store, DAILY_STRATEGY_REVIEW_TASK, china_now):
        run_task(
            store, alerts, DAILY_STRATEGY_REVIEW_TASK.task_id,
            lambda: run_daily_strategy_review(store, now=china_now),
        )
    if store.get_setting(
        timer_enabled_setting_key(SHUTDOWN_TASK),
        str(timer_default_enabled(SHUTDOWN_TASK)).lower(),
    ) == "true" and _timer_due(store, SHUTDOWN_TASK, china_now):
        dry_run_flag = (
            store.get_setting("AUTO_SHUTDOWN_DRY_RUN", "") or os.getenv("AUTO_SHUTDOWN_DRY_RUN", "false")
        ).strip().lower() == "true"
        run_task(
            store, alerts, SHUTDOWN_TASK.task_id,
            lambda: shutdown_computer(
                store,
                dry_run=dry_run_flag,
                now=china_now,
            ),
        )
    if store.get_setting(
        timer_enabled_setting_key(DAILY_UNIVERSE_RANKING_TASK),
        str(timer_default_enabled(DAILY_UNIVERSE_RANKING_TASK)).lower(),
    ) == "true" and _timer_due(store, DAILY_UNIVERSE_RANKING_TASK, china_now):
        run_task(
            store, alerts, DAILY_UNIVERSE_RANKING_TASK.task_id,
            lambda: run_daily_universe_ranking(store, now=china_now),
        )
    if store.get_setting(
        timer_enabled_setting_key(WEEKLY_UNIVERSE_RECALL_TASK),
        str(timer_default_enabled(WEEKLY_UNIVERSE_RECALL_TASK)).lower(),
    ) == "true" and _timer_due(store, WEEKLY_UNIVERSE_RECALL_TASK, china_now):
        run_task(
            store, alerts, WEEKLY_UNIVERSE_RECALL_TASK.task_id,
            lambda: run_weekly_universe_recall(store, now=china_now),
        )
    if not futu_sdk_preflight(store, alerts, settings, heartbeat_name=service_heartbeat_name):
        store.set_setting("connectivity_degraded", "true")
        return
    if tenant:
        broker = FutuPaperBroker(
            settings,
            trading_env_override=tenant.trading_env,
            account_id_override=tenant.account_id,
        )
    else:
        broker = FutuPaperBroker(settings)
    executor = ExecutionManager(broker, store, tenant=tenant)
    if store.get_setting("order_push_connected", "false") == "true" and due(
        store, "last_run:heartbeat:order_push", 60, now,
    ):
        store.heartbeat("order_push", "HEALTHY", "推送通道待命，轮询兜底正常", os.getpid())
        mark_run(store, "last_run:heartbeat:order_push", now)
    run_task(store, alerts, "shadow_matching", lambda: match_shadow_orders(store, broker))
    positions_snapshot = None
    if due(store, "last_run:equity_snapshot", 60, now):
        try:
            account = broker.account_info()
            positions = broker.positions()
            positions_snapshot = positions
            store.save_broker_snapshot("accounts", broker.accounts())
            store.save_broker_snapshot("account_info", account)
            store.save_broker_snapshot("positions", positions)
            store.save_broker_snapshot("orders", broker.orders())
            current_env = getattr(broker, "trading_env", "SIMULATE").upper()
            if current_env == "REAL":
                store.save_broker_snapshot("account_info_real", account)
                store.save_broker_snapshot("positions_real", positions)
                store.save_broker_snapshot("account_info_futuhk_real", account)
                store.save_broker_snapshot("positions_futuhk_real", positions)
                # 检查 Moomoo US 备用账户是否开启统一管理；关闭时完全跳过采集，不占用连接与资源
                moomoo_cfg_raw = store.get_setting("real_account_config:283445327916889204", "")
                moomoo_is_en = False
                if moomoo_cfg_raw:
                    try:
                        moomoo_is_en = bool(json.loads(moomoo_cfg_raw).get("enabled", False))
                    except Exception:
                        pass
                if moomoo_is_en:
                    try:
                        if not hasattr(broker, "_broker_moomoo_companion"):
                            broker._broker_moomoo_companion = FutuPaperBroker(
                                settings, trading_env_override="REAL", account_id_override=283445327916889204,
                                security_firm_override="FUTUINC",
                            )
                        moomoo_b = broker._broker_moomoo_companion
                        moomoo_acc = moomoo_b.account_info()
                        moomoo_pos = moomoo_b.positions()
                        store.save_broker_snapshot("account_info_moomoo_real", moomoo_acc)
                        store.save_broker_snapshot("positions_moomoo_real", moomoo_pos)
                        store.save_broker_snapshot("orders_moomoo_real", moomoo_b.orders())
                    except Exception:
                        pass
                try:
                    if not hasattr(broker, "_broker_sim_companion"):
                        broker._broker_sim_companion = FutuPaperBroker(
                            settings, trading_env_override="SIMULATE", account_id_override=20170229,
                            security_firm_override="FUTUSECURITIES",
                        )
                    sim_b = broker._broker_sim_companion
                    sim_acc = sim_b.account_info()
                    sim_pos = sim_b.positions()
                    store.save_broker_snapshot("account_info_sim", sim_acc)
                    store.save_broker_snapshot("positions_sim", sim_pos)
                    store.save_broker_snapshot("account_info_sim_primary", sim_acc)
                    store.save_broker_snapshot("positions_sim_primary", sim_pos)
                    store.save_broker_snapshot("orders_sim", sim_b.orders())
                    if not sim_acc.empty:
                        s_row = sim_acc.iloc[0]
                        store.record_equity(
                            float(s_row.get("total_assets", 0) or 0),
                            float(s_row.get("cash", 0) or 0),
                            float(s_row.get("market_val", 0) or 0),
                            env="SIMULATE",
                        )
                except Exception:
                    pass
            else:
                store.save_broker_snapshot("account_info_sim", account)
                store.save_broker_snapshot("positions_sim", positions)

            if not account.empty:
                row = account.iloc[0]
                broker_acc_id = str(getattr(broker, "account_id", "") or "").strip()
                # 针对多实盘账户，按账户 ID 隔离记录权益快照，杜绝跨账户计算日内虚假亏损！
                equity_env = f"{current_env}_{broker_acc_id}" if broker_acc_id and current_env == "REAL" else current_env
                store.record_equity(
                    float(row.get("total_assets", 0) or 0), float(row.get("cash", 0) or 0),
                    float(row.get("market_val", row.get("securities_assets", 0)) or 0),
                    env=equity_env,
                )
                position_loss = -float(account_pnl_metrics(positions)["today_position_pnl"])
                day_start = ny_trading_day_start_utc(now).isoformat()
                before = store.latest_equity_before(day_start, env=equity_env)
                today_rows = store.equity_since(day_start, env=equity_env)
                baseline_assets = float(
                    before["total_assets"] if before else
                    today_rows[0]["total_assets"] if today_rows else row.get("total_assets", 0)
                )
                cash_value = float(row.get("cash", 0) or 0)
                market_value = float(row.get("market_val", row.get("securities_assets", 0)) or 0)
                total_assets = float(row.get("total_assets", 0) or 0)
                # 跨环境与换账户隔离保护：
                # 如果 baseline_assets 与当前实际 total_assets 相差过大（例如多账户切换或历史合并基线），
                # 说明存在不同环境或账户切换，必须以当前账户本身的资产为基准，绝不能跨环境算日内亏损！
                if baseline_assets > 0 and total_assets > 0:
                    ratio = baseline_assets / total_assets
                    if ratio > 1.25 or ratio < 0.8:
                        baseline_assets = total_assets
                snapshot_gap = abs(total_assets - (cash_value + market_value))
                snapshot_tolerance = max(100.0, abs(total_assets) * 0.01)
                snapshot_valid = snapshot_gap <= snapshot_tolerance
                equity_loss = baseline_assets - total_assets if snapshot_valid else 0.0
                if not snapshot_valid:
                    store.operational_event(
                        "trading_service", "EQUITY_SNAPSHOT_INVALID", "DEGRADED",
                        {"total_assets": total_assets, "cash": cash_value,
                         "market_value": market_value, "snapshot_gap": snapshot_gap,
                         "tolerance": snapshot_tolerance},
                    )
                loss = max(0.0, position_loss, equity_loss)
                limit = RiskLimits.load().max_daily_loss
                if session.regular_open(now) and loss >= limit and not store.kill_switch(account_id=broker_acc_id):
                    store.set_kill_switch(True, account_id=broker_acc_id)
                    store.audit("DAILY_LOSS_KILL_SWITCH", {
                        "loss": loss, "position_loss": position_loss,
                        "equity_loss": equity_loss, "baseline_assets": baseline_assets,
                        "limit": limit, "trading_date_ny": now.date().isoformat(),
                        "account_id": broker_acc_id,
                    }, actor="scheduler:main")
                    target_name = f"账户 {broker_acc_id}" if broker_acc_id else "系统"
                    alerts.send(
                        "ERROR", "DAILY_LOSS_KILL_SWITCH",
                        f"{target_name} 当日持仓亏损 ${loss:,.2f} 已达到急停线 ${limit:,.2f}，该账户已自动急停（其他账户继续运行）",
                    )
            mark_run(store, "last_run:equity_snapshot", now)
            alerts.recover("EQUITY_SNAPSHOT_FAILED", "账户权益快照已恢复")
        except Exception as exc:
            alerts.send("ERROR", "EQUITY_SNAPSHOT_FAILED", str(exc))
    connected = run_task(store, alerts, "order_reconciliation", executor.reconcile)
    store.set_setting("connectivity_degraded", str(not connected).lower())
    # Never feed a persisted (possibly stale) positions snapshot into trade
    # decisions. The combo monitor refreshes directly from the broker when the
    # current cycle did not fetch live positions.
    combo_positions_snapshot = positions_snapshot
    run_task(
        store, alerts, "option_combo_monitor",
        lambda: ComboExecutionManager(
            broker, store, executor, positions_snapshot=combo_positions_snapshot,
        ).monitor_all(
            active_volatility_combo_ids(store),
            close_orphan_open=True,
            allow_open_execution=session.regular_open(now),
        ),
    )
    if session.regular_open(now) and due(store, "last_run:combo_market_snapshot", 60, now):
        run_task(
            store, alerts, "combo_market_snapshot",
            lambda: collect_combo_market_snapshots(broker, store),
        )
        mark_run(store, "last_run:combo_market_snapshot", now)
    if due(store, "last_run:option_lifecycle", 300, now):
        def lifecycle_check():
            rows = monitor_option_lifecycle(broker, store)
            risky = [row for row in rows if row["severity"] in {"CRITICAL", "HIGH"}]
            if risky:
                summary = "；".join(
                    f"{row['code']}：{row['reason']}" for row in risky[:5]
                )
                alerts.send(
                    "ERROR", "OPTION_LIFECYCLE_RISK", summary,
                    incident_key="OPTION_LIFECYCLE_RISK",
                )
            else:
                alerts.recover(
                    "OPTION_LIFECYCLE_RISK", "期权到期与行权风险已解除",
                    incident_key="OPTION_LIFECYCLE_RISK",
                )
        run_task(store, alerts, "option_lifecycle", lifecycle_check)
        mark_run(store, "last_run:option_lifecycle", now)
    enabled_dispositions = any(
        plan["enabled"] and plan["action"] == "AUTO_CLOSE"
        for plan in store.option_dispositions()
    )
    if enabled_dispositions and session.regular_open(now):
        if due(store, "last_run:option_disposition", 30, now):
            run_task(
                store, alerts, "option_disposition",
                lambda: execute_disposition_plans(broker, store, executor, session, now),
            )
            mark_run(store, "last_run:option_disposition", now)
    elif enabled_dispositions:
        store.heartbeat(
            "option_disposition", "HEALTHY", "等待美股常规交易时段与预设平仓窗口",
            os.getpid(),
        )
    if session.regular_open(now) and due(store, "last_run:option_history", 60, now):
        run_task(
            store, alerts, "option_history",
            lambda: collect_option_snapshots(broker, store),
        )
        mark_run(store, "last_run:option_history", now)
    elif not session.regular_open(now):
        store.heartbeat("option_history", "HEALTHY", "等待美股常规交易时段采集", os.getpid())
    if session.regular_open(now) and due(store, "last_run:portfolio_stress", 900, now):
        run_task(
            store, alerts, "portfolio_stress",
            lambda: portfolio_stress_check(
                broker, store, alerts, tenant.tenant_id if tenant else None,
            ),
        )
        mark_run(store, "last_run:portfolio_stress", now)
    elif not session.regular_open(now):
        store.heartbeat("portfolio_stress", "HEALTHY", "等待常规交易时段更新压力矩阵", os.getpid())
    if due(store, "last_run:ledger_reconciliation", 60, now):
        def ledger_check():
            report = reconcile_ledgers(store, broker, tenant_id=tenant.tenant_id if tenant else None)
            mismatches = report[report["status"] == "MISMATCH"] if not report.empty else report
            store.set_setting("recovery_blocked", str(not mismatches.empty).lower())
            if not mismatches.empty:
                diff_lines = []
                for _, r in mismatches.iterrows():
                    diff_lines.append(
                        f"• **标的**: `{r.get('symbol')}` | **券商实物**: {r.get('broker_quantity'):g} | "
                        f"**虚拟账本**: {r.get('ledger_quantity'):g} | **差异**: {r.get('difference'):+g} | "
                        f"**关联策略**: `{r.get('affected_strategies')}`"
                    )
                diff_summary = "\n".join(diff_lines)
                tenant_desc = f" [{tenant.display_name}]" if tenant else ""
                alerts.send(
                    "ERROR", "LEDGER_MISMATCH",
                    f"三账对账发现 {len(mismatches)} 个标的存在持仓差异{tenant_desc}：\n{diff_summary}\n\n"
                    f"🛡️ **系统动作**: 相关标的 SELL 通道已锁定，策略新开仓已自动暂停\n"
                    f"💡 **处置指引**: 请前往 Streamlit「策略审批与对账」页面执行【按富途持仓同步】一键平账恢复！",
                )
            else:
                tenant_desc = f" [{tenant.display_name}]" if tenant else ""
                alerts.recover(
                    "LEDGER_MISMATCH",
                    f"三账对账{tenant_desc}已恢复一致，持仓与账本完全对齐，自动解除恢复阻断与策略暂停",
                )
        if run_task(store, alerts, "ledger_reconciliation", ledger_check):
            mark_run(store, "last_run:ledger_reconciliation", now)

    configured_env = {
        value.strip() for value in os.getenv(
            "SERVICE_STRATEGIES", "Core Satellite,Earnings Gamma"
        ).split(",") if value.strip()
    }
    tenant_id = tenant.tenant_id if tenant else None
    if tenant_id:
        paused = store.get_tenant_setting(tenant_id, "strategies_paused", "false") == "true"
    else:
        paused = store.get_setting("strategies_paused", "false") == "true"
    configured = resolve_configured_strategies(store, tenant_id, configured_env)
    if paused:
        configured.clear()
    if "Earnings Gamma" in configured and session.regular_open(now):
        interval = max(10, int(os.getenv("GAMMA_INTERVAL_SECONDS", "30")))
        if due(store, "last_run:gamma", interval, now):
            run_task(
                store, alerts, "earnings_gamma",
                lambda: run_gamma_once(reconcile=False),
            )
            mark_run(store, "last_run:gamma", now)
    elif "Earnings Gamma" in configured:
        store.heartbeat("earnings_gamma", "HEALTHY", "等待美股常规交易时段", os.getpid())
    if "Volatility Long Gamma" in configured and session.regular_open(now):
        # Run the exit/entry monitor at its configured cadence whether or not
        # the account currently holds an option. Expression review has its own
        # slower timer inside run_volatility_once; never throttle risk checks.
        interval = max(30, int(os.getenv("VOLATILITY_INTERVAL_SECONDS", "30")))
        if due(store, "last_run:volatility_long_gamma", interval, now):
            started = time.monotonic()
            # 1. 专属受控通道：在当前租户绑定的账户中执行受控期权打猎
            succeeded = run_task(
                store, alerts, "volatility_long_gamma",
                lambda: run_volatility_once(reconcile=False, broker=broker, executor=executor),
            )
            # 2. 模拟沙盒伴生通道：仅在未传 --tenant 的旧单体实盘模式下兼容运行，独立租户 Worker 运行自己的实例
            if getattr(broker, "trading_env", "SIMULATE") == "REAL" and tenant is None:
                try:
                    if not hasattr(broker, "_broker_sim_companion"):
                        broker._broker_sim_companion = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
                    sim_b = broker._broker_sim_companion
                    sim_exec = ExecutionManager(sim_b, store)
                    run_task(
                        store, alerts, "volatility_long_gamma_sim",
                        lambda: run_volatility_once(reconcile=False, broker=sim_b, executor=sim_exec),
                    )
                except Exception as exc:
                    store.operational_event("volatility_long_gamma_sim", "EXECUTION_SKIPPED", "DEGRADED", {"error": str(exc)})
            elapsed = time.monotonic() - started
            store.operational_event(
                "volatility_long_gamma", "MONITOR_CYCLE_DURATION",
                "HEALTHY" if succeeded and elapsed <= interval else "DEGRADED",
                {"duration_seconds": round(elapsed, 3), "target_seconds": interval,
                 "target_is_hard_timeout": False, "success": succeeded},
            )
            if succeeded:
                mark_run(store, "last_run:volatility_long_gamma", now)
    elif "Volatility Long Gamma" in configured:
        store.heartbeat("volatility_long_gamma", "HEALTHY", "等待美股常规交易时段", os.getpid())

    regular = session.regular_open(now)
    strategy_interval = settings.interval_seconds
    for strategy in configured.intersection({"SMA Cross", "RSI Mean Reversion", "20 Day Breakout"}):
        key = f"last_run:{strategy}"
        if regular and due(store, key, strategy_interval, now):
            plugin = BY_SERVICE_NAME[strategy]
            run_task(
                store, alerts, plugin.strategy_id,
                lambda item=plugin: run_strategy_once(
                    broker, item.runner_key or item.service_name, executor, item.strategy_id,
                    tenant_id=_ACTIVE_TENANT_ID.get(),
                ),
            )
            mark_run(store, key, now)

    core_due, core_reason = core_rebalance_window(store, session, now)
    if "Core Satellite" in configured and core_due:
        # 在底仓调仓前执行期权超额利润锁箱沉淀 (Profit Sweeper)
        try:
            from futu_quant.profit_sweeper import ProfitSweeper
            ProfitSweeper(store).sweep_profit_to_spy(min_threshold_usd=1000.0)
        except Exception:
            pass
        key = "last_run:core_satellite"
        core_broker = broker
        core_executor = executor
        # 1. 富途香港实盘 Core Satellite 现货调仓
        succeeded = False
        try:
            if getattr(broker, "trading_env", "SIMULATE") == "REAL":
                if not hasattr(broker, "_broker_futuhk_core"):
                    broker._broker_futuhk_core = FutuPaperBroker(
                        settings,
                        trading_env_override="REAL",
                        account_id_override=281756481476169109,
                        security_firm_override="FUTUSECURITIES",
                    )
                core_broker = broker._broker_futuhk_core
                core_executor = ExecutionManager(core_broker, store, alerts)
            succeeded = run_task(
                store, alerts, "core_satellite",
                lambda: run_strategy_once(core_broker, "Core Satellite", core_executor, tenant_id=_ACTIVE_TENANT_ID.get() or "futu_real_master"),
            )
            if succeeded:
                mark_run(store, key, now)
        except Exception as exc:
            store.operational_event("core_satellite", "EXECUTION_ERROR", "ERROR", {"error": str(exc)})

        # 2. 伴生模拟沙盒调度：美股模拟盘 20170229 独立执行 100 万大资金底仓调仓（彻底隔离，互不阻塞）
        if getattr(broker, "trading_env", "SIMULATE") == "REAL":
            try:
                if not hasattr(broker, "_broker_sim_companion"):
                    broker._broker_sim_companion = FutuPaperBroker(
                        settings, trading_env_override="SIMULATE", account_id_override=20170229,
                        security_firm_override="FUTUSECURITIES",
                    )
                sim_b = broker._broker_sim_companion
                sim_exec = ExecutionManager(sim_b, store, alerts)
                run_task(
                    store, alerts, "core_satellite_sim",
                    lambda: run_strategy_once(sim_b, "Core Satellite", sim_exec, tenant_id="futu_paper_sandbox"),
                )
            except Exception as exc:
                store.operational_event("core_satellite_sim", "EXECUTION_SKIPPED", "DEGRADED", {"error": str(exc)})
        if succeeded:
            mark_run(store, key, now)
            if "补偿调仓" in core_reason:
                store.audit(
                    "CORE_DAILY_VALIDATION_CATCHUP",
                    {"trading_date_ny": now.date().isoformat(), "reason": core_reason},
                    "core_satellite",
                 actor="scheduler:main")
    elif "Core Satellite" in configured:
        store.heartbeat("core_satellite", "HEALTHY", core_reason, os.getpid())
    if due(store, "last_run:heartbeat:trading_service", 60, now):
        store.heartbeat(service_heartbeat_name, "HEALTHY", f"调度循环完成 · {now:%F %T %Z}", os.getpid())
        # P1-21：全局兼容行也要写回 HEALTHY，否则历史 DEGRADED 永远留在页面上；
        # 判故障以 `trading_service:<tenant>` 为准（全局行只做兼容显示）。
        if service_heartbeat_name != "trading_service":
            store.heartbeat(
                "trading_service", "HEALTHY",
                "调度循环完成（全局兼容行，不用于判故障）", os.getpid(),
            )
        mark_run(store, "last_run:heartbeat:trading_service", now)
    if due(store, "last_run:maintenance", 300, now):
        run_task(store, alerts, "system_health", lambda: maintenance(store, broker))
        mark_run(store, "last_run:maintenance", now)
    if due(store, "last_run:campaign_cleanup", 300, now):
        run_task(
            store, alerts, "campaign_cleanup",
            lambda: cleanup_expired_campaigns(store, broker),
        )
        mark_run(store, "last_run:campaign_cleanup", now)
    if (due(store, "last_success:earnings_calendar", 20 * 60 * 60, now)
            and due(store, "last_attempt:earnings_calendar", 10 * 60, now)):
        def refresh_calendar():
            count = refresh_earnings_events(store, settings.gamma_symbols, broker=broker)
            campaigns, updated = sync_campaign_events(store, load_gamma_campaigns())
            if updated:
                for campaign in campaigns:
                    upsert_campaign("GAMMA_CAMPAIGNS", campaign)
            store.audit("EARNINGS_CALENDAR_REFRESHED", {"events": count, "campaigns_updated": updated}, actor="scheduler:main")
        succeeded = run_task(store, alerts, "earnings_calendar", refresh_calendar)
        mark_run(store, "last_attempt:earnings_calendar", now)
        if succeeded:
            mark_run(store, "last_success:earnings_calendar", now)
    if due(store, "last_run:daily_report", 20 * 60 * 60, now):
        run_task(store, alerts, "daily_report", lambda: daily_operations_report(store))
        mark_run(store, "last_run:daily_report", now)
    if due(store, "last_run:correlation", 20 * 60 * 60, now):
        run_task(store, alerts, "correlation_risk", lambda: update_correlation_clusters(
            store, broker, tuple(dict.fromkeys((*settings.watchlist, *settings.core_satellite_symbols)))
        ))
        mark_run(store, "last_run:correlation", now)
    if not session.regular_open(now) and due(
        store, "last_run:research_data_backfill", 20 * 60 * 60, now
    ):
        def backfill_research_data():
            symbols = tuple(dict.fromkeys((
                "US.SPY", *settings.watchlist, *settings.core_satellite_symbols,
                *settings.gamma_symbols, *settings.volatility_symbols,
            )))
            result = backfill_research_history(
                FutuMarketDataProvider(broker), symbols,
                (now.date() - timedelta(days=14)).isoformat(), now.date().isoformat(),
            )
            store.audit("RESEARCH_DATA_BACKFILL", result, actor="scheduler:main")
            if result["requested"] and not result["succeeded"]:
                raise RuntimeError("研究日线补全全部失败，请检查富途历史行情权限")
        run_task(store, alerts, "research_data_backfill", backfill_research_data)
        mark_run(store, "last_run:research_data_backfill", now)
    if due(store, "last_run:validation_calibration", 20 * 60 * 60, now):
        run_task(
            store, alerts, "validation_calibration",
            lambda: run_validation_cycle(broker, store),
        )
        mark_run(store, "last_run:validation_calibration", now)
    if due(store, "last_run:validation_daily_report", 20 * 60 * 60, now):
        run_task(
            store, alerts, "validation_report",
            lambda: validation_report(store, "DAILY"),
        )
        mark_run(store, "last_run:validation_daily_report", now)
    if due(store, "last_run:validation_weekly_report", 6 * 24 * 60 * 60, now):
        run_task(
            store, alerts, "validation_report",
            lambda: validation_report(store, "WEEKLY"),
        )
        mark_run(store, "last_run:validation_weekly_report", now)
    if due(store, "last_run:parquet_archive", 20 * 60 * 60, now):
        run_task(store, alerts, "parquet_archive", lambda: archive_market_data(store))
        mark_run(store, "last_run:parquet_archive", now)
    if due(store, "last_run:recovery_point", 20 * 60 * 60, now):
        run_task(store, alerts, "recovery_point", lambda: create_recovery_point(store))
        mark_run(store, "last_run:recovery_point", now)
    if due(store, "last_run:disaster_recovery_drill", 6 * 24 * 60 * 60, now):
        run_task(store, alerts, "disaster_recovery_drill", lambda: latest_recovery_drill(store))
        mark_run(store, "last_run:disaster_recovery_drill", now)
    if due(store, "last_run:storage_monitor", 300, now):
        def check_storage():
            report = storage_report()
            previous_status = store.get_setting("storage_capacity_status", "")
            # 仍然持续告警，但不每 5 分钟重复写一条完全相同的事件。
            if previous_status != report["status"]:
                store.operational_event("storage", "STORAGE_CAPACITY", report["status"], report)
                store.set_setting("storage_capacity_status", report["status"])
            if report["status"] == "WARNING":
                alerts.send(
                    "ERROR", "STORAGE_CAPACITY_WARNING",
                    f"磁盘可用空间仅剩 {report['free_ratio']:.1%}，请清理或扩容",
                    incident_key="STORAGE_CAPACITY_WARNING",
                )
            else:
                alerts.recover(
                    "STORAGE_CAPACITY_WARNING", "磁盘容量已恢复正常",
                    incident_key="STORAGE_CAPACITY_WARNING",
                )
        run_task(store, alerts, "storage_monitor", check_storage)
        mark_run(store, "last_run:storage_monitor", now)
    if due(store, "last_run:observability", 300, now):
        run_task(store, alerts, "observability", lambda: observability_snapshot(store))
        mark_run(store, "last_run:observability", now)
    # S3 投递探针：**必须真实投递**（`ALERT_DELIVERY_PROBE` 在不可静音清单里，
    # 「静音」只能理解为页面折叠/不刷屏，不能是不投递）。
    # 判据 `!=1` 即失败：`-1` → `ALERT_CHANNEL_NOT_CONFIGURED`，
    # `0` → `ALERT_DELIVERY_FAILED`。间隔默认 30 分钟。
    if due(
        store, "last_run:alert_delivery_probe",
        max(60, int(os.getenv("ALERT_PROBE_INTERVAL_SECONDS", str(ALERT_PROBE_INTERVAL_DEFAULT)))),
        now,
    ):
        run_task(store, alerts, "alert_delivery_probe", alerts.probe_delivery)
        mark_run(store, "last_run:alert_delivery_probe", now)
    # P2-1a：ML 模型健康巡检（**只读检测 + 告警，不改任何打分行为**）。
    # 背景：ML 曾静默降级为规则打分 6 周没人发现 —— 必须周期体检，且告警要能在
    # 告警投递中心看到 delivered（只写库不推送正是 P1-1 查出的形态）。
    if due(
        store, "last_run:ml_model_health",
        max(600, int(os.getenv("ML_HEALTH_INTERVAL_SECONDS", str(6 * 3600)))),
        now,
    ):
        def ml_model_health_cycle():
            report = check_ml_model_health(
                store, alerts, tenant_id=tenant.tenant_id if tenant else None,
            )
            store.operational_event(
                "ml", "ML_MODEL_HEALTH",
                "WARNING" if report.get("alerts") else "HEALTHY",
                report,
            )
            return report
        run_task(store, alerts, "ml_model_health", ml_model_health_cycle)
        mark_run(store, "last_run:ml_model_health", now)
    # 兜底 rule tie-break 落地审计：**收盘后**每天一次（Chief 2026-10-25 裁定）。
    # 只算 rule 分数 + 实际盈亏（几毫秒），**不加载模型、不跑 run_ml_vs_rule_comparison**
    # —— 那条要读 CSV + 打分，塞进主循环会阻塞调度。同一天重复运行按 (交易日, 口径版本)
    # 幂等跳过（保留首条），不写重复行污染滚动统计。
    if not session.regular_open(now) and due(
        store, "last_run:ml_tiebreak_audit", 20 * 60 * 60, now
    ):
        run_task(
            store, alerts, "ml_tiebreak_audit",
            lambda: run_ml_tiebreak_audit(store),
        )
        mark_run(store, "last_run:ml_tiebreak_audit", now)
    # P2-4 自动月度重训：**默认关闭**（改策略行为，需用户拍板后才开启）。
    # 开启后按天判定、交独立子进程跑，不阻塞主循环。
    if due(store, "last_run:ml_auto_retrain", 24 * 60 * 60, now):
        if maybe_trigger_auto_retrain(store):
            store.operational_event(
                "ml_retrain", "ML_AUTO_RETRAIN_DISPATCHED", "HEALTHY", {},
            )
        mark_run(store, "last_run:ml_auto_retrain", now)
    # P0-5 候选排除留痕的**保留策略**：按天保留 7 天。
    # 高频写入（每轮每候选一条），无保留策略就是下一个 `reconciliation_reports`（129 万行）。
    if due(store, "last_run:candidate_exclusions_prune", 24 * 60 * 60, now):
        run_task(
            store, alerts, "candidate_exclusions_prune",
            lambda: store.prune_candidate_exclusions(
                int(os.getenv("CANDIDATE_EXCLUSIONS_RETAIN_DAYS", "7"))
            ),
        )
        mark_run(store, "last_run:candidate_exclusions_prune", now)
    # P2-14 效率：对账明细降采样。`reconciliation_reports` 实测 38 万行、其中
    # 37.9 万是 MATCHED（正常）—— 60 秒一轮 × 每标的 × 多租户的产物。
    # 只删「旧且正常」，MISMATCH 永久保留（那是事故证据）。
    if due(store, "last_run:reconciliation_prune", 24 * 60 * 60, now):
        run_task(
            store, alerts, "reconciliation_prune",
            lambda: store.prune_reconciliation_reports(
                int(os.getenv("RECONCILIATION_RETAIN_DAYS", "7"))
            ),
        )
        mark_run(store, "last_run:reconciliation_prune", now)
    if due(store, "last_run:automatic_recovery", 300, now):
        run_task(store, alerts, "automatic_recovery", lambda: automatic_recovery_cycle(store))
        mark_run(store, "last_run:automatic_recovery", now)
    if due(store, "last_run:idempotency_audit", 300, now):
        run_task(store, alerts, "idempotency_audit", lambda: idempotency_audit(store))
        mark_run(store, "last_run:idempotency_audit", now)
    if due(store, "last_run:page_compatibility", 20 * 60 * 60, now):
        def check_pages():
            result = audit_streamlit_pages()
            store.operational_event(
                "readiness", "PAGE_COMPATIBILITY",
                "HEALTHY" if result["passed"] else "ERROR", result,
            )
            if not result["passed"]:
                raise RuntimeError(f"页面兼容性巡检发现 {result['errors']} 个错误")
        run_task(store, alerts, "page_compatibility", check_pages)
        mark_run(store, "last_run:page_compatibility", now)
    if due(store, "last_run:paper_e2e", 6 * 24 * 60 * 60, now):
        run_task(store, alerts, "paper_e2e", lambda: paper_e2e_check(store))
        mark_run(store, "last_run:paper_e2e", now)
    if due(store, "last_run:dynamic_allocation", 20 * 60 * 60, now):
        run_task(store, alerts, "dynamic_allocation", lambda: dynamic_allocation_plan(store))
        mark_run(store, "last_run:dynamic_allocation", now)
    if due(store, "last_run:operations_report", 20 * 60 * 60, now):
        run_task(store, alerts, "operations_report", lambda: operations_report(store))
        mark_run(store, "last_run:operations_report", now)
    if due(store, "last_run:long_term_validation", 20 * 60 * 60, now):
        run_task(store, alerts, "long_term_validation", lambda: long_term_validation_cycle(store))
        mark_run(store, "last_run:long_term_validation", now)


def _wait_or_stop(seconds: float, stop_file: Path | None = None) -> None:
    """Wait up to *seconds*, waking early when a graceful stop is requested.

    POSIX wakes immediately when ``SIGTERM`` sets ``STOP_EVENT``.  Windows
    cannot deliver SIGTERM to the process, so the wait also polls the stop
    flag file that the control layer touches on stop/restart requests.

    Two flag files are honoured on purpose:

    * the tenant-level ``stop_file`` (``data/trading-service-<tenant>.stop``)
      written for this worker, so one tenant can be stopped without touching
      the others; and
    * the global ``STOP_FILE`` (``data/trading-service.stop``), which the UI
      stop button and the legacy single-service launcher still touch and which
      must keep meaning "stop everything".

    Polling only the global file would silently ignore a per-tenant stop, and
    polling only the tenant file would leave the global kill-switch dead.
    """
    watched = [STOP_FILE]
    if stop_file is not None and stop_file != STOP_FILE:
        watched.append(stop_file)
    deadline = time.monotonic() + max(0.0, seconds)
    while not STOP_EVENT.is_set():
        if any(path.exists() for path in watched):
            STOP_EVENT.set()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        STOP_EVENT.wait(min(1.0, remaining))


def main() -> None:
    # P0（2026-10-25）：**只有服务启动路径**可授权迁移。其余脚本/测试打开存量库
    # 遇到 pending 迁移会直接失败，避免「跑一条巡检脚本就顺手迁移生产库」。
    os.environ["TRADEYI_ALLOW_MIGRATION"] = "1"
    parser = argparse.ArgumentParser(description="Unified Futu paper strategy service")
    parser.add_argument("--once", action="store_true", help="Run one scheduling cycle")
    parser.add_argument("--poll", type=int, default=10, help="Scheduler polling seconds")
    parser.add_argument("--tenant", type=str, default=None, help="Target tenant ID (e.g. futu_real_master, futu_paper_sandbox)")
    args = parser.parse_args()
    settings = Settings.load(refresh=True)
    if not settings.enable_paper_orders:
        raise RuntimeError("ENABLE_PAPER_ORDERS=false; automatic paper execution is disabled")

    tenant_profile = None
    if args.tenant:
        from futu_quant.tenant import resolve_tenant
        tenant_profile = resolve_tenant(args.tenant, strict=True)

    files = service_files(tenant_profile.tenant_id if tenant_profile else None)
    lock_file = files["lock"]
    pid_file = files["pid"]
    stop_file = files["stop"]

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.unlink(missing_ok=True)
    # 全局 stop 文件是「急停全部」的总闸：一旦残留（例如强杀进程时没来得及清），
    # 新拉起的 Worker 会在第一次 _wait_or_stop 就立刻自杀，start.bat 静默失效。
    # 启动语义即「开始运行」，故在此一并清除；运行中由 stop() 重新写入仍然有效。
    STOP_FILE.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    with lock_file.open("a+b") as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except FileLockError as exc:
            tenant_desc = f" [{tenant_profile.tenant_id}]" if tenant_profile else ""
            raise RuntimeError(f"交易服务实例{tenant_desc}已经在运行，拒绝启动重复实例") from exc
        lock.seek(0)
        lock.write(f"{os.getpid()}\n".encode())
        lock.flush()
        lock.truncate()
        pid_file.write_text(str(os.getpid()))
        store = TradingStore(bootstrap=True)
        service_name = f"trading_service:{tenant_profile.tenant_id}" if tenant_profile else "trading_service"
        runtime_manifest = publish_approval_runtime_manifest(store, os.getpid())
        ensure_default_budgets(store)
        sync_strategy_experiments(store, settings)
        store.operational_event(
            service_name, "PROCESS_START", "STARTING",
            {"pid": os.getpid(), "tenant_id": getattr(tenant_profile, "tenant_id", None), "approval_runtime_manifest": runtime_manifest},
        )
        try:
            from futu_quant.radar_control import start as start_radar
            start_radar()
        except Exception as exc:
            store.operational_event(
                "volatility_scanner", "AUTO_START", "DEGRADED", {"error": str(exc)},
            )
        push_context = None

        def _make_broker():
            if tenant_profile:
                return FutuPaperBroker(
                    settings,
                    trading_env_override=tenant_profile.trading_env,
                    account_id_override=tenant_profile.account_id,
                )
            return FutuPaperBroker(settings)

        try:
            if futu_sdk_preflight(store, AlertManager(store), settings, force=True):
                try:
                    push_context = open_order_push(_make_broker(), store)
                except Exception as exc:
                    store.set_setting("order_push_connected", "false")
                    store.heartbeat(
                        "order_push", "DEGRADED",
                        f"推送连接失败，已启用轮询兜底：{exc}", os.getpid(),
                    )
                try:
                    broker = _make_broker()
                    startup_recovery(store, broker, ExecutionManager(broker, store, tenant=tenant_profile))
                except Exception as exc:
                    store.set_setting("connectivity_degraded", "true")
                    store.set_setting("recovery_blocked", "true")
                    store.operational_event("recovery", "STARTUP_RECOVERY", "ERROR", {"error": str(exc)})
            else:
                store.set_setting("connectivity_degraded", "true")
                store.set_setting("recovery_blocked", "true")
                store.operational_event(
                    "recovery", "STARTUP_RECOVERY_SKIPPED", "DEGRADED",
                    {"reason": "OpenD SDK 预检未通过，跳过启动恢复以避免主进程卡死"},
                )
            while not STOP_EVENT.is_set():
                if push_context is None:
                    if futu_sdk_preflight(store, AlertManager(store), settings):
                        try:
                            push_context = open_order_push(_make_broker(), store)
                        except Exception as exc:
                            store.set_setting("order_push_connected", "false")
                            store.heartbeat(
                                "order_push", "DEGRADED", f"等待自动重连：{exc}", os.getpid(),
                            )
                try:
                    settings = Settings.load(refresh=True)
                    service_cycle(settings, store, tenant=tenant_profile)
                except Exception as exc:
                    store.heartbeat(service_name, "ERROR", str(exc), os.getpid())
                    AlertManager(store).send("ERROR", "SERVICE_CYCLE_ERROR", str(exc))
                if args.once or STOP_EVENT.is_set():
                    break
                _wait_or_stop(max(5, args.poll), stop_file)
        finally:
            if push_context is not None:
                push_context.close()
            store.set_setting("order_push_connected", "false")
            store.heartbeat(service_name, "STOPPED", "服务已停止", os.getpid())
            store.operational_event(service_name, "PROCESS_STOP", "STOPPED", {"pid": os.getpid()})
            pid_file.unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)
            try:
                from futu_quant.broker import close_all_connections
                close_all_connections()
            except Exception:
                pass


if __name__ == "__main__":
    main()
