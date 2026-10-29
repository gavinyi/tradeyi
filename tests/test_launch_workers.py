from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("scripts").resolve()))
from launch_workers import get_active_tenants, is_pid_alive
from run_service import service_files


def test_get_active_tenants_filters_disabled(monkeypatch):
    """开关缺省时按注册表默认值：实盘 + 模拟盘启用，moomoo 备用停用。"""
    import launch_workers

    monkeypatch.setattr(
        launch_workers,
        "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({}),
    )
    active_ids = {t.tenant_id for t in get_active_tenants()}

    # 1. 默认启用的实盘与模拟沙盒租户必须在活跃列表中
    assert "futu_real_master" in active_ids
    assert "futu_paper_sandbox" in active_ids

    # 2. 默认禁用的备用专户 moomoo_real 绝不得被误启动
    assert "moomoo_real" not in active_ids


def test_get_active_tenants_follows_switch(monkeypatch):
    """2026-10-28：开关（tenant_active:<id>）优先于注册表常量——开了几个就跑几个。"""
    import launch_workers

    monkeypatch.setattr(
        launch_workers,
        "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({"tenant_active:futu_real_master": "false"}),
    )
    assert {t.tenant_id for t in get_active_tenants()} == {"futu_paper_sandbox"}
    assert {t.tenant_id for t in launch_workers.get_disabled_tenants()} == {
        "futu_real_master",
        "moomoo_real",
    }

    # 非法值按关闭处理（fail-closed：宁可不启动，也不凭空多起一个真金 Worker）
    monkeypatch.setattr(
        launch_workers,
        "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({"tenant_active:futu_paper_sandbox": "maybe"}),
    )
    assert "futu_paper_sandbox" not in {t.tenant_id for t in get_active_tenants()}


def test_is_pid_alive_contract():
    import os
    # 当前进程必定存活
    assert is_pid_alive(os.getpid()) is True

    # 负数与 0 判定为非活跃
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False

    # 极大不存在 PID 判定为非活跃
    assert is_pid_alive(999999) is False


def test_service_files_isolation_for_all_active_tenants():
    active = get_active_tenants()
    locks = set()
    pids = set()
    logs = set()

    for t in active:
        files = service_files(t.tenant_id)
        lock_name = files["lock"].name
        pid_name = files["pid"].name
        log_name = files["log"].name

        # 各租户文件绝对独立，无同名碰撞
        assert lock_name not in locks
        assert pid_name not in pids
        assert log_name not in logs

        locks.add(lock_name)
        pids.add(pid_name)
        logs.add(log_name)


def test_launch_tenant_worker_skips_alive_pid(monkeypatch, tmp_path):
    import os
    from launch_workers import launch_tenant_worker
    from futu_quant.tenant import DEFAULT_TENANTS

    tenant = DEFAULT_TENANTS["futu_paper_sandbox"]
    fake_pid_file = tmp_path / "trading-service-futu_paper_sandbox.pid"
    fake_pid_file.write_text(str(os.getpid()), encoding="utf-8")

    # 模拟 service_files 指向临时目录
    def mock_service_files(_tenant_id):
        return {
            "lock": tmp_path / "lock",
            "pid": fake_pid_file,
            "log": tmp_path / "log.log",
            "stop": tmp_path / "stop",
            "restart": tmp_path / "restart",
        }

    monkeypatch.setattr("launch_workers.service_files", mock_service_files)
    # 若被误重复拉起，Popen 将被触发并报错
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("不应触发 Popen"))

    ok, pid, msg = launch_tenant_worker(tenant)
    assert ok is True
    assert pid == os.getpid()
    assert "已在运行中" in msg


def test_reconcile_runtime_enables_only_target_tenant(monkeypatch, tmp_path):
    """关闭→启用只拉起目标 Worker；不写 REAL_TRADING_ENABLED、不重复拉起。"""
    import launch_workers

    real = launch_workers.DEFAULT_TENANTS["futu_real_master"]
    paper = launch_workers.DEFAULT_TENANTS["futu_paper_sandbox"]
    launched: list[str] = []
    settings = launch_workers._ReadOnlySettings({
        "tenant_active:futu_real_master": "true",
        "tenant_active:futu_paper_sandbox": "true",
    })
    monkeypatch.setattr(launch_workers, "_read_tenant_switch_settings", lambda: settings)
    monkeypatch.setattr(
        launch_workers, "_worker_pid",
        lambda tenant_id: 101 if tenant_id == paper.tenant_id else None,
    )
    monkeypatch.setattr(
        launch_workers, "service_files",
        lambda tenant_id: {
            "pid": tmp_path / f"{tenant_id}.pid", "stop": tmp_path / f"{tenant_id}.stop",
            "log": tmp_path / f"{tenant_id}.log", "lock": tmp_path / f"{tenant_id}.lock",
            "restart": tmp_path / f"{tenant_id}.restart",
        },
    )
    monkeypatch.setattr(
        launch_workers, "launch_tenant_worker",
        lambda tenant, py_exe=None: (launched.append(tenant.tenant_id) or True, 202, "ok"),
    )

    result = launch_workers.reconcile_tenant_workers()

    assert launched == [real.tenant_id]
    assert result["started"] == [real.tenant_id]
    # 编排器只读 tenant_active；不得调用设置写接口去打开真金总门禁。
    source = Path(launch_workers.__file__).read_text(encoding="utf-8")
    assert "set_setting(" not in source
    assert "save_setting(" not in source


