# Volatility Radar Adaptive Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 15-minute volatility radar loop with a 2-to-15-minute interval calculated from configured symbol count, scan duration, and consecutive errors.

**Architecture:** Add one pure scheduling module that owns interval calculation, error backoff, and JSON state serialization. The radar service uses that module after every cycle and persists one state object through `TradingStore`; the Streamlit page reads the same module and state so displayed values cannot drift from the background scheduler.

**Tech Stack:** Python 3.11, dataclasses, pandas timestamps, SQLite-backed `TradingStore`, Streamlit, pytest, Ruff.

## Global Constraints

- Scan only symbols returned by `load_volatility_symbols()`.
- Normal interval formula is `clamp(max(clamp(60 + symbol_count * 20, 120, 900), scan_duration_seconds * 2), 120, 900)`.
- Zero configured symbols skip broker construction and scanning, then wait 900 seconds.
- Error backoff sequence is exactly 60, 120, 300, and 900 seconds; subsequent errors stay at 900 seconds.
- A successful scan resets consecutive errors to zero.
- Market-closed polling remains 900 seconds.
- Existing signal generation, option selection, SHADOW execution, and simulated-account restrictions must not change.
- The workspace is not a Git repository, so commit steps are intentionally omitted.

---

### Task 1: Scheduling rules and persisted state

**Files:**
- Create: `src/futu_quant/radar_schedule.py`
- Create: `tests/test_radar_schedule.py`

**Interfaces:**
- Produces: `adaptive_interval_seconds(symbol_count: int, scan_duration_seconds: float = 0.0) -> int`
- Produces: `error_backoff_seconds(consecutive_errors: int) -> int`
- Produces: `RadarScheduleState`
- Produces: `save_radar_schedule_state(store, state: RadarScheduleState) -> None`
- Produces: `load_radar_schedule_state(store) -> RadarScheduleState | None`

- [ ] **Step 1: Write failing interval and backoff tests**

```python
from futu_quant.radar_schedule import adaptive_interval_seconds, error_backoff_seconds


def test_adaptive_intervals_scale_with_symbol_count():
    assert adaptive_interval_seconds(0) == 900
    assert adaptive_interval_seconds(1) == 120
    assert adaptive_interval_seconds(3) == 120
    assert adaptive_interval_seconds(9) == 240
    assert adaptive_interval_seconds(20) == 460
    assert adaptive_interval_seconds(42) == 900


def test_scan_duration_can_extend_but_not_exceed_interval_cap():
    assert adaptive_interval_seconds(3, 150) == 300
    assert adaptive_interval_seconds(3, 800) == 900


def test_error_backoff_sequence_is_bounded():
    assert [error_backoff_seconds(value) for value in range(1, 6)] == [60, 120, 300, 900, 900]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py`

Expected: collection fails because `futu_quant.radar_schedule` does not exist.

- [ ] **Step 3: Implement the pure scheduling functions**

```python
from __future__ import annotations

import math


def adaptive_interval_seconds(symbol_count: int, scan_duration_seconds: float = 0.0) -> int:
    if symbol_count <= 0:
        return 900
    base = min(900, max(120, 60 + int(symbol_count) * 20))
    duration_floor = math.ceil(max(0.0, float(scan_duration_seconds)) * 2)
    return min(900, max(120, base, duration_floor))


def error_backoff_seconds(consecutive_errors: int) -> int:
    sequence = (60, 120, 300, 900)
    index = min(len(sequence) - 1, max(0, int(consecutive_errors) - 1))
    return sequence[index]
```

- [ ] **Step 4: Add failing state round-trip tests**

```python
from datetime import datetime, timezone

from futu_quant.radar_schedule import (
    RadarScheduleState, load_radar_schedule_state, save_radar_schedule_state,
)


class FakeStore:
    def __init__(self):
        self.values = {}

    def set_setting(self, key, value):
        self.values[key] = value

    def get_setting(self, key, default=""):
        return self.values.get(key, default)


def test_schedule_state_round_trips_through_store():
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


def test_invalid_persisted_state_returns_none():
    store = FakeStore()
    store.values["volatility_radar_schedule_state"] = "not-json"
    assert load_radar_schedule_state(store) is None
```

- [ ] **Step 5: Implement the state object and persistence**

`RadarScheduleState` is a frozen dataclass with fields `symbol_count: int`, `scan_duration_seconds: float`, `interval_seconds: int`, `next_scan_at: str`, `consecutive_errors: int`, and `backoff_active: bool`. Its `build(...)` classmethod calculates `next_scan_at` as `now + timedelta(seconds=interval_seconds)` and serializes it as an ISO-8601 UTC value. Persist the dataclass as compact JSON under the exact setting key `volatility_radar_schedule_state`; malformed or incomplete JSON returns `None` instead of raising.

- [ ] **Step 6: Run focused tests and Ruff**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py && UV_CACHE_DIR=.uv-cache uv run ruff check src/futu_quant/radar_schedule.py tests/test_radar_schedule.py`

Expected: all scheduling tests pass and Ruff reports no errors.

---

### Task 2: Background radar loop integration

**Files:**
- Modify: `scripts/run_radar_service.py`
- Modify: `tests/test_radar_schedule.py`

**Interfaces:**
- Consumes: all interfaces produced by Task 1.
- Produces: persisted `RadarScheduleState` after every open-session success, empty-watchlist cycle, or scan error.

- [ ] **Step 1: Add failing service-source regression tests**

```python
from pathlib import Path


def test_radar_service_no_longer_uses_fixed_fifteen_minute_success_wait():
    source = (Path(__file__).parents[1] / "scripts" / "run_radar_service.py").read_text()
    assert "adaptive_interval_seconds" in source
    assert "save_radar_schedule_state" in source
    assert "time.monotonic()" in source
