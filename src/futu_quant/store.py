from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import math
import sqlite3
import time
from typing import Any, Iterator


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "trading.db"


def _json_safe(value, active: set[int] | None = None):
    """Return a JSON-compatible copy and cut accidental object cycles."""
    active = active if active is not None else set()
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            return "<循环引用>"
        active.add(identity)
        result = {str(key): _json_safe(item, active) for key, item in value.items()}
        active.remove(identity)
        return result
    if isinstance(value, (list, tuple, set)):
        identity = id(value)
        if identity in active:
            return "<循环引用>"
        active.add(identity)
        result = [_json_safe(item, active) for item in value]
        active.remove(identity)
        return result
    return value


def _json_dumps(value) -> str:
    return json.dumps(
        _json_safe(value), separators=(",", ":"), ensure_ascii=False, default=str,
    )


class TradingStore:
    def __init__(self, path: Path | str = DEFAULT_DB, tenant_registry: dict[str, Any] | None = None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tenant_registry = tenant_registry
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA cache_size=-64000")  # 64MB 高速页面缓存
        connection.execute("PRAGMA temp_store=MEMORY")  # 临时表与排序运算全部内存化
        connection.execute("PRAGMA mmap_size=268435456")  # 256MB 内存映射 I/O
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
            ).fetchone()
            if existing is not None:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS candidate_universe (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        week_start TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        underlying_name TEXT NOT NULL DEFAULT '',
                        industry TEXT NOT NULL DEFAULT 'OTHER',
                        option_daily_volume REAL NOT NULL,
                        open_interest REAL NOT NULL,
                        stock_turnover_usd REAL NOT NULL,
                        median_spread_pct REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'ACTIVE',
                        created_at TEXT NOT NULL,
                        UNIQUE(week_start, symbol)
                    );
                    CREATE INDEX IF NOT EXISTS idx_candidate_lookup ON candidate_universe(week_start, status);
                    CREATE TABLE IF NOT EXISTS shadow_incubator (
                        symbol TEXT PRIMARY KEY,
                        first_observed_at TEXT NOT NULL,
                        observation_days INTEGER NOT NULL DEFAULT 0,
                        signal_triggers INTEGER NOT NULL DEFAULT 0,
                        simulated_wins INTEGER NOT NULL DEFAULT 0,
                        simulated_losses INTEGER NOT NULL DEFAULT 0,
                        simulated_pnl_usd REAL NOT NULL DEFAULT 0.0,
                        simulated_max_dd REAL NOT NULL DEFAULT 0.0,
                        avg_slippage_pct REAL NOT NULL DEFAULT 0.0,
                        volume_retention_ratio REAL NOT NULL DEFAULT 0.0,
                        latest_ml_score REAL NOT NULL DEFAULT 0.0,
                        promotion_eligible INTEGER NOT NULL DEFAULT 0,
                        track_mode TEXT NOT NULL DEFAULT 'STANDARD',
                        last_evaluated_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_incubator_score ON shadow_incubator(promotion_eligible, latest_ml_score DESC);
                    CREATE TABLE IF NOT EXISTS promoted_universe (
                        symbol TEXT PRIMARY KEY,
                        promoted_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        promotion_score REAL NOT NULL,
                        industry TEXT NOT NULL DEFAULT 'OTHER',
                        live_trades_count INTEGER NOT NULL DEFAULT 0,
                        live_realized_pnl REAL NOT NULL DEFAULT 0.0,
                        consecutive_hard_stops INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'ACTIVE',
                        eviction_reason TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_promoted_active ON promoted_universe(status);
                    CREATE TABLE IF NOT EXISTS universe_lifecycle_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    """
                )
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")}
                if "tenant_id" not in columns:
                    connection.execute("ALTER TABLE order_intents ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
                    connection.execute("CREATE INDEX IF NOT EXISTS idx_intents_tenant ON order_intents(tenant_id, strategy_id, created_at DESC)")
                # 存量意图按尾段模式和状态精确派生回填，严禁残留空白或哑数据
                connection.execute(
                    """UPDATE order_intents SET tenant_id = 'futu_real_master'
                    WHERE (intent_key LIKE '%:REAL' OR status LIKE 'REAL%')
                    AND (tenant_id IS NULL OR tenant_id = '')"""
                )
                connection.execute(
                    """UPDATE order_intents SET tenant_id = 'futu_paper_sandbox'
                    WHERE (tenant_id IS NULL OR tenant_id = '')"""
                )
                v_cols = {row["name"] for row in connection.execute("PRAGMA table_info(virtual_allocations)")}
                if "tenant_id" not in v_cols:
                    connection.execute("ALTER TABLE virtual_allocations ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
                    connection.execute("CREATE INDEX IF NOT EXISTS idx_virtual_alloc_tenant ON virtual_allocations(tenant_id, strategy_id, symbol)")
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    intent_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    broker_order_id TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(strategy_id, intent_key)
                );
                CREATE INDEX IF NOT EXISTS idx_intents_broker_order
                    ON order_intents(broker_order_id);
                CREATE TABLE IF NOT EXISTS order_state_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL,
                    previous_status TEXT,
                    new_status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    broker_order_id TEXT,
                    filled_quantity REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES order_intents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_state_events_intent
                    ON order_state_events(intent_id,id);
                CREATE TABLE IF NOT EXISTS option_lifecycle_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    expiration TEXT NOT NULL,
                    strike REAL NOT NULL,
                    quantity REAL NOT NULL,
                    underlying_price REAL NOT NULL DEFAULT 0,
                    intrinsic_value REAL NOT NULL DEFAULT 0,
                    days_to_expiry INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    projected_cash_need REAL NOT NULL DEFAULT 0,
                    projected_share_need REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS option_disposition_plans (
                    code TEXT PRIMARY KEY,
                    action TEXT NOT NULL DEFAULT 'MONITOR',
                    close_days_before INTEGER NOT NULL DEFAULT 0,
                    close_window_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_risk_budgets (
                    strategy_id TEXT PRIMARY KEY,
                    capital_ratio REAL NOT NULL,
                    max_loss_ratio REAL NOT NULL,
                    option_premium_ratio REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_stress_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity REAL NOT NULL,
                    worst_loss REAL NOT NULL,
                    worst_loss_ratio REAL NOT NULL,
                    scenarios_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    version_hash TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    universe_json TEXT NOT NULL DEFAULT '[]',
                    data_version TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'REGISTERED',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    promoted_at TEXT,
                    UNIQUE(strategy_id,version_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_experiments_active
                    ON strategy_experiments(strategy_id,is_active);
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    strategy_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_heartbeats (
                    strategy_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    pid INTEGER,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    duration_seconds REAL,
                    message TEXT NOT NULL DEFAULT '',
                    pid INTEGER,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_task_started
                    ON task_runs(task_id,started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_runs_id_desc
                    ON task_runs(id DESC);
                CREATE TABLE IF NOT EXISTS option_combos (
                    combo_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    structure TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'DRAFT',
                    max_net_debit REAL NOT NULL DEFAULT 0,
                    min_net_credit REAL NOT NULL DEFAULT 0,
                    assembly_timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    naked_timeout_seconds INTEGER NOT NULL DEFAULT 15,
                    auto_rollback INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    parent_combo_id TEXT,
                    requires_parent_closed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS option_combo_legs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    combo_id TEXT NOT NULL,
                    leg_order INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PLANNED',
                    intent_id INTEGER,
                    close_intent_id INTEGER,
                    close_attempts INTEGER NOT NULL DEFAULT 0,
                    filled_quantity REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(combo_id,leg_order),
                    FOREIGN KEY(combo_id) REFERENCES option_combos(combo_id),
                    FOREIGN KEY(intent_id) REFERENCES order_intents(id),
                    FOREIGN KEY(close_intent_id) REFERENCES order_intents(id)
                );
                CREATE TABLE IF NOT EXISTS option_combo_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    combo_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(combo_id) REFERENCES option_combos(combo_id)
                );
                CREATE TABLE IF NOT EXISTS combo_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    combo_id TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    quality_score REAL NOT NULL,
                    quality_grade TEXT NOT NULL,
                    mid_net_debit REAL,
                    executable_net_debit REAL,
                    package_spread REAL,
                    net_delta REAL,net_gamma REAL,net_theta REAL,net_vega REAL,
                    capacity INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(combo_id) REFERENCES option_combos(combo_id)
                );
                CREATE INDEX IF NOT EXISTS idx_combo_snapshots_combo_time
                    ON combo_market_snapshots(combo_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS calibration_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calibration_type TEXT NOT NULL,
                    strategy_id TEXT,
                    status TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    recommendation TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS validation_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    markdown TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(period_type,period_key)
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_lineage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,dataset_type TEXT NOT NULL,
                    dataset_key TEXT NOT NULL,path TEXT NOT NULL,source TEXT NOT NULL,
                    row_count INTEGER NOT NULL,quality_grade TEXT NOT NULL,
                    content_hash TEXT NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_points (
                    recovery_id TEXT PRIMARY KEY,manifest_path TEXT NOT NULL,
                    database_hash TEXT NOT NULL,config_hash TEXT,data_hash TEXT,
                    status TEXT NOT NULL,verified_at TEXT,created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,config_key TEXT NOT NULL,
                    base_version INTEGER NOT NULL,draft_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'DRAFT',diff_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,approved_by TEXT,reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,published_at TEXT
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    severity TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    total_assets REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_logs_lookup
                    ON decision_logs(strategy_id, symbol, id DESC);
                CREATE TABLE IF NOT EXISTS virtual_allocations (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    average_cost REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS reconciliation_adjustments (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS strategy_approvals (
                    strategy_id TEXT PRIMARY KEY,
                    version_hash TEXT NOT NULL,
                    backtest_passed INTEGER NOT NULL,
                    shadow_passed INTEGER NOT NULL,
                    drill_passed INTEGER NOT NULL,
                    risk_confirmed INTEGER NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    broker_quantity REAL NOT NULL,
                    ledger_quantity REAL NOT NULL,
                    difference REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES order_intents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_fills_intent ON shadow_fills(intent_id);
                CREATE TABLE IF NOT EXISTS fill_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    multiplier REAL NOT NULL DEFAULT 1,
                    commission REAL NOT NULL DEFAULT 0,
                    regulatory_fee REAL NOT NULL DEFAULT 0,
                    fee_total REAL NOT NULL DEFAULT 0,
                    bid_price REAL,
                    ask_price REAL,
                    mid_price REAL,
                    reference_price REAL,
                    spread_cost REAL NOT NULL DEFAULT 0,
                    price_impact_cost REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    broker_deal_id TEXT,
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    filled_at TEXT NOT NULL,
                    UNIQUE(source, broker_deal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fill_ledger_time ON fill_ledger(filled_at);
                CREATE INDEX IF NOT EXISTS idx_fill_ledger_strategy_time ON fill_ledger(strategy_id,filled_at);
                CREATE TABLE IF NOT EXISTS broker_snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_broker_snapshots_time ON broker_snapshots(captured_at);
                CREATE TABLE IF NOT EXISTS acceptance_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    value REAL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS earnings_events (
                    symbol TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    session TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(symbol,event_date)
                );
                CREATE TABLE IF NOT EXISTS operational_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,event_type TEXT NOT NULL,status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'system',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parameter TEXT NOT NULL,current_value REAL NOT NULL,recommended_value REAL NOT NULL,
                    reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS validation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,symbol TEXT NOT NULL,passed INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,windows_json TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS option_history (
                    code TEXT NOT NULL,time_key TEXT NOT NULL,underlying TEXT,
                    underlying_price REAL,bid REAL,ask REAL,iv REAL,
                    delta REAL,gamma REAL,theta REAL,vega REAL,rho REAL,
                    volume REAL,open_interest REAL,source TEXT NOT NULL DEFAULT 'UNKNOWN',
                    fetched_at TEXT,quality_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    quality_reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(code,time_key)
                );
                CREATE TABLE IF NOT EXISTS runtime_config (
                    config_key TEXT PRIMARY KEY,value_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_state (
                    state_key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_key TEXT PRIMARY KEY,severity TEXT NOT NULL,status TEXT NOT NULL,
                    message TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,recovered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS candidate_universe (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    underlying_name TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT 'OTHER',
                    option_daily_volume REAL NOT NULL,
                    open_interest REAL NOT NULL,
                    stock_turnover_usd REAL NOT NULL,
                    median_spread_pct REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    UNIQUE(week_start, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_lookup ON candidate_universe(week_start, status);
                CREATE TABLE IF NOT EXISTS shadow_incubator (
                    symbol TEXT PRIMARY KEY,
                    first_observed_at TEXT NOT NULL,
                    observation_days INTEGER NOT NULL DEFAULT 0,
                    signal_triggers INTEGER NOT NULL DEFAULT 0,
                    simulated_wins INTEGER NOT NULL DEFAULT 0,
                    simulated_losses INTEGER NOT NULL DEFAULT 0,
                    simulated_pnl_usd REAL NOT NULL DEFAULT 0.0,
                    simulated_max_dd REAL NOT NULL DEFAULT 0.0,
                    avg_slippage_pct REAL NOT NULL DEFAULT 0.0,
                    volume_retention_ratio REAL NOT NULL DEFAULT 0.0,
                    latest_ml_score REAL NOT NULL DEFAULT 0.0,
                    promotion_eligible INTEGER NOT NULL DEFAULT 0,
                    track_mode TEXT NOT NULL DEFAULT 'STANDARD',
                    last_evaluated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_incubator_score ON shadow_incubator(promotion_eligible, latest_ml_score DESC);
                CREATE TABLE IF NOT EXISTS promoted_universe (
                    symbol TEXT PRIMARY KEY,
                    promoted_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    promotion_score REAL NOT NULL,
                    industry TEXT NOT NULL DEFAULT 'OTHER',
                    live_trades_count INTEGER NOT NULL DEFAULT 0,
                    live_realized_pnl REAL NOT NULL DEFAULT 0.0,
                    consecutive_hard_stops INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    eviction_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_promoted_active ON promoted_universe(status);
                CREATE TABLE IF NOT EXISTS universe_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")}
            for name, definition in {
                "filled_quantity": "REAL NOT NULL DEFAULT 0",
                "average_fill_price": "REAL NOT NULL DEFAULT 0",
                "last_broker_update": "TEXT",
                "reprice_count": "INTEGER NOT NULL DEFAULT 0",
                "last_reprice_at": "TEXT",
                "fees": "REAL NOT NULL DEFAULT 0",
                "tenant_id": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE order_intents ADD COLUMN {name} {definition}")
            v_cols = {row["name"] for row in connection.execute("PRAGMA table_info(virtual_allocations)")}
            if "tenant_id" not in v_cols:
                connection.execute("ALTER TABLE virtual_allocations ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_virtual_alloc_tenant ON virtual_allocations(tenant_id, strategy_id, symbol)")
            # 存量意图按尾段模式和状态精确派生回填，严禁残留空白或哑数据
            connection.execute(
                """UPDATE order_intents SET tenant_id = 'futu_real_master'
                WHERE (intent_key LIKE '%:REAL' OR status LIKE 'REAL%')
                AND (tenant_id IS NULL OR tenant_id = '')"""
            )
            connection.execute(
                """UPDATE order_intents SET tenant_id = 'futu_paper_sandbox'
                WHERE (tenant_id IS NULL OR tenant_id = '')"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_intents_tenant ON order_intents(tenant_id, strategy_id, created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_decision_logs_id_desc ON decision_logs(id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_decision_strategy_id ON decision_logs(strategy_id, id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_decision_symbol_id ON decision_logs(symbol, id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_decision_created ON decision_logs(created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_intents_status ON order_intents(status, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_intents_strategy ON order_intents(strategy_id, created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_fill_ledger_symbol ON fill_ledger(symbol, filled_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_option_history_code_time ON option_history(code, time_key)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_option_history_underlying ON option_history(underlying, time_key)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_operational_events_id_desc ON operational_events(id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_operational_events_component ON operational_events(component, id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_alerts_id_desc ON alerts(id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_equity_snapshots_id_desc ON equity_snapshots(id DESC)")
            connection.execute(
                """INSERT INTO fill_ledger
                (intent_id,strategy_id,symbol,side,quantity,price,multiplier,fee_total,
                 reference_price,source,quote_json,filled_at)
                SELECT f.intent_id,i.strategy_id,i.symbol,i.side,f.quantity,f.price,
                CASE WHEN i.symbol GLOB 'US.*[0-9][CP][0-9]*' THEN 100 ELSE 1 END,
                f.fees,json_extract(i.metadata_json,'$.reference_price'),'SHADOW',f.quote_json,f.created_at
                FROM shadow_fills f JOIN order_intents i ON i.id=f.intent_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM fill_ledger l WHERE l.source='SHADOW' AND l.intent_id=f.intent_id
                    AND l.quantity=f.quantity AND l.price=f.price AND l.filled_at=f.created_at
                )"""
            )
            connection.execute(
                """INSERT INTO fill_ledger
                (intent_id,strategy_id,symbol,side,quantity,price,multiplier,fee_total,
                 reference_price,source,quote_json,filled_at)
                SELECT intent.id,intent.strategy_id,intent.symbol,intent.side,intent.filled_quantity,
                intent.average_fill_price,
                CASE WHEN intent.symbol GLOB 'US.*[0-9][CP][0-9]*' THEN 100 ELSE 1 END,
                intent.fees,json_extract(intent.metadata_json,'$.reference_price'),
                'LEGACY_INTENT_AGGREGATE','{}',intent.updated_at
                FROM order_intents intent
                WHERE intent.filled_quantity > 0
                AND NOT EXISTS (SELECT 1 FROM fill_ledger l WHERE l.intent_id=intent.id)"""
            )
            connection.execute(
                """INSERT INTO order_state_events
                (intent_id,previous_status,new_status,source,broker_order_id,filled_quantity,
                average_fill_price,details_json,created_at)
                SELECT intent.id,NULL,intent.status,'MIGRATION',intent.broker_order_id,
                intent.filled_quantity,intent.average_fill_price,'{}',intent.updated_at
                FROM order_intents intent
                WHERE NOT EXISTS (
                    SELECT 1 FROM order_state_events event WHERE event.intent_id=intent.id
                )"""
            )
            alert_columns = {row["name"] for row in connection.execute("PRAGMA table_info(alerts)")}
            for name, definition in {
                "acknowledged": "INTEGER NOT NULL DEFAULT 0",
                "action_status": "TEXT NOT NULL DEFAULT 'OPEN'",
            }.items():
                if name not in alert_columns:
                    connection.execute(f"ALTER TABLE alerts ADD COLUMN {name} {definition}")
            option_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(option_history)")
            }
            for name, definition in {
                "vega": "REAL", "rho": "REAL",
                "source": "TEXT NOT NULL DEFAULT 'UNKNOWN'", "fetched_at": "TEXT",
                "quality_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
                "quality_reason": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in option_columns:
                    connection.execute(f"ALTER TABLE option_history ADD COLUMN {name} {definition}")
            combo_leg_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(option_combo_legs)")
            }
            if combo_leg_columns and "close_attempts" not in combo_leg_columns:
                connection.execute(
                    "ALTER TABLE option_combo_legs ADD COLUMN close_attempts INTEGER NOT NULL DEFAULT 0"
                )
            combo_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(option_combos)")
            }
            for name, definition in {
                "parent_combo_id": "TEXT", "requires_parent_closed": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if combo_columns and name not in combo_columns:
                    connection.execute(f"ALTER TABLE option_combos ADD COLUMN {name} {definition}")
            incubator_cols = {row["name"] for row in connection.execute("PRAGMA table_info(shadow_incubator)")}
            if incubator_cols and "track_mode" not in incubator_cols:
                connection.execute("ALTER TABLE shadow_incubator ADD COLUMN track_mode TEXT NOT NULL DEFAULT 'STANDARD'")
            connection.execute(
                "INSERT OR IGNORE INTO system_settings(key,value,updated_at) VALUES(?,?,?)",
                ("kill_switch", "false", self.now()),
            )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version,name,checksum,applied_at)
                VALUES(1,'baseline_sqlite_schema','baseline-v1',?)""", (self.now(),),
            )
            from .migrations import apply_migrations
            apply_migrations(connection)

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_intent(
        self, strategy_id: str, intent_key: str, symbol: str, side: str,
        quantity: int, limit_price: float, metadata: dict | None = None,
        tenant_id: str | None = None,
    ) -> tuple[dict, bool]:
        now = self.now()
        effective_tenant = tenant_id or (metadata or {}).get("tenant_id", "")
        try:
            with self.connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO order_intents
                    (strategy_id,intent_key,symbol,side,quantity,limit_price,metadata_json,created_at,updated_at,tenant_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (strategy_id, intent_key, symbol, side, quantity, limit_price,
                     json.dumps(metadata or {}, separators=(",", ":")), now, now, str(effective_tenant or "")),
                )
                row = connection.execute(
                    "SELECT * FROM order_intents WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                connection.execute(
                    """INSERT INTO order_state_events
                    (intent_id,previous_status,new_status,source,details_json,created_at)
                    VALUES(?,NULL,'CREATED','STRATEGY','{}',?)""",
                    (cursor.lastrowid, now),
                )
                return dict(row), True
        except sqlite3.IntegrityError:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM order_intents WHERE strategy_id=? AND intent_key=?",
                    (strategy_id, intent_key),
                ).fetchone()
                retryable_rejection = row and row["status"] == "REJECTED" and any(
                    marker in str(row["error"] or "") for marker in (
                        "启动恢复发现三账差异",
                        "OpenD/券商连接异常",
                        "连接降级",
                        "行情数据暂不可用",
                        "账户日内权益损失达到急停线",
                        "账户亏损已达到",
                    )
                )
                can_retry = row and not row["broker_order_id"] and (
                    row["status"] == "FAILED" or retryable_rejection
                )
                if can_retry:
                    connection.execute(
                        """UPDATE order_intents SET status='CREATED',quantity=?,limit_price=?,
                        metadata_json=?,error=NULL,updated_at=? WHERE id=?""",
                        (quantity, limit_price, json.dumps(metadata or {}, separators=(",", ":")),
                         now, row["id"]),
                    )
                    retried = connection.execute(
                        "SELECT * FROM order_intents WHERE id=?", (row["id"],)
                    ).fetchone()
                    return dict(retried), True
                return dict(row), False

    def update_intent(
        self, intent_id: int, status: str, broker_order_id: str | None = None,
        error: str | None = None, source: str = "EXECUTION",
    ) -> None:
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT status,broker_order_id,filled_quantity,average_fill_price FROM order_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if previous is None:
                return
            # Late submit/cancel acknowledgements must not revive a filled order.
            terminal = {"FILLED", "SHADOW_FILLED", "CANCELLED", "CANCELLED_ALL",
                        "CANCELLED_PART", "REJECTED", "FAILED", "SHADOW_EXPIRED"}
            if previous["status"] in terminal and status != previous["status"]:
                return
            if previous["status"] in {"FILLED_PART", "PARTIAL_FILLED", "CANCEL_REQUESTED"} and status in {
                "CREATED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED"
            }:
                return
            if previous["status"] == "CANCEL_REQUESTED" and status in {"FILLED_PART", "PARTIAL_FILLED"}:
                return
            connection.execute(
                """UPDATE order_intents SET status=?, broker_order_id=COALESCE(?,broker_order_id),
                error=?, updated_at=? WHERE id=?""",
                (status, broker_order_id, error, self.now(), intent_id),
            )
            if previous["status"] != status or (broker_order_id and previous["broker_order_id"] != broker_order_id):
                connection.execute(
                    """INSERT INTO order_state_events
                    (intent_id,previous_status,new_status,source,broker_order_id,filled_quantity,
                    average_fill_price,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (intent_id, previous["status"], status, source,
                     broker_order_id or previous["broker_order_id"], previous["filled_quantity"],
                     previous["average_fill_price"], json.dumps({"error": error} if error else {}),
                     self.now()),
                )

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, self.now()),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        with self.connection() as connection:
            return {row["key"]: row["value"] for row in connection.execute(
                "SELECT key,value FROM system_settings WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()}

    def get_runtime_config(self, key: str):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value_json,version FROM runtime_config WHERE config_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"]), int(row["version"])

    def set_runtime_config(self, key: str, value, actor: str = "system") -> int:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM runtime_config WHERE config_key=?", (key,)
            ).fetchone()
            version = int(row["version"]) + 1 if row else 1
            connection.execute(
                """INSERT INTO runtime_config(config_key,value_json,version,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(config_key) DO UPDATE SET
                value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at""",
                (key, payload, version, self.now()),
            )
        self.record_config_version(key, value, actor)
        return version

    def mutate_runtime_config(self, key: str, default, updater, actor: str = "system"):
        """Atomically read-modify-write one versioned runtime configuration value."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value_json,version FROM runtime_config WHERE config_key=?", (key,)
            ).fetchone()
            current = json.loads(row["value_json"]) if row else default
            value = updater(current)
            version = int(row["version"]) + 1 if row else 1
            connection.execute(
                """INSERT INTO runtime_config(config_key,value_json,version,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(config_key) DO UPDATE SET
                value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at""",
                (key, json.dumps(value, separators=(",", ":"), default=str), version, self.now()),
            )
        self.record_config_version(key, value, actor)
        return value, version

    def campaign_states(self) -> dict:
        with self.connection() as connection:
            return {row["state_key"]: json.loads(row["value_json"]) for row in connection.execute(
                "SELECT state_key,value_json FROM campaign_state"
            ).fetchall()}

    def save_campaign_states(self, states: dict) -> None:
        with self.connection() as connection:
            for key, value in states.items():
                connection.execute(
                    """INSERT INTO campaign_state(state_key,value_json,updated_at) VALUES(?,?,?)
                    ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=excluded.updated_at""",
                    (key, json.dumps(value, separators=(",", ":"), default=str), self.now()),
                )

    def delete_campaign_states(self, keys: tuple[str, ...] | list[str]) -> int:
        unique_keys = tuple(dict.fromkeys(str(key) for key in keys if key))
        if not unique_keys:
            return 0
        with self.connection() as connection:
            before = connection.total_changes
            connection.executemany(
                "DELETE FROM campaign_state WHERE state_key=?",
                [(key,) for key in unique_keys],
            )
            return connection.total_changes - before

    def prune_campaign_states(self, prefix: str, active_keys: set[str]) -> int:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT state_key FROM campaign_state WHERE state_key LIKE ?", (f"{prefix}%",)
            ).fetchall()
            stale = [row["state_key"] for row in rows if row["state_key"] not in active_keys]
            connection.executemany("DELETE FROM campaign_state WHERE state_key=?", [(key,) for key in stale])
        return len(stale)

    def record_incident(self, key: str, severity: str, message: str) -> tuple[bool, int]:
        now = self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status,count FROM incidents WHERE incident_key=?", (key,)
            ).fetchone()
            opened = row is None or row["status"] == "RECOVERED"
            if row is None:
                connection.execute(
                    "INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)",
                    (key, severity, "OPEN", message, 1, now, now, None),
                )
                count = 1
            else:
                count = int(row["count"]) + 1
                connection.execute(
                    """UPDATE incidents SET severity=?,status='OPEN',message=?,count=?,
                    last_seen=?,recovered_at=NULL WHERE incident_key=?""",
                    (severity, message, count, now, key),
                )
        return opened, count

    def recover_incident(self, key: str) -> bool:
        now = self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM incidents WHERE incident_key=?", (key,)
            ).fetchone()
            if not row or row["status"] != "OPEN":
                return False
            connection.execute(
                "UPDATE incidents SET status='RECOVERED',recovered_at=?,last_seen=? WHERE incident_key=?",
                (now, now, key),
            )
        return True

    def recover_all_incidents(self) -> int:
        now = self.now()
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE incidents SET status='RECOVERED',recovered_at=?,last_seen=? WHERE status='OPEN'",
                (now, now),
            )
            return cursor.rowcount

    def incidents(self, status: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM incidents"
        params: list = []
        if status:
            query += " WHERE status=?"
            params.append(status.upper())
        query += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def kill_switch(self, account_id: str | int | None = None, tenant_id: str | None = None) -> bool:
        if self.get_setting("kill_switch", "false").lower() == "true":
            return True
        if tenant_id and self.get_setting(f"kill_switch:{tenant_id}", "false").lower() == "true":
            return True
        if account_id:
            return self.get_setting(f"kill_switch:{account_id}", "false").lower() == "true"
        return False

    def set_kill_switch(
        self, enabled: bool, account_id: str | int | None = None, tenant_id: str | None = None,
    ) -> None:
        val = "true" if enabled else "false"
        if tenant_id:
            self.set_setting(f"kill_switch:{tenant_id}", val)
        if account_id:
            self.set_setting(f"kill_switch:{account_id}", val)
        if not tenant_id and not account_id:
            self.set_setting("kill_switch", val)

    def get_tenant_setting(self, tenant_id: str, key: str, default: str = "") -> str:
        """获取指定租户命名空间下的配置；若租户未配置，回退到全局默认配置"""
        if tenant_id:
            val = self.get_setting(f"{key}:{tenant_id}", "")
            if val:
                return val
        return self.get_setting(key, default)

    def set_tenant_setting(self, tenant_id: str, key: str, value: str) -> None:
        """写入指定租户命名空间下的配置项"""
        target_key = f"{key}:{tenant_id}" if tenant_id else key
        self.set_setting(target_key, value)

    def audit(self, event_type: str, payload: dict, strategy_id: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO audit_events(event_type,strategy_id,payload_json,created_at) VALUES(?,?,?,?)",
                (event_type, strategy_id, _json_dumps(payload), self.now()),
            )

    def intents(self, limit: int = 500, tenant_id: str | None = None) -> list[dict]:
        with self.connection() as connection:
            if tenant_id:
                rows = connection.execute(
                    "SELECT * FROM order_intents WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM order_intents ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def intent(self, intent_id: int) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE id=?", (intent_id,)
            ).fetchone()
            return dict(row) if row else None

    def intent_by_broker_order(self, broker_order_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE broker_order_id=?", (str(broker_order_id),)
            ).fetchone()
            return dict(row) if row else None

    def submitted_notional_since(self, iso_timestamp: str, side: str | None = None) -> float:
        side_filter = " AND side=?" if side else ""
        parameters = (iso_timestamp, side) if side else (iso_timestamp,)
        with self.connection() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(quantity * limit_price *
                CASE WHEN symbol GLOB 'US.*[0-9][CP][0-9]*' THEN 100 ELSE 1 END), 0) AS total
                FROM order_intents
                WHERE created_at >= ? AND status IN ('SUBMITTED','FILLED','FILLED_PART')"""
                + side_filter,
                parameters,
            ).fetchone()
            return float(row["total"])

    def update_by_broker_order(
        self, broker_order_id: str, status: str, filled_quantity: float = 0,
        average_fill_price: float = 0, fees: float = 0, source: str = "BROKER_POLL",
    ) -> None:
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT id,status,filled_quantity,average_fill_price,fees FROM order_intents WHERE broker_order_id=?",
                (str(broker_order_id),),
            ).fetchone()
            previous_qty = float(previous["filled_quantity"] or 0) if previous else 0.0
            if previous:
                terminal = {"FILLED", "SHADOW_FILLED", "CANCELLED", "CANCELLED_ALL",
                            "CANCELLED_PART", "REJECTED", "FAILED", "SHADOW_EXPIRED"}
                earlier = {"CREATED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED"}
                if (float(filled_quantity or 0) < previous_qty
                        or previous["status"] in terminal and status != previous["status"]
                        or previous["status"] in {"FILLED_PART", "PARTIAL_FILLED", "CANCEL_REQUESTED"}
                        and status in earlier):
                    return
            connection.execute(
                """UPDATE order_intents SET status=?,filled_quantity=?,average_fill_price=?,fees=?,
                last_broker_update=?,updated_at=? WHERE broker_order_id=?""",
                (status, filled_quantity, average_fill_price, fees, self.now(), self.now(), str(broker_order_id)),
            )
            delta = float(filled_quantity or 0) - previous_qty
            if previous and delta > 0:
                intent = connection.execute("SELECT * FROM order_intents WHERE id=?", (previous["id"],)).fetchone()
                metadata = json.loads(intent["metadata_json"] or "{}")
                connection.execute(
                    """INSERT OR IGNORE INTO fill_ledger
                    (intent_id,strategy_id,symbol,side,quantity,price,multiplier,fee_total,reference_price,source,quote_json,filled_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intent["id"], intent["strategy_id"], intent["symbol"], intent["side"], delta,
                     average_fill_price, float(metadata.get("multiplier", 100 if "C" in intent["symbol"] or "P" in intent["symbol"] else 1)),
                     max(0.0, float(fees or 0) - float(previous["fees"] or 0)),
                     metadata.get("reference_price"), "BROKER_POLL_DIFF", "{}", self.now()),
                )
            if previous and (
                previous["status"] != status
                or float(previous["filled_quantity"]) != float(filled_quantity)
                or float(previous["average_fill_price"]) != float(average_fill_price)
            ):
                connection.execute(
                    """INSERT INTO order_state_events
                    (intent_id,previous_status,new_status,source,broker_order_id,filled_quantity,
                    average_fill_price,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (previous["id"], previous["status"], status, source, str(broker_order_id),
                     filled_quantity, average_fill_price, "{}", self.now()),
                )

    def order_state_events(self, limit: int = 2000) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT event.*,intent.strategy_id,intent.symbol,intent.side,intent.quantity
                FROM order_state_events event JOIN order_intents intent ON intent.id=event.intent_id
                ORDER BY event.id DESC LIMIT ?""", (limit,)
            ).fetchall()]

    def record_option_lifecycle(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        now = self.now()
        with self.connection() as connection:
            connection.executemany(
                """INSERT INTO option_lifecycle_snapshots
                (code,underlying,option_type,expiration,strike,quantity,underlying_price,
                intrinsic_value,days_to_expiry,severity,action,reason,projected_cash_need,
                projected_share_need,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(row["code"], row["underlying"], row["option_type"], row["expiration"],
                  row["strike"], row["quantity"], row["underlying_price"], row["intrinsic_value"],
                  row["days_to_expiry"], row["severity"], row["action"], row["reason"],
                  row["projected_cash_need"], row["projected_share_need"], now) for row in rows],
            )
        return len(rows)

    def latest_option_lifecycle(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT snapshot.* FROM option_lifecycle_snapshots snapshot
                JOIN (SELECT code,MAX(id) id FROM option_lifecycle_snapshots GROUP BY code) latest
                ON latest.id=snapshot.id ORDER BY
                CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                WHEN 'WARNING' THEN 3 ELSE 4 END, days_to_expiry,code"""
            ).fetchall()]

    def save_option_disposition(
        self, code: str, action: str, close_days_before: int,
        close_window_minutes: int, enabled: bool, actor: str,
    ) -> None:
        allowed = {"MONITOR", "MANUAL_REVIEW", "ACCEPT_EXERCISE", "AUTO_CLOSE"}
        if action not in allowed:
            raise ValueError("不支持的期权到期处置方式")
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO option_disposition_plans
                (code,action,close_days_before,close_window_minutes,enabled,actor,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
                action=excluded.action,close_days_before=excluded.close_days_before,
                close_window_minutes=excluded.close_window_minutes,enabled=excluded.enabled,
                actor=excluded.actor,updated_at=excluded.updated_at""",
                (code, action, max(0, int(close_days_before)),
                 max(15, int(close_window_minutes)), int(enabled), actor, self.now()),
            )
        self.audit("OPTION_DISPOSITION_UPDATED", {
            "code": code, "action": action, "close_days_before": close_days_before,
            "close_window_minutes": close_window_minutes, "enabled": enabled, "actor": actor,
        })

    def option_dispositions(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM option_disposition_plans ORDER BY code"
            ).fetchall()]

    def option_disposition(self, code: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM option_disposition_plans WHERE code=?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def save_strategy_risk_budgets(self, budgets: list[dict]) -> None:
        enabled_total = sum(
            float(item["capital_ratio"]) for item in budgets if item.get("enabled", True)
        )
        if enabled_total > 1.000001:
            raise ValueError(f"启用策略的资金配额合计 {enabled_total:.1%}，不能超过 100%")
        for item in budgets:
            for key in ("capital_ratio", "max_loss_ratio", "option_premium_ratio"):
                value = float(item[key])
                if value < 0 or value > 1:
                    raise ValueError(f"{item['strategy_id']} 的 {key} 必须在 0–100% 之间")
        now = self.now()
        with self.connection() as connection:
            connection.executemany(
                """INSERT INTO strategy_risk_budgets
                (strategy_id,capital_ratio,max_loss_ratio,option_premium_ratio,enabled,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET
                capital_ratio=excluded.capital_ratio,max_loss_ratio=excluded.max_loss_ratio,
                option_premium_ratio=excluded.option_premium_ratio,enabled=excluded.enabled,
                updated_at=excluded.updated_at""",
                [(item["strategy_id"], float(item["capital_ratio"]),
                  float(item["max_loss_ratio"]), float(item["option_premium_ratio"]),
                  int(item.get("enabled", True)), now) for item in budgets],
            )
        self.audit("STRATEGY_RISK_BUDGETS_CHANGED", {"budgets": budgets})

    def strategy_risk_budgets(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM strategy_risk_budgets ORDER BY strategy_id"
            ).fetchall()]

    def strategy_risk_budget(self, strategy_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_risk_budgets WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_portfolio_stress(self, equity: float, rows: list[dict]) -> int:
        worst = min((float(row["estimated_pnl"]) for row in rows), default=0.0)
        ratio = max(0.0, -worst / equity) if equity > 0 else 0.0
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO portfolio_stress_runs
                (equity,worst_loss,worst_loss_ratio,scenarios_json,created_at)
                VALUES(?,?,?,?,?)""",
                (equity, worst, ratio, json.dumps(rows, separators=(",", ":")), self.now()),
            )
            return int(cursor.lastrowid)

    def portfolio_stress_runs(self, limit: int = 100) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM portfolio_stress_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def register_experiment(
        self, strategy_id: str, version_hash: str, code_version: str,
        parameters: dict, universe: list[str], data_version: str,
        metrics: dict | None = None, status: str = "REGISTERED",
    ) -> int:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO strategy_experiments
                (strategy_id,version_hash,code_version,parameters_json,universe_json,
                data_version,metrics_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id,version_hash) DO UPDATE SET
                parameters_json=excluded.parameters_json,universe_json=excluded.universe_json,
                data_version=excluded.data_version,metrics_json=excluded.metrics_json,
                status=CASE WHEN strategy_experiments.is_active=1 THEN strategy_experiments.status
                ELSE excluded.status END""",
                (strategy_id, version_hash, code_version,
                 json.dumps(parameters, separators=(",", ":"), default=str),
                 json.dumps(universe, separators=(",", ":")), data_version,
                 json.dumps(metrics or {}, separators=(",", ":"), default=str), status, self.now()),
            )
            row = connection.execute(
                "SELECT id FROM strategy_experiments WHERE strategy_id=? AND version_hash=?",
                (strategy_id, version_hash),
            ).fetchone()
            return int(row["id"])

    def promote_experiment(self, experiment_id: int) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT strategy_id FROM strategy_experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise ValueError("实验版本不存在")
            connection.execute(
                "UPDATE strategy_experiments SET is_active=0 WHERE strategy_id=?",
                (row["strategy_id"],),
            )
            connection.execute(
                """UPDATE strategy_experiments SET is_active=1,status='ACTIVE',promoted_at=?
                WHERE id=?""", (self.now(), experiment_id),
            )
        self.audit("STRATEGY_EXPERIMENT_PROMOTED", {"experiment_id": experiment_id})

    def strategy_experiments(self, strategy_id: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM strategy_experiments"
        params: list = []
        if strategy_id:
            query += " WHERE strategy_id=?"
            params.append(strategy_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def active_experiment(self, strategy_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_experiments WHERE strategy_id=? AND is_active=1",
                (strategy_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_reprice(self, intent_id: int, price: float) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE order_intents SET limit_price=?,reprice_count=reprice_count+1,
                last_reprice_at=?,updated_at=? WHERE id=?""",
                (price, self.now(), self.now(), intent_id),
            )

    def update_shadow_intent(
        self, intent_id: int, status: str, filled_quantity: float,
        average_fill_price: float, fees: float,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE order_intents SET status=?,filled_quantity=?,average_fill_price=?,
                fees=?,updated_at=? WHERE id=?""",
                (status, filled_quantity, average_fill_price, fees, self.now(), intent_id),
            )

    def clear_symbol_activity(
        self, strategy_id: str, symbol: str, state_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, int]:
        keys = tuple(dict.fromkeys(str(key) for key in (state_keys or ()) if key))
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM order_intents WHERE strategy_id=? AND symbol=?",
                (strategy_id, symbol),
            ).fetchall()
            intent_ids = [int(row["id"]) for row in rows]
            shadow_deleted = order_events_deleted = intents_deleted = 0
            if intent_ids:
                placeholders = ",".join("?" for _ in intent_ids)
                shadow_deleted = connection.execute(
                    f"DELETE FROM shadow_fills WHERE intent_id IN ({placeholders})",  # noqa: S608
                    intent_ids,
                ).rowcount
                order_events_deleted = connection.execute(
                    f"DELETE FROM order_state_events WHERE intent_id IN ({placeholders})",  # noqa: S608
                    intent_ids,
                ).rowcount
                intents_deleted = connection.execute(
                    f"DELETE FROM order_intents WHERE id IN ({placeholders})",  # noqa: S608
                    intent_ids,
                ).rowcount
            state_deleted = 0
            if keys:
                for key in keys:
                    state_deleted += connection.execute(
                        "DELETE FROM campaign_state WHERE state_key=?",
                        (key,),
                    ).rowcount
            return {
                "intent_ids": len(intent_ids),
                "shadow_fills": int(shadow_deleted or 0),
                "order_state_events": int(order_events_deleted or 0),
                "order_intents": int(intents_deleted or 0),
                "campaign_state": int(state_deleted or 0),
            }

    def heartbeat(self, strategy_id: str, status: str, message: str = "", pid: int | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO strategy_heartbeats(strategy_id,status,message,pid,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET
                status=excluded.status,message=excluded.message,pid=excluded.pid,
                updated_at=excluded.updated_at""",
                (strategy_id, status, message, pid, self.now()),
            )

    def save_broker_snapshot(self, key: str, value) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO broker_snapshots(snapshot_key,payload_json,captured_at)
                VALUES(?,?,?) ON CONFLICT(snapshot_key) DO UPDATE SET
                payload_json=excluded.payload_json,captured_at=excluded.captured_at""",
                (key, _json_dumps(value.to_dict(orient="records") if hasattr(value, "to_dict") else value), self.now()),
            )

    def broker_snapshot(self, key: str, tenant_id: str | None = None) -> dict | None:
        with self.connection() as connection:
            row = None
            if tenant_id:
                row = connection.execute(
                    "SELECT * FROM broker_snapshots WHERE snapshot_key=?", (f"{key}_{tenant_id}",)
                ).fetchone()
            if not row:
                row = connection.execute(
                    "SELECT * FROM broker_snapshots WHERE snapshot_key=?", (key,)
                ).fetchone()
        if not row:
            return None
        return {"key": row["snapshot_key"], "payload": json.loads(row["payload_json"]), "captured_at": row["captured_at"]}

    def append_batch_lot(
        self, batch_id: str, *, quantity: float, price: float, opened_at: str | None = None,
        fill_id: int | None = None, intent_id: int | None = None, initial_risk: float = 0.0,
        metadata: dict | None = None,
    ) -> None:
        timestamp = opened_at or self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO batch_lots
                (batch_id,fill_id,intent_id,opened_at,open_price,open_qty,remaining_qty,initial_risk,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (batch_id, fill_id, intent_id, timestamp, price, quantity, quantity, initial_risk,
                 _json_dumps(metadata or {})),
            )
            # The batch header is a materialized summary of its lots, not merely
            # the first fill passed to create_position_batch.
            connection.execute(
                """UPDATE position_batches SET
                entry_qty=(SELECT SUM(open_qty) FROM batch_lots WHERE batch_id=?),
                remaining_qty=(SELECT SUM(remaining_qty) FROM batch_lots WHERE batch_id=?),
                status='OPEN',updated_at=? WHERE batch_id=?""",
                (batch_id, batch_id, self.now(), batch_id),
            )

    def allocate_exit(self, batch_id: str, *, quantity: float, close_price: float,
                      policy: str = "FIFO", close_intent_id: int | None = None,
                      close_fill_id: int | None = None) -> list[dict]:
        if quantity <= 0:
            return []
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_lots WHERE batch_id=? AND remaining_qty>0 ORDER BY opened_at,id",
                (batch_id,),
            ).fetchall()
            ordered = list(rows)
            available = sum(float(row["remaining_qty"]) for row in ordered)
            if available + 1e-9 < quantity:
                raise ValueError(f"批次 {batch_id} 可分配数量不足，剩余 {quantity - available:g}")
            mode = str(policy or "FIFO").upper()
            if mode == "LIFO":
                ordered.reverse()
            remaining, allocations = float(quantity), []
            for row in ordered:
                if remaining <= 0:
                    break
                allocated = min(remaining, float(row["remaining_qty"]))
                pnl = (float(close_price) - float(row["open_price"])) * allocated
                connection.execute(
                    "UPDATE batch_lots SET remaining_qty=remaining_qty-? WHERE id=?",
                    (allocated, row["id"]),
                )
                connection.execute(
                    """INSERT INTO exit_allocations
                    (batch_id,close_fill_id,close_intent_id,allocated_qty,open_price,close_price,realized_pnl,allocation_rank,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (batch_id, close_fill_id, close_intent_id, allocated, row["open_price"],
                     close_price, pnl, len(allocations) + 1, self.now()),
                )
                allocations.append({"lot_id": row["id"], "quantity": allocated, "realized_pnl": pnl})
                remaining -= allocated
            if remaining > 1e-9:
                raise ValueError(f"批次 {batch_id} 可分配数量不足，剩余 {remaining:g}")
            connection.execute(
                """UPDATE position_batches SET
                remaining_qty=(SELECT COALESCE(SUM(remaining_qty),0) FROM batch_lots WHERE batch_id=?),
                status=CASE WHEN (SELECT COALESCE(SUM(remaining_qty),0)
                                  FROM batch_lots WHERE batch_id=?) <= 1e-9
                            THEN 'CLOSED' ELSE 'OPEN' END,
                updated_at=? WHERE batch_id=?""",
                (batch_id, batch_id, self.now(), batch_id),
            )
            return allocations

    def create_position_batch(
        self, batch_id: str, *, strategy_id: str, symbol: str, quantity: float,
        entry_price: float, opened_at: str | None = None, campaign_id: str | None = None,
        underlying: str | None = None, structure: str | None = None,
        combo_id: str | None = None, initial_risk: float = 0.0,
        allocation_policy: str = "FIFO", metadata: dict | None = None,
    ) -> None:
        timestamp = opened_at or self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO position_batches
                (batch_id,strategy_id,campaign_id,symbol,underlying,structure,combo_id,
                 opened_at,status,allocation_policy,entry_qty,remaining_qty,entry_cost,
                 initial_risk,metadata_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?)""",
                (batch_id, strategy_id, campaign_id, symbol, underlying, structure, combo_id,
                 timestamp, allocation_policy, quantity, quantity, entry_price, initial_risk,
                 _json_dumps(metadata or {}), timestamp, timestamp),
            )

    def position_batches(self, strategy_id: str | None = None, symbol: str | None = None) -> list[dict]:
        clauses, params = [], []
        if strategy_id:
            clauses.append("strategy_id=?")
            params.append(strategy_id)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM position_batches{where} ORDER BY opened_at,batch_id", params
            ).fetchall()]

    def update_batch_metrics(self, batch_id: str, *, remaining_qty: float | None = None,
                             mfe_pnl_ratio: float | None = None, mae_pnl_ratio: float | None = None,
                             r_multiple: float | None = None, status: str | None = None) -> None:
        fields, params = [], []
        for name, value in (("remaining_qty", remaining_qty), ("mfe_pnl_ratio", mfe_pnl_ratio),
                            ("mae_pnl_ratio", mae_pnl_ratio), ("r_multiple", r_multiple), ("status", status)):
            if value is not None:
                fields.append(f"{name}=?")
                params.append(value)
        if not fields:
            return
        fields.append("updated_at=?")
        params.append(self.now())
        params.append(batch_id)
        with self.connection() as connection:
            connection.execute(f"UPDATE position_batches SET {', '.join(fields)} WHERE batch_id=?", params)

    def arm_oco_group(self, oco_id: str, *, batch_id: str | None, strategy_id: str,
                      symbol: str, quantity: float, take_profit_price: float | None,
                      stop_loss_price: float | None) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO oco_groups
                (oco_id,batch_id,strategy_id,symbol,quantity,take_profit_price,stop_loss_price,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'ARMED',?,?)""",
                (oco_id, batch_id, strategy_id, symbol, quantity, take_profit_price, stop_loss_price, now, now),
            )

    def trigger_oco_group(self, oco_id: str, reason: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE oco_groups SET status='TRIGGERED',trigger_reason=?,updated_at=? WHERE oco_id=? AND status='ARMED'",
                (reason, self.now(), oco_id),
            )
            return cursor.rowcount > 0

    def cancel_oco_group(self, oco_id: str, reason: str = "") -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE oco_groups SET status='CANCELLED',trigger_reason=?,updated_at=? WHERE oco_id=? AND status IN ('ARMED','TRIGGERED','CANCEL_PENDING')",
                (reason, self.now(), oco_id),
            )
            return cursor.rowcount > 0

    def heartbeats(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM strategy_heartbeats ORDER BY strategy_id"
            ).fetchall()]

    def start_task_run(self, task_id: str, timeout_seconds: float, pid: int | None) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO task_runs(task_id,status,timeout_seconds,pid,started_at)
                VALUES(?,?,?,?,?)""",
                (task_id, "RUNNING", timeout_seconds, pid, self.now()),
            )
            return int(cursor.lastrowid)

    def finish_task_run(
        self, run_id: int, status: str, duration_seconds: float, message: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE task_runs SET status=?,duration_seconds=?,message=?,finished_at=?
                WHERE id=?""",
                (status, duration_seconds, message, self.now(), run_id),
            )

    def task_runs(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM task_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def create_option_combo(self, combo: dict, legs: list[dict]) -> str:
        now = self.now()
        combo_id = str(combo["combo_id"])
        for leg in legs:
            if isinstance(leg.get("quantity"), bool) or float(leg.get("quantity", 0)) <= 0 or not float(leg.get("quantity", 0)).is_integer():
                raise ValueError("组合腿数量必须是正整数")
            if not math.isfinite(float(leg.get("limit_price", 0))) or float(leg.get("limit_price", 0)) <= 0:
                raise ValueError("组合腿限价必须是有限正数")
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO option_combos
                (combo_id,strategy_id,name,underlying,structure,status,max_net_debit,
                min_net_credit,assembly_timeout_seconds,naked_timeout_seconds,auto_rollback,
                error,parent_combo_id,requires_parent_closed,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (combo_id, combo["strategy_id"], combo["name"], combo["underlying"],
                 combo["structure"], "DRAFT", float(combo.get("max_net_debit", 0)),
                 float(combo.get("min_net_credit", 0)),
                 int(combo.get("assembly_timeout_seconds", 300)),
                 int(combo.get("naked_timeout_seconds", 15)),
                 int(bool(combo.get("auto_rollback", True))), "", combo.get("parent_combo_id"),
                 int(bool(combo.get("requires_parent_closed", False))), now, now),
            )
            connection.executemany(
                """INSERT INTO option_combo_legs
                (combo_id,leg_order,role,symbol,side,quantity,limit_price,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'PLANNED',?,?)""",
                [(combo_id, index, leg["role"], leg["symbol"], leg["side"],
                  int(leg["quantity"]), float(leg["limit_price"]), now, now)
                 for index, leg in enumerate(legs, 1)],
            )
        self.option_combo_event(combo_id, "CREATED", "DRAFT", "组合草稿已创建")
        return combo_id

    def option_combos(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM option_combos ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def option_combo(self, combo_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM option_combos WHERE combo_id=?", (combo_id,)
            ).fetchone()
            return dict(row) if row else None

    def option_combo_legs(self, combo_id: str) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM option_combo_legs WHERE combo_id=? ORDER BY leg_order", (combo_id,)
            ).fetchall()]

    def update_option_combo(self, combo_id: str, status: str, error: str = "") -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE option_combos SET status=?,error=?,updated_at=? WHERE combo_id=?",
                (status, error, self.now(), combo_id),
            )

    def update_option_combo_leg(self, leg_id: int, **values) -> None:
        allowed = {"status", "intent_id", "close_intent_id", "filled_quantity",
                   "average_fill_price", "close_filled_quantity", "broker_status",
                   "remaining_quantity", "last_broker_update", "error", "close_attempts", "limit_price"}
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return
        fields["updated_at"] = self.now()
        clause = ",".join(f"{key}=?" for key in fields)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE option_combo_legs SET {clause} WHERE id=?",  # noqa: S608
                (*fields.values(), leg_id),
            )

    def option_combo_event(
        self, combo_id: str, event_type: str, status: str, message: str,
        details: dict | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO option_combo_events
                (combo_id,event_type,status,message,details_json,created_at) VALUES(?,?,?,?,?,?)""",
                (combo_id, event_type, status, message,
                 json.dumps(details or {}, separators=(",", ":")), self.now()),
            )

    def option_combo_events(self, combo_id: str | None = None, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            if combo_id:
                rows = connection.execute(
                    """SELECT * FROM option_combo_events WHERE combo_id=?
                    ORDER BY id DESC LIMIT ?""", (combo_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM option_combo_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def record_combo_market_snapshot(self, row: dict) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO combo_market_snapshots
                (combo_id,underlying,regime,quality_score,quality_grade,mid_net_debit,
                executable_net_debit,package_spread,net_delta,net_gamma,net_theta,net_vega,
                capacity,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["combo_id"], row["underlying"], row["regime"], row["quality_score"],
                 row["quality_grade"], row.get("mid_net_debit"), row.get("executable_net_debit"),
                 row.get("package_spread"), row.get("net_delta"), row.get("net_gamma"),
                 row.get("net_theta"), row.get("net_vega"), int(row.get("capacity", 0)),
                 json.dumps(row.get("details", {}), separators=(",", ":"), default=str), self.now()),
            )
            return int(cursor.lastrowid)

    def combo_market_snapshots(self, combo_id: str | None = None, limit: int = 2000) -> list[dict]:
        with self.connection() as connection:
            if combo_id:
                rows = connection.execute(
                    """SELECT * FROM combo_market_snapshots WHERE combo_id=?
                    ORDER BY id DESC LIMIT ?""", (combo_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM combo_market_snapshots ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def record_calibration(
        self, calibration_type: str, status: str, sample_count: int, metrics: dict,
        recommendation: str, strategy_id: str | None = None,
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO calibration_runs
                (calibration_type,strategy_id,status,sample_count,metrics_json,recommendation,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (calibration_type, strategy_id, status, sample_count,
                 json.dumps(metrics, separators=(",", ":"), default=str), recommendation, self.now()),
            )
            return int(cursor.lastrowid)

    def calibration_runs(self, calibration_type: str | None = None, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            if calibration_type:
                rows = connection.execute(
                    """SELECT * FROM calibration_runs WHERE calibration_type=?
                    ORDER BY id DESC LIMIT ?""", (calibration_type, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM calibration_runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def save_validation_report(
        self, period_type: str, period_key: str, status: str, summary: dict, markdown: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO validation_reports
                (period_type,period_key,status,summary_json,markdown,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(period_type,period_key) DO UPDATE SET
                status=excluded.status,summary_json=excluded.summary_json,
                markdown=excluded.markdown,created_at=excluded.created_at""",
                (period_type, period_key, status, json.dumps(summary, separators=(",", ":"), default=str),
                 markdown, self.now()),
            )

    def validation_reports(self, limit: int = 100) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM validation_reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def schema_migrations(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()]

    def record_lineage(self, row: dict) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO data_lineage
                (dataset_type,dataset_key,path,source,row_count,quality_grade,content_hash,
                metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["dataset_type"], row["dataset_key"], row["path"], row["source"],
                 int(row["row_count"]), row["quality_grade"], row["content_hash"],
                 json.dumps(row.get("metadata", {}), separators=(",", ":"), default=str), self.now()),
            )
            return int(cursor.lastrowid)

    def lineage(self, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM data_lineage ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def save_recovery_point(self, row: dict) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO recovery_points
                (recovery_id,manifest_path,database_hash,config_hash,data_hash,status,
                verified_at,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (row["recovery_id"], row["manifest_path"], row["database_hash"],
                 row.get("config_hash"), row.get("data_hash"), row["status"],
                 row.get("verified_at"), row.get("created_at", self.now())),
            )

    def recovery_points(self, limit: int = 100) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM recovery_points ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def delete_recovery_points(self, recovery_ids: list[str] | tuple[str, ...]) -> int:
        ids = tuple(dict.fromkeys(str(item) for item in recovery_ids if item))
        if not ids:
            return 0
        with self.connection() as connection:
            before = connection.total_changes
            connection.executemany(
                "DELETE FROM recovery_points WHERE recovery_id=?",
                [(item,) for item in ids],
            )
            return connection.total_changes - before

    def create_config_release(
        self, config_key: str, draft, created_by: str, reason: str = "",
    ) -> int:
        current = self.get_runtime_config(config_key)
        current_value, version = current if current else (None, 0)
        diff = {"before": current_value, "after": draft}
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO config_releases
                (config_key,base_version,draft_json,status,diff_json,created_by,reason,created_at)
                VALUES(?,?,?,'DRAFT',?,?,?,?)""",
                (config_key, version, json.dumps(draft, separators=(",", ":"), default=str),
                 json.dumps(diff, separators=(",", ":"), default=str), created_by, reason, self.now()),
            )
            return int(cursor.lastrowid)

    def publish_config_release(self, release_id: int, approved_by: str) -> int:
        with self.connection() as connection:
            release = connection.execute(
                "SELECT * FROM config_releases WHERE id=?", (release_id,)
            ).fetchone()
            if not release or release["status"] != "DRAFT":
                raise ValueError("配置草稿不存在或已经处理")
            current = connection.execute(
                "SELECT version FROM runtime_config WHERE config_key=?", (release["config_key"],)
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if current_version != int(release["base_version"]):
                raise ValueError("配置基线已变化，请重新创建草稿")
        previous = json.loads(release["diff_json"]).get("before")
        version = self.set_runtime_config(release["config_key"], json.loads(release["draft_json"]), approved_by)
        try:
            with self.connection() as connection:
                connection.execute(
                    """UPDATE config_releases SET status='PUBLISHED',approved_by=?,published_at=?
                    WHERE id=?""", (approved_by, self.now(), release_id),
                )
        except Exception:
            self.set_runtime_config(release["config_key"], previous, "automatic_publish_rollback")
            raise
        return version

    def rollback_config_release(self, release_id: int, actor: str) -> int:
        with self.connection() as connection:
            release = connection.execute(
                "SELECT * FROM config_releases WHERE id=?", (release_id,)
            ).fetchone()
            if not release or release["status"] != "PUBLISHED":
                raise ValueError("只有已发布配置可以回滚")
        previous = json.loads(release["diff_json"]).get("before")
        version = self.set_runtime_config(release["config_key"], previous, actor)
        with self.connection() as connection:
            connection.execute(
                "UPDATE config_releases SET status='ROLLED_BACK' WHERE id=?", (release_id,)
            )
        return version

    def reject_config_release(self, release_id: int, approved_by: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE config_releases SET status='REJECTED',approved_by=?,published_at=?
                WHERE id=? AND status='DRAFT'""", (approved_by, self.now(), release_id),
            )

    def config_releases(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM config_releases ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def add_alert(self, severity: str, event_type: str, message: str, delivered: bool = False) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO alerts(severity,event_type,message,delivered,created_at) VALUES(?,?,?,?,?)",
                (severity, event_type, message, int(delivered), self.now()),
            )
            return int(cursor.lastrowid)

    def alert_recently_exists(
        self, severity: str, event_type: str, message: str, seconds: int = 900,
    ) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        with self.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM alerts WHERE severity=? AND event_type=? AND message=?
                AND created_at>=? LIMIT 1""",
                (severity, event_type, message, cutoff),
            ).fetchone()
        return row is not None

    def alerts(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def update_alert(self, alert_id: int, acknowledged: bool = True, action_status: str = "ACKNOWLEDGED") -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE alerts SET acknowledged=?,action_status=? WHERE id=?",
                (int(acknowledged), action_status, alert_id),
            )

    def import_option_history(self, records: list[dict]) -> int:
        rows = []
        for item in records:
            rows.append((
                item["code"], item["time_key"], item.get("underlying"),
                item.get("underlying_price"), item.get("bid"), item.get("ask"), item.get("iv"),
                item.get("delta"), item.get("gamma"), item.get("theta"), item.get("volume"),
                item.get("vega"), item.get("rho"), item.get("open_interest"),
                item.get("source", "IMPORT"), item.get("fetched_at", self.now()),
                item.get("quality_status", "UNKNOWN"),
                item.get("quality_reason", ""),
            ))
        with self.connection() as connection:
            connection.executemany(
                """INSERT INTO option_history
                (code,time_key,underlying,underlying_price,bid,ask,iv,delta,gamma,theta,
                volume,vega,rho,open_interest,source,fetched_at,quality_status,quality_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,time_key) DO UPDATE SET
                underlying=excluded.underlying,underlying_price=excluded.underlying_price,
                bid=excluded.bid,ask=excluded.ask,iv=excluded.iv,delta=excluded.delta,
                gamma=excluded.gamma,theta=excluded.theta,vega=excluded.vega,rho=excluded.rho,
                volume=excluded.volume,open_interest=excluded.open_interest,
                source=excluded.source,fetched_at=excluded.fetched_at,
                quality_status=excluded.quality_status,quality_reason=excluded.quality_reason""",
                rows,
            )
        return len(rows)

    def option_history_count(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM option_history").fetchone()[0])

    def option_history(self, code: str, start: str | None = None, end: str | None = None) -> list[dict]:
        query = "SELECT * FROM option_history WHERE code=?"
        params: list = [code]
        if start:
            query += " AND time_key>=?"
            params.append(start)
        if end:
            query += " AND time_key<=?"
            params.append(end)
        query += " ORDER BY time_key"
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def option_history_for_underlying(
        self, underlying: str, start: str | None = None, end: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM option_history WHERE underlying=?"
        params: list = [underlying]
        if start:
            query += " AND time_key>=?"
            params.append(start)
        if end:
            query += " AND time_key<=?"
            params.append(end)
        query += " ORDER BY time_key,code"
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def option_history_coverage(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT code,underlying,COUNT(*) sample_count,MIN(time_key) first_time,
                MAX(time_key) last_time,
                SUM(CASE WHEN quality_status='GOOD' THEN 1 ELSE 0 END) good_samples,
                COUNT(DISTINCT substr(time_key,1,10)) trading_days
                FROM option_history GROUP BY code,underlying ORDER BY last_time DESC"""
            ).fetchall()]

    def performance(self) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT strategy_id,
                COUNT(*) AS intent_count,
                SUM(CASE WHEN status IN ('FILLED','FILLED_ALL') THEN 1 ELSE 0 END) AS filled_orders,
                SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) AS rejected_orders,
                SUM(CASE WHEN side='SELL' THEN 1 ELSE -1 END * filled_quantity * average_fill_price *
                    CASE WHEN symbol GLOB 'US.*[0-9][CP][0-9]*' THEN 100 ELSE 1 END) AS net_cash_flow,
                SUM(filled_quantity * average_fill_price *
                    CASE WHEN symbol GLOB 'US.*[0-9][CP][0-9]*' THEN 100 ELSE 1 END) AS filled_notional
                FROM order_intents GROUP BY strategy_id ORDER BY strategy_id"""
            ).fetchall()
            return [dict(row) for row in rows]

    def record_equity(self, total_assets: float, cash: float, market_value: float, env: str = "SIMULATE") -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO equity_snapshots(total_assets,cash,market_value,created_at,env) VALUES(?,?,?,?,?)",
                (total_assets, cash, market_value, self.now(), env.upper()),
            )

    def equity_since(self, iso_timestamp: str, env: str | None = None) -> list[dict]:
        with self.connection() as connection:
            query = "SELECT * FROM equity_snapshots WHERE created_at>=?"
            params = [iso_timestamp]
            if env:
                query += " AND env=?"
                params.append(env.upper())
            query += " ORDER BY id"
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def latest_equity_before(self, before_iso: str, env: str | None = None) -> dict | None:
        with self.connection() as connection:
            query = "SELECT * FROM equity_snapshots WHERE created_at < ?"
            params = [before_iso]
            if env:
                query += " AND env=?"
                params.append(env.upper())
            query += " ORDER BY id DESC LIMIT 1"
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def equity_history(self, limit: int = 10_000, env: str | None = None) -> list[dict]:
        with self.connection() as connection:
            query = "SELECT * FROM equity_snapshots"
            params = []
            if env:
                query += " WHERE env=?"
                params.append(env.upper())
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [dict(row) for row in connection.execute(query, params).fetchall()][::-1]

    def record_metric(self, symbol: str, metric: str, value: float) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO market_metrics(symbol,metric,value,created_at) VALUES(?,?,?,?)",
                (symbol, metric, value, self.now()),
            )

    def metric_history(self, symbol: str, metric: str, limit: int = 500) -> list[float]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT value FROM market_metrics WHERE symbol=? AND metric=?
                ORDER BY id DESC LIMIT ?""", (symbol, metric, limit),
            ).fetchall()
            return [float(row["value"]) for row in rows]

    def integrity_check(self) -> str:
        with self.connection() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def backup(self, destination: Path | str) -> Path:
        """Create a consistent SQLite backup and close every file handle explicitly.

        SQLite's connection context manager commits/rolls back transactions but does
        not guarantee that the connection object is closed. Windows refuses to
        remove a temporary database while that handle is still open, which can make
        recovery-point creation fail during TemporaryDirectory cleanup.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path, timeout=30)
        target = sqlite3.connect(destination)
        try:
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("PRAGMA foreign_keys=ON")
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        return destination

    def cleanup(self, before_iso: str) -> dict[str, int]:
        deleted = {}
        with self.connection() as connection:
            for table in (
                "audit_events", "alerts", "equity_snapshots", "market_metrics", "decision_logs",
                "decision_snapshots", "reconciliation_reports",
                "option_lifecycle_snapshots", "combo_market_snapshots",
            ):
                cursor = connection.execute(f"DELETE FROM {table} WHERE created_at < ?", (before_iso,))
                deleted[table] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM task_runs WHERE started_at < ?", (before_iso,),
            )
            deleted["task_runs"] = cursor.rowcount
        return deleted

    _DECISION_CACHE: dict[tuple[str, str | None], tuple[str, str, float]] = {}

    def decision(
        self, strategy_id: str, symbol: str | None, decision: str, reason: str,
        payload: dict | None = None,
    ) -> None:
        self._DECISION_CACHE[(strategy_id, symbol)] = (decision, reason, time.time())
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO decision_logs
                (strategy_id,symbol,decision,reason,payload_json,created_at) VALUES(?,?,?,?,?,?)""",
                (strategy_id, symbol, decision, reason,
                 _json_dumps(payload or {}), self.now()),
            )

    def decision_if_changed(
        self, strategy_id: str, symbol: str | None, decision: str, reason: str,
        payload: dict | None = None, repeat_after_seconds: int = 900,
    ) -> bool:
        cache_key = (strategy_id, symbol)
        now_ts = time.time()
        cached = self._DECISION_CACHE.get(cache_key)
        if cached is not None:
            prev_decision, prev_reason, prev_ts = cached
            if prev_decision == decision and prev_reason == reason:
                if (now_ts - prev_ts) < repeat_after_seconds:
                    return False

        self.decision(strategy_id, symbol, decision, reason, payload)
        return True

    def decisions(self, limit: int = 1000) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM decision_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def audit_events(self, limit: int = 1000) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def set_virtual_allocation(
        self, strategy_id: str, symbol: str, quantity: float, average_cost: float = 0,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO virtual_allocations(strategy_id,symbol,quantity,average_cost,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(strategy_id,symbol) DO UPDATE SET
                quantity=excluded.quantity,average_cost=excluded.average_cost,
                updated_at=excluded.updated_at""",
                (strategy_id, symbol, quantity, average_cost, self.now()),
            )

    def sync_virtual_allocation_to_target(
        self, strategy_id: str, symbol: str, target_quantity: float,
        average_cost: float = 0,
    ) -> dict[str, float]:
        """Set the allocation so allocation + settled fills equals broker target.

        Reconciliation must not overwrite the historical fill ledger.  The
        initial virtual allocation is therefore the residual after applying
        non-shadow filled orders for this strategy and symbol.
        """
        with self.connection() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN filled_quantity
                ELSE -filled_quantity END), 0) AS net_fills
                FROM order_intents WHERE strategy_id=? AND symbol=?
                AND status NOT LIKE 'SHADOW%'""",
                (strategy_id, symbol),
            ).fetchone()
            net_fills = float(row["net_fills"] or 0)
        residual = float(target_quantity) - net_fills
        self.set_virtual_allocation(strategy_id, symbol, 0.0, average_cost)
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO reconciliation_adjustments
                (strategy_id,symbol,quantity,reason,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(strategy_id,symbol) DO UPDATE SET quantity=excluded.quantity,
                reason=excluded.reason,updated_at=excluded.updated_at""",
                (strategy_id, symbol, residual, "外部券商持仓调整", self.now()),
            )
        return {"target_quantity": float(target_quantity), "net_fills": net_fills, "residual": residual}

    def virtual_allocations(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM virtual_allocations ORDER BY strategy_id,symbol"
            ).fetchall()]

    def delete_virtual_allocation(self, strategy_id: str, symbol: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM virtual_allocations WHERE strategy_id=? AND symbol=?",
                (strategy_id, symbol),
            )

    def virtual_positions(
        self,
        env: str | None = None,
        account_id: int | str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict]:
        from .tenant import resolve_tenant
        if tenant_id:
            target_profile = resolve_tenant(tenant_id, account_id=account_id, env=env, registry=self.tenant_registry, strict=True)
        elif account_id or env:
            target_profile = resolve_tenant(None, account_id=account_id, env=env, registry=self.tenant_registry, strict=False)
        else:
            target_profile = None

        target_env = (target_profile.trading_env if target_profile else env).upper() if (target_profile or env) else None
        target_account = str(target_profile.account_id if target_profile else account_id or "").strip()
        target_tid = str(target_profile.tenant_id if target_profile else tenant_id or "").strip()
        if target_env == "REAL":
            # 严格实盘模式：持仓严格以真实成交流水为唯一真理，并按账户 ID / 租户 ID 隔离
            account_filter = ""
            params = []
            if target_tid and target_account:
                account_filter = "AND (tenant_id = ? OR json_extract(metadata_json, '$.account_id') = ?)"
                params.extend([target_tid, target_account])
            elif target_account:
                account_filter = "AND json_extract(metadata_json, '$.account_id') = ?"
                params.append(target_account)
            elif target_tid:
                account_filter = "AND tenant_id = ?"
                params.append(target_tid)
            v_param = target_tid if target_tid else ""
            params.append(v_param)
            with self.connection() as connection:
                query = f"""WITH fills AS (
                    SELECT strategy_id, symbol,
                    SUM(CASE WHEN side='BUY' THEN filled_quantity ELSE -filled_quantity END) qty,
                    SUM(filled_quantity * average_fill_price) / MAX(1e-9, SUM(filled_quantity)) as avg_cost
                    FROM order_intents
                    WHERE status NOT LIKE 'SHADOW%' AND (intent_key LIKE '%:REAL' OR status LIKE 'REAL%')
                    {account_filter}
                    GROUP BY strategy_id, symbol
                ), v_alloc AS (
                    SELECT strategy_id, symbol, quantity, average_cost
                    FROM virtual_allocations
                    WHERE tenant_id = ? OR tenant_id = ''
                ), keys AS (
                    SELECT strategy_id, symbol FROM fills UNION
                    SELECT strategy_id, symbol FROM v_alloc
                )
                SELECT keys.strategy_id, keys.symbol,
                COALESCE(v.quantity, 0) + COALESCE(f.qty, 0) AS quantity,
                COALESCE(NULLIF(v.average_cost, 0), f.avg_cost, 0) AS average_cost
                FROM keys
                LEFT JOIN fills f USING(strategy_id, symbol)
                LEFT JOIN v_alloc v USING(strategy_id, symbol)
                WHERE ABS(COALESCE(v.quantity, 0) + COALESCE(f.qty, 0)) > 1e-9
                ORDER BY keys.strategy_id, keys.symbol"""
                rows = connection.execute(query, params).fetchall()
                return [dict(row) for row in rows]

        env_filter = "AND intent_key NOT LIKE '%:REAL' AND status NOT LIKE 'REAL%'" if target_env in {"SIMULATE", "PAPER"} else ""
        with self.connection() as connection:
            query = f"""WITH fills AS (
                SELECT strategy_id,symbol,
                SUM(CASE WHEN side='BUY' THEN filled_quantity ELSE -filled_quantity END) qty,
                SUM(CASE WHEN side='BUY' THEN filled_quantity * average_fill_price ELSE 0 END) / MAX(1e-9, SUM(CASE WHEN side='BUY' THEN filled_quantity ELSE 0 END)) as avg_cost
                FROM order_intents WHERE status NOT LIKE 'SHADOW%' {env_filter}
                GROUP BY strategy_id,symbol
            ), adjustments AS (
                SELECT strategy_id,symbol,quantity FROM reconciliation_adjustments
            ), keys AS (
                SELECT strategy_id,symbol FROM fills UNION
                SELECT strategy_id,symbol FROM virtual_allocations UNION
                SELECT strategy_id,symbol FROM adjustments
            )
            SELECT keys.strategy_id,keys.symbol,
            COALESCE(a.quantity,0)+COALESCE(f.qty,0)+COALESCE(r.quantity,0) quantity,
            COALESCE(NULLIF(a.average_cost,0), f.avg_cost, 0) average_cost
            FROM keys LEFT JOIN fills f USING(strategy_id,symbol)
            LEFT JOIN virtual_allocations a USING(strategy_id,symbol)
            LEFT JOIN adjustments r USING(strategy_id,symbol)
            ORDER BY keys.strategy_id,keys.symbol"""
            rows = connection.execute(query).fetchall()
            return [dict(row) for row in rows]

    def strategy_position(self, strategy_id: str, symbol: str, env: str | None = None) -> float:
        rows = self.virtual_positions(env=env)
        return sum(float(row["quantity"]) for row in rows
                   if row["strategy_id"] == strategy_id and row["symbol"] == symbol)

    def approve_strategy(
        self, strategy_id: str, version_hash: str, approved_by: str,
        backtest_passed: bool, shadow_passed: bool, drill_passed: bool,
        risk_confirmed: bool,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO strategy_approvals
                (strategy_id,version_hash,backtest_passed,shadow_passed,drill_passed,
                 risk_confirmed,approved_by,approved_at) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id) DO UPDATE SET version_hash=excluded.version_hash,
                backtest_passed=excluded.backtest_passed,shadow_passed=excluded.shadow_passed,
                drill_passed=excluded.drill_passed,risk_confirmed=excluded.risk_confirmed,
                approved_by=excluded.approved_by,approved_at=excluded.approved_at""",
                (strategy_id, version_hash, int(backtest_passed), int(shadow_passed),
                 int(drill_passed), int(risk_confirmed), approved_by, self.now()),
            )

    def approvals(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM strategy_approvals ORDER BY strategy_id"
            ).fetchall()]

    def strategy_approved(self, strategy_id: str, version_hash: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_approvals WHERE strategy_id=? AND version_hash=?",
                (strategy_id, version_hash),
            ).fetchone()
            return bool(row and all(row[key] for key in (
                "backtest_passed", "shadow_passed", "drill_passed", "risk_confirmed"
            )))

    def decision_snapshot(self, strategy_id: str, symbol: str, snapshot: dict) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO decision_snapshots(strategy_id,symbol,snapshot_json,created_at)
                VALUES(?,?,?,?)""",
                (strategy_id, symbol, json.dumps(snapshot, separators=(",", ":")), self.now()),
            )

    def decision_snapshots(self, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM decision_snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def record_reconciliation(
        self, symbol: str, broker_quantity: float, ledger_quantity: float,
        status: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO reconciliation_reports
                (symbol,broker_quantity,ledger_quantity,difference,status,created_at)
                VALUES(?,?,?,?,?,?)""",
                (symbol, broker_quantity, ledger_quantity,
                 broker_quantity - ledger_quantity, status, self.now()),
            )

    def reconciliation_reports(self, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM reconciliation_reports ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def record_fill(
        self, intent_id: int, quantity: float, price: float, fees: float,
        *, source: str, quote: dict | None = None, broker_deal_id: str | None = None,
        filled_at: str | None = None, reference_price: float | None = None,
        multiplier: float = 1.0,
    ) -> int | None:
        intent = self.intent(intent_id)
        if not intent or quantity <= 0 or price <= 0:
            return None
        quote = quote or {}
        bid = float(quote.get("bid_price") or 0)
        ask = float(quote.get("ask_price") or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask >= bid else None
        direction = 1 if str(intent["side"]).upper() == "BUY" else -1
        multiplier = float(multiplier or 1.0)
        spread_cost = max(0.0, ask - bid) / 2 * quantity * multiplier if mid else 0.0
        price_impact = max(0.0, (price - mid) * direction * quantity * multiplier) if mid else 0.0
        reference = reference_price if reference_price and reference_price > 0 else None
        payload = json.dumps(quote, separators=(",", ":"), default=str)
        timestamp = filled_at or self.now()
        batch_metadata = json.loads(intent.get("metadata_json") or "{}")
        batch_id = batch_metadata.get("batch_id")
        with self.connection() as connection:
            if broker_deal_id:
                existing = connection.execute(
                    "SELECT id FROM fill_ledger WHERE broker_deal_id=?",
                    (str(broker_deal_id),),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            if not broker_deal_id:
                duplicate = connection.execute(
                    """SELECT id FROM fill_ledger WHERE intent_id=? AND source=? AND side=?
                    AND ABS(quantity-?) < 0.000001 AND ABS(price-?) < 0.000001
                    AND ABS((julianday(filled_at)-julianday(?))*86400) < 30""",
                    (intent_id, source, intent["side"], quantity, price, timestamp),
                ).fetchone()
                if duplicate:
                    return int(duplicate["id"])
            cursor = connection.execute(
                """INSERT INTO fill_ledger
                (intent_id,strategy_id,symbol,side,quantity,price,multiplier,commission,
                 regulatory_fee,fee_total,bid_price,ask_price,mid_price,reference_price,
                 spread_cost,price_impact_cost,source,broker_deal_id,quote_json,filled_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent_id, intent["strategy_id"], intent["symbol"], intent["side"],
                 quantity, price, multiplier, fees, 0.0, fees, bid or None, ask or None,
                 mid, reference, spread_cost, price_impact, source,
                 str(broker_deal_id) if broker_deal_id else None, payload, timestamp),
            )
            fill_id = int(cursor.lastrowid)
        if batch_id and fill_id:
            if str(intent["side"]).upper() == "BUY":
                self.create_position_batch(
                    str(batch_id), strategy_id=intent["strategy_id"], symbol=intent["symbol"],
                    quantity=quantity, entry_price=price, opened_at=timestamp,
                    underlying=batch_metadata.get("underlying"),
                    structure=batch_metadata.get("structure"),
                    initial_risk=float(batch_metadata.get("initial_risk") or 0),
                    allocation_policy=str(batch_metadata.get("exit_allocation_policy", "FIFO")),
                    metadata=batch_metadata,
                )
                self.append_batch_lot(
                    str(batch_id), quantity=quantity, price=price, opened_at=timestamp,
                    fill_id=fill_id, intent_id=intent_id,
                    initial_risk=float(batch_metadata.get("initial_risk") or 0),
                    metadata={"source": source, "multiplier": multiplier},
                )
            else:
                try:
                    self.allocate_exit(
                        str(batch_id), quantity=quantity, close_price=price,
                        policy=str(batch_metadata.get("exit_allocation_policy", "FIFO")),
                        close_intent_id=intent_id, close_fill_id=fill_id,
                    )
                except ValueError:
                    self.audit("BATCH_EXIT_ALLOCATION_FAILED", {
                        "batch_id": batch_id, "fill_id": fill_id, "quantity": quantity,
                    }, intent["strategy_id"])
        return fill_id

    def record_shadow_fill(
        self, intent_id: int, quantity: float, price: float, fees: float, quote: dict,
    ) -> None:
        self.record_fill(intent_id, quantity, price, fees, source="SHADOW", quote=quote)
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO shadow_fills(intent_id,quantity,price,fees,quote_json,created_at)
                VALUES(?,?,?,?,?,?)""",
                (intent_id, quantity, price, fees,
                 json.dumps(quote, separators=(",", ":"), default=str), self.now()),
            )

    def fills(
        self, start: str | None = None, end: str | None = None,
        strategy_id: str | None = None, symbol: str | None = None, limit: int = 50_000,
    ) -> list[dict]:
        clauses, params = [], []
        if start:
            clauses.append("filled_at >= ?")
            params.append(start)
        if end:
            clauses.append("filled_at < ?")
            params.append(end)
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM fill_ledger{where} ORDER BY filled_at,id LIMIT ?",
                (*params, limit),
            ).fetchall()]

    def shadow_fills(self, limit: int = 10_000) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT f.*,i.strategy_id,i.symbol,i.side,i.limit_price,i.created_at intent_created_at
                FROM shadow_fills f JOIN order_intents i ON i.id=f.intent_id
                ORDER BY f.id DESC LIMIT ?""", (limit,)
            ).fetchall()]

    def shadow_position(self, strategy_id: str, symbol: str) -> tuple[float, float]:
        """Return net quantity and average open cost from the Shadow fill ledger."""
        quantity = 0.0
        average_cost = 0.0
        rows = reversed(self.shadow_fills(limit=50_000))
        for row in rows:
            if row["strategy_id"] != strategy_id or row["symbol"] != symbol:
                continue
            fill_quantity = float(row["quantity"])
            fill_price = float(row["price"])
            if row["side"] == "BUY":
                total_cost = quantity * average_cost + fill_quantity * fill_price
                quantity += fill_quantity
                average_cost = total_cost / quantity if quantity > 0 else 0.0
            else:
                quantity = max(0.0, quantity - fill_quantity)
                if quantity == 0:
                    average_cost = 0.0
        return quantity, average_cost

    def shadow_positions_for_strategy(self, strategy_id: str) -> dict[str, tuple[float, float]]:
        """Aggregate one strategy's Shadow positions in a single ledger scan."""
        positions: dict[str, list[float]] = {}
        for row in reversed(self.shadow_fills(limit=50_000)):
            if row["strategy_id"] != strategy_id:
                continue
            symbol = str(row["symbol"])
            quantity, average_cost = positions.get(symbol, [0.0, 0.0])
            fill_quantity = float(row["quantity"])
            fill_price = float(row["price"])
            if row["side"] == "BUY":
                total_cost = quantity * average_cost + fill_quantity * fill_price
                quantity += fill_quantity
                average_cost = total_cost / quantity if quantity > 0 else 0.0
            else:
                quantity = max(0.0, quantity - fill_quantity)
                if quantity == 0:
                    average_cost = 0.0
            positions[symbol] = [quantity, average_cost]
        return {symbol: (values[0], values[1]) for symbol, values in positions.items()}

    def shadow_open_positions(self) -> list[dict]:
        """Aggregate all Shadow fills so risk is enforced across campaigns."""
        keys = {(row["strategy_id"], row["symbol"]) for row in self.shadow_fills(limit=50_000)}
        result = []
        for strategy_id, symbol in sorted(keys):
            quantity, average_cost = self.shadow_position(strategy_id, symbol)
            if quantity > 0:
                result.append({
                    "strategy_id": strategy_id, "symbol": symbol,
                    "quantity": quantity, "average_cost": average_cost,
                })
        return result

    def strategy_has_completed_exit(
        self, strategy_id: str, symbol: str, intent_prefix: str | None = None,
    ) -> bool:
        with self.connection() as connection:
            query = """SELECT 1 FROM order_intents WHERE strategy_id=? AND symbol=? AND side='SELL'
                AND filled_quantity>0"""
            params: list = [strategy_id, symbol]
            if intent_prefix:
                query += " AND intent_key LIKE ?"
                params.append(f"{intent_prefix}%")
            row = connection.execute(query + " LIMIT 1", params).fetchone()
        return row is not None

    def record_acceptance_evidence(
        self, strategy_id: str, evidence_type: str, passed: bool,
        value: float | None = None, details: dict | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO acceptance_evidence
                (strategy_id,evidence_type,passed,value,details_json,created_at)
                VALUES(?,?,?,?,?,?)""",
                (strategy_id, evidence_type, int(passed), value,
                 json.dumps(details or {}, separators=(",", ":")), self.now()),
            )

    def acceptance_evidence(self, strategy_id: str | None = None) -> list[dict]:
        with self.connection() as connection:
            query = "SELECT * FROM acceptance_evidence"
            params: tuple = ()
            if strategy_id:
                query += " WHERE strategy_id=?"
                params = (strategy_id,)
            query += " ORDER BY id DESC"
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def upsert_earnings_event(
        self, symbol: str, event_date: str, session: str, source: str, confirmed: bool,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO earnings_events(symbol,event_date,session,source,confirmed,fetched_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,event_date) DO UPDATE SET
                session=excluded.session,source=excluded.source,confirmed=excluded.confirmed,
                fetched_at=excluded.fetched_at""",
                (symbol, event_date, session, source, int(confirmed), self.now()),
            )

    def earnings_events(self, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM earnings_events ORDER BY event_date,symbol LIMIT ?", (limit,)
            ).fetchall()]

    def operational_event(self, component: str, event_type: str, status: str, payload: dict | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO operational_events(component,event_type,status,payload_json,created_at) VALUES(?,?,?,?,?)",
                (component, event_type, status, _json_dumps(payload or {}), self.now()),
            )

    def operational_events(self, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM operational_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def record_config_version(
        self, config_key: str, value, actor: str = "system", reason: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO config_versions(config_key,value_json,actor,reason,created_at)
                VALUES(?,?,?,?,?)""",
                (config_key, json.dumps(value, ensure_ascii=False, default=str), actor, reason, self.now()),
            )

    def config_versions(self, config_key: str | None = None, limit: int = 500) -> list[dict]:
        with self.connection() as connection:
            if config_key:
                rows = connection.execute(
                    "SELECT * FROM config_versions WHERE config_key=? ORDER BY id DESC LIMIT ?",
                    (config_key, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM config_versions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def add_risk_recommendation(self, parameter: str, current: float, recommended: float, reason: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO risk_recommendations(parameter,current_value,recommended_value,reason,created_at) VALUES(?,?,?,?,?)",
                (parameter, current, recommended, reason, self.now()),
            )

    def risk_recommendations(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM risk_recommendations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def decide_risk_recommendation(self, recommendation_id: int, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE risk_recommendations SET status=?,decided_at=? WHERE id=?",
                (status, self.now(), recommendation_id),
            )

    def record_validation(self, strategy_id: str, symbol: str, passed: bool, metrics: dict, windows: list[dict]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO validation_runs(strategy_id,symbol,passed,metrics_json,windows_json,created_at) VALUES(?,?,?,?,?,?)",
                (strategy_id, symbol, int(passed), json.dumps(metrics, separators=(",", ":")),
                 json.dumps(windows, separators=(",", ":")), self.now()),
            )

    def validation_runs(self, limit: int = 200) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM validation_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]

    def save_candidate_symbols(self, week_start: str, candidates: list[dict]) -> None:
        """保存全市场周度召回标的候选。"""
        with self.connection() as connection:
            for c in candidates:
                connection.execute(
                    """INSERT INTO candidate_universe
                    (week_start, symbol, underlying_name, industry, option_daily_volume,
                     open_interest, stock_turnover_usd, median_spread_pct, status, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(week_start, symbol) DO UPDATE SET
                    option_daily_volume=excluded.option_daily_volume,
                    open_interest=excluded.open_interest,
                    stock_turnover_usd=excluded.stock_turnover_usd,
                    median_spread_pct=excluded.median_spread_pct,
                    status=excluded.status""",
                    (week_start, c["symbol"], c.get("underlying_name", ""),
                     c.get("industry", "OTHER"), float(c.get("option_daily_volume", 0)),
                     float(c.get("open_interest", 0)), float(c.get("stock_turnover_usd", 0)),
                     float(c.get("median_spread_pct", 0)), c.get("status", "ACTIVE"), self.now()),
                )

    def candidate_symbols(self, week_start: str | None = None, status: str = "ACTIVE") -> list[dict]:
        with self.connection() as connection:
            if week_start:
                rows = connection.execute(
                    "SELECT * FROM candidate_universe WHERE week_start=? AND status=? ORDER BY option_daily_volume DESC",
                    (week_start, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM candidate_universe WHERE status=? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            return [dict(r) for r in rows]

    def upsert_incubator_metrics(self, symbol: str, metrics: dict) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO shadow_incubator
                (symbol, first_observed_at, observation_days, signal_triggers,
                 simulated_wins, simulated_losses, simulated_pnl_usd, simulated_max_dd,
                 avg_slippage_pct, volume_retention_ratio, latest_ml_score, promotion_eligible,
                 track_mode, last_evaluated_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                observation_days=excluded.observation_days,
                signal_triggers=excluded.signal_triggers,
                simulated_wins=excluded.simulated_wins,
                simulated_losses=excluded.simulated_losses,
                simulated_pnl_usd=excluded.simulated_pnl_usd,
                simulated_max_dd=excluded.simulated_max_dd,
                avg_slippage_pct=excluded.avg_slippage_pct,
                volume_retention_ratio=excluded.volume_retention_ratio,
                latest_ml_score=excluded.latest_ml_score,
                promotion_eligible=excluded.promotion_eligible,
                track_mode=excluded.track_mode,
                last_evaluated_at=excluded.last_evaluated_at,
                updated_at=excluded.updated_at""",
                (symbol, metrics.get("first_observed_at", now),
                 int(metrics.get("observation_days", 0)), int(metrics.get("signal_triggers", 0)),
                 int(metrics.get("simulated_wins", 0)), int(metrics.get("simulated_losses", 0)),
                 float(metrics.get("simulated_pnl_usd", 0)), float(metrics.get("simulated_max_dd", 0)),
                 float(metrics.get("avg_slippage_pct", 0)), float(metrics.get("volume_retention_ratio", 0)),
                 float(metrics.get("latest_ml_score", 0)), int(bool(metrics.get("promotion_eligible", 0))),
                 str(metrics.get("track_mode", "STANDARD")), now, now),
            )

    def incubator_symbol(self, symbol: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_incubator WHERE symbol=?", (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def all_incubator_symbols(self, eligible_only: bool = False) -> list[dict]:
        with self.connection() as connection:
            if eligible_only:
                rows = connection.execute(
                    "SELECT * FROM shadow_incubator WHERE promotion_eligible=1 ORDER BY latest_ml_score DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM shadow_incubator ORDER BY latest_ml_score DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def promote_universe_symbol(
        self, symbol: str, promotion_score: float, expires_at: str, industry: str = "OTHER",
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO promoted_universe
                (symbol, promoted_at, expires_at, promotion_score, industry, status, created_at, updated_at)
                VALUES(?,?,?,?,?,'ACTIVE',?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                promoted_at=excluded.promoted_at,
                expires_at=excluded.expires_at,
                promotion_score=excluded.promotion_score,
                industry=excluded.industry,
                status='ACTIVE',
                eviction_reason='',
                updated_at=excluded.updated_at""",
                (symbol, now, expires_at, float(promotion_score), industry, now, now),
            )

    def active_promoted_symbols(self) -> list[dict]:
        now = self.now()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM promoted_universe WHERE status='ACTIVE' AND expires_at >= ? ORDER BY promotion_score DESC",
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    def evict_promoted_symbol(self, symbol: str, reason: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE promoted_universe SET status='EVICTED', eviction_reason=?, updated_at=? WHERE symbol=?",
                (reason, now, symbol),
            )

    def record_universe_lifecycle_event(self, event_type: str, symbol: str, details: dict) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO universe_lifecycle_events(event_type, symbol, details_json, created_at) VALUES(?,?,?,?)",
                (event_type, symbol, json.dumps(details, ensure_ascii=False), self.now()),
            )

    def universe_lifecycle_events(self, limit: int = 100) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM universe_lifecycle_events ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def universe_lifecycle_summary(self, today_prefix: str | None = None) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT event_type, count(*) FROM universe_lifecycle_events GROUP BY event_type"
            ).fetchall()
            counts = {str(r[0]): int(r[1]) for r in rows}
            counts["_total"] = sum(counts.values())
            if today_prefix:
                row = connection.execute(
                    "SELECT count(*) FROM universe_lifecycle_events WHERE created_at LIKE ?",
                    (f"{today_prefix}%",),
                ).fetchone()
                counts["_today"] = int(row[0]) if row and row[0] is not None else 0
            else:
                counts["_today"] = 0
            return counts
