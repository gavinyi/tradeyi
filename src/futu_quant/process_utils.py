"""Process helpers that behave sanely on both POSIX and Windows.

Two pitfalls on Windows motivate this module:

* ``os.kill(pid, 0)`` is a *process existence probe* on POSIX but on Windows
  it calls ``TerminateProcess`` and unconditionally kills the process — a mere
  status check must never use it.
* ``os.kill(pid, signal.SIGTERM)`` terminates the process outright on Windows
  (no signal handler can run), so graceful shutdown has to go through the
  application's ``*.stop`` flag files instead.
"""

from __future__ import annotations

import ctypes
import os
import signal
from ctypes import wintypes


def pid_alive(pid: int, *, python_only: bool = False) -> bool:
    """Return whether *pid* refers to a live process.

    ``python_only=True`` 时额外校验进程映像是 ``python``/``pythonw`` ——
    用于区分「PID 被系统回收复用」。Windows 会复用 PID：10-23 凌晨
    `volatility-radar.pid` 里的 8208 被 `csrss.exe` 复用，
    `start.bat` 与 `launch_workers.is_pid_alive` 因此误判"服务还活着"，
    服务被**静默跳过**。服务状态判定（`service_control` / `radar_control`）
    是同一个坑，而且更危险：``stop()`` 的等待循环会因系统进程永远活跃而**挂死**。

    默认 ``False`` 保持既有行为（POSIX 探针与其他调用方不受影响）。
    """
    if os.name == "nt":
        return _pid_alive_windows(int(pid), python_only=python_only)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live process owned by another user is still live.
        return True


def request_stop(pid: int, *, force_on_windows: bool = True) -> None:
    """Ask a tracked child process to stop.

    POSIX delivers ``SIGTERM`` so the service can exit gracefully.  Windows
    cannot deliver signals: when ``force_on_windows`` is true the process is
    terminated directly (exit code 0), otherwise nothing is sent and callers
    must rely on the ``*.stop`` flag files the services poll.
    """
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        return
    if force_on_windows:
        try:
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        except ProcessLookupError:
            pass
        except OSError:
            pass


def _pid_alive_windows(pid: int, *, python_only: bool = False) -> bool:
    """Probe liveness via OpenProcess + GetExitCodeProcess (never kills).

    `python_only=True` 时再加一道映像名校验（`QueryFullProcessImageNameW`）——
    取不到映像名即视为不匹配（宁可误判为"未运行"而重新拉起，
    也不要因 PID 复用而让服务被静默跳过）。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION lets us inspect any process we own or
    # that runs as the same user without requiring administrative rights.
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED usually means a live process we may not inspect.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(code)):
            return True  # Cannot tell; assume alive rather than kill logic
        if code.value != STILL_ACTIVE:
            return False
        if not python_only:
            return True
        return _win_image_is_python(kernel32, handle)
    finally:
        close_handle(handle)


def _win_image_is_python(kernel32, handle) -> bool:
    """校验进程映像名是否为 python/pythonw（排除 PID 被系统进程复用）。"""
    try:
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                          ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query(handle, 0, buffer, ctypes.byref(size)):
            return False
        name = (buffer.value or "").lower()
        return name.endswith(("python.exe", "pythonw.exe"))
    except Exception:
        return False
