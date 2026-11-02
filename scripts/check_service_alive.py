r"""外部存活检测（watchdog）—— 服务死了之后由**服务之外**的人来喊。

## 为什么必须有它（2026-11-02 事故）

生产 Worker 停了约 19 小时，期间**一条告警都没有**：告警是服务自己发的，服务死了就没人报告。
`pid` 文件还在、进程已退出，页面也没人看 —— 只能靠人偶然发现。这是监控架构上的盲区，
不是某条规则漏配。

## 它做什么

只做**只读**判断，不启停任何进程、不写库：

1. `data/*.pid` 里的进程是否还活着（Windows 用 `tasklist` 判定）；
2. 关键 pid（`tenant-worker-supervisor` / `trading-service*`）是否齐全；
3. 最近一次日志写入距今是否超过阈值（默认 15 分钟）—— 兜住「进程在、但卡死不干活」；
4. 需要时推一张飞书告警卡片（复用 `FeishuReporter`，通道未配置就只在控制台喊）。

## 怎么用

手动跑一次（排查用）：

```bash
python scripts/check_service_alive.py
```

注册成 Windows 计划任务（**由用户从桌面执行**，每 10 分钟一次）：

```bat
schtasks /Create /TN "TradeyiWatchdog" /SC MINUTE /MO 10 ^
  /TR "E:\Thisyi\trade\.venv\Scripts\python.exe E:\Thisyi\trade\scripts\check_service_alive.py --push" ^
  /RL HIGHEST /F
```

删除：

```bat
schtasks /Delete /TN "TradeyiWatchdog" /F
```

## 退出码

- `0` 一切正常
- `1` 发现异常（进程缺失 / 进程已死 / 日志停滞）
- `2` 检测本身没跑成（例如 data 目录不存在）—— 也让计划任务显示失败，别静默

**禁止**据此脚本自动拉起服务：启动生产必须由用户从桌面双击 `start.bat`（2026-10-24 铁律）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REQUIRED_PIDS = ("tenant-worker-supervisor", "trading-service-futu_real_master")
LOG_STALE_MINUTES = 15


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_alive(pid: int) -> bool:
    """Windows 下用 tasklist 判定；非 Windows 用 os.kill(pid, 0)。"""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        out = (completed.stdout or "").strip()
        if not out or "No tasks" in out or "没有运行的任务" in out:
            return False
        return str(pid) in out
    try:
        import os

        os.kill(pid, 0)  # 只探测存在性，不发信号
        return True
    except (OSError, PermissionError):
        return False


def _latest_log_age_minutes(data_dir: Path | None = None) -> float | None:
    logs = list((data_dir or DATA).glob("*.log"))
    if not logs:
        return None
    newest = max(log.stat().st_mtime for log in logs)
    return (time.time() - newest) / 60.0


def collect_problems(
    data_dir: Path | None = None,
    process_alive=None,
    log_age_minutes=None,
) -> list[str]:
    """收集异常项。三个参数只为可测性注入：默认走真实实现（`data_dir` 默认生产 `data/`）。"""
    data_dir = Path(data_dir or DATA)
    is_alive = process_alive or _process_alive
    log_age = log_age_minutes or _latest_log_age_minutes

    problems: list[str] = []
    if not data_dir.is_dir():
        raise FileNotFoundError(f"数据目录不存在：{data_dir}")

    pid_files = sorted(data_dir.glob("*.pid"))
    if not pid_files:
        problems.append("data 目录里没有任何 pid 文件 —— 服务可能从未启动或已被 stop.bat 清理")

    for name in REQUIRED_PIDS:
        if not (data_dir / f"{name}.pid").is_file():
            problems.append(f"缺少关键 pid 文件：{name}.pid")

    for pid_file in pid_files:
        pid = _read_pid(pid_file)
        if pid is None:
            problems.append(f"pid 文件内容无法解析：{pid_file.name}")
            continue
        if not is_alive(pid):
            problems.append(f"进程已退出：{pid_file.name}（记录的 PID {pid} 不存在）")

    age = log_age(data_dir)
    if age is None:
        problems.append("data 目录下没有任何日志文件，无法判断服务是否在干活")
    elif age > LOG_STALE_MINUTES:
        problems.append(
            f"日志已 {age:.0f} 分钟没有写入（阈值 {LOG_STALE_MINUTES} 分钟）—— 可能进程在但卡死"
        )
    return problems


def push_alert(problems: list[str]) -> str:
    """推一张飞书告警卡片；返回三态描述。通道未配置时不抛错，只在控制台说明。

    store 用 **只读** 打开：检测脚本绝不能在服务停机时去写库或触发迁移。
    """
    message = "生产服务存活检测失败：\n" + "\n".join(f"- {item}" for item in problems)
    try:
        from futu_quant.alerts import feishu_configured
        from futu_quant.feishu_reporter import FeishuReporter
        from futu_quant.store import TradingStore

        store = TradingStore(read_only=True)
        if not feishu_configured(store):
            return "飞书未配置，未推送（仅控制台输出）"
        reporter = FeishuReporter(store)
        ok = reporter.send_alert_card("CRITICAL", "SERVICE_WATCHDOG_DOWN", message, is_recovered=False)
        return "已推送飞书" if ok else "飞书推送失败（见日志）"
    except Exception as exc:  # noqa: BLE001 - 检测脚本自身不许因上报失败而崩
        return f"推送异常（不影响退出码）：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="生产服务外部存活检测（只读）")
    parser.add_argument("--push", action="store_true", help="发现异常时推飞书告警")
    parser.add_argument("--quiet", action="store_true", help="正常时也打印一行摘要")
    args = parser.parse_args()

    try:
        problems = collect_problems()
    except Exception as exc:  # noqa: BLE001 - 检测本身没跑成要显式失败
        print(f"[watchdog] 检测未完成：{exc}")
        return 2

    if not problems:
        if args.quiet:
            print("[watchdog] 正常：pid 进程存活，日志新鲜")
        return 0

    print("[watchdog] 发现异常：")
    for item in problems:
        print(f"  - {item}")
    print("[watchdog] 请从桌面双击 start.bat 启动生产（本脚本不会自动拉起）")
    if args.push:
        print(f"[watchdog] {push_alert(problems)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
