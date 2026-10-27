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

