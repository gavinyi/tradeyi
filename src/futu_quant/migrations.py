from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3


#: 进程级迁移授权（P0，2026-10-25/26）。
#:
#: **为什么不用环境变量**：2026-10-26 00:06:44 生产库被提前应用迁移 20。根因正是
#: `run_service.py` 把 `TRADEYI_ALLOW_MIGRATION=1` 写进 `os.environ`：Worker 派生的
#: 子进程（`env=os.environ.copy()`）继承后，只要磁盘上已是新代码，构造 `TradingStore()`
#: 就会顺带迁移 —— 授权面跟着进程树扩散，无法收敛。
#:
#: 现在授权只存在于 Python 进程内，且必须由**服务启动路径显式调用**：
#:
#:     authorize_migrations("service:run_service")
#:
#: 子进程不继承 Python 全局变量 → 无授权时遇 pending 迁移一律
#: `PendingMigrationsError`（fail-closed，绝不静默迁移）。
_PROCESS_AUTHORIZATION: str | None = None


def authorize_migrations(actor: str) -> None:
    """**仅服务启动路径**（`scripts/run_service.py:main`）可调用。

    actor 是审计落点（`audit_events.actor`），必填、禁止为空 —— 2026-10-26 之前
    `apply_migrations` 的 actor 默认值写死 `service:run_service`，导致「谁迁的库」查不出来。
    """
    global _PROCESS_AUTHORIZATION
    normalized = str(actor or "").strip()
    if not normalized:
        raise ValueError("迁移授权必须显式传 actor（审计要能定位调用者）")
    _PROCESS_AUTHORIZATION = normalized


def revoke_migrations() -> None:
    """撤销进程级授权（测试隔离用；生产进程不调用）。"""
    global _PROCESS_AUTHORIZATION
    _PROCESS_AUTHORIZATION = None


def migrations_allowed() -> bool:
    return _PROCESS_AUTHORIZATION is not None


def authorized_actor() -> str | None:
    """当前进程的迁移授权调用者；无授权返回 ``None``。"""
    return _PROCESS_AUTHORIZATION



def pending_migrations(connection: sqlite3.Connection) -> list[int]:
    """返回尚未应用的迁移版本（表不存在时视为全部 pending）。"""
    try:
        applied = {
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        }
    except sqlite3.OperationalError:
        applied = set()
    return [migration.version for migration in MIGRATIONS if migration.version not in applied]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


