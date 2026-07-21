from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import signal
import threading
import time
from zoneinfo import ZoneInfo

from futu_quant.alerts import AlertManager
from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.health import maintenance
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.shadow_matching import match_shadow_orders
from futu_quant.earnings_calendar import refresh_earnings_events, sync_campaign_events
from futu_quant.order_push import open_order_push
from futu_quant.recovery import daily_operations_report, startup_recovery
from futu_quant.correlation_risk import update_correlation_clusters
from futu_quant.config import load_gamma_campaigns, upsert_campaign
from futu_quant.account_metrics import account_pnl_metrics
from futu_quant.risk import RiskLimits, ny_trading_day_start_utc
from futu_quant.option_lifecycle import execute_disposition_plans, monitor_option_lifecycle
from futu_quant.option_history import collect_option_snapshots
from futu_quant.portfolio_allocator import ensure_default_budgets
from futu_quant.stress import monitor_portfolio_stress
from futu_quant.strategy_plugins import BY_SERVICE_NAME, strategy_plugins
from futu_quant.experiments import sync_strategy_experiments
from futu_quant.combo_execution import ComboExecutionManager
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


NY = ZoneInfo("America/New_York")
LOCK_FILE = Path("data/trading-service.lock")
PID_FILE = Path("data/trading-service.pid")
STOP_FILE = Path("data/trading-service.stop")
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
    "system_health": 90,
    "earnings_calendar": 180,
    "daily_report": 60,
    "correlation_risk": 120,
    "research_data_backfill": 300,
}


def _request_stop(_signum=None, _frame=None) -> None:
    STOP_EVENT.set()
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()


def due(store: TradingStore, key: str, interval: int, now: datetime) -> bool:
    raw = store.get_setting(key)
    if not raw:
        return True
    return (now - datetime.fromisoformat(raw)).total_seconds() >= interval


def mark_run(store: TradingStore, key: str, now: datetime) -> None:
    store.set_setting(key, now.isoformat())


def run_task(
    store: TradingStore, alerts: AlertManager, strategy_id: str, callback,
    timeout_seconds: float | None = None,
) -> bool:
    backoff_raw = store.get_setting(f"task_backoff_until:{strategy_id}")
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
        store.set_setting(f"task_failure_count:{strategy_id}", "0")
        store.set_setting(f"task_backoff_until:{strategy_id}", "")
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
    except Exception as exc:
        duration = time.monotonic() - started
        store.finish_task_run(run_id, "ERROR", duration, str(exc))
        failures = int(store.get_setting(f"task_failure_count:{strategy_id}", "0") or 0) + 1
        delay = min(300, 5 * (2 ** min(failures - 1, 6)))
        store.set_setting(f"task_failure_count:{strategy_id}", str(failures))
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


