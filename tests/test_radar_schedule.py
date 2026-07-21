from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from futu_quant.radar_schedule import (
    RadarScheduleState,
    adaptive_interval_seconds,
    error_backoff_seconds,
    load_radar_schedule_state,
    save_radar_schedule_state,
)


class FakeStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_setting(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)


def test_adaptive_intervals_scale_with_symbol_count() -> None:
    assert adaptive_interval_seconds(0) == 900
    assert adaptive_interval_seconds(1) == 120
    assert adaptive_interval_seconds(3) == 120
    assert adaptive_interval_seconds(9) == 240
    assert adaptive_interval_seconds(20) == 460
    assert adaptive_interval_seconds(42) == 900


def test_scan_duration_can_extend_but_not_exceed_interval_cap() -> None:
    assert adaptive_interval_seconds(3, 150) == 300
    assert adaptive_interval_seconds(3, 800) == 900


def test_error_backoff_sequence_is_bounded() -> None:
    assert [error_backoff_seconds(value) for value in range(1, 6)] == [60, 120, 300, 900, 900]


def test_schedule_state_round_trips_through_store() -> None:
    store = FakeStore()
    state = RadarScheduleState.build(
        symbol_count=9,
        scan_duration_seconds=12.5,
        interval_seconds=240,
        consecutive_errors=0,
        now=datetime(2026, 7, 18, 16, 0, tzinfo=timezone.utc),
    )

    save_radar_schedule_state(store, state)

    assert load_radar_schedule_state(store) == state
    assert state.next_scan_at == "2026-07-18T16:04:00+00:00"
    assert state.backoff_active is False


def test_invalid_persisted_state_returns_none() -> None:
    store = FakeStore()
    store.values["volatility_radar_schedule_state"] = "not-json"

    assert load_radar_schedule_state(store) is None


def test_radar_service_uses_adaptive_schedule_and_persists_state() -> None:
    source = (
        Path(__file__).parents[1] / "scripts" / "run_radar_service.py"
    ).read_text(encoding="utf-8")

    assert "adaptive_interval_seconds" in source
    assert "error_backoff_seconds" in source
    assert "save_radar_schedule_state" in source
    assert "time.monotonic()" in source


def test_volatility_page_exposes_adaptive_schedule_state() -> None:
    source = (
        Path(__file__).parents[1] / "pages" / "10_波动率期权策略.py"
    ).read_text(encoding="utf-8")

    assert "load_radar_schedule_state" in source
    assert "预计下次扫描" in source
    assert "2 至 15 分钟" in source
    assert "每 15 分钟只扫描" not in source
