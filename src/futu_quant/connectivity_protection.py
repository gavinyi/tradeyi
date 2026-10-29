from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from zoneinfo import ZoneInfo

import pandas as pd

from .option_lifecycle import parse_option_code

NEW_YORK = ZoneInfo("America/New_York")


def _today_new_york() -> date:
    return datetime.now(NEW_YORK).date()


PROTECTION_EVENT_TYPES = {
    "PROTECTIVE_RESTART_REQUESTED",
    "PROTECTIVE_RADAR_RESTART_REQUESTED",
    "PROTECTIVE_SERVICE_RESTART_REQUESTED",
    "PROTECTIVE_MODE_ACTIVE",
}

# 中断窗口的“解除”事件。**单一事实源**：此前本模块与
# `home_dashboard.latest_strategy_interruption_from_frame` 各写一份白名单，
# 前者漏了 `STARTUP_RECOVERY` —— 同一条历史故障，两处判定可能给出相反结论。
# 任何新增的恢复类事件都必须加在这里。
RECOVERY_EVENT_TYPES = {
    "PROTECTIVE_RECOVERY_CONFIRMED",
    "BROKER_CONNECTIVITY_RECOVERED",
    # 服务重启后会先完成账户/订单对账并写入 STARTUP_RECOVERY；这同样证明
    # 此前的 OpenD 链路中断已恢复，不能继续把历史故障当成“当前中断”。
    "STARTUP_RECOVERY",
}


@dataclass(frozen=True)
class StrategyInterruption:
    started_at: str
    started_at_china: str
    last_at: str
    last_at_china: str
    duration_minutes: float
    failures: int
    open_option_count: int
    task: str
    message: str
    recovered: bool


def json_payload(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def china_label(value: object) -> str:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return "-"
    return timestamp.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")


def active_protected_option_positions(store) -> list[dict]:
    """Return currently open option positions that require live exit management."""
    rows: dict[str, dict] = {}
    for source, items in (
        ("virtual", store.virtual_positions()),
        ("shadow", store.shadow_open_positions()),
    ):
        for item in items:
            symbol = str(item.get("symbol") or item.get("code") or "")
            contract = parse_option_code(symbol)
            if contract is None:
                continue
            if contract.expiration < _today_new_york():
                # 已到期合约不可能还有持仓，也不需要「实时退出管理」。
                # 2026-10-27 事故：账本残留 9 张 8 月到期的合约，导致每次 OpenD 抖动
                # 都被判成「有未平期权」→ 触发持仓保护模式 → 暂停新开仓 + 重启整个服务。
                continue
            try:
                quantity = float(item.get("quantity") or item.get("qty") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if abs(quantity) <= 1e-9:
                continue
            strategy_id = str(item.get("strategy_id") or "")
            if strategy_id and strategy_id not in {
                "volatility_long_gamma",
                "earnings_gamma",
                "earnings_gamma_manual",
            }:
                continue
            existing = rows.setdefault(symbol, {
                "symbol": symbol,
                "quantity": 0.0,
                "sources": set(),
                "strategy_id": strategy_id or "unknown",
            })
            existing["quantity"] += quantity
            existing["sources"].add(source)
            if strategy_id:
                existing["strategy_id"] = strategy_id
    result = []
    for item in rows.values():
        normalized = dict(item)
        normalized["sources"] = sorted(normalized["sources"])
        result.append(normalized)
    return sorted(result, key=lambda row: row["symbol"])


def has_active_protected_options(store) -> bool:
    return bool(active_protected_option_positions(store))


def _event_timestamp(row: dict) -> pd.Timestamp:
    return pd.to_datetime(row.get("created_at"), utc=True, errors="coerce")


def latest_strategy_interruption(store, *, hours: int = 48) -> StrategyInterruption | None:
    events = store.operational_events(limit=5000)
    if not events:
        return None
    frame = pd.DataFrame(events)
    if frame.empty or not {"event_type", "created_at"}.issubset(frame.columns):
        return None
    frame["_ts"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
    frame = frame[frame["_ts"].notna()].copy()
    if frame.empty:
        return None
    cutoff = pd.Timestamp.now(tz=timezone.utc) - pd.Timedelta(hours=hours)
    frame = frame[frame["_ts"].ge(cutoff)]
    if frame.empty:
        return None

    event_type = frame["event_type"].astype(str).str.upper()
    start_mask = event_type.isin(PROTECTION_EVENT_TYPES)
    broker_mask = event_type.eq("BROKER_CONNECTIVITY_FAILURE")
    recovery_mask = event_type.isin(RECOVERY_EVENT_TYPES)
    candidates = frame[start_mask | broker_mask].sort_values("_ts")
    if candidates.empty:
        return None

    latest = candidates.iloc[-1]
    latest_ts = latest["_ts"]
    related = candidates[candidates["_ts"].le(latest_ts)].tail(50)
    # Collapse adjacent failures into one interruption window. A gap above 10
    # minutes means the earlier burst is not part of the current outage.
    start_ts = latest_ts
    failures = 0
    for _, row in reversed(list(related.iterrows())):
        row_ts = row["_ts"]
        if (start_ts - row_ts).total_seconds() > 600:
            break
        start_ts = row_ts
        if str(row.get("event_type", "")).upper() == "BROKER_CONNECTIVITY_FAILURE":
            failures += 1
    recoveries = frame[recovery_mask & frame["_ts"].gt(latest_ts)].sort_values("_ts")
    recovered = not recoveries.empty
    end_ts = recoveries.iloc[0]["_ts"] if recovered else pd.Timestamp.now(tz=timezone.utc)
    payload = json_payload(latest.get("payload_json"))
    return StrategyInterruption(
        started_at=start_ts.isoformat(),
        started_at_china=china_label(start_ts),
        last_at=latest_ts.isoformat(),
        last_at_china=china_label(latest_ts),
        duration_minutes=max(0.0, (end_ts - start_ts).total_seconds() / 60),
        failures=max(failures, int(payload.get("failures") or 0)),
        open_option_count=int(payload.get("open_option_count") or 0),
        task=str(payload.get("task") or payload.get("trigger_task") or ""),
        message=str(payload.get("message") or payload.get("reason") or ""),
        recovered=recovered,
    )