```

- [ ] **Step 2: Run the test and verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py`

Expected: the service-source regression test fails because the adaptive scheduler is not imported.

- [ ] **Step 3: Integrate adaptive success scheduling**

In `main()`, initialize `consecutive_errors = 0` before the loop. During an open market session, load symbols before constructing `FutuPaperBroker`. If the tuple is empty, persist a state with 0 symbols and a 900-second interval, emit a healthy “未配置标的” heartbeat, and skip the scanner. Otherwise record `started = time.monotonic()`, run the existing scanner, calculate duration, reset errors, calculate the adaptive interval, build and save state, and include interval plus next-scan time in the healthy heartbeat message.

- [ ] **Step 4: Integrate error backoff**

In the existing exception handler, increment `consecutive_errors`, calculate `next_wait_seconds = error_backoff_seconds(consecutive_errors)`, build and save a backoff-active state using the current symbol count and measured duration, and include the retry interval in both the error heartbeat and alert message. Do not alter the existing alert event type.

- [ ] **Step 5: Preserve closed-market behavior**

When the regular session is closed, keep `next_wait_seconds = 900`, retain the existing healthy heartbeat, and do not call the broker or scanner. The next open-session success resets any prior consecutive errors.

- [ ] **Step 6: Run service-focused validation**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py tests/test_volatility_scanner.py && UV_CACHE_DIR=.uv-cache uv run ruff check scripts/run_radar_service.py src/futu_quant/radar_schedule.py tests/test_radar_schedule.py`

Expected: all tests pass and Ruff reports no errors.

---

### Task 3: Streamlit schedule visibility

**Files:**
- Modify: `pages/10_volatility_options_strategy.py`
- Modify: `tests/test_radar_schedule.py`

**Interfaces:**
- Consumes: `adaptive_interval_seconds(...)` and `load_radar_schedule_state(...)` from Task 1.
- Produces: user-visible radar schedule metrics and dynamic explanatory copy.

- [ ] **Step 1: Add failing page regression tests**

```python
def test_volatility_page_exposes_adaptive_schedule_state():
    source = (Path(__file__).parents[1] / "pages" / "10_volatility_options_strategy.py").read_text()
    assert "load_radar_schedule_state" in source
    assert "预计下次扫描" in source
    assert "2 至 15 分钟" in source
    assert "每 15 分钟只扫描" not in source
```

- [ ] **Step 2: Run the test and verify RED**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py`

Expected: page regression test fails because the page still contains fixed 15-minute copy.

- [ ] **Step 3: Add schedule metrics to the radar expander**

Load the persisted state once inside the existing “自动波动率雷达” expander. Use a horizontal `st.container` with bordered metrics for “扫描标的数”, “当前扫描间隔”, “上次扫描耗时”, and “连续错误”. If persisted state is missing, show the symbol-count estimate from `adaptive_interval_seconds(len(current_radar_symbols))`; display “等待首次扫描” for unavailable runtime values.

- [ ] **Step 4: Add next-scan and backoff explanations**

Convert `next_scan_at` from UTC to `Asia/Shanghai` before displaying “预计下次扫描”. When `backoff_active` is true, show a warning containing the retry interval and error count. Replace fixed copy with “常规交易时段按标的数量和扫描耗时动态调整，间隔为 2 至 15 分钟”。

- [ ] **Step 5: Validate the Streamlit page**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py && UV_CACHE_DIR=.uv-cache uv run ruff check pages/10_volatility_options_strategy.py && UV_CACHE_DIR=.uv-cache uv run python scripts/check_streamlit_pages.py`

Expected: schedule tests pass, Ruff is clean, and the 22-page static巡检 reports 0 errors and 0 warnings.

---

### Task 4: Runtime activation and end-to-end verification

**Files:**
- Runtime state: `data/volatility-radar.pid`
- Runtime log: `data/volatility-radar.log`

**Interfaces:**
- Consumes: completed Tasks 1 through 3.
- Produces: a running radar process using adaptive scheduling.

- [ ] **Step 1: Run the complete focused test set**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest -q tests/test_radar_schedule.py tests/test_volatility_scanner.py`

Expected: all tests pass.

- [ ] **Step 2: Run full lint and Streamlit巡检**

Run: `UV_CACHE_DIR=.uv-cache uv run ruff check src/futu_quant/radar_schedule.py scripts/run_radar_service.py pages/10_volatility_options_strategy.py tests/test_radar_schedule.py && UV_CACHE_DIR=.uv-cache uv run python scripts/check_streamlit_pages.py`

Expected: no lint errors; all 22 pages report 0 errors and 0 warnings.

- [ ] **Step 3: Restart only the radar service**

Run: `UV_CACHE_DIR=.uv-cache uv run python -c "import time; from futu_quant.radar_control import stop, start, status; print(stop()); time.sleep(1); print(start()); time.sleep(3); print(status())"`

Expected: the final status reports `running=True`. This does not restart or change the unified trading service.

- [ ] **Step 4: Verify persisted schedule state**

Run: `UV_CACHE_DIR=.uv-cache uv run python -c "from futu_quant.radar_schedule import load_radar_schedule_state; from futu_quant.store import TradingStore; print(load_radar_schedule_state(TradingStore()))"`

Expected during regular market hours: state includes the current configured symbol count, interval between 120 and 900 seconds, a future `next_scan_at`, and `backoff_active=False` after a successful scan. Outside regular hours, the service remains healthy and the prior state may remain visible until the next open-session scan.

- [ ] **Step 5: Verify the running app**

Open `http://127.0.0.1:8501/波动率期权策略` and confirm the radar expander displays dynamic interval metrics, next scan time in China time, and no fixed 15-minute copy.