def service_cycle(settings: Settings, store: TradingStore) -> None:
    now = datetime.now(NY)
    session = MARKET_CALENDAR.session(now)
    alerts = AlertManager(store)
    broker = FutuPaperBroker(settings)
    executor = ExecutionManager(broker, store)
    if store.get_setting("order_push_connected", "false") == "true":
        store.heartbeat("order_push", "HEALTHY", "推送通道待命，轮询兜底正常", os.getpid())
    run_task(store, alerts, "shadow_matching", lambda: match_shadow_orders(store, broker))
    if due(store, "last_run:equity_snapshot", 60, now):
        try:
            account = broker.account_info()
            positions = broker.positions()
            if not account.empty:
                row = account.iloc[0]
                store.record_equity(
                    float(row.get("total_assets", 0) or 0), float(row.get("cash", 0) or 0),
                    float(row.get("market_val", row.get("securities_assets", 0)) or 0),
                )
                position_loss = -float(account_pnl_metrics(positions)["today_position_pnl"])
                day_start = ny_trading_day_start_utc(now).isoformat()
                before = store.latest_equity_before(day_start)
                today_rows = store.equity_since(day_start)
                baseline_assets = float(
                    before["total_assets"] if before else
                    today_rows[0]["total_assets"] if today_rows else row.get("total_assets", 0)
                )
                equity_loss = baseline_assets - float(row.get("total_assets", 0) or 0)
                loss = max(0.0, position_loss, equity_loss)
                limit = RiskLimits.load().max_daily_loss
                if session.regular_open(now) and loss >= limit and not store.kill_switch():
                    store.set_setting("kill_switch", "true")
                    store.audit("DAILY_LOSS_KILL_SWITCH", {
                        "loss": loss, "position_loss": position_loss,
                        "equity_loss": equity_loss, "baseline_assets": baseline_assets,
                        "limit": limit, "trading_date_ny": now.date().isoformat(),
                    })
                    alerts.send(
                        "ERROR", "DAILY_LOSS_KILL_SWITCH",
                        f"当日持仓亏损 ${loss:,.2f} 已达到急停线 ${limit:,.2f}，已禁止新增风险",
                    )
            mark_run(store, "last_run:equity_snapshot", now)
            alerts.recover("EQUITY_SNAPSHOT_FAILED", "账户权益快照已恢复")
        except Exception as exc:
            alerts.send("ERROR", "EQUITY_SNAPSHOT_FAILED", str(exc))
    connected = run_task(store, alerts, "order_reconciliation", executor.reconcile)
    store.set_setting("connectivity_degraded", str(not connected).lower())
    run_task(
        store, alerts, "option_combo_monitor",
        lambda: ComboExecutionManager(broker, store, executor).monitor_all(),
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
        def stress_check():
            result = monitor_portfolio_stress(broker, store)
            if result.empty:
                return
            worst = result.iloc[0]
            threshold = float(os.getenv("PORTFOLIO_STRESS_ALERT_PCT", "0.10"))
            if float(worst["equity_loss_ratio"]) >= threshold:
                alerts.send(
                    "ERROR", "PORTFOLIO_STRESS_BREACH",
                    f"组合压力场景“{worst['scenario']}”估算损失 "
                    f"{float(worst['equity_loss_ratio']):.1%}，超过 {threshold:.1%}",
                    incident_key="PORTFOLIO_STRESS_BREACH",
                )
            else:
                alerts.recover(
                    "PORTFOLIO_STRESS_BREACH", "组合压力损失已回到警戒线以内",
                    incident_key="PORTFOLIO_STRESS_BREACH",
                )
        run_task(store, alerts, "portfolio_stress", stress_check)
        mark_run(store, "last_run:portfolio_stress", now)
    elif not session.regular_open(now):
        store.heartbeat("portfolio_stress", "HEALTHY", "等待常规交易时段更新压力矩阵", os.getpid())
    if due(store, "last_run:ledger_reconciliation", 60, now):
        def ledger_check():
            report = reconcile_ledgers(store, broker)
            mismatches = report[report["status"] == "MISMATCH"] if not report.empty else report
            store.set_setting("recovery_blocked", str(not mismatches.empty).lower())
            if not mismatches.empty:
                alerts.send(
                    "ERROR", "LEDGER_MISMATCH",
                    f"三账对账发现 {len(mismatches)} 个标的差异，相关策略已暂停",
                )
        run_task(store, alerts, "ledger_reconciliation", ledger_check)
        mark_run(store, "last_run:ledger_reconciliation", now)

    configured_env = {
        value.strip() for value in os.getenv(
            "SERVICE_STRATEGIES", "Core Satellite,Earnings Gamma"
        ).split(",") if value.strip()
    }
    known_strategies = {plugin.service_name for plugin in strategy_plugins(scheduled_only=True)}
    paused = store.get_setting("strategies_paused", "false") == "true"
    configured = {
        name for name in known_strategies
        if store.get_setting(
            f"strategy_enabled:{name}", "true" if name in configured_env else "false"
        ) == "true"
    }
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
        interval = max(10, int(os.getenv("GAMMA_INTERVAL_SECONDS", "30")))
        if due(store, "last_run:volatility_long_gamma", interval, now):
            run_task(
                store, alerts, "volatility_long_gamma",
                lambda: run_volatility_once(reconcile=False),
            )
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
                ),
            )
            mark_run(store, key, now)

    if "Core Satellite" in configured and session.pre_close(now, 15):
        key = "last_run:core_satellite"
        if due(store, key, 20 * 60 * 60, now):
            run_task(
                store, alerts, "core_satellite",
                lambda: run_strategy_once(broker, "Core Satellite", executor),
            )
            mark_run(store, key, now)
    elif "Core Satellite" in configured:
        store.heartbeat("core_satellite", "HEALTHY", "等待收盘前 15 分钟调仓窗口", os.getpid())
    store.heartbeat("trading_service", "HEALTHY", f"调度循环完成 · {now:%F %T %Z}", os.getpid())
    if due(store, "last_run:maintenance", 300, now):
        run_task(store, alerts, "system_health", lambda: maintenance(store, broker))
        mark_run(store, "last_run:maintenance", now)
    if (due(store, "last_success:earnings_calendar", 20 * 60 * 60, now)
            and due(store, "last_attempt:earnings_calendar", 10 * 60, now)):
        def refresh_calendar():
            count = refresh_earnings_events(store, settings.gamma_symbols, broker=broker)
            campaigns, updated = sync_campaign_events(store, load_gamma_campaigns())
            if updated:
                for campaign in campaigns:
                    upsert_campaign("GAMMA_CAMPAIGNS", campaign)
            store.audit("EARNINGS_CALENDAR_REFRESHED", {"events": count, "campaigns_updated": updated})
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
            store.audit("RESEARCH_DATA_BACKFILL", result)
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
            store.operational_event("storage", "STORAGE_CAPACITY", report["status"], report)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Futu paper strategy service")
    parser.add_argument("--once", action="store_true", help="Run one scheduling cycle")
    parser.add_argument("--poll", type=int, default=10, help="Scheduler polling seconds")
    args = parser.parse_args()
    settings = Settings.load(refresh=True)
    if not settings.enable_paper_orders:
        raise RuntimeError("ENABLE_PAPER_ORDERS=false; automatic paper execution is disabled")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("交易服务已经在运行，拒绝启动第二个实例") from exc
        lock.write(str(os.getpid()))
        lock.flush()
        PID_FILE.write_text(str(os.getpid()))
        store = TradingStore()
        ensure_default_budgets(store)
        sync_strategy_experiments(store, settings)
        store.operational_event("trading_service", "PROCESS_START", "STARTING", {"pid": os.getpid()})
        push_context = None
        try:
            try:
                push_context = open_order_push(FutuPaperBroker(settings), store)
            except Exception as exc:
                store.set_setting("order_push_connected", "false")
                store.heartbeat("order_push", "ERROR", f"推送连接失败，已启用轮询兜底：{exc}", os.getpid())
            try:
                broker = FutuPaperBroker(settings)
                startup_recovery(store, broker, ExecutionManager(broker, store))
            except Exception as exc:
                store.set_setting("connectivity_degraded", "true")
                store.set_setting("recovery_blocked", "true")
                store.operational_event("recovery", "STARTUP_RECOVERY", "ERROR", {"error": str(exc)})
            while not STOP_EVENT.is_set():
                if push_context is None:
                    try:
                        push_context = open_order_push(FutuPaperBroker(settings), store)
                    except Exception as exc:
                        store.set_setting("order_push_connected", "false")
                        store.heartbeat("order_push", "ERROR", f"等待自动重连：{exc}", os.getpid())
                try:
                    settings = Settings.load(refresh=True)
                    service_cycle(settings, store)
                except Exception as exc:
                    store.heartbeat("trading_service", "ERROR", str(exc), os.getpid())
                    AlertManager(store).send("ERROR", "SERVICE_CYCLE_ERROR", str(exc))
                if args.once or STOP_EVENT.is_set():
                    break
                STOP_EVENT.wait(max(5, args.poll))
        finally:
            if push_context is not None:
                push_context.close()
            store.set_setting("order_push_connected", "false")
            store.heartbeat("trading_service", "STOPPED", "服务已停止", os.getpid())
            store.operational_event("trading_service", "PROCESS_STOP", "STOPPED", {"pid": os.getpid()})
            PID_FILE.unlink(missing_ok=True)
            STOP_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