MIGRATIONS = (
    Migration(1, "baseline_sqlite_schema", "SELECT 1"),
    Migration(2, "infrastructure_indexes", """
        CREATE INDEX IF NOT EXISTS idx_lineage_type_key ON data_lineage(dataset_type,dataset_key);
        CREATE INDEX IF NOT EXISTS idx_config_releases_status ON config_releases(status,created_at);
        CREATE INDEX IF NOT EXISTS idx_recovery_points_created ON recovery_points(created_at);
    """),
    Migration(3, "volatility_tranche_protection", """
        CREATE TABLE IF NOT EXISTS position_batches (
            batch_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, campaign_id TEXT,
            symbol TEXT NOT NULL, underlying TEXT, structure TEXT, combo_id TEXT,
            opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL DEFAULT 'OPEN',
            allocation_policy TEXT NOT NULL DEFAULT 'FIFO', entry_qty REAL NOT NULL DEFAULT 0,
            remaining_qty REAL NOT NULL DEFAULT 0, entry_cost REAL NOT NULL DEFAULT 0,
            initial_risk REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0,
            mfe_pnl_ratio REAL NOT NULL DEFAULT 0, mae_pnl_ratio REAL NOT NULL DEFAULT 0,
            r_multiple REAL, metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_position_batches_open ON position_batches(strategy_id,symbol,status);
        CREATE TABLE IF NOT EXISTS batch_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            fill_id INTEGER, intent_id INTEGER, opened_at TEXT NOT NULL,
            open_price REAL NOT NULL, open_qty REAL NOT NULL, remaining_qty REAL NOT NULL,
            initial_risk REAL NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(batch_id) REFERENCES position_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS exit_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
            close_fill_id INTEGER, close_intent_id INTEGER, allocated_qty REAL NOT NULL,
            open_price REAL NOT NULL, close_price REAL NOT NULL, realized_pnl REAL NOT NULL,
            r_multiple REAL, allocation_rank INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES position_batches(batch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_exit_allocations_batch ON exit_allocations(batch_id,created_at);
        CREATE TABLE IF NOT EXISTS oco_groups (
            oco_id TEXT PRIMARY KEY, batch_id TEXT, strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL, quantity REAL NOT NULL, take_profit_price REAL,
            stop_loss_price REAL, status TEXT NOT NULL DEFAULT 'ARMED',
            trigger_reason TEXT, tp_intent_id INTEGER, sl_intent_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """),
    Migration(4, "combo_lifecycle_safety", """
        ALTER TABLE option_combo_legs ADD COLUMN close_filled_quantity REAL NOT NULL DEFAULT 0;
        ALTER TABLE option_combo_legs ADD COLUMN broker_status TEXT NOT NULL DEFAULT '';
        ALTER TABLE option_combo_legs ADD COLUMN remaining_quantity REAL NOT NULL DEFAULT 0;
        ALTER TABLE option_combo_legs ADD COLUMN last_broker_update TEXT;
        CREATE INDEX IF NOT EXISTS idx_combo_legs_symbol_status ON option_combo_legs(symbol,status);
    """),
    Migration(5, "shadow_incubator_track_mode", """
        ALTER TABLE shadow_incubator ADD COLUMN track_mode TEXT NOT NULL DEFAULT 'STANDARD';
    """),
    Migration(6, "equity_snapshots_env_isolation", """
        ALTER TABLE equity_snapshots ADD COLUMN env TEXT NOT NULL DEFAULT 'SIMULATE';
        CREATE INDEX IF NOT EXISTS idx_equity_snapshots_env_time ON equity_snapshots(env, created_at);
    """),
    Migration(7, "reconciliation_adjustments_tenant_account", """
        CREATE TABLE IF NOT EXISTS reconciliation_adjustments_v2 (
            tenant_id TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id, account_id, strategy_id, symbol)
        );
        INSERT OR REPLACE INTO reconciliation_adjustments_v2
            (tenant_id, account_id, strategy_id, symbol, quantity, reason, updated_at)
        SELECT '', '', strategy_id, symbol, quantity, reason, updated_at
        FROM reconciliation_adjustments;
        DROP TABLE reconciliation_adjustments;
        ALTER TABLE reconciliation_adjustments_v2 RENAME TO reconciliation_adjustments;
        CREATE INDEX IF NOT EXISTS idx_recon_adj_tenant
            ON reconciliation_adjustments(tenant_id, account_id, strategy_id, symbol);
    """),
    # P0-2 告警投递通道三态。**必须走版本化迁移**，不能写在 `initialize()` 的
    # 「新建库」分支里：存量库在 `apply_migrations()` 之后就 `return` 了
    # （`store.py:190-191`），**永远走不到那个分支**。
    # 2026-10-24 部署实测：生产 `alerts` 表仍只有 8 列，而新的 `add_alert` 已引用
    # 这三列 → 下一次写告警直接 OperationalError。
    # **一列一条迁移**：`apply_migrations` 只吞 "duplicate column name"，而
    # `executescript` 在首条失败后会中断剩余语句 —— 合在一条里会让
    # 「已有部分列」的库补不齐剩下的列，却仍被记为已应用。
    Migration(8, "alerts_webhook_delivered_column",
              "ALTER TABLE alerts ADD COLUMN webhook_delivered INTEGER NOT NULL DEFAULT -1;"),
    Migration(9, "alerts_feishu_delivered_column",
              "ALTER TABLE alerts ADD COLUMN feishu_delivered INTEGER NOT NULL DEFAULT -1;"),
    Migration(10, "alerts_muted_column",
              "ALTER TABLE alerts ADD COLUMN muted INTEGER NOT NULL DEFAULT 0;"),
    # P0-5 候选排除/降权留痕。**走版本化迁移，不写在 `initialize()` 的建表语句里** ——
    # 存量库在 `apply_migrations()` 之后就 `return`（`store.py:190-191`），
    # 只写在新库分支里的表，存量库永远没有（2026-10-24 alerts 三列就是这么漏的）。
    Migration(11, "candidate_exclusions", """
        CREATE TABLE IF NOT EXISTS candidate_exclusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT '',
            strategy_id TEXT NOT NULL DEFAULT '',
            underlying TEXT NOT NULL DEFAULT '',
            code TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            age_seconds REAL,
            spread_pct REAL,
            volume REAL,
            phase TEXT NOT NULL DEFAULT 'HARD_EXCLUDE',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_exclusions_tenant
            ON candidate_exclusions(tenant_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_exclusions_reason
            ON candidate_exclusions(reason);
    """),
    # P1-C1 配置审计：audit_events 加 actor。
    # **一列一条、索引单独一条** —— 同 Migration(8/9/10) 的理由：
    # `apply_migrations` 只吞 "duplicate column name"，而 `executescript` 在首条失败后
    # 会中断剩余语句。若把 ALTER 与 CREATE INDEX 合成一条，遇到「列已存在」的库时
    # ALTER 被吞掉、**索引永远建不上**，而迁移却被记为已应用。
    # 版本号说明：11 已被 P0-5 `candidate_exclusions`（先合入 test）占用，
    # 故本批审计迁移顺延为 12（列）/ 13（索引）。
    Migration(12, "audit_events_actor_column",
              "ALTER TABLE audit_events ADD COLUMN actor TEXT NOT NULL DEFAULT '';"),
    Migration(13, "audit_events_actor_index",
              "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor, id DESC);"),
    # P2：ML 重训历史（PRD 区块②）。每次触发一行，SKIP 也要留痕 ——
    # 「重训了但没换模型」和「重训根本没跑起来」必须能从数据上区分。
    Migration(14, "ml_retrain_runs", """
        CREATE TABLE IF NOT EXISTS ml_retrain_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_by TEXT NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            new_auc REAL,
            old_auc REAL,
            report_path TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ml_retrain_runs_started
            ON ml_retrain_runs(started_at DESC);
    """),
    # P2-14 效率：**热路径索引补齐**。
    # 问题形态与「alerts 三列」同源：`store.initialize()` 有两个分支，新加索引只写在
    # 「新建库」分支时，**存量库永远拿不到**。实测生产缺 idx_decision_logs_lookup
    # （决策查询按 strategy+symbol 走的是全表扫）。
    # 走版本化迁移 = 存量库必经之路；IF NOT EXISTS 保证新库/已建过的库都幂等。
    Migration(15, "hot_path_indexes", """
        CREATE INDEX IF NOT EXISTS idx_decision_logs_lookup
            ON decision_logs(strategy_id, symbol, id DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_tenant
            ON decision_logs(tenant_id, strategy_id, symbol, id DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_created
            ON decision_logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_operational_events_id_desc
            ON operational_events(id DESC);
        CREATE INDEX IF NOT EXISTS idx_operational_events_component
            ON operational_events(component, id DESC);
    """),
    # G4 跨日回撤熔断状态表。**必须走版本化迁移**：存量库在 `initialize()` 里
    # `apply_migrations()` 之后就 return，只写「新建库」分支的话生产永远没有这张表
    # （与 alerts 三列、hot_path_indexes 同源缺陷）。IF NOT EXISTS + 幂等建表。
    Migration(16, "risk_state_circuit_breaker", """
        CREATE TABLE IF NOT EXISTS risk_state (
            tenant_id   TEXT NOT NULL,
            scope       TEXT NOT NULL DEFAULT 'equity',
            peak_hist            REAL NOT NULL,
            peak_hist_at         TEXT NOT NULL,
            peak_hist_source     TEXT NOT NULL DEFAULT 'live',
            baseline_equity      REAL NOT NULL,
            baseline_at          TEXT NOT NULL,
            baseline_source      TEXT NOT NULL DEFAULT 'live',
            peak_reset           REAL NOT NULL,
            breaker_level   INTEGER NOT NULL DEFAULT 0,
            breaker_set_at  TEXT,
            breaker_reason  TEXT NOT NULL DEFAULT '',
            ack_dd_vs_peak  REAL,
            ack_at          TEXT,
            de_risk_scale   REAL NOT NULL DEFAULT 1.0,
            reset_count_30d INTEGER NOT NULL DEFAULT 0,
            last_reset_at   TEXT,
            updated_at      TEXT NOT NULL,
            PRIMARY KEY (tenant_id, scope)
        );
    """),
    # P1-13 费用三态（真值 / 估算 / 不可用）。**一列一条迁移**：`apply_migrations`
    # 只吞 "duplicate column name"，多列合在一条里会让「部分列已存在」的库补不齐剩余列。
    Migration(17, "fill_ledger_fee_is_estimate", """
        ALTER TABLE fill_ledger ADD COLUMN fee_is_estimate INTEGER NOT NULL DEFAULT 0;
    """),
    Migration(18, "fill_ledger_fee_unavailable_reason", """
        ALTER TABLE fill_ledger ADD COLUMN fee_unavailable_reason TEXT;
    """),
    # 历史 689 行 `fee_total=1.0` 是回填占位值：**只打标记、值不改、不回填**
    # （真值已不可知，不伪造）。纯数据迁移，无 DDL；`reason IS NULL` 保证重复执行幂等。
    Migration(19, "fill_ledger_legacy_placeholder_marker", """
        UPDATE fill_ledger SET fee_unavailable_reason='legacy_placeholder_1usd'
        WHERE ABS(fee_total-1.0) < 1e-9 AND fee_unavailable_reason IS NULL;
    """),
    # 期权「盘口年龄」：**观测专用**，不参与 quality_status（训练样本口径不可动）。
    # 历史行为 NULL（真值不可知，不回填）。
    Migration(20, "option_history_book_active_seconds", """
        ALTER TABLE option_history ADD COLUMN book_active_seconds REAL;
    """),
    # P1-11 批 1（2026-10-27）：按策略维度的净值快照（**只写不读**，观测用）。
    # 唯一键含「桶起点」as_of：同桶重试/补写走 UPSERT 覆盖，不产生重复行。
    Migration(21, "strategy_equity_snapshots", """
        CREATE TABLE IF NOT EXISTS strategy_equity_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id     TEXT NOT NULL,
            strategy_id   TEXT NOT NULL,
            bucket        TEXT NOT NULL,
            as_of         TEXT NOT NULL,
            realized_pnl  REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL,
            attribution_status TEXT NOT NULL DEFAULT 'COMPLETE',
            net_exposure  REAL NOT NULL DEFAULT 0,
            market_value  REAL NOT NULL DEFAULT 0,
            position_count INTEGER NOT NULL DEFAULT 0,
            account_total_assets REAL,
            attribution_diff REAL,
            source        TEXT NOT NULL DEFAULT 'cycle',
            created_at    TEXT NOT NULL,
            UNIQUE(tenant_id, strategy_id, bucket, as_of)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_equity_tenant_time
            ON strategy_equity_snapshots(tenant_id, strategy_id, as_of);
    """),
)




