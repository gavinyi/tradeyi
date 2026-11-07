"""历史信号回测引擎 · 信号重放层（只读）。

职责（PRD §2 L-A）：按时间序读取 `decision_logs` 中 volatility_scanner /
volatility_long_gamma 的判定，用**当前生效配置**重算 BUY 判定，与实际触
发逐条对齐——任何差异都是回放器 bug，无容差（PRD 硬门槛 1）。

**只读铁律**：本模块只接受 `read_only=True` 的 store；禁止任何写库与下单路径。
**确定性**：禁止 `datetime.now()` / `random`——全部时间来自数据行，依赖注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json

SCANNER_STRATEGY = "volatility_scanner"
EXECUTOR_STRATEGY = "volatility_long_gamma"


@dataclass(frozen=True)
class SignalEvent:
    """一条信号触发记录（重放输出与实际日志共用同一形态，便于逐条 diff）。"""

    created_at: str
    symbol: str
    decision: str
    tenant_id: str
    payload: dict


@dataclass
class ReplayResult:
    """一次重放的完整输出（报告 A 的数据源）。"""

    actual_events: list[SignalEvent] = field(default_factory=list)
    replayed_events: list[SignalEvent] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""

    @property
    def aligned(self) -> bool:
        return not self.mismatches

    @property
    def alignment_ratio(self) -> float:
        total = max(len(self.actual_events), 1)
        return 1.0 - (len(self.mismatches) / total)


def _parse_payload(raw: object) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_actual_signals(store, window_start: str, window_end: str) -> list[SignalEvent]:
    """读取窗口内的实际 BUY 触发（决策日志是唯一真值来源）。"""
    events: list[SignalEvent] = []
    for strategy in (SCANNER_STRATEGY, EXECUTOR_STRATEGY):
        rows = store.decisions_for_strategy(strategy, limit=200_000)
        for row in rows:
            created = str(row.get("created_at") or "")
            if not (window_start <= created <= window_end):
                continue
            decision = str(row.get("decision") or "").upper()
            if decision != "BUY":
                continue
            events.append(SignalEvent(
                created_at=created,
                symbol=str(row.get("symbol") or ""),
                decision=decision,
                tenant_id=str(row.get("tenant_id") or ""),
                payload=_parse_payload(row.get("payload_json")),
            ))
    events.sort(key=lambda event: (event.created_at, event.symbol, event.tenant_id))
    return events


def replay_signals(store, window_start: str, window_end: str, config: dict) -> ReplayResult:
    """重放窗口内的 BUY 判定（差异显式落 mismatch，无吞差异路径）。"""
    result = ReplayResult(window_start=window_start, window_end=window_end)
    actual = load_actual_signals(store, window_start, window_end)
    result.actual_events = actual

    thresholds = config.get("thresholds") or {}
    z_threshold = float(thresholds.get("entry_zscore", 2.0))
    minimum_move = float(thresholds.get("minimum_underlying_move", 0.02))

    replayed: list[SignalEvent] = []
    for event in actual:
        payload_regime = event.payload.get("regime") or {}
        z_value = payload_regime.get("zscore_20d")
        move_value = payload_regime.get("intraday_return")
        if z_value is None or move_value is None:
            result.mismatches.append({
                "created_at": event.created_at, "symbol": event.symbol,
                "reason": "payload_missing_regime",
            })
            continue
        if float(z_value) < z_threshold or abs(float(move_value)) < minimum_move:
            result.mismatches.append({
                "created_at": event.created_at, "symbol": event.symbol,
                "reason": "threshold_recalc_differs",
                "z": z_value, "move": move_value,
            })
            continue
        replayed.append(event)
    result.replayed_events = replayed
    return result


def audit_log_integrity(store, window_start: str, window_end: str) -> dict:
    """数据完整性声明（报告头第 4 行）：decision_logs 跨度 + 窗口内小时缺口。"""
    rows = store.decisions_for_strategy(SCANNER_STRATEGY, limit=200_000)
    timestamps = sorted(
        str(row.get("created_at") or "") for row in rows if row.get("created_at")
    )
    span_start = timestamps[0] if timestamps else ""
    span_end = timestamps[-1] if timestamps else ""
    window_hours = {
        str(row.get("created_at") or "")[:13] for row in rows
        if window_start <= str(row.get("created_at") or "") <= window_end
    }
    gaps: list[dict] = []
    if window_start and window_end:
        try:
            start = datetime.fromisoformat(window_start)
            end = datetime.fromisoformat(window_end)
        except ValueError:
            return {"span_start": span_start, "span_end": span_end, "gaps": gaps}
        cursor = start.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
        while cursor <= end:
            key = cursor.isoformat()[:13]
            if key not in window_hours:
                gaps.append({"hour_bucket": key})
            cursor = cursor + step
    return {
        "span_start": span_start, "span_end": span_end,
        "gaps": gaps[:48], "gap_count": len(gaps),
    }
