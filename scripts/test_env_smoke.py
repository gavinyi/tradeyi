"""测试环境全链路冒烟（SIMULATE 模拟仓，绝不触碰生产）。

串起来的四段（用户要求「用模拟仓做测试全链路」）：

1. **初始化测试库**：`trade-test/data/trading.db`，按正式 schema + 全量迁移；
2. **跑一个调度周期**：`run_service.py --once --tenant futu_paper_sandbox`
   （只跑一轮，不常驻 —— 避免与生产模拟租户在同一 SIMULATE 账户上长期双跑）；
3. **下单/撤单探针**：$0.01 不可能成交价，验证「提交 → 券商受理 → 毫秒撤单 → 零成交」；
4. **隔离断言**：全过程前后读生产库 `runtime_config` 指纹，**必须完全一致**。

用法（在 `E:\\Thisyi\\trade-test` 下）：
    python scripts/test_env_smoke.py            # 全链路
    python scripts/test_env_smoke.py --skip-order  # 只跑周期 + 隔离断言
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = Path(r"E:\Thisyi\trade\data\trading.db")

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import test_env  # noqa: E402


def _print_safe(*args, **kwargs) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑脚本会直接崩，`pytest -s` 也会假红。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)


def _child_env() -> dict[str, str]:
    env = test_env.child_env(ROOT)
    env.update(
        {
            "ENABLE_PAPER_ORDERS": "true",
            "FUTU_TRADING_ENV": "SIMULATE",
            "REAL_TRADING_ENABLED": "false",
            # 测试区不依赖生产审批链（审批是上生产前的门禁，测试只为跑通链路）
            "REQUIRE_STRATEGY_APPROVAL": "false",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    return env


def production_fingerprint() -> str | None:
    """只读生产库 runtime_config 指纹（隔离断言用）。"""
    import hashlib
    import sqlite3

    if not PRODUCTION_DB.exists():
        return None
    connection = sqlite3.connect(f"file:{PRODUCTION_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT config_key, version, value_json FROM runtime_config ORDER BY config_key"
        ).fetchall()
    finally:
        connection.close()
    payload = "\n".join(f"{key}\x1f{version}\x1f{value}" for key, version, value in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_step(name: str, cmd: list[str], *, timeout: int, env: dict[str, str]) -> bool:
    print(f"\n=== {name} ===")
    print("$", " ".join(cmd))
    # Windows 默认按本地代码页（GBK）解码子进程输出，遇到 UTF-8 中文会抛
    # UnicodeDecodeError（本机已实测），所以显式指定 utf-8 + replace。
    completed = subprocess.run(
        cmd, cwd=str(ROOT), env=env, capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )
    tail = (completed.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("  ", line)
    if completed.returncode != 0:
        print(f"  [FAILED] exit={completed.returncode}")
        stderr_tail = (completed.stderr or "").strip().splitlines()[-6:]
        for line in stderr_tail:
            print("  ", line)
    return completed.returncode == 0


def verify_test_db() -> dict:
    """核对测试库确实承载了这条链路的数据。"""
    import sqlite3

    db = test_env.db_path(ROOT)
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        tasks = connection.execute("SELECT count(1) FROM task_runs").fetchone()[0]
        successes = connection.execute(
            "SELECT count(1) FROM task_runs WHERE status='SUCCESS'"
        ).fetchone()[0]
        intents = connection.execute("SELECT count(1) FROM order_intents").fetchone()[0]
        heartbeats = connection.execute(
            "SELECT count(1) FROM strategy_heartbeats"
        ).fetchone()[0]
        migrations = connection.execute(
            "SELECT count(1) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "task_runs": tasks, "task_success": successes,
        "order_intents": intents, "heartbeats": heartbeats,
        "migrations": migrations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="测试环境全链路冒烟（SIMULATE）")
    parser.add_argument("--skip-order", action="store_true")
    parser.add_argument("--tenant", default="futu_paper_sandbox")
    args = parser.parse_args()

    test_env.assert_not_production(ROOT)
    env = _child_env()
    before = production_fingerprint()
    print(f"生产库指纹（开始）：{(before or '未读取')[:16]}")

    db = test_env.initialize_database(ROOT)
    print(f"测试库：{db}")

    python = str(ROOT / ".venv" / "Scripts" / "python.exe")
    cycle_ok = run_step(
        "调度周期（--once，单轮不常驻）",
        [python, "-u", "scripts/run_service.py", "--once", "--tenant", args.tenant],
        timeout=300, env=env,
    )
    order_ok = True
    if not args.skip_order:
        order_ok = run_step(
            "下单/撤单探针（$0.01 不可能成交价）",
            [python, "-u", "scripts/probe_order_submit_cancel.py",
             "--tenant", args.tenant, "--symbol", "US.AAPL"],
            timeout=240, env=env,
        )

    stats = verify_test_db()
    after = production_fingerprint()
    print("\n=== 测试库数据 ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"生产库指纹（结束）：{(after or '未读取')[:16]}")
    isolated = before == after
    _print_safe(f"隔离断言（生产库未被写）：{'✅ 一致' if isolated else '❌ 指纹变化'}")

    ok = cycle_ok and order_ok and isolated and stats["task_success"] > 0
    _print_safe("\n结论：", "✅ 全链路通过" if ok else "❌ 有环节未通过（见上）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
