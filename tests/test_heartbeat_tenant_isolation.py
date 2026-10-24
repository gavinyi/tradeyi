"""多租户心跳隔离守卫（Reviewer 批 B 边界隐患）。

原缺陷：实盘与模拟 Worker 都把心跳写进 `strategy_heartbeats` 的同一行
`trading_service`（该表按 `strategy_id` 作键）。一方僵死、另一方仍每分钟刷新时，
**僵死永远判不出来** —— 进程级失联（PID 退出/复用）有效，但心跳级失联被互相掩盖。

修法：心跳名带租户（`trading_service:<tenant>`），读者兼容旧全局行但**必须校验 pid**。

变异：把 `_heartbeat_updated_at` 的 pid 校验去掉 → 第 2 条用例转红。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import scheduled_log_audit as audit  # noqa: E402
from futu_quant.store import TradingStore

NOW = datetime.now(timezone.utc)


def _write_heartbeat(store, name: str, pid: int, *, age_seconds: int = 0) -> None:
    updated = (NOW - timedelta(seconds=age_seconds)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO strategy_heartbeats"
            " (strategy_id, status, message, pid, updated_at) VALUES (?,?,?,?,?)",
            (name, "HEALTHY", "", pid, updated),
        )


def test_tenant_scoped_heartbeat_is_read_directly(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _write_heartbeat(store, "trading_service:futu_real_master", pid=111, age_seconds=30)

    value = audit._heartbeat_updated_at(
        store, "trading_service:futu_real_master", expected_pid=111,
    )
    assert value is not None and (NOW - value).total_seconds() < 120


def test_legacy_row_only_used_when_pid_matches(tmp_path):
    """旧全局行不能被别的租户"借用"：pid 不符时视为没有本进程心跳。"""
    store = TradingStore(tmp_path / "t.db")
    _write_heartbeat(store, "trading_service", pid=222, age_seconds=5)

    # 本进程 pid=111：旧行不属于它 → 不得采信（否则僵死被掩盖）
    assert audit._heartbeat_updated_at(
        store, "trading_service:futu_real_master", expected_pid=111,
    ) is None
    # 本进程 pid=222：旧行确实是自己的 → 兼容采信
    assert audit._heartbeat_updated_at(
        store, "trading_service:futu_real_master", expected_pid=222,
    ) is not None


def test_two_worker_heartbeats_do_not_overwrite(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    _write_heartbeat(store, "trading_service:futu_real_master", pid=111)
    _write_heartbeat(store, "trading_service:futu_paper_sandbox", pid=222)

    rows = {h["strategy_id"]: h["pid"] for h in store.heartbeats()}
    assert rows["trading_service:futu_real_master"] == 111
    assert rows["trading_service:futu_paper_sandbox"] == 222, "两个 Worker 的心跳不得再互相覆盖"
