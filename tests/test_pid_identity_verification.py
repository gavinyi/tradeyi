"""回归：PID 存活判定必须校验进程身份，不能只看 PID 是否存在。

背景（10-23 凌晨，Chief/Ops 定位）：
`data/volatility-radar.pid` 里存的 **8208** 已被系统回收复用给 **`csrss.exe`**。
`start.bat` 的 `:pid_alive` 只判断「这个 PID 存在」→ 认为雷达还活着 →
`goto radar_done` **直接跳过启动雷达** → 03:29 那次重启里雷达被静默跳过、
整个启动失败（雷达日志因此一直停在 00:15:29）。

同类缺陷当时存在于**三处**，后果各不相同：
1. `start.bat` —— 启动路径，服务被静默跳过（已修，Ops）；
2. `scripts/launch_workers.py` —— Worker 启动路径，租户 Worker 静默不启动（已修，Chief）；
3. **`src/futu_quant/process_utils.py:_pid_alive_windows`** —— **运行时**状态判定，
   被 `service_control` / `radar_control` 共用：
   - `status()` 会把「PID 被复用的陌生进程」报成"服务运行中" → 界面状态错误；
   - `stop()` 的 `while _pid_alive(...)` 等待循环会因系统进程**永远活跃**而**挂死**。

本文件锁定「服务状态判定必须校验身份」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.process_utils import pid_alive  # noqa: E402


def _non_python_pid() -> int | None:
    """找一个**非 python** 的活跃进程 PID（用于复现 PID 复用场景）。

    用 Toolhelp 快照枚举，优先取 csrss/smss/wininit 这类系统进程
    （跳过 PID 0=Idle、4=System —— 它们不可 OpenProcess）。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import os
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return None

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
            ]

        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        candidates: list[tuple[int, str]] = []
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                pid = int(entry.th32ProcessID)
                name = entry.szExeFile.decode(errors="replace").lower()
                if pid > 4 and not name.endswith(("python.exe", "pythonw.exe")):
                    candidates.append((pid, name))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)
        # 优先系统进程（正是 PID 复用的目标），其次任意非 python 进程
        for preferred in ("csrss.exe", "smss.exe", "wininit.exe", "services.exe", "svchost.exe"):
            for pid, name in candidates:
                if name == preferred:
                    return pid
        own = os.getpid()
        return next((pid for pid, _ in candidates if pid != own), None)
    except Exception:
        return None


def test_invalid_pids_are_not_alive():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(999999) is False


def test_own_process_is_alive_with_python_only():
    """当前测试进程是 python —— 必须被 python_only 判定为存活。"""
    if sys.platform == "win32":
        assert pid_alive(int(__import__("os").getpid()), python_only=True) is True


@pytest.mark.skipif(sys.platform != "win32", reason="PID 复用是 Windows 特有现象")
def test_system_process_is_rejected_with_python_only():
    """核心：非 python 进程（PID 复用的目标）必须被 python_only 拒绝。

    这正是今晚 8208 → csrss.exe 的场景：只判 PID 存活会返回 True（误判），
    校验身份后返回 False（正确）。
    """
    pid = _non_python_pid()
    if pid is None:
        pytest.skip("未能找到非 python 的活跃进程")
    assert pid_alive(pid) is True, "系统进程应当是活跃的（前提）"
    assert pid_alive(pid, python_only=True) is False, (
        f"PID {pid} 是非 python 进程 —— python_only 必须拒绝，"
        "否则 PID 复用会让服务被静默跳过"
    )


def test_service_control_uses_identity_check():
    """服务状态判定必须走 python_only（否则 stop 会挂死、status 会误报）。"""
    src = (ROOT / "src" / "futu_quant" / "service_control.py").read_text(encoding="utf-8")
    assert "python_only=True" in src, (
        "service_control._pid_alive 未校验进程身份 —— stop() 等待循环会挂死"
    )
    radar = (ROOT / "src" / "futu_quant" / "radar_control.py").read_text(encoding="utf-8")
    assert "python_only=True" in radar, "radar_control._pid_alive 未校验进程身份"


def test_pid_alive_default_keeps_backward_compatible_behaviour():
    """默认参数不得改变既有行为（crypto 等调用方未传 python_only）。"""
    import inspect

    sig = inspect.signature(pid_alive)
    assert sig.parameters["python_only"].default is False, (
        "python_only 默认值必须是 False —— 否则会破坏未迁移的调用方"
    )
