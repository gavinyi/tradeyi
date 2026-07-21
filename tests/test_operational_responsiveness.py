from __future__ import annotations

from pathlib import Path

from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
import futu_quant.radar_control as radar_control
import futu_quant.service_control as service_control


def test_service_status_is_local_and_reports_stopping(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "service.pid"
    stop_file = tmp_path / "service.stop"
    pid_file.write_text("12345")
    stop_file.touch()
    monkeypatch.setattr(service_control, "PID_FILE", pid_file)
    monkeypatch.setattr(service_control, "STOP_FILE", stop_file)
    monkeypatch.setattr(service_control, "_pid_alive", lambda _pid: True)

    result = service_control.status()

    assert result.running is True
    assert result.pid == 12345
    assert "正在停止" in result.message


def test_radar_status_reports_stopping_without_process_lookup(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "radar.pid"
    stop_file = tmp_path / "radar.stop"
    pid_file.write_text("23456")
    stop_file.touch()
    monkeypatch.setattr(radar_control, "PID_FILE", pid_file)
    monkeypatch.setattr(radar_control, "STOP_FILE", stop_file)
    monkeypatch.setattr(radar_control.os, "kill", lambda _pid, _signal: None)

    result = radar_control.status()

    assert result.running is True
    assert "正在停止" in result.message


def test_radar_status_recovers_pid_from_lock_owner(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "radar.pid"
    lock_file = tmp_path / "radar.lock"
    lock_file.touch()
    monkeypatch.setattr(radar_control, "PID_FILE", pid_file)
    monkeypatch.setattr(radar_control, "LOCK_FILE", lock_file)
    monkeypatch.setattr(radar_control, "STOP_FILE", tmp_path / "radar.stop")
    monkeypatch.setattr(radar_control, "_pid_alive", lambda pid: pid == 34567)
    monkeypatch.setattr(radar_control, "_lock_owner_pid", lambda: 34567)

    result = radar_control.status()

    assert result.running is True
    assert result.pid == 34567
    assert pid_file.read_text() == "34567"


def test_task_run_ledger_records_completion(tmp_path: Path):
    store = TradingStore(tmp_path / "trading.db")
    run_id = store.start_task_run("order_reconciliation", 60, 321)
    store.finish_task_run(run_id, "SUCCESS", 1.25, "完成")

    row = store.task_runs(limit=1)[0]
    assert row["task_id"] == "order_reconciliation"
    assert row["status"] == "SUCCESS"
    assert row["duration_seconds"] == 1.25
    assert row["finished_at"] is not None


def test_broker_sets_bounded_sync_connect_timeout(monkeypatch):
    values: list[float] = []

    class Context:
        def set_sync_query_connect_timeout(self, value: float) -> None:
            values.append(value)

    monkeypatch.setenv("FUTU_CONNECT_TIMEOUT_SECONDS", "4")
    FutuPaperBroker._set_connect_timeout(Context())

    assert values == [4.0]
