"""守卫：单次短暂断线必须留下**配对的**恢复事件，首页不得把已恢复当成中断中。

事故形态（2026-10-29 22:57，实盘）：

    22:57:33  BROKER_CONNECTIVITY_FAILURE  (failures=1)   ┐ 首页认这两个是“中断起点”
    22:57:33  PROTECTIVE_MODE_ACTIVE       (failures=1)   ┘
    22:57:35  CRITICAL OPEND_DISCONNECTED                 （告警层）
    22:57:37  OPEND_DISCONNECTED_RECOVERED                （告警层，断了约 4 秒）
    —— 事件层没有任何恢复记录 ——

恢复路径只在 `strategy_interrupted == true`（失败 ≥3 次、真正升级过）时才写
`PROTECTIVE_RECOVERY_CONFIRMED`，而单次失败从不会置这个标志。于是首页
`latest_strategy_interruption_from_frame` 找不到恢复事件 → 红色横幅一直挂着、
时长持续累加，直到 48 小时窗口过期。

本文件锁两件事：
1. `futu_sdk_preflight` 成功时，只要本轮进过失败态就必须写恢复事件（且
   **只在**这种状态转变时写 —— 否则一天上万条，把真实事件淹掉）；
2. 首页即使面对历史脏数据（有失败无恢复），只要券商账户读得到就按已恢复渲染。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

from futu_quant.home_dashboard import latest_strategy_interruption_from_frame
from futu_quant.store import TradingStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

UTC = timezone.utc


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.recovered: list[tuple] = []

    def send(self, *args, **kwargs):
        self.sent.append(args)
        return None

    def recover(self, *args, **kwargs):
        self.recovered.append(args)
        return None


def _store(tmp_path) -> TradingStore:
    return TradingStore(tmp_path / "connectivity.db")


def _preflight(store, alerts, returncode: int, monkeypatch):
    import run_service

    monkeypatch.setattr(
        run_service.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode),
    )
    return run_service.futu_sdk_preflight(
        store, alerts, None, force=True,
        heartbeat_name="trading_service:futu_real_master",
    )


def _event_types(store) -> list[str]:
    return [row["event_type"] for row in store.operational_events(limit=200)]


# --------------------------------------------------------------- 服务端：对称写入


def test_single_failure_then_success_writes_recovery_event(tmp_path, monkeypatch):
    """**核心回归**：失败 1 次（未升级）+ 恢复 → 必须写 BROKER_CONNECTIVITY_RECOVERED。

    这就是 10-29 22:57 那条孤儿横幅的根因。没有它，首页永远等不到恢复事件。
    """
    store = _store(tmp_path)
    alerts = _Alerts()

    assert _preflight(store, alerts, 1, monkeypatch) is False
    assert "BROKER_CONNECTIVITY_FAILURE" in _event_types(store)

    assert _preflight(store, alerts, 0, monkeypatch) is True
    types = _event_types(store)
    assert "BROKER_CONNECTIVITY_RECOVERED" in types, (
        "单次失败后的恢复没有写事件 —— 首页会一直显示“中断中”（10-29 22:57 事故）"
    )


def test_recovery_event_carries_baseline_failure_count(tmp_path, monkeypatch):
    """恢复事件要能自证“这次之前确实失败过几次”，便于事后对账。"""
    import json

    store = _store(tmp_path)
    alerts = _Alerts()
    _preflight(store, alerts, 1, monkeypatch)
    _preflight(store, alerts, 0, monkeypatch)

    rows = [r for r in store.operational_events(limit=50)
            if r["event_type"] == "BROKER_CONNECTIVITY_RECOVERED"]
    assert rows, "缺少恢复事件"
    payload = json.loads(rows[0]["payload_json"] or "{}")
    assert payload.get("failures_before_recovery") == 1
    assert payload.get("was_degraded") is True


def test_healthy_success_does_not_write_recovery_events(tmp_path, monkeypatch):
    """**不得无条件每次成功都写**：预检每 30 秒一次，无条件写等于每天上万条噪音。

    `operational_events` 已有 17 万行，被恢复事件淹没后就再也看不出真实故障。
    """
    store = _store(tmp_path)
    alerts = _Alerts()
    for _ in range(3):
        assert _preflight(store, alerts, 0, monkeypatch) is True
    assert "BROKER_CONNECTIVITY_RECOVERED" not in _event_types(store), (
        "从未失败过却写了恢复事件 —— 事件表会被噪音淹没"
    )


def test_escalated_outage_writes_both_recovery_events(tmp_path, monkeypatch):
    """升级过的中断（strategy_interrupted=true）要同时写通用恢复与保护恢复。"""
    store = _store(tmp_path)
    alerts = _Alerts()
    store.set_setting("strategy_interrupted", "true")
    store.set_setting("connectivity_degraded", "true")
    store.set_setting("opend_consecutive_failures", "3")

    assert _preflight(store, alerts, 0, monkeypatch) is True
    types = _event_types(store)
    assert "BROKER_CONNECTIVITY_RECOVERED" in types
    assert "PROTECTIVE_RECOVERY_CONFIRMED" in types
    assert store.get_setting("strategy_interrupted", "true") == "false"


def test_recovery_is_idempotent_across_repeated_success(tmp_path, monkeypatch):
    """恢复后再成功若干次，不能反复追加恢复事件。"""
    store = _store(tmp_path)
    alerts = _Alerts()
    _preflight(store, alerts, 1, monkeypatch)
    _preflight(store, alerts, 0, monkeypatch)
    first = _event_types(store).count("BROKER_CONNECTIVITY_RECOVERED")
    _preflight(store, alerts, 0, monkeypatch)
    _preflight(store, alerts, 0, monkeypatch)
    assert _event_types(store).count("BROKER_CONNECTIVITY_RECOVERED") == first == 1


# ----------------------------------------------------------------- 首页：脏数据兜底


def _frame(*rows: tuple[str, pd.Timestamp, str]) -> pd.DataFrame:
    return pd.DataFrame([
        {"event_type": t, "created_at": ts.isoformat(), "payload_json": payload}
        for t, ts, payload in rows
    ])


def test_lone_failure_renders_recovered_when_broker_reachable():
    """历史脏数据兜底：只有失败、没有恢复事件，但账户读得到 → 按已恢复渲染。

    这是 10-29 22:57 那条横幅的真实现场。修根因只能管未来，历史数据必须有兜底，
    否则那条红条会一直挂到 48 小时过期。
    """
    now = pd.Timestamp.now(tz=UTC)
    failure_ts = now - pd.Timedelta(minutes=10)
    frame = _frame(
        ("BROKER_CONNECTIVITY_FAILURE", failure_ts, '{"failures":1}'),
        ("PROTECTIVE_MODE_ACTIVE", failure_ts, '{"failures":1,"open_option_count":2}'),
    )
    out = latest_strategy_interruption_from_frame(frame, account_ok=True)
    assert out is not None
    assert out["recovered"] is True, "账户读得到却仍显示“中断中” —— 假告警"
    assert out["open_option_count"] == 2


def test_recovered_event_makes_banner_yellow_with_real_duration():
    """有配对恢复事件时，时长必须是真实恢复时刻，不是“距失败多久”。"""
    now = pd.Timestamp.now(tz=UTC)
    failure_ts = now - pd.Timedelta(minutes=10)
    frame = _frame(
        ("BROKER_CONNECTIVITY_FAILURE", failure_ts, '{"failures":1,"open_option_count":2}'),
        ("BROKER_CONNECTIVITY_RECOVERED", failure_ts + pd.Timedelta(seconds=4), "{}"),
    )
    out = latest_strategy_interruption_from_frame(frame, account_ok=True)
    assert out is not None and out["recovered"] is True
    assert out["duration_minutes"] < 1.0, "4 秒的中断被显示成十分钟 —— 时长口径错了"


def test_active_outage_is_not_masked_when_broker_unreachable():
    """账户读不到时必须继续报红 —— 兜底不能把真实故障藏起来。"""
    now = pd.Timestamp.now(tz=UTC)
    frame = _frame(
        ("BROKER_CONNECTIVITY_FAILURE", now - pd.Timedelta(minutes=10), '{"failures":1}'),
    )
    out = latest_strategy_interruption_from_frame(frame, account_ok=False)
    assert out is not None
    assert out["recovered"] is False, "券商不可达却按已恢复渲染 —— 真实故障被吞"


def test_stale_interruption_drops_off_home_after_an_hour():
    """账户正常且中断已超过 1 小时：不再占用首页（与 `_opend_incident` 同口径）。"""
    now = pd.Timestamp.now(tz=UTC)
    frame = _frame(
        ("BROKER_CONNECTIVITY_FAILURE", now - pd.Timedelta(hours=3), '{"failures":1}'),
    )
    assert latest_strategy_interruption_from_frame(frame, account_ok=True) is None


# ------------------------------------------------------------ 常量单一事实源


def test_recovery_event_types_cover_all_producers():
    """两份实现曾各自维护白名单，`STARTUP_RECOVERY` 只在其中一份里。

    恢复白名单缺项 = 同一条历史故障两处判定相反。收敛成一个常量后锁死覆盖。
    """
    from futu_quant.connectivity_protection import RECOVERY_EVENT_TYPES

    assert RECOVERY_EVENT_TYPES == {
        "PROTECTIVE_RECOVERY_CONFIRMED",
        "BROKER_CONNECTIVITY_RECOVERED",
        "STARTUP_RECOVERY",
    }
    # 首页必须引用共享常量，不得再内联一份
    src = Path(latest_strategy_interruption_from_frame.__module__.replace(".", "/") + ".py")
    source = (Path(__file__).resolve().parents[1] / "src" / src).read_text(encoding="utf-8")
    assert 'event_type.isin({"PROTECTIVE_RECOVERY_CONFIRMED"' not in source, (
        "首页又内联了一份恢复白名单 —— 与 connectivity_protection 分叉"
    )


def test_home_dashboard_uses_shared_recovery_constant():
    from futu_quant import home_dashboard

    assert home_dashboard.RECOVERY_EVENT_TYPES is not None
    assert "STARTUP_RECOVERY" in home_dashboard.RECOVERY_EVENT_TYPES
