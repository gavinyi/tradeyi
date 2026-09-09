"""Minimal cross-platform advisory file locking.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` on a one-byte
range, which is the Windows equivalent of an advisory exclusive lock and is
released automatically when the owning handle is closed (including process
exit).  No third-party dependency is required, which keeps the project
installable from ``uv.lock`` alone.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import IO

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class FileLockError(OSError):
    """Raised when a non-blocking lock is already held elsewhere."""


def lock_exclusive(fh: IO[bytes], *, blocking: bool = True) -> None:
    """Acquire an exclusive advisory lock on an open *binary* file object.

    The file handle must be kept open while the lock is held; closing it
    (or the process exiting) releases the lock on every supported platform.
    """
    if fcntl is not None:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError as exc:  # POSIX: lock held elsewhere
            raise FileLockError(exc.errno, exc.strerror) from exc
        return
    # Windows: msvcrt locks a byte range starting at the current position and
    # the range must already exist in the file, so make sure at least one byte
    # is present before locking byte 0.
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(fh.fileno(), mode, 1)
    except OSError as exc:  # Windows: lock held elsewhere (EACCES)
        raise FileLockError(exc.errno, exc.strerror) from exc


def unlock_exclusive(fh: IO[bytes]) -> None:
    """Release an exclusive lock previously taken with :func:`lock_exclusive`."""
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        with contextlib.suppress(OSError):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def exclusive_lock(path: Path | str, *, blocking: bool = True):
    """Context manager holding an exclusive advisory lock on *path*."""
    handle = open(path, "a+b")
    try:
        lock_exclusive(handle, blocking=blocking)
        yield handle
    finally:
        with contextlib.suppress(Exception):
            unlock_exclusive(handle)
        handle.close()
