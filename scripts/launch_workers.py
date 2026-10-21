"""
自适应多租户服务编排启动器 (Adaptive Multi-Tenant Worker Orchestrator)
读取租户注册中心，自动为所有处于启用状态 (is_active=True) 的租户自适应拉起独立的后台 Worker 进程。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

# 确保在导入 futu_quant 之前重定向 APPDATA 避免沙箱权限问题
if sys.platform == "win32":
    _local_appdata = Path(__file__).resolve().parents[1] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.tenant import DEFAULT_TENANTS, TenantProfile
from run_service import service_files


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_active_tenants() -> list[TenantProfile]:
    """获取所有处于启用状态的租户"""
    return [prof for prof in DEFAULT_TENANTS.values() if prof.is_active]


def launch_tenant_worker(tenant: TenantProfile, py_exe: str | None = None) -> tuple[bool, int | None, str]:
    """拉起单个租户的独立 Worker 守护进程"""
    py = py_exe or sys.executable
    files = service_files(tenant.tenant_id)
    pid_file = files["pid"]
    log_file_path = files["log"]

    # 1. 检查是否已有活跃实例
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            if is_pid_alive(old_pid):
                return True, old_pid, f"租户已在运行中 (PID: {old_pid})"
            else:
                pid_file.unlink(missing_ok=True)
        except Exception:
            pid_file.unlink(missing_ok=True)

    # 2. 准备启动参数与日志输出文件
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_file_path, "a", encoding="utf-8")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    root_dir = Path(__file__).resolve().parents[1]
    cmd = [py, "-u", str(root_dir / "scripts" / "run_service.py"), "--tenant", tenant.tenant_id]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root_dir),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=(sys.platform != "win32"),
        )
        time.sleep(1.0)
        new_pid = proc.pid
        return True, new_pid, f"启动成功 (PID: {new_pid})"
    except Exception as exc:
        return False, None, f"启动失败: {exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive Multi-Tenant Worker Orchestrator")
    parser.add_argument("--check", action="store_true", help="仅检查所有启用租户状态，不拉起")
    parser.add_argument("--py", type=str, default=None, help="自定义 Python 解释器路径")
    args = parser.parse_args()

    active_tenants = get_active_tenants()
    if not active_tenants:
        print("[!] 警告：注册表中未发现任何处于启用状态 (is_active=True) 的租户。")
        sys.exit(1)

    print(f"[*] 发现 {len(active_tenants)} 个活跃启用租户，开始自适应调度编排：")
    for t in active_tenants:
        env_tag = "实盘真金" if t.trading_env == "REAL" else "模拟沙盒"
        if args.check:
            files = service_files(t.tenant_id)
            pid = None
            alive = False
            if files["pid"].exists():
                try:
                    pid = int(files["pid"].read_text().strip())
                    alive = is_pid_alive(pid)
                except Exception:
                    pass
            status_desc = f"正在运行 (PID: {pid})" if alive else "已停止"
            print(f"    -> [{env_tag}] {t.display_name} ({t.tenant_id}): {status_desc}")
        else:
            ok, pid, msg = launch_tenant_worker(t, py_exe=args.py)
            mark = "[✓]" if ok else "[✗]"
            print(f"    {mark} [{env_tag}] {t.display_name} ({t.tenant_id}): {msg}")


if __name__ == "__main__":
    main()
