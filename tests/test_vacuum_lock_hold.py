"""VACUUM 抢锁守卫：探测与执行必须用**同一把锁**，中途不释放。

2026-10-27 事故：`ac32dd3` 只做到「抢锁探测 → 立刻释放」，探测与 VACUUM 之间有一段
无锁窗口。服务恰好在这段里启动，就会对活库执行 VACUUM（阻塞对账/下单，可能写坏 1.6 GB 库）。

变异可红：把 `_hold_service_locks` 的 `yield` 之前改成「抢完就 unlock」，
`test_lock_held_across_whole_body` 会转红。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from futu_quant.filelock import FileLockError, lock_exclusive, unlock_exclusive

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vacuum_db.py"
_spec = importlib.util.spec_from_file_location("vacuum_db_under_test", _SCRIPT)
vacuum_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vacuum_db)


def _make_root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trading-service.lock").write_bytes(b"")
    (tmp_path / "data" / "volatility-radar.lock").write_bytes(b"")
    return tmp_path


def test_lock_held_across_whole_body(tmp_path):
    """进入上下文后，同一把锁必须**已被持有**：第二个抢锁者必须失败。"""
    root = _make_root(tmp_path)
    with vacuum_db._hold_service_locks(root):
        for name in ("trading-service.lock", "volatility-radar.lock"):
            handle = (root / "data" / name).open("a+b")
            try:
                with pytest.raises(FileLockError):
                    lock_exclusive(handle, blocking=False)
            finally:
                handle.close()


def test_pre_held_lock_rejects_vacuum(tmp_path):
    """服务真在跑（锁被持有）时，必须拒绝，而不是等一会儿再抢。"""
    root = _make_root(tmp_path)
    holder = (root / "data" / "trading-service.lock").open("a+b")
    lock_exclusive(holder, blocking=False)
    try:
        with pytest.raises(RuntimeError, match="服务锁仍被持有"):
            with vacuum_db._hold_service_locks(root):
                pytest.fail("不应该进入上下文")
    finally:
        unlock_exclusive(holder)
        holder.close()


def test_locks_released_after_context(tmp_path):
    """退出上下文必须释放，否则下次维护永远抢不到。"""
    root = _make_root(tmp_path)
    with vacuum_db._hold_service_locks(root):
        pass
    handle = (root / "data" / "trading-service.lock").open("a+b")
    try:
        lock_exclusive(handle, blocking=False)
        unlock_exclusive(handle)
    finally:
        handle.close()