def _parse_add_column(sql: str) -> tuple[str, str, str | None] | None:
    """从 `ALTER TABLE t ADD COLUMN c TYPE ... DEFAULT x` 里取出 (表, 列, 期望类型)。

    只处理本仓迁移里实际使用的简单形式；解析不出就返回 None（不臆测）。
    """
    import re

    match = re.search(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+([A-Za-z]+)",
        sql, re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3).upper()


def _verify_existing_column(
    connection: sqlite3.Connection, sql: str, exc: Exception,
) -> None:
    parsed = _parse_add_column(sql)
    if parsed is None:
        return
    table, column, expected_type = parsed
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1]: str(row[2] or "").upper() for row in rows}
    actual_type = existing.get(column)
    if actual_type is None:
        raise RuntimeError(
            f"迁移报 duplicate column 但 {table}.{column} 并不存在：{exc}"
        )
    if expected_type and actual_type and expected_type != actual_type:
        raise RuntimeError(
            f"{table}.{column} 已存在但类型不一致：期望 {expected_type}，实际 {actual_type} "
            f"（迁移报 duplicate column 时被吞掉，若不校验就会静默不一致）"
        )




class PendingMigrationsError(RuntimeError):
    """存量库存在未授权迁移（UI 应捕获并提示「系统升级中」，不得重试写库）。"""


