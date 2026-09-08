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


def pid_alive(pid: int) -> bool:
    """Return whether *pid* refers to a live process."""
    if os.name == "nt":
        return _pid_alive_windows(int(pid))
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


def _pid_alive_windows(pid: int) -> bool:
    """Probe liveness via OpenProcess + GetExitCodeProcess (never kills)."""
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
        return code.value == STILL_ACTIVE
    finally:
        close_handle(handle)
