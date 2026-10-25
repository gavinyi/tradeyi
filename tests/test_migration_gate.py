"""迁移授权 + 只读模式守卫（P0，2026-10-25 提前迁移事故）。

事故事实：v18 代码落盘生产目录（22:08:10）后，任何打开存量库的 Python 进程
都会顺带执行 pending 迁移（22:20:42 生产库被迁移 17/18/19）。
本文件锁死三条制度：

1. 迁移只在 `TRADEYI_ALLOW_MIGRATION=1` 时执行，且只有 `run_service` 启动路径设置；
2. 其他脚本/测试打开存量库遇到 pending 迁移 → 直接失败（不静默迁）；
3. 巡检/诊断/清理脚本使用 `TradingStore(read_only=True)`，禁止写库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from futu_quant.migrations import (
    MIGRATION_ALLOW_ENV,
    migrations_allowed,
    pending_migrations,
)
from futu_quant.store import TradingStore

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _store_with_pending_migration(tmp_path) -> Path:
    path = tmp_path / "pending.db"
    TradingStore(path)  # 全新库：bootstrap 允许，迁移到最新
    with sqlite3.connect(path) as connection:
        # 模拟「旧版本库」：抹掉最后一条迁移记录 → 产生 pending
        connection.execute(
            "DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        )
    return path


def test_fresh_db_bootstraps_without_authorization(tmp_path, monkeypatch):
    """新建库 = bootstrap，不需要授权（否则首次部署无法建库）。"""
    monkeypatch.delenv(MIGRATION_ALLOW_ENV, raising=False)
    store = TradingStore(tmp_path / "fresh.db")
    with store.connection() as connection:
        assert pending_migrations(connection) == []


def test_existing_db_with_pending_migration_fails_without_authorization(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    monkeypatch.delenv(MIGRATION_ALLOW_ENV, raising=False)
    with pytest.raises(RuntimeError, match="未获迁移授权"):
        TradingStore(path)
    # 失败后库不能被迁移（pending 仍在）
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "拒绝启动后仍然把库迁移了"


def test_existing_db_migrates_only_with_authorization(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    assert migrations_allowed() is True
    TradingStore(path)
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection) == []


def test_only_run_service_sets_migration_authorization():
    """授权开关只能出现在服务启动路径；其他脚本一律不得设置。"""
    offenders = []
    for script in SCRIPTS.glob("*.py"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        if MIGRATION_ALLOW_ENV in text and script.name != "run_service.py":
            offenders.append(script.name)
    assert not offenders, f"以下脚本擅自设置迁移授权：{offenders}"
    service = (SCRIPTS / "run_service.py").read_text(encoding="utf-8")
    assert f'os.environ["{MIGRATION_ALLOW_ENV}"] = "1"' in service


def test_read_only_store_cannot_write_and_never_migrates(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    monkeypatch.delenv(MIGRATION_ALLOW_ENV, raising=False)
    # 只读打开：不 initialize、不迁移、不因 pending 报错
    store = TradingStore(path, read_only=True)
    with store.connection() as connection:
        assert pending_migrations(connection), "只读模式不得改动迁移状态"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM schema_migrations")


def test_read_only_store_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        TradingStore(tmp_path / "nope.db", read_only=True)


def test_pure_read_scripts_use_read_only_store():
    """巡检/诊断/清理类脚本必须只读打开（禁止顺带写库/迁移）。"""
    for name in ("print_mismatches.py", "diagnose_real_health.py", "check_pwd_saved.py"):
        text = (SCRIPTS / name).read_text(encoding="utf-8", errors="ignore")
        assert "TradingStore(read_only=True)" in text, f"{name} 未使用只读打开"
        assert "TradingStore()" not in text, f"{name} 仍有可写打开"


def test_migration_gate_preserves_existing_db_data(tmp_path, monkeypatch):
    """授权迁移后业务数据不变（迁移只加列/打标记）。"""
    path = tmp_path / "data.db"
    store = TradingStore(path)
    store.set_setting("keep_me", "yes")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        )
        before = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    TradingStore(path)
    reopened = TradingStore(path, read_only=True)
    assert reopened.get_setting("keep_me") == "yes"
    with reopened.connection() as connection:
        after = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
    assert after == before
def test_readonly_handle_rejects_migration_attempt(tmp_path, monkeypatch):
    """Product/Chief 第 4 条：只读句柄下**任何迁移尝试必须失败**。

    变异：把 `mode=ro` 改成 `mode=rw` → 本用例与
    `test_read_only_store_cannot_write_and_never_migrates` 必须变红。
    """
    from futu_quant.migrations import apply_migrations

    path = _store_with_pending_migration(tmp_path)
    monkeypatch.delenv(MIGRATION_ALLOW_ENV, raising=False)
    store = TradingStore(path, read_only=True)
    with store.connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection)  # 只读句柄连 CREATE TABLE 都写不了
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "只读句柄下迁移尝试竟然改动了库"
# ------------------------------------------------------------------ 并发/原子性/审计
def test_migration_is_atomic_on_failure(tmp_path, monkeypatch):
    """任一语句失败 → 整个事务回滚，绝不留半迁移状态。"""
    import futu_quant.migrations as migrations_module

    path = _store_with_pending_migration(tmp_path)
    broken = migrations_module.Migration(
        999, "broken_for_test",
        "ALTER TABLE fill_ledger ADD COLUMN broken_col REAL; THIS IS NOT SQL;",
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", migrations_module.MIGRATIONS + (broken,))
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    with pytest.raises(sqlite3.OperationalError):
        TradingStore(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(fill_ledger)")}
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert "broken_col" not in columns, "失败迁移留下了半迁移列（事务未回滚）"
    assert 999 not in versions


def test_second_migration_run_is_noop(tmp_path, monkeypatch):
    """并发第二写者拿到锁后二次校验版本 → 直接 no-op，不重复迁移。"""
    from futu_quant.migrations import apply_migrations

    path = _store_with_pending_migration(tmp_path)
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    TradingStore(path)  # 第一个写者迁完
    with sqlite3.connect(path) as connection:
        again = apply_migrations(connection)  # 第二个写者：版本已齐
    assert again == []


def test_migration_writes_audit_event(tmp_path, monkeypatch):
    """迁移必须留审计（事故教训：operational/audit 全 0 行查不出是谁迁的）。"""
    path = _store_with_pending_migration(tmp_path)
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    TradingStore(path)
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type='MIGRATION_APPLIED'"
        ).fetchall()
    assert rows, "迁移没有写 audit_events"
    payload = rows[-1][0]
    for key in ("from_version", "to_version", "applied", "duration_ms", "trigger"):
        assert key in payload, f"迁移审计缺字段 {key}"


def test_ui_store_shows_upgrade_message_instead_of_stack(tmp_path, monkeypatch):
    """UI 连到待迁移库 → 显示「系统升级中」并停止，不抛堆栈、不重试写库。"""
    import futu_quant.ui as ui_module

    path = _store_with_pending_migration(tmp_path)
    monkeypatch.delenv(MIGRATION_ALLOW_ENV, raising=False)
    captured: dict = {}

    class _FakeSt:
        def error(self, message):
            captured["error"] = message

        def caption(self, message):
            captured["caption"] = message

        def stop(self):
            raise SystemExit("st.stop")

    monkeypatch.setattr(ui_module, "st", _FakeSt())
    with pytest.raises(SystemExit):
        ui_module.open_trading_store(path)
    assert "升级" in captured.get("error", "")
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "UI 路径不得触发迁移"


def test_pages_use_ui_store_entry():
    """页面/主入口必须走 UI 句柄（捕获待迁移异常），不得直接构造 store。"""
    root = Path(__file__).resolve().parents[1]
    files = [root / "app.py", *sorted((root / "pages").glob("*.py"))]
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "from futu_quant.store import TradingStore" in text:
            offenders.append(path.name)
    assert not offenders, f"以下页面仍直接导入 TradingStore：{offenders}"
# ------------------------------------------------------------------ Reviewer 补充五条
def test_concurrent_migration_two_connections_behaviour(tmp_path, monkeypatch):
    """行为验收：两个连接同时初始化落后库 → 最终恰好最新版本、业务行数不变、无半迁移。"""
    import threading

    path = _store_with_pending_migration(tmp_path)
    with sqlite3.connect(path) as connection:
        before_rows = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
        latest = max(m.version for m in __import__("futu_quant.migrations", fromlist=["MIGRATIONS"]).MIGRATIONS)
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    errors: list[Exception] = []

    def _worker():
        try:
            TradingStore(path)
        except Exception as exc:  # noqa: BLE001 - 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, f"并发迁移抛错：{errors}"
    with sqlite3.connect(path) as connection:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        after_rows = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
    assert versions == list(range(1, latest + 1)), f"迁移版本不连续/不完整：{versions}"
    assert after_rows == before_rows, "并发迁移改动了业务行数"


def test_audit_failure_rolls_back_migration(tmp_path, monkeypatch):
    """审计与迁移必须同事务：审计写失败 → 迁移整体回滚。"""
    import futu_quant.migrations as migrations_module

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    path = _store_with_pending_migration(tmp_path)
    with sqlite3.connect(path) as connection:
        audit_before = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='MIGRATION_APPLIED'"
        ).fetchone()[0]
    monkeypatch.setattr(migrations_module, "_audit_migration", _boom)
    monkeypatch.setenv(MIGRATION_ALLOW_ENV, "1")
    with pytest.raises(RuntimeError, match="audit write failed"):
        TradingStore(path)
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "审计失败后迁移未回滚（半迁移）"
        audit_after = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='MIGRATION_APPLIED'"
        ).fetchone()[0]
    assert audit_after == audit_before


def test_readonly_store_enforces_writes_via_sqlite(tmp_path):
    """只读由 SQLite 强制（mode=ro + PRAGMA query_only）：写方法必须抛 OperationalError。"""
    path = tmp_path / "ro.db"
    TradingStore(path)  # 正常建库
    store = TradingStore(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        store.set_setting("x", "1")
    with pytest.raises(sqlite3.OperationalError):
        store.record_metric("US.QQQ", "ro_guard", 1.0)


def test_missing_db_rejected_without_explicit_bootstrap(tmp_path, monkeypatch):
    """路径写错不得静默创建空库（「系统健康但数据全空」）。"""
    monkeypatch.delenv("TRADEYI_ALLOW_BOOTSTRAP", raising=False)
    with pytest.raises(FileNotFoundError, match="非显式 bootstrap"):
        TradingStore(tmp_path / "typo.db")
    assert not (tmp_path / "typo.db").exists()
    # 显式 bootstrap 才允许首次建库
    created = TradingStore(tmp_path / "explicit.db", bootstrap=True)
    with created.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] > 0


def test_fresh_bootstrap_has_audit_table(tmp_path):
    store = TradingStore(tmp_path / "audit.db")  # 测试环境默认允许 bootstrap
    with store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone()[0] == 1