"""数据库停机维护：`VACUUM` 回收空间（P2-14 效率）。

为什么单独成脚本、且必须带守卫：
- `VACUUM` 需要独占写锁，**服务在跑时执行会失败或长时间阻塞**（对账/下单都会卡）；
- 它会把整库重写到临时文件，磁盘不够会中途失败（生产库 1.6 GB 级）；
- 这不是"优化一下"那么随意 —— 必须停机、必须留操作记录。

用法（**先双击 stop.bat，再跑**）：
    python scripts/vacuum_db.py --dry-run     # 只看能回收多少
    python scripts/vacuum_db.py --yes         # 真正执行
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from futu_quant.filelock import FileLockError, lock_exclusive, unlock_exclusive

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "trading.db"

LOCK_MARKERS = ("trading-service.lock", "volatility-radar.lock")


def _lock_held(path: Path) -> bool:
    """锁文件是否被服务真实持有：非阻塞抢同一把建议锁。

    只看文件存在不行（stop.bat/进程退出都不删 .lock）；
    用 ``open(path, "a")`` 试开也不行：Windows 字节锁（``msvcrt.locking``）
    不阻止别人 open，服务真在跑也会成功打开（2026-10-25 实测 LockFile 持锁 +
    open 仍成功），等于把守卫改成 fail-open —— 那会让 ``--yes`` 在服务运行中放行
    VACUUM，直接破坏 1 GB 生产库。
    """
    try:
        handle = path.open("a+b")
    except OSError:
        return True
    try:
        lock_exclusive(handle, blocking=False)
    except FileLockError:
        return True
    else:
        unlock_exclusive(handle)
        return False
    finally:
        handle.close()


def _service_running(root: Path) -> list[str]:
    """服务是否还在跑：看 PID 文件与实际持锁状态。"""
    running = []
    data_dir = root / "data"
    for p in data_dir.glob("*.pid"):
        running.append(p.name)
    for pattern in ("trading-service*.lock", "volatility-radar.lock"):
        for p in data_dir.glob(pattern):
            if _lock_held(p):
                running.append(p.name)
    return running


def _stats(db: Path) -> dict:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        connection.close()
    return {
        "size_mb": round(page_size * page_count / 1024 / 1024, 1),
        "reclaimable_mb": round(page_size * freelist / 1024 / 1024, 1),
        "freelist_pages": freelist,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="停机 VACUUM（回收 SQLite 空间）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="确认已停机并执行")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"数据库不存在：{db}")
        return 2

    running = _service_running(ROOT)
    stats = _stats(db)
    print(f"数据库：{db}")
    print(f"当前大小：{stats['size_mb']} MB，可回收约 {stats['reclaimable_mb']} MB")
    if running:
        print(f"❌ 检测到服务锁文件，服务可能仍在运行：{running}")
        print("   请先双击 stop.bat，确认进程全部退出后再执行。")
        return 3
    if args.dry_run:
        print("（dry-run：未执行 VACUUM）")
        return 0
    if not args.yes:
        print("未带 --yes，不执行。VACUUM 需要停机并独占数据库。")
        return 1

    free_disk = shutil.disk_usage(db.parent).free
    if free_disk < stats["size_mb"] * 1024 * 1024 * 1.2:
        print(f"❌ 磁盘余量不足（剩余 {free_disk / 1024 / 1024 / 1024:.1f} GB），VACUUM 需约 1.2 倍库大小")
        return 4

    qa_dir = ROOT / "data" / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(qa_dir)
    os.environ["TMP"] = str(qa_dir)

    connection = sqlite3.connect(str(db))
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    after = _stats(db)
    print(f"✅ VACUUM 完成：{stats['size_mb']} MB -> {after['size_mb']} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
