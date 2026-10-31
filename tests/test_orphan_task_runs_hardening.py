"""task_runs 终态保障与孤儿任务自愈收敛守卫 (P3 Batch 2)。

覆盖 Reviewer 核心验收门槛：
1. 进程存活性双向判定：PID 已死立即收敛为 ORPHAN_KILLED，无需等待 7200s；
2. 存活进程坚决不误伤：PID 存活且未达 7200s 兜底阈值时绝对保持 RUNNING；
3. Windows API 错误码隔离：只有 87 (ERROR_INVALID_PARAMETER) 判死，5 (ERROR_ACCESS_DENIED) 判活；
4. 运行期自动恢复闭环：automatic_recovery_cycle 优先收敛死任务，解开阻断 incident 恢复的僵尸行；
5. run_task 统一 finally 兜底：未正常写终态时 finally 补写，且自身异常被隔离。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from futu_quant.store import TradingStore
from futu_quant.operational_readiness import automatic_recovery_cycle


def test_reconcile_orphan_task_runs_immediately_kills_dead_pid(tmp_path):
    """断言 1：对应 PID 已死亡的任务，无论 started_at 过去多久 (即使仅 20s)，立即收敛为 ORPHAN_KILLED。"""
    store = TradingStore(tmp_path / "test_dead_pid.db")
    dead_pid = 99999999  # 不存在的死进程 PID

    # 模拟 20 秒前启动的任务 (对应生产 id=1013751 场景)
    recent_past = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO task_runs(task_id, status, timeout_seconds, pid, started_at)
            VALUES(?, 'RUNNING', 60.0, ?, ?)""",
            ("order_reconciliation", dead_pid, recent_past),
        )

    # 执行收敛
    reconciled = store.reconcile_orphan_task_runs(max_running_seconds=7200.0)
    assert reconciled == 1

    runs = store.task_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "ORPHAN_KILLED"
    assert "已退出死亡" in runs[0]["message"]
    assert runs[0]["finished_at"] is not None


def test_reconcile_orphan_task_runs_does_not_harm_alive_pid(tmp_path):
    """断言 2：对应 PID 仍存活的任务，在未达到 7200s 超大门槛前，绝对保持 RUNNING 不被误伤。"""
    store = TradingStore(tmp_path / "test_alive_pid.db")
    current_pid = os.getpid()  # 当前必存活的测试进程 PID

    # 模拟 60 秒前启动的任务
    past_60s = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO task_runs(task_id, status, timeout_seconds, pid, started_at)
            VALUES(?, 'RUNNING', 60.0, ?, ?)""",
            ("order_reconciliation", current_pid, past_60s),
        )

    reconciled = store.reconcile_orphan_task_runs(max_running_seconds=7200.0)
    assert reconciled == 0  # 绝不误伤

    runs = store.task_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "RUNNING"


def test_reconcile_orphan_task_runs_kills_alive_pid_only_after_max_threshold(tmp_path):
    """断言 3：对应 PID 虽然存活，但超过 7200s 超长大门槛 (严重卡死挂起)，仍兜底收敛。"""
    store = TradingStore(tmp_path / "test_alive_stale.db")
    current_pid = os.getpid()

    # 模拟 8000 秒前启动的任务 (严重卡死)
    past_8000s = (datetime.now(timezone.utc) - timedelta(seconds=8000)).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO task_runs(task_id, status, timeout_seconds, pid, started_at)
            VALUES(?, 'RUNNING', 60.0, ?, ?)""",
            ("order_reconciliation", current_pid, past_8000s),
        )

    reconciled = store.reconcile_orphan_task_runs(max_running_seconds=7200.0)
    assert reconciled == 1

    runs = store.task_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "ORPHAN_KILLED"
    assert "严重卡死超大门槛" in runs[0]["message"]


