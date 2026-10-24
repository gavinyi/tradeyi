"""测试环境运行器（用户 2026-10-24 要求：测试区要能跑**全部功能**全流程验证）。

与生产的三条硬隔离：

1. **代码来源**：只允许在 `E:\\Thisyi\\trade-test` 工作区内运行；
   从生产目录或开发 worktree 调用一律拒绝（否则 DB/锁/PID 会写到别处）。
2. **数据库**：`trade-test/data/trading.db`（本工作区自己的库），
   `TRADEYI_ENV=test` 注入给所有子进程；**绝不触碰生产库**。
3. **端口**：Streamlit 用 **8601**（生产 8501），避免抢占生产 UI；
   OpenD 仍用 11111（SIMULATE 账户，只读行情 + 模拟下单，不下真金单）。

用法（在 `E:\\Thisyi\\trade-test` 下）：
    python scripts/test_env.py start
    python scripts/test_env.py status
    python scripts/test_env.py stop
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PRODUCTION_ROOT = Path(r"E:\Thisyi\trade").resolve()
TEST_ROOT = Path(r"E:\Thisyi\trade-test").resolve()

TEST_STREAMLIT_PORT = 8601
TEST_TENANT = "futu_paper_sandbox"
TEST_DB_RELATIVE = Path("data") / "trading.db"


def assert_not_production(root: Path | None = None) -> None:
    """只允许在测试工作区运行；生产目录/开发 worktree 一律拒绝。"""
    current = (root or ROOT).resolve()
    if current == PRODUCTION_ROOT:
        raise SystemExit(
            f"拒绝执行：当前目录是生产根 {PRODUCTION_ROOT} —— 测试环境运行器不能在生产区启动"
        )
    if current != TEST_ROOT:
        raise SystemExit(
            f"拒绝执行：当前目录 {current} 不是测试工作区 {TEST_ROOT} —— "
            "请先 cd 到 E:\\Thisyi\\trade-test"
        )


def db_path(root: Path | None = None) -> Path:
    path = ((root or ROOT) / TEST_DB_RELATIVE).resolve()
    if PRODUCTION_ROOT in path.parents:
        raise SystemExit(f"拒绝执行：测试库落在生产目录内 {path}")
    if (root or ROOT).resolve() not in path.parents:
        raise SystemExit(f"拒绝执行：测试库不在测试工作区内 {path}")
    return path


def child_env(root: Path | None = None) -> dict[str, str]:
    """子进程环境：标记测试区、指到独立 DB、独立 TEMP。"""
    base = (root or ROOT).resolve()
    env = dict(os.environ)
    env["TRADEYI_ENV"] = "test"
    env["TRADEYI_TEST_ROOT"] = str(base)
    env["TRADEYI_DB_PATH"] = str(db_path(base))
    qa = base / "data" / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(qa)
    env["TMP"] = str(qa)
    env["PYTHONPATH"] = str(base / "src")
    env["ENABLE_PAPER_ORDERS"] = "true"
    return env


def initialize_database(root: Path | None = None) -> Path:
    """按正式 schema 初始化测试库（含全部版本化迁移），不写入任何生产数据。"""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    from futu_quant.store import TradingStore

    store = TradingStore(path)  # initialize() + apply_migrations()
    store.set_setting("ENABLE_PAPER_ORDERS", "true")
    store.set_setting("enable_paper_orders", "true")
    return path


def planned_services(root: Path | None = None) -> list[dict]:
    """要拉起的测试服务清单（只含 SIMULATE 模拟租户 + 雷达 + UI）。"""
    base = (root or ROOT).resolve()
    py = base / ".venv" / "Scripts" / "python.exe"
    return [
        {
            "name": "worker",
            "pid_file": base / "data" / f"trading-service-{TEST_TENANT}.pid",
            "cmd": [str(py), "-u", "scripts/run_service.py", "--tenant", TEST_TENANT],
        },
        {
            "name": "radar",
            "pid_file": base / "data" / "volatility-radar.pid",
            "cmd": [str(py), "-u", "scripts/run_radar_service.py"],
        },
        {
            "name": "ui",
            "pid_file": base / "data" / "test-ui.pid",
            "cmd": [str(py), "-m", "streamlit", "run", "app.py", "--server.port", str(TEST_STREAMLIT_PORT)],
        },
    ]


def start(root: Path | None = None, *, dry_run: bool = False) -> list[str]:
    assert_not_production(root)
    base = (root or ROOT).resolve()
    initialize_database(base)
    env = child_env(base)
    started: list[str] = []
    for service in planned_services(base):
        if dry_run:
            started.append(f"[dry-run] {service['name']}: {' '.join(service['cmd'])}")
            continue
        process = subprocess.Popen(  # 子进程独立运行，生命周期不挂在调用方
            service["cmd"], cwd=str(base), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        service["pid_file"].write_text(str(process.pid), encoding="utf-8")
        started.append(f"{service['name']} pid={process.pid}")
    return started


def stop(root: Path | None = None) -> list[str]:
    assert_not_production(root)
    base = (root or ROOT).resolve()
    stopped: list[str] = []
    for service in planned_services(base):
        pid_file = service["pid_file"]
        if not pid_file.exists():
            continue
        raw = pid_file.read_text(encoding="utf-8", errors="replace").strip()
        pid_file.unlink(missing_ok=True)
        if not raw.isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/PID", raw, "/T", "/F"],
                           capture_output=True, check=False)
            stopped.append(f"{service['name']} pid={raw}")
        except Exception:  # noqa: BLE001
            continue
    return stopped


def status(root: Path | None = None) -> dict:
    assert_not_production(root)
    base = (root or ROOT).resolve()
    db = db_path(base)
    return {
        "root": str(base),
        "db": str(db),
        "db_exists": db.exists(),
        "streamlit_port": TEST_STREAMLIT_PORT,
        "tenant": TEST_TENANT,
        "pid_files": {
            service["name"]: service["pid_file"].exists()
            for service in planned_services(base)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="测试环境运行器（独立 DB/端口/租户）")
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.action == "start":
        for line in start(dry_run=args.dry_run):
            print(line)
    elif args.action == "stop":
        for line in stop():
            print(line)
    else:
        info = status()
        for key, value in info.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
