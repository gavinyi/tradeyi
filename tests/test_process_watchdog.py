"""P1-13 Layer 1 判据守卫：进程失联双校验（PID 身份 + 心跳时效）。

为什么单独钉这些判据：
- 只看 PID 存活会被 Windows PID 复用骗到（自家 `start.bat :pid_alive` 就踩过，
  服务被静默跳过启动）；
- 只看心跳状态会发现不了进程暴毙（状态永远停在最后一次写的 HEALTHY）；
- 维护标记必须有**有效期**，否则一次失败的停止会留下标记、守护永久静默。

变异：
- `maintenance_active` 去掉有效期判断 → 过期标记用例转红；
- `cmdline_matches` 改成恒 True → PID 复用用例转红；
- `heartbeat_timeout_seconds` 对雷达返回固定 420 → 雷达阈值用例转红。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from futu_quant.process_watchdog import (
    EVENT_HEARTBEAT_TIMEOUT,
    EVENT_TERMINATED,
    RADAR_HEARTBEAT_TIMEOUT_FLOOR,
    TARGETS,
    cmdline_matches,
    evaluate_heartbeat,
    heartbeat_timeout_seconds,
    maintenance_active,
)

NOW = datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc)


def test_maintenance_marker_expires(tmp_path):
    marker = tmp_path / ".maintenance"
    assert maintenance_active(marker, now=NOW) is False

    marker.write_text("stopping", encoding="utf-8")
    assert maintenance_active(marker, now=NOW) is True

    # 过期标记必须失效：一次失败的停止不能永久静默守护
    old = (NOW - timedelta(hours=3)).timestamp()
    os.utime(marker, (old, old))
    assert maintenance_active(marker, now=NOW) is False


def test_heartbeat_timeouts_are_per_component():
    assert heartbeat_timeout_seconds("trading_service") == 420
    assert heartbeat_timeout_seconds("volatility_scanner", interval_seconds=300) == RADAR_HEARTBEAT_TIMEOUT_FLOOR
    assert heartbeat_timeout_seconds("volatility_scanner", interval_seconds=900) == 1800


def test_cmdline_identity_rejects_pid_reuse():
    worker = next(t for t in TARGETS if t.tenant_id == "futu_real_master")
    assert cmdline_matches(
        "pythonw.exe -u E:\\Thisyi\\trade\\scripts\\run_service.py --tenant futu_real_master",
        worker.cmd_markers,
    )
    # 同一个 PID 被别的程序复用：映像名可能仍是 python，但命令行不符 → 必须判失联
    assert not cmdline_matches("python.exe -c print(1)", worker.cmd_markers)
    # 另一个租户的 Worker 不算本租户生存
    assert not cmdline_matches(
        "python.exe scripts/run_service.py --tenant futu_paper_sandbox",
        worker.cmd_markers,
    )
    assert not cmdline_matches(None, worker.cmd_markers)


def test_evaluate_reports_terminated_when_pid_missing_or_reused():
    worker = next(t for t in TARGETS if t.tenant_id == "futu_real_master")
    ok_cmdline = "python.exe scripts/run_service.py --tenant futu_real_master"
    fresh = NOW - timedelta(seconds=30)

    event, payload = evaluate_heartbeat(
        target=worker, pid_present=False, pid_alive=False, cmdline=None,
        heartbeat_updated_at=fresh, now=NOW,
    )
    assert event == EVENT_TERMINATED and "PID 文件" in payload["reason"]

    event, payload = evaluate_heartbeat(
        target=worker, pid_present=True, pid_alive=True, cmdline="python.exe -c 1",
        heartbeat_updated_at=fresh, now=NOW,
    )
    assert event == EVENT_TERMINATED and "复用" in payload["reason"]

    # 健康：不得误报
    assert evaluate_heartbeat(
        target=worker, pid_present=True, pid_alive=True, cmdline=ok_cmdline,
        heartbeat_updated_at=fresh, now=NOW,
    ) is None


def test_evaluate_reports_heartbeat_timeout():
    worker = next(t for t in TARGETS if t.tenant_id == "futu_real_master")
    ok_cmdline = "python.exe scripts/run_service.py --tenant futu_real_master"

    event, payload = evaluate_heartbeat(
        target=worker, pid_present=True, pid_alive=True, cmdline=ok_cmdline,
        heartbeat_updated_at=NOW - timedelta(seconds=421), now=NOW,
    )
    assert event == EVENT_HEARTBEAT_TIMEOUT
    assert payload["timeout_seconds"] == 420

    # 没有心跳记录同样是僵死证据，不能当成"没触发"
    event, payload = evaluate_heartbeat(
        target=worker, pid_present=True, pid_alive=True, cmdline=ok_cmdline,
        heartbeat_updated_at=None, now=NOW,
    )
    assert event == EVENT_HEARTBEAT_TIMEOUT

    # 边界内不得误报
    assert evaluate_heartbeat(
        target=worker, pid_present=True, pid_alive=True, cmdline=ok_cmdline,
        heartbeat_updated_at=NOW - timedelta(seconds=419), now=NOW,
    ) is None
