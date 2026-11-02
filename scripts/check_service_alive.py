r"""外部存活检测（watchdog）—— 服务死了之后由**服务之外**的人来喊。

## 为什么必须有它（2026-11-02 事故）

生产 Worker 停了约 19 小时，期间**一条告警都没有**：告警是服务自己发的，服务死了就没人报告。
`pid` 文件还在、进程已退出，页面也没人看 —— 只能靠人偶然发现。这是监控架构上的盲区，
不是某条规则漏配。

## 它做什么

只做**只读**判断，不启停任何进程、不写库：

1. `data/*.pid` 里的进程是否还活着（Windows 用 `tasklist` 判定）；
2. 关键 pid（`tenant-worker-supervisor` / `trading-service*`）是否齐全；
3. **服务心跳**（`strategy_heartbeats` 最新 `updated_at`）是否新鲜 —— **这是「活着」的
   主判据**：服务每轮都写心跳，日志只是副作用；
4. 日志停滞（默认 15 分钟）**仅在美股常规交易时段**作为辅助信号 —— 休市/周末日志本来就不写，
   拿它判活会每晚刷几十条假 CRITICAL；
5. 连续失败升级：第一次 `WARNING`，连续第 2 次起才 `CRITICAL`（抑制窗口，避免抖动刷屏）；
6. **恢复通知**：上一次报过警、这次正常 → 推一条 `is_recovered=True` 的恢复卡片再清状态，
   否则修好了也没人知道闭环。

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
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
#: 逐租户判断：实盘停了、纸盘还在写是**很常见**的形态，只看「最新一条心跳」会误判为健康。
TENANTS = ("futu_real_master", "futu_paper_sandbox")
#: 每个租户必须有自己那支 worker 的 pid 文件。
#: **不要用** `trading-service.pid` —— 它指向「谁最后启动就是谁」，身份会漂（Ops 2026-11-02 实测）。
REQUIRED_PIDS = ("tenant-worker-supervisor",) + tuple(f"trading-service-{t}" for t in TENANTS)
#: 心跳阈值 5 分钟：实测最差 49 秒，留 6 倍余量（Ops 实测）。
HEARTBEAT_STALE_MINUTES = 5
#: 日志仅作**辅助**信号，且只在常规交易时段判：高频率任务每 11 秒跑一次，日志反而不常写，
#: 实测健康状态下两份 service 日志也能 14 分钟不写（Ops 2026-11-02 实测）。
LOG_STALE_MINUTES = 60
#: 连续失败第几次起升级为 CRITICAL（第一次只 WARNING，压住抖动）。
ESCALATE_AFTER = 2
STATE_FILE_NAME = "watchdog_state.json"


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


def heartbeat_age_minutes(tenant_id: str) -> float | None:
    """**主判据（逐租户）**：`trading_service:{tenant}` 这条心跳距今多少分钟。

    数据来自 `strategy_heartbeats` 表：该表的 `strategy_id` 列里放的就是
    形如 `trading_service:futu_real_master` / `trading_service:futu_paper_sandbox`
    的**按租户分行**的键（由 `store.heartbeat()` 写入）。只取「最新一条」会让
    「实盘停了、纸盘还在写」被判成健康 —— 必须逐租户判，谁停了报谁（Reviewer 2026-11-02）。

    **读不到心跳视为异常并进入报警，不得静默跳过**：本函数返回 `None` 时，
    调用方会把它计入 `problems`（「租户 X：读不到服务心跳…」），进而进入连续失败计数
    并升级为 CRITICAL。把这里改成「未统计就不报」等于把最危险的静默形态装回来。
    """
    store = _open_store_readonly()
    if store is None:
        return None
    try:
        with store.connection() as connection:
            row = connection.execute(
                "SELECT updated_at FROM strategy_heartbeats WHERE strategy_id=?",
                (f"trading_service:{tenant_id}",),
            ).fetchone()
    except Exception:  # noqa: BLE001 - 表不存在/库不可读 → 未统计，不抛给主循环
        return None
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
            pass
    raw = str(row["updated_at"] or "") if row else ""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 60.0


def _open_store_readonly():
    """只读打开生产库；失败返回 None（检测脚本绝不写库、绝不触发迁移）。"""
    try:
        from futu_quant.store import TradingStore

        return TradingStore(read_only=True)
    except Exception:  # noqa: BLE001 - 打不开库时用其他判据继续，不阻断检测
        return None


def is_regular_session(now: datetime | None = None) -> bool:
    """现在是否美股常规交易时段。日历不可用时返回 False（宁可少报，不刷屏）。"""
    try:
        from futu_quant.market_calendar import USMarketCalendar

        moment = now or datetime.now(timezone.utc)
        return bool(USMarketCalendar().session(moment).regular_open(moment))
    except Exception:  # noqa: BLE001 - 日历异常 → 跳过日志停滞判据，避免误报
        return False


def collect_problems(
    data_dir: Path | None = None,
    process_alive=None,
    log_age_minutes=None,
    heartbeat_age=None,
    in_regular_session=None,
) -> list[str]:
    """收集异常项。默认参数只为可测性注入，生产走真实实现。

    `heartbeat_age` 是**逐租户**函数：`heartbeat_age(tenant_id) -> float | None`。
    """
    data_dir = Path(data_dir or DATA)
    is_alive = process_alive or _process_alive
    log_age = log_age_minutes or _latest_log_age_minutes
    beat_age = heartbeat_age or heartbeat_age_minutes
    session_open = in_regular_session or is_regular_session

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

    # 判据 1（主，逐租户）：心跳新鲜度 —— 任何时段都成立，谁停了报谁
    for tenant in TENANTS:
        age = beat_age(tenant)
        if age is None:
            problems.append(f"租户 {tenant}：读不到服务心跳，无法确认是否在干活")
        elif age > HEARTBEAT_STALE_MINUTES:
            problems.append(
                f"租户 {tenant}：服务心跳已 {age:.0f} 分钟没有更新"
                f"（阈值 {HEARTBEAT_STALE_MINUTES} 分钟）—— 该租户服务已停止或卡死"
            )

    # 判据 2（辅助）：日志停滞 —— **仅在常规交易时段**判，且阈值放宽到 60 分钟
    if session_open():
        log_minutes = log_age(data_dir)
        if log_minutes is not None and log_minutes > LOG_STALE_MINUTES:
            problems.append(
                f"交易时段内 {data_dir.name}/*.log 已 {log_minutes:.0f} 分钟没有写入"
                f"（阈值 {LOG_STALE_MINUTES} 分钟）—— 可能进程在但卡死"
            )
    return problems


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE_NAME


def load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    try:
        _state_path(data_dir).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8",
        )
    except OSError:
        pass  # silent-ok: 状态写不进去最多导致重复告警，不能反过来弄崩检测


def _push(message: str, *, severity: str, recovered: bool) -> str:
    """推飞书卡片；返回三态描述。只读打开库，通道未配置就只在控制台说明。"""
    try:
        from futu_quant.alerts import feishu_configured
        from futu_quant.feishu_reporter import FeishuReporter

        store = _open_store_readonly()
        if store is None:
            return "库打不开，未推送（仅控制台输出）"
        try:
            if not feishu_configured(store):
                return "飞书未配置，未推送（仅控制台输出）"
            reporter = FeishuReporter(store)
            ok = reporter.send_alert_card(
                severity, "SERVICE_WATCHDOG_DOWN", message, is_recovered=recovered,
            )
            return "已推送飞书" if ok else "飞书推送失败（见日志）"
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - 检测脚本自身不许因上报失败而崩
        return f"推送异常（不影响退出码）：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="生产服务外部存活检测（只读、只告警、不自愈）")
    parser.add_argument("--push", action="store_true", help="异常/恢复时推飞书")
    parser.add_argument("--quiet", action="store_true", help="正常时也打印一行摘要")
    args = parser.parse_args()

    try:
        problems = collect_problems()
    except Exception as exc:  # noqa: BLE001 - 检测本身没跑成要显式失败
        print(f"[watchdog] 检测未完成：{exc}")
        return 2

    state = load_state(DATA)
    failures = int(state.get("consecutive_failures") or 0)
    alerted = bool(state.get("alerted"))

    if not problems:
        if alerted and args.push:
            print(f"[watchdog] {_push('生产服务存活检测已恢复正常。', severity='INFO', recovered=True)}")
        save_state(DATA, {"consecutive_failures": 0, "alerted": False})
        if args.quiet:
            print("[watchdog] 正常：进程存活、心跳新鲜")
        return 0

    failures += 1
    print("[watchdog] 发现异常" + (f"（连续第 {failures} 次）" if failures > 1 else "") + "：")
    for item in problems:
        print(f"  - {item}")
    print("[watchdog] 请从桌面双击 start.bat 启动生产（本脚本不会自动拉起）")

    should_alert = failures >= ESCALATE_AFTER
    if args.push and should_alert:
        severity = "CRITICAL"
        print(f"[watchdog] {_push('生产服务存活检测失败：\n' + '\n'.join(f'- {i}' for i in problems), severity=severity, recovered=False)}")
    elif args.push:
        print(f"[watchdog] 首次异常只记录不推送（连续 {ESCALATE_AFTER} 次才升级 CRITICAL）")

    save_state(DATA, {"consecutive_failures": failures, "alerted": alerted or should_alert})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