def test_automatic_recovery_cycle_cleans_orphan_runs_and_unblocks_incident(tmp_path):
    """断言 4：运行期自动恢复周期 automatic_recovery_cycle 自动收敛孤儿任务，解开阻断 incident 恢复的僵尸行。"""
    store = TradingStore(tmp_path / "test_auto_recovery.db")

    # 1. 记一条 OPEN 的 STRATEGY_ERROR incident
    store.record_incident("STRATEGY_ERROR:test_task", "ERROR", "失败")

    # 2. 模拟该任务进程被强杀，留下一条 dead PID 的 RUNNING 记录 (原本会永久卡死)
    dead_pid = 99999999
    past_30s = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO task_runs(task_id, status, timeout_seconds, pid, started_at)
            VALUES('test_task', 'RUNNING', 10.0, ?, ?)""",
            (dead_pid, past_30s),
        )

    # 运行自动恢复周期：死任务被收敛
    res1 = automatic_recovery_cycle(store)
    assert res1["reconciled_orphans"] >= 1
    runs = store.task_runs()
    assert runs[0]["status"] == "ORPHAN_KILLED"

    # 3. 后续调度正常拉起任务并成功执行完成
    r_success = store.start_task_run("test_task", 10, os.getpid())
    store.finish_task_run(r_success, "SUCCESS", 0.1)

    # 再次运行恢复周期：成功解开卡死，顺利恢复 incident！
    res2 = automatic_recovery_cycle(store)
    assert "STRATEGY_ERROR:test_task" in res2["recovered"]
    assert store.incidents()[0]["status"] == "RECOVERED"


def test_run_task_finally_guarantees_settlement_on_abrupt_exit(tmp_path, monkeypatch):
    """断言 5：run_task 在发生非捕获中断或异常跳出时，finally 块强制兜底收敛终态，不留 RUNNING。"""
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import run_service as rs

    store = TradingStore(tmp_path / "test_finally.db")
    dummy_alerts = type("Alerts", (), {"send": staticmethod(lambda *a, **k: None), "recover": staticmethod(lambda *a, **k: None)})()

    class CustomBypassException(BaseException):
        """非标准 Exception，用来模拟直接绕过 except Exception 的异常退出"""
        pass

    def _broken_callback():
        raise CustomBypassException("unhandled abort")

    with pytest.raises(CustomBypassException):
        rs.run_task(
            store,
            dummy_alerts,
            "test_task",
            _broken_callback,
            timeout_seconds=5,
        )

    # 断言 finally 块强制将该 task_run 写入了 ERROR 终态
    runs = store.task_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "ERROR"
    assert "finally 兜底收敛" in runs[0]["message"]


def test_windows_openprocess_error_code_isolation(tmp_path, monkeypatch):
    """断言 6：Windows API 错误码隔离验证：
    只有错误码 87 (ERROR_INVALID_PARAMETER) 确凿判死；
    错误码 5 (ERROR_ACCESS_DENIED) 遵循 fail-safe 铁律必须判定为存活，防止权限受限误杀。
    """
    store = TradingStore(tmp_path / "test_api_error_code.db")
    past_20s = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO task_runs(task_id, status, timeout_seconds, pid, started_at)
            VALUES('test_task', 'RUNNING', 60.0, 12345, ?)""",
            (past_20s,),
        )

    # 模拟 Windows ctypes 返回 NULL 且 get_last_error=5 (拒绝访问)
    import ctypes

    class MockHandleAccessDenied:
        def __call__(self, *args, **kwargs):
            ctypes.set_last_error(5)
            return 0

    class MockWinDLLAccessDenied:
        def __init__(self, *args, **kwargs):
            self.OpenProcess = MockHandleAccessDenied()
            self.CloseHandle = lambda h: True

    monkeypatch.setattr(ctypes, "WinDLL", MockWinDLLAccessDenied)
    ctypes.set_last_error(5)
    assert store.reconcile_orphan_task_runs(max_running_seconds=7200.0) == 0

    # 模拟 Windows ctypes 返回 NULL 且 get_last_error=87 (进程不存在/参数错误)
    class MockHandleDead:
        def __call__(self, *args, **kwargs):
            ctypes.set_last_error(87)
            return 0

    class MockWinDLLDead:
        def __init__(self, *args, **kwargs):
            self.OpenProcess = MockHandleDead()
            self.CloseHandle = lambda h: True

    monkeypatch.setattr(ctypes, "WinDLL", MockWinDLLDead)
    ctypes.set_last_error(87)
    assert store.reconcile_orphan_task_runs(max_running_seconds=7200.0) == 1
    runs = store.task_runs()
    assert runs[0]["status"] == "ORPHAN_KILLED"
