"""
自适应多租户服务编排启动器 (Adaptive Multi-Tenant Worker Orchestrator)
读取租户注册中心，自动为所有处于启用状态 (is_active=True) 的租户自适应拉起独立的后台 Worker 进程。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import logging
import subprocess
import sys
import time
from typing import Callable

# 确保在导入 futu_quant 之前重定向 APPDATA 避免沙箱权限问题
if sys.platform == "win32":
    _local_appdata = Path(__file__).resolve().parents[1] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.process_watchdog import cmdline_matches
from futu_quant.subprocess_env import subprocess_env
from futu_quant.tenant import DEFAULT_TENANTS, TenantProfile, resolve_tenant_active
from run_service import service_files

logger = logging.getLogger("tenant_worker_supervisor")
SUPERVISOR_PID_FILE = Path(__file__).resolve().parents[1] / "data" / "tenant-worker-supervisor.pid"
SUPERVISOR_STOP_FILE = Path(__file__).resolve().parents[1] / "data" / "tenant-worker-supervisor.stop"
RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0
_RETRY_STATE: dict[str, tuple[int, float]] = {}


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
    # 子进程环境统一走 subprocess_env：注入 src 到 PYTHONPATH + UTF8，
    # 避免 venv 缺 editable .pth 时 worker 起不来（2026-10-28 假红根因）。
    sub_env = subprocess_env(extra={
        "PYTHONUNBUFFERED": "1",  # 与上面的 -u 双保险：stdout 重定向到文件时会块缓冲
    })

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


def _process_cmdline(pid: int) -> str | None:
    """读取进程命令行；读取失败即不信任该 PID。"""
    if sys.platform != "win32":
        try:
            return Path(f"/proc/{pid}/cmdline").read_text(errors="replace").replace("\0", " ")
        except OSError:
            return None
    command = (
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
        "-ErrorAction SilentlyContinue).CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    return (result.stdout or "").strip() or None


def _pid_matches(pid: int, markers: tuple[str, ...]) -> bool:
    return is_pid_alive(pid) and cmdline_matches(_process_cmdline(pid), markers)


def _worker_pid(tenant_id: str) -> int | None:
    pid_file = service_files(tenant_id)["pid"]
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    markers = ("run_service.py", f"--tenant {tenant_id}")
    return pid if _pid_matches(pid, markers) else None


def request_tenant_worker_stop(tenant: TenantProfile) -> tuple[bool, str]:
    """通过租户独立 stop 文件安全停机，绝不直接杀其他租户进程。"""
    files = service_files(tenant.tenant_id)
    pid = _worker_pid(tenant.tenant_id)
    if pid is None:
        files["pid"].unlink(missing_ok=True)
        files["stop"].unlink(missing_ok=True)
        return True, "租户已停止"
    try:
        files["stop"].parent.mkdir(parents=True, exist_ok=True)
        files["stop"].write_text("tenant disabled\n", encoding="utf-8")
        return True, f"已请求安全停机 (PID: {pid})"
    except OSError as exc:
        return False, f"写入租户停止标记失败: {exc}"


def reconcile_tenant_workers(
    *,
    py_exe: str | None = None,
    notify: Callable[[str, str, str], None] | None = None,
    now: float | None = None,
) -> dict[str, list[str]]:
    """将运行中的 Worker 与动态租户开关收敛一致。

    本函数只管理 Worker 进程；绝不写 ``REAL_TRADING_ENABLED``，也不改变任何
    下单/确认/额度门禁。单租户失败被隔离并告警，不影响其他租户。
    """
    active_ids = {tenant.tenant_id for tenant in get_active_tenants()}
    current = time.monotonic() if now is None else float(now)
    result: dict[str, list[str]] = {
        "started": [], "stopping": [], "unchanged": [], "backoff": [], "failed": [],
    }
    for tenant in DEFAULT_TENANTS.values():
        running = _worker_pid(tenant.tenant_id) is not None
        if tenant.tenant_id in active_ids:
            if running:
                _RETRY_STATE.pop(tenant.tenant_id, None)
                result["unchanged"].append(tenant.tenant_id)
                continue
            failures, retry_at = _RETRY_STATE.get(tenant.tenant_id, (0, 0.0))
            if current < retry_at:
                result["backoff"].append(tenant.tenant_id)
                continue
            # 清除过去停机留下的租户 stop 标记，否则新进程会立即退出。
            service_files(tenant.tenant_id)["stop"].unlink(missing_ok=True)
            ok, _pid, message = launch_tenant_worker(tenant, py_exe=py_exe)
            bucket = "started" if ok else "failed"
            result[bucket].append(tenant.tenant_id)
            if ok:
                _RETRY_STATE.pop(tenant.tenant_id, None)
            else:
                failures += 1
                delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (failures - 1)))
                _RETRY_STATE[tenant.tenant_id] = (failures, current + delay)
                if notify:
                    notify("ERROR", "TENANT_WORKER_START_FAILED", f"{tenant.tenant_id}: {message}")
        elif running:
            _RETRY_STATE.pop(tenant.tenant_id, None)
            ok, message = request_tenant_worker_stop(tenant)
            bucket = "stopping" if ok else "failed"
            result[bucket].append(tenant.tenant_id)
            if not ok and notify:
                notify("ERROR", "TENANT_WORKER_STOP_FAILED", f"{tenant.tenant_id}: {message}")
        else:
            result["unchanged"].append(tenant.tenant_id)
    return result


def _alert_notify(severity: str, event_type: str, message: str) -> None:
    """失败通过统一告警链投递；告警自身失败不击穿 supervisor。"""
    try:
        from futu_quant.alerts import AlertManager
        from futu_quant.store import TradingStore

        AlertManager(TradingStore()).send(
            severity, event_type, message, incident_key=f"tenant-worker-supervisor:{event_type}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("租户 Worker supervisor 告警投递失败: %s", message)


def run_supervisor(*, py_exe: str | None = None, poll_seconds: float = 5.0) -> None:
    """持续对账租户开关；精确身份单实例、幂等拉起、租户级退避。"""
    SUPERVISOR_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_pid = int(SUPERVISOR_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        existing_pid = 0
    markers = ("launch_workers.py", "--supervise")
    if existing_pid and existing_pid != os.getpid() and _pid_matches(existing_pid, markers):
        raise RuntimeError(f"租户 Worker supervisor 已运行 (PID: {existing_pid})")
    SUPERVISOR_STOP_FILE.unlink(missing_ok=True)
    SUPERVISOR_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    try:
        while not SUPERVISOR_STOP_FILE.exists():
            try:
                reconcile_tenant_workers(py_exe=py_exe, notify=_alert_notify)
            except Exception:  # noqa: BLE001 - supervisor 必须存活并在下一轮重试
                logger.exception("租户 Worker 动态对账失败")
            time.sleep(max(0.1, poll_seconds))
    finally:
        SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        SUPERVISOR_STOP_FILE.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive Multi-Tenant Worker Orchestrator")
    parser.add_argument("--check", action="store_true", help="仅检查所有启用租户状态，不拉起")
    parser.add_argument("--supervise", action="store_true", help="持续监听租户开关并运行时启停 Worker")
    parser.add_argument("--poll", type=float, default=5.0, help="supervisor 对账间隔秒数")
    parser.add_argument("--py", type=str, default=None, help="自定义 Python 解释器路径")
    args = parser.parse_args()

    if args.supervise:
        run_supervisor(py_exe=args.py, poll_seconds=args.poll)
        return

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