def test_reconcile_runtime_disable_uses_tenant_stop_only(monkeypatch, tmp_path):
    """启用→停用只写本租户 stop 文件，不杀进程、不波及 paper。"""
    import launch_workers

    real_id = "futu_real_master"
    paper_id = "futu_paper_sandbox"
    monkeypatch.setattr(
        launch_workers, "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({
            f"tenant_active:{real_id}": "false", f"tenant_active:{paper_id}": "true",
        }),
    )
    monkeypatch.setattr(
        launch_workers, "_worker_pid",
        lambda tenant_id: 303 if tenant_id == real_id else 404 if tenant_id == paper_id else None,
    )
    monkeypatch.setattr(
        launch_workers, "service_files",
        lambda tenant_id: {
            "pid": tmp_path / f"{tenant_id}.pid", "stop": tmp_path / f"{tenant_id}.stop",
            "log": tmp_path / f"{tenant_id}.log", "lock": tmp_path / f"{tenant_id}.lock",
            "restart": tmp_path / f"{tenant_id}.restart",
        },
    )

    result = launch_workers.reconcile_tenant_workers()

    assert result["stopping"] == [real_id]
    assert (tmp_path / f"{real_id}.stop").exists()
    assert not (tmp_path / f"{paper_id}.stop").exists()


def test_worker_pid_rejects_reused_or_other_tenant_pid(monkeypatch, tmp_path):
    import launch_workers

    pid_file = tmp_path / "worker.pid"
    pid_file.write_text("123", encoding="utf-8")
    monkeypatch.setattr(
        launch_workers, "service_files",
        lambda _tenant_id: {"pid": pid_file},
    )
    monkeypatch.setattr(launch_workers, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        launch_workers, "_process_cmdline",
        lambda _pid: "python.exe scripts/run_service.py --tenant futu_paper_sandbox",
    )

    assert launch_workers._worker_pid("futu_real_master") is None


def test_failed_start_uses_per_tenant_exponential_backoff(monkeypatch, tmp_path):
    import launch_workers

    launch_workers._RETRY_STATE.clear()
    monkeypatch.setattr(
        launch_workers, "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({
            "tenant_active:futu_real_master": "true",
            "tenant_active:futu_paper_sandbox": "false",
        }),
    )
    monkeypatch.setattr(launch_workers, "_worker_pid", lambda _tenant_id: None)
    monkeypatch.setattr(
        launch_workers, "service_files",
        lambda tenant_id: {
            "pid": tmp_path / f"{tenant_id}.pid", "stop": tmp_path / f"{tenant_id}.stop",
        },
    )
    attempts: list[str] = []
    monkeypatch.setattr(
        launch_workers, "launch_tenant_worker",
        lambda tenant, py_exe=None: (attempts.append(tenant.tenant_id) and False, None, "boom"),
    )

    assert launch_workers.reconcile_tenant_workers(now=100)["failed"] == ["futu_real_master"]
    assert launch_workers.reconcile_tenant_workers(now=104)["backoff"] == ["futu_real_master"]
    assert launch_workers.reconcile_tenant_workers(now=105)["failed"] == ["futu_real_master"]
    assert launch_workers.reconcile_tenant_workers(now=114)["backoff"] == ["futu_real_master"]
    assert len(attempts) == 2


def test_supervisor_stale_pid_is_replaced_and_runs(monkeypatch, tmp_path):
    import launch_workers

    pid_file = tmp_path / "supervisor.pid"
    stop_file = tmp_path / "supervisor.stop"
    pid_file.write_text("456", encoding="utf-8")
    stop_file.write_text("stop", encoding="utf-8")
    monkeypatch.setattr(launch_workers, "SUPERVISOR_PID_FILE", pid_file)
    monkeypatch.setattr(launch_workers, "SUPERVISOR_STOP_FILE", stop_file)
    monkeypatch.setattr(launch_workers, "SUPERVISOR_LOCK_FILE", tmp_path / "supervisor.lock")
    monkeypatch.setattr(launch_workers, "reconcile_tenant_workers", lambda **kwargs: stop_file.write_text("stop"))
    monkeypatch.setattr(launch_workers.time, "sleep", lambda _seconds: None)

    launch_workers.run_supervisor(poll_seconds=0.1)

    # 正常退出会清理自身文件；关键是陈旧 PID 没有阻止函数进入循环。
    assert not pid_file.exists()


