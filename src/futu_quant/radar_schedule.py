from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Protocol


RADAR_SCHEDULE_STATE_KEY = "volatility_radar_schedule_state"
ERROR_BACKOFF_SECONDS = (60, 120, 300, 900)


class SettingStore(Protocol):
    def set_setting(self, key: str, value: str) -> None: ...

    def get_setting(self, key: str, default: str = "") -> str: ...


def adaptive_interval_seconds(symbol_count: int, scan_duration_seconds: float = 0.0) -> int:
    """Return a rate-limit-aware interval based on watchlist size and scan cost."""
    if symbol_count <= 0:
        return 900
    base = min(900, max(120, 60 + int(symbol_count) * 20))
    duration_floor = math.ceil(max(0.0, float(scan_duration_seconds)) * 2)
    return min(900, max(120, base, duration_floor))


def error_backoff_seconds(consecutive_errors: int) -> int:
    index = min(
        len(ERROR_BACKOFF_SECONDS) - 1,
        max(0, int(consecutive_errors) - 1),
    )
    return ERROR_BACKOFF_SECONDS[index]


@dataclass(frozen=True)
class RadarScheduleState:
    symbol_count: int
    scan_duration_seconds: float
    interval_seconds: int
    next_scan_at: str
    consecutive_errors: int
    backoff_active: bool

    @classmethod
    def build(
        cls,
        *,
        symbol_count: int,
        scan_duration_seconds: float,
        interval_seconds: int,
        consecutive_errors: int,
        now: datetime | None = None,
    ) -> RadarScheduleState:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        return cls(
            symbol_count=max(0, int(symbol_count)),
            scan_duration_seconds=max(0.0, round(float(scan_duration_seconds), 3)),
            interval_seconds=max(0, int(interval_seconds)),
            next_scan_at=(current + timedelta(seconds=interval_seconds)).isoformat(),
            consecutive_errors=max(0, int(consecutive_errors)),
            backoff_active=consecutive_errors > 0,
        )


def save_radar_schedule_state(store: SettingStore, state: RadarScheduleState) -> None:
    store.set_setting(
        RADAR_SCHEDULE_STATE_KEY,
        json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":")),
    )


def load_radar_schedule_state(store: SettingStore) -> RadarScheduleState | None:
    raw = store.get_setting(RADAR_SCHEDULE_STATE_KEY, "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return RadarScheduleState(
            symbol_count=int(payload["symbol_count"]),
            scan_duration_seconds=float(payload["scan_duration_seconds"]),
            interval_seconds=int(payload["interval_seconds"]),
            next_scan_at=str(payload["next_scan_at"]),
            consecutive_errors=int(payload["consecutive_errors"]),
            backoff_active=bool(payload["backoff_active"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
