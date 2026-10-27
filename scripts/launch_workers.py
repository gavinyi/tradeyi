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

from futu_quant.tenant import DEFAULT_TENANTS, TenantProfile, resolve_tenant_active
from run_service import service_files


def _win_process_image_name(pid: int) -> str:
    """返回进程映像的完整路径；取不到返回空串。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def is_pid_alive(pid: int) -> bool:
    """判断 pid 是否是**我们的** Worker，而不只是"这个 PID 活着"。

    Windows 会回收复用 PID：今晚 `volatility-radar.pid` 里的 8208 被 `csrss.exe`
    复用，`start.bat` 因此判定"雷达还活着"、直接跳过启动 —— 服务被静默跳过。
    Worker 启动路径上是同一个坑，而且更危险：一旦某租户的 PID 文件里是一个被复用的
    陌生 PID，这里就会返回 True → **该租户的 Worker 静默不启动**。
    所以除了"进程活跃"，还必须校验映像名是 python/pythonw。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                exit_code = wintypes.DWORD()
                success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel32.CloseHandle(handle)
                if not (success and exit_code.value == 259):
                    return False
            else:
                return False
            image = _win_process_image_name(pid).rsplit("\\", 1)[-1].lower()
            return image.startswith("python")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _ReadOnlySettings:
    """只读设置视图：让 `resolve_tenant_active` 走同一份判定逻辑。

    启动器**不得**构造 `TradingStore`（会触发 initialize/迁移副作用），
    所以这里用 sqlite3 只读连接读 `system_settings`，再包成 `get_setting`。
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get_setting(self, key: str, default: str = "") -> str:  # noqa: D401
        return self._values.get(key, default)


def _read_tenant_switch_settings() -> _ReadOnlySettings:
    import sqlite3

    from futu_quant.store import DEFAULT_DB

    values: dict[str, str] = {}
    try:
        if not Path(DEFAULT_DB).exists():
            return _ReadOnlySettings(values)
        con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select key, value from system_settings where key like 'tenant_active:%'"
            ).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 读不到开关就回落注册表默认值（不阻断启动）
        return _ReadOnlySettings(values)
    for key, value in rows:
        values[str(key)] = "" if value is None else str(value)
    return _ReadOnlySettings(values)


def get_active_tenants() -> list[TenantProfile]:
    """返回**开关启用**的租户（`tenant_active:<id>` 优先，缺省回落注册表默认值）。

    2026-10-28 用户裁定：「开了几个就跑几个」。原先只认 `TenantProfile.is_active`
    这个代码常量，界面上关掉实盘账号也照旧拉起实盘 Worker。
    """
    settings = _read_tenant_switch_settings()
    return [
        profile
        for profile in DEFAULT_TENANTS.values()
        if resolve_tenant_active(profile.tenant_id, store=settings)
    ]


def get_disabled_tenants() -> list[TenantProfile]:
    """开关关闭的租户（启动时打印一行，便于核对「为什么只起了一个」）。"""
    active_ids = {profile.tenant_id for profile in get_active_tenants()}
    return [
        profile for profile in DEFAULT_TENANTS.values() if profile.tenant_id not in active_ids
    ]


def launch_tenant_worker(tenant: TenantProfile, py_exe: str | None = None) -> tuple[bool, int | None, str]:
    """拉起单个租户的独立 Worker 守护进程"""
    py_candidate = Path(py_exe or sys.executable)
    if sys.platform == "win32":
        pyw = py_candidate.parent / "pythonw.exe"
        py = str(pyw) if pyw.exists() else str(py_candidate)
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        py = str(py_candidate)
        creationflags = 0

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

    root_dir = Path(__file__).resolve().parents[1]
    cmd = [py, "-u", str(root_dir / "scripts" / "run_service.py"), "--tenant", tenant.tenant_id]
    sub_env = os.environ.copy()
    sub_env["PYTHONUTF8"] = "1"
    sub_env["PYTHONIOENCODING"] = "utf-8"
    # Unbuffered stdout (belt and braces with the "-u" above): Python block-buffers
    # stdout when it is a file, which freezes the worker log and makes a running
    # worker invisible.
    sub_env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root_dir),
            env=sub_env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=(sys.platform != "win32"),
        )
        time.sleep(1.0)
        exit_code = proc.poll()
        if exit_code is not None:
            return False, None, f"启动后异常退出 (退出码: {exit_code})，详情见 {log_file_path.name}"
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
    disabled = get_disabled_tenants()
    if disabled:
        names = ", ".join(f"{p.tenant_id}（{p.display_name}）" for p in disabled)
        print(f"[*] 开关关闭、不启动: {names}")
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