def test_supervisor_lock_competitor_has_zero_pid_side_effects(monkeypatch, tmp_path):
    import launch_workers
    from futu_quant.filelock import FileLockError

    pid_file = tmp_path / "supervisor.pid"
    stop_file = tmp_path / "supervisor.stop"
    pid_file.write_text("456", encoding="utf-8")
    stop_file.write_text("owner", encoding="utf-8")
    monkeypatch.setattr(launch_workers, "SUPERVISOR_PID_FILE", pid_file)
    monkeypatch.setattr(launch_workers, "SUPERVISOR_STOP_FILE", stop_file)

    class BusyLock:
        def __enter__(self):
            raise FileLockError("busy")

        def __exit__(self, *_args):
            raise AssertionError("未持锁者不得执行退出清理")

    monkeypatch.setattr(launch_workers, "exclusive_lock", lambda *_args, **_kwargs: BusyLock())

    with pytest.raises(RuntimeError, match="已运行"):
        launch_workers.run_supervisor(poll_seconds=0.1)
    assert pid_file.read_text(encoding="utf-8") == "456"
    assert stop_file.read_text(encoding="utf-8") == "owner"


def test_two_supervisor_starts_only_lock_holder_reconciles(monkeypatch, tmp_path):
    import launch_workers
    from futu_quant.filelock import FileLockError

    pid_file = tmp_path / "supervisor.pid"
    stop_file = tmp_path / "supervisor.stop"
    monkeypatch.setattr(launch_workers, "SUPERVISOR_PID_FILE", pid_file)
    monkeypatch.setattr(launch_workers, "SUPERVISOR_STOP_FILE", stop_file)
    entries = 0
    reconciles = 0

    class RacingLock:
        def __enter__(self):
            nonlocal entries
            entries += 1
            if entries > 1:
                raise FileLockError("busy")
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(launch_workers, "exclusive_lock", lambda *_args, **_kwargs: RacingLock())

    def reconcile(**_kwargs):
        nonlocal reconciles
        reconciles += 1
        with pytest.raises(RuntimeError, match="已运行"):
            launch_workers.run_supervisor(poll_seconds=0.1)
        stop_file.write_text("stop", encoding="utf-8")

    monkeypatch.setattr(launch_workers, "reconcile_tenant_workers", reconcile)
    monkeypatch.setattr(launch_workers.time, "sleep", lambda _seconds: None)
    launch_workers.run_supervisor(poll_seconds=0.1)

    assert reconciles == 1


def test_reconcile_failure_isolated_and_alerted(monkeypatch):
    import launch_workers

    launch_workers._RETRY_STATE.clear()
    monkeypatch.setattr(
        launch_workers, "_read_tenant_switch_settings",
        lambda: launch_workers._ReadOnlySettings({
            "tenant_active:futu_real_master": "true",
            "tenant_active:futu_paper_sandbox": "false",
        }),
    )
    monkeypatch.setattr(launch_workers, "_worker_pid", lambda _tenant_id: None)
    monkeypatch.setattr(
        launch_workers, "launch_tenant_worker",
        lambda tenant, py_exe=None: (False, None, "boom"),
    )
    alerts: list[tuple[str, str, str]] = []

    result = launch_workers.reconcile_tenant_workers(notify=lambda *args: alerts.append(args))

    assert result["failed"] == ["futu_real_master"]
    assert alerts == [("ERROR", "TENANT_WORKER_START_FAILED", "futu_real_master: boom")]


def test_start_and_stop_scripts_manage_persistent_supervisor():
    encoding = "cp936"
    start = Path("start.bat").read_bytes().decode(encoding)
    stop = Path("stop.bat").read_bytes().decode(encoding)

    assert "launch_workers.py --supervise" in start
    assert "`r`n" not in start
    lines = start.splitlines()
    assert any("launch_workers.py --py" in line and "--supervise" not in line for line in lines)
    assert any("launch_workers.py --supervise" in line for line in lines)
    assert "tenant-worker-supervisor.pid" not in start
    assert "if not exist" not in next(
        line for line in lines if "launch_workers.py --supervise" in line
    ).lower()
    assert "tenant-worker-supervisor.stop" in stop


def test_launch_tenant_worker_fault_isolation(monkeypatch, tmp_path):
    """测试单个租户拉起异常时优雅捕获，实现故障隔离"""
    from launch_workers import launch_tenant_worker
    from futu_quant.tenant import DEFAULT_TENANTS

    tenant = DEFAULT_TENANTS["futu_real_master"]
    fake_pid_file = tmp_path / "trading-service-futu_real_master.pid"

    def mock_service_files(_tenant_id):
        return {
            "lock": tmp_path / "lock",
            "pid": fake_pid_file,
            "log": tmp_path / "log.log",
            "stop": tmp_path / "stop",
            "restart": tmp_path / "restart",
        }

    monkeypatch.setattr("launch_workers.service_files", mock_service_files)

    def mock_popen_fail(*args, **kwargs):
        raise OSError("模拟系统资源耗尽或执行权限拒绝")

    monkeypatch.setattr("subprocess.Popen", mock_popen_fail)

    ok, pid, msg = launch_tenant_worker(tenant)
    assert ok is False
    assert pid is None
    assert "启动失败" in msg
    assert "模拟系统资源耗尽" in msg