def _split_statements(sql: str) -> list[str]:
    """把迁移脚本安全拆成单条语句（`executescript` 会隐式提交，破坏事务原子性）。

    用 `sqlite3.complete_statement` 判断语句边界，能正确处理字符串/注释里的分号。
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if statement]


def _audit_migration(
    connection: sqlite3.Connection, *, actor: str, payload: dict,
) -> None:
    """迁移审计：没有审计的迁移等于「查不出谁改的库」（2026-10-25 事故教训）。"""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(audit_events)")
    }
    if not columns:
        return  # 极老库还没有 audit_events 表：不因此阻断迁移
    import json as _json

    payload_json = _json.dumps(payload, ensure_ascii=False, default=str)
    if "actor" in columns:
        connection.execute(
            "INSERT INTO audit_events(event_type,strategy_id,payload_json,created_at,actor) "
            "VALUES(?,?,?,datetime('now'),?)",
            ("MIGRATION_APPLIED", None, payload_json, actor),
        )
    else:
        connection.execute(
            "INSERT INTO audit_events(event_type,strategy_id,payload_json,created_at) "
            "VALUES(?,?,?,datetime('now'))",
            ("MIGRATION_APPLIED", None, payload_json),
        )


def apply_migrations(
    connection: sqlite3.Connection, *,
    actor: str,
    trigger: str = "",
) -> list[int]:
    """应用 pending 迁移（**单写者 + 全事务 + 审计**）。

    `actor` 必填：写进 `audit_events.actor`，回答「谁迁的库」。**禁止默认值** ——
    2026-10-26 之前默认写死 `service:run_service`，审计无法区分调用者。

    并发安全（Architect 2026-10-25）：`BEGIN IMMEDIATE` 取得库级写锁后再**二次读取**
    已应用版本；第二个 Worker 会阻塞在锁上，进事务后发现版本已齐 → 直接 no-op。
    任一语句失败 → 整个事务回滚，绝不留半迁移状态。
    """
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("apply_migrations 必须显式传 actor（审计要能定位调用者）")
    import time

    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL
    )""")
    # 审计表必须先存在（否则「迁移成功但审计写失败」又变成查不出来）
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        strategy_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    started = time.time()
    # 调用方（新建库分支）可能已开隐式事务（INSERT OR IGNORE 基线版本），先落盘再取写锁
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # 二次校验：拿到写锁后重读，避免并发 Worker 各迁一遍
        applied = {int(row[0]): str(row[1]) for row in connection.execute(
            "SELECT version,checksum FROM schema_migrations"
        )}
        for migration in MIGRATIONS:
            if migration.version in applied and migration.version != 1:
                if applied[migration.version] != migration.checksum:
                    raise RuntimeError(f"迁移 {migration.version} 校验和不一致，拒绝继续")
        to_apply = [item for item in MIGRATIONS if item.version not in applied]
        completed: list[int] = []
        for migration in to_apply:
            for statement in _split_statements(migration.sql):
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" in str(exc).lower():
                        _verify_existing_column(connection, statement, exc)
                    else:
                        raise
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,datetime('now'))",
                (migration.version, migration.name, migration.checksum),
            )
            completed.append(migration.version)
        if completed:
            _audit_migration(connection, actor=actor, payload={
                "from_version": max(applied or {0}),
                "to_version": max(completed),
                "applied": completed,
                "duration_ms": int((time.time() - started) * 1000),
                "trigger": trigger or "explicit",
            })
        connection.commit()
        return completed
    except Exception:
        connection.rollback()
        raise


def migration_status(store) -> dict:
    applied = {row["version"] for row in store.schema_migrations()}
    expected = {item.version for item in MIGRATIONS}
    return {"current": max(applied or {0}), "latest": max(expected),
            "pending": sorted(expected - applied), "healthy": expected.issubset(applied)}
