"""迁移授权 + 只读模式守卫（P0，2026-10-25 提前迁移事故 / 2026-10-26 提前迁移 20）。

事故事实：
1. v18 代码落盘生产目录（22:08:10）后，任何打开存量库的 Python 进程都会顺带执行
   pending 迁移（22:20:42 生产库被迁移 17/18/19）。
2. 2026-10-26 00:06:44 迁移 20 被**继承了 `TRADEYI_ALLOW_MIGRATION=1` 的 Worker 子进程**
   提前应用 —— 环境变量授权会随进程树扩散，所以本轮把授权收回进程内。

本文件锁死四条制度：

1. 迁移授权只能由服务启动路径显式授予（`authorize_migrations(actor)` 或
   `TradingStore(..., allow_migrations=True, migration_actor=...)`）；**环境变量不再授权**；
2. 其他脚本/子进程打开存量库遇到 pending 迁移 → 直接失败（不静默迁）；
3. 巡检/诊断/清理脚本使用 `TradingStore(read_only=True)`，禁止写库；
4. 迁移审计必须能回答「谁迁的库」（`audit_events.actor` 显式记录）。
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from futu_quant.migrations import (
    authorize_migrations,
    authorized_actor,
    migrations_allowed,
    pending_migrations,
    revoke_migrations,
)
from futu_quant.store import TradingStore

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROOT = Path(__file__).resolve().parents[1]


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
    revoke_migrations()
    store = TradingStore(tmp_path / "fresh.db")
    with store.connection() as connection:
        assert pending_migrations(connection) == []


def test_existing_db_with_pending_migration_fails_without_authorization(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    with pytest.raises(RuntimeError, match="未获迁移授权"):
        TradingStore(path)
    # 失败后库不能被迁移（pending 仍在）
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "拒绝启动后仍然把库迁移了"


def test_existing_db_migrates_only_with_authorization(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    authorize_migrations("test:migration_gate")
    assert migrations_allowed() is True
    TradingStore(path)
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection) == []


def test_only_run_service_sets_migration_authorization():
    """进程级授权只能出现在服务启动路径（run_service.main）；其他脚本一律不得授予。"""
    offenders = []
    for script in SCRIPTS.glob("*.py"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        if "authorize_migrations(" in text and script.name != "run_service.py":
            offenders.append(script.name)
    assert not offenders, f"以下脚本擅自授予迁移授权：{offenders}"
    service = (SCRIPTS / "run_service.py").read_text(encoding="utf-8")
    assert 'authorize_migrations("service:run_service")' in service
    # 环境变量授权已废除：服务端也不得再把开关写进进程环境（会随子进程继承）
    assert 'os.environ["TRADEYI_ALLOW_MIGRATION"]' not in service, (
        "run_service 仍在使用环境变量授权 —— 会随子进程继承，必须改回进程内显式授权"
    )
    assert "MIGRATION_ALLOW_ENV" not in service, "run_service 仍引用环境变量授权常量"


def test_env_variable_no_longer_authorizes_migration(tmp_path, monkeypatch):
    """2026-10-26 洞：子进程继承 `TRADEYI_ALLOW_MIGRATION=1` 就能迁库。

    修复后该环境变量**不再有任何授权能力**：置上之后仍然必须报 pending 异常。
    """
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    monkeypatch.setenv("TRADEYI_ALLOW_MIGRATION", "1")
    with pytest.raises(RuntimeError, match="未获迁移授权"):
        TradingStore(path)
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "环境变量竟然仍能授权迁移"


def test_child_process_cannot_inherit_authorization(tmp_path):
    """真子进程验收：父进程授权不随 spawn 继承；子进程遇 pending 必须失败且不迁库。"""
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    authorize_migrations("test:parent-process")
    assert migrations_allowed() is True
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["TRADEYI_ALLOW_MIGRATION"] = "1"  # 继承环境变量的旧攻击面：必须无效
    result = subprocess.run(
        [sys.executable, "-c",
         "from futu_quant.store import TradingStore\n"
         f"TradingStore({str(path)!r})\n"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode != 0, f"子进程竟然迁移成功：{result.stdout}"
    assert "未获迁移授权" in result.stderr, result.stderr[-2000:]
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection), "子进程失败后库仍被迁移"


def test_service_startup_authorization_migrates_and_records_actor(tmp_path):
    """服务启动路径授权（authorize_migrations）→ 允许迁移，审计 actor 可定位调用者。"""
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    authorize_migrations("service:run_service")
    assert authorized_actor() == "service:run_service"
    TradingStore(path)
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection) == []
        row = connection.execute(
            "SELECT actor,payload_json FROM audit_events "
            "WHERE event_type='MIGRATION_APPLIED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "迁移没有写审计"
    assert row[0] == "service:run_service", f"审计 actor 未记录调用者：{row[0]}"
    assert "process-authorized" in row[1], f"审计 trigger 未标授权来源：{row[1]}"


def test_explicit_param_authorization_records_actor(tmp_path):
    """显式传参授权（allow_migrations=True + migration_actor）同样迁移并留 actor。"""
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    TradingStore(path, allow_migrations=True, migration_actor="script:ops-migration")
    with sqlite3.connect(path) as connection:
        assert pending_migrations(connection) == []
        row = connection.execute(
            "SELECT actor,payload_json FROM audit_events "
            "WHERE event_type='MIGRATION_APPLIED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "script:ops-migration", f"审计 actor 未记录调用者：{row[0]}"
    assert "explicit-param" in row[1], f"审计 trigger 未标授权来源：{row[1]}"


def test_explicit_authorization_requires_actor(tmp_path):
    """显式授权必须带 actor：审计不能退化成「不知道谁迁的库」。"""
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
    with pytest.raises(ValueError, match="migration_actor"):
        TradingStore(path, allow_migrations=True)
    with pytest.raises(ValueError, match="actor"):
        authorize_migrations("   ")


def test_read_only_store_cannot_write_and_never_migrates(tmp_path, monkeypatch):
    path = _store_with_pending_migration(tmp_path)
    revoke_migrations()
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
    authorize_migrations("test:migration_gate")
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
    revoke_migrations()
    store = TradingStore(path, read_only=True)
    with store.connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, actor="test:migration_gate")  # 只读句柄连 CREATE TABLE 都写不了
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
    authorize_migrations("test:migration_gate")
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
    authorize_migrations("test:migration_gate")
    TradingStore(path)  # 第一个写者迁完
    with sqlite3.connect(path) as connection:
        again = apply_migrations(connection, actor="test:migration_gate")  # 第二个写者：版本已齐
    assert again == []


def test_migration_writes_audit_event(tmp_path, monkeypatch):
    """迁移必须留审计（事故教训：operational/audit 全 0 行查不出是谁迁的）。"""
    path = _store_with_pending_migration(tmp_path)
    authorize_migrations("test:migration_gate")
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
    revoke_migrations()
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
    authorize_migrations("test:migration_gate")
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
    authorize_migrations("test:migration_gate")
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


def test_radar_service_exits_cleanly_on_pending_migrations(tmp_path, monkeypatch, capsys):
    """雷达进程无迁移授权（授权只在 Worker 进程内）→ 中文提示 + 退出码 2，不刷堆栈。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import run_radar_service as radar_module

    from futu_quant.migrations import PendingMigrationsError

    monkeypatch.setattr(radar_module, "LOCK_FILE", tmp_path / "radar.lock")
    monkeypatch.setattr(radar_module, "PID_FILE", tmp_path / "radar.pid")
    monkeypatch.setattr(radar_module, "STOP_FILE", tmp_path / "radar.stop")

    def _pending(*_args, **_kwargs):
        raise PendingMigrationsError("检测到 1 条未应用迁移 [20]，本进程未获迁移授权。")

    monkeypatch.setattr(radar_module, "TradingStore", _pending)
    with pytest.raises(SystemExit) as excinfo:
        radar_module.main()
    assert excinfo.value.code == 2
    assert "待迁移" in capsys.readouterr().err
    assert not (tmp_path / "radar.pid").exists(), "失败时不得留下 PID 文件"
