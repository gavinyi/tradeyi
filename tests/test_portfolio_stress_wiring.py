"""P1-8 接线测试：`run_service.portfolio_stress_check` 真打一遍 alert→recover。

为什么必须单独立这条：模块函数（`stress_alerts.next_stress_action` 等）测了不等于
**接线**正确。Reviewer 在静态扫描里抓到过一处未导入的 `STATE_OK` —— 模块用例全绿、
全量也绿，但 recover 分支每轮抛 `NameError`，滞回状态永远停在 ALERTING。

变异：
- 删掉 `STATE_OK` 的 import（接线里用到它）→ 本文件红（`NameError`）；
- 把缓冲带判断去掉 → 本文件「缓冲带静默」用例红。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from futu_quant.store import TradingStore
from futu_quant.stress_alerts import (
    STATE_ALERTING,
    STATE_OK,
    portfolio_stress_incident_key,
    read_hysteresis_state,
    write_hysteresis_state,
)

REAL = "futu_real_master"


class _Recorder:
    def __init__(self):
        self.sent: list[dict] = []
        self.recovered: list[dict] = []

    def send(self, level, event_type, message, **kwargs):
        self.sent.append(
            {"level": level, "event": event_type, "message": message, **kwargs}
        )

    def recover(self, key, message, **kwargs):
        self.recovered.append({"key": key, "message": message, **kwargs})


def _import_run_service():
    """`run_service.py` 用裸名导入同目录脚本（`from run_gamma_strategy ...`），
    所以必须先把它自己的目录放上 `sys.path` —— 与以脚本方式运行时的行为一致。"""
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import run_service

    return run_service


def _stub_monitor(monkeypatch, ratio: float):
    run_service = _import_run_service()

    monkeypatch.setattr(
        run_service, "monitor_portfolio_stress",
        lambda broker, store: pd.DataFrame(
            [{"scenario": "下跌10%", "equity_loss_ratio": ratio}]
        ),
    )
    return run_service


def test_stress_check_alerts_on_breach_and_recovers_below_recover_line(tmp_path, monkeypatch):
    run_service = _stub_monitor(monkeypatch, 0.15)
    store = TradingStore(tmp_path / "t.db")
    alerts = _Recorder()
    incident_key = portfolio_stress_incident_key(REAL)

    # 1) 破线：发告警 + 状态置 ALERTING
    run_service.portfolio_stress_check(None, store, alerts, REAL)
    assert any(item["event"] == "PORTFOLIO_STRESS_BREACH" for item in alerts.sent), (
        "破线必须真的发出告警（接线断了这里会是空的）"
    )
    assert any(item.get("incident_key") == incident_key for item in alerts.sent), (
        "incident 键必须是租户级"
    )
    assert read_hysteresis_state(store, REAL) == STATE_ALERTING

    # 2) 跌破恢复线（默认 8%）：发恢复 + 状态回 OK（这条正是 STATE_OK 的接线）
    run_service = _stub_monitor(monkeypatch, 0.05)
    run_service.portfolio_stress_check(None, store, alerts, REAL)
    assert any(item.get("incident_key") == incident_key for item in alerts.recovered), (
        "恢复分支没跑通 —— 这里出 NameError 时状态会永远停在 ALERTING"
    )
    assert read_hysteresis_state(store, REAL) == STATE_OK


def test_stress_check_is_silent_inside_hysteresis_band(tmp_path, monkeypatch):
    run_service = _stub_monitor(monkeypatch, 0.095)
    store = TradingStore(tmp_path / "t.db")
    write_hysteresis_state(store, REAL, STATE_ALERTING)
    alerts = _Recorder()

    run_service.portfolio_stress_check(None, store, alerts, REAL)

    incident_key = portfolio_stress_incident_key(REAL)
    assert not [
        item for item in alerts.sent if item.get("incident_key") == incident_key
    ], "缓冲带内不得重发告警（否则就是 flapping）"
    assert not [
        item for item in alerts.recovered if item.get("incident_key") == incident_key
    ], "缓冲带内不得误消警"
    assert read_hysteresis_state(store, REAL) == STATE_ALERTING
