from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import TradingStore


CONFIG_KEY = "CORE_SATELLITE_DAILY_VALIDATION"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


@dataclass(frozen=True)
class CoreDailyValidation:
    enabled: bool
    active: bool
    target_sessions: int
    completed_sessions: tuple[str, ...]
    total_orders: int
    weight_tolerance: float
    min_trade_usd: float
    started_at: datetime | None
    expires_at: datetime | None
    reason: str

    @property
    def remaining_sessions(self) -> int:
        return max(0, self.target_sessions - len(self.completed_sessions))


@dataclass(frozen=True)
class EffectiveCoreRebalanceSettings:
    frequency: str
    weight_tolerance: float
    min_trade_usd: float
    validation_mode: bool
    validation_session: str
    formal_frequency: str


def _default_config() -> dict:
    return {
        "enabled": False,
        "target_sessions": 10,
        "completed_sessions": [],
        "orders_by_session": {},
        "weight_tolerance": 0.01,
        "min_trade_usd": 500.0,
        "started_at": "",
        "expires_at": "",
        "disabled_reason": "尚未启用",
    }


def load_core_daily_validation(
    store: TradingStore, now: datetime | None = None,
) -> CoreDailyValidation:
    saved = store.get_runtime_config(CONFIG_KEY)
    raw = saved[0] if saved and isinstance(saved[0], dict) else _default_config()
    current = _as_utc(now)
    enabled = bool(raw.get("enabled", False))
    target = min(30, max(1, int(raw.get("target_sessions", 10))))
    sessions = tuple(dict.fromkeys(
        str(item) for item in raw.get("completed_sessions", []) if str(item)
    ))
    orders = raw.get("orders_by_session", {})
    if not isinstance(orders, dict):
        orders = {}
    total_orders = sum(max(0, int(value)) for value in orders.values())
    started_at = _parse_time(raw.get("started_at"))
    expires_at = _parse_time(raw.get("expires_at"))
    completed = len(sessions) >= target
    expired = bool(expires_at and current > expires_at)
    active = enabled and not completed and not expired
    if active:
        reason = f"每日验证进行中，还需 {target - len(sessions)} 个交易日"
    elif not enabled:
        reason = str(raw.get("disabled_reason") or "管理员已停用")
    elif completed:
        reason = f"已完成 {target} 个交易日验证，已自动恢复正式调仓参数"
    elif expired:
        reason = "验证窗口已到期，已自动恢复正式调仓参数"
    else:
        reason = "验证模式未激活"
    return CoreDailyValidation(
        enabled=enabled,
        active=active,
        target_sessions=target,
        completed_sessions=sessions,
        total_orders=total_orders,
        weight_tolerance=min(0.20, max(0.0, float(raw.get("weight_tolerance", 0.01)))),
        min_trade_usd=max(0.0, float(raw.get("min_trade_usd", 500.0))),
        started_at=started_at,
        expires_at=expires_at,
        reason=reason,
    )


def enable_core_daily_validation(
    store: TradingStore,
    target_sessions: int = 10,
    weight_tolerance: float = 0.01,
    min_trade_usd: float = 500.0,
    now: datetime | None = None,
    max_calendar_days: int = 28,
) -> CoreDailyValidation:
    if not 1 <= int(target_sessions) <= 30:
        raise ValueError("验证交易日数必须在 1 到 30 之间")
    if not 0 <= float(weight_tolerance) <= 0.20:
        raise ValueError("验证容忍带必须在 0 到 20% 之间")
    if float(min_trade_usd) < 0:
        raise ValueError("验证最低交易金额不能为负数")
    if not 1 <= int(max_calendar_days) <= 90:
        raise ValueError("验证安全期限必须在 1 到 90 天之间")
    started = _as_utc(now)
    payload = {
        "enabled": True,
        "target_sessions": int(target_sessions),
        "completed_sessions": [],
        "orders_by_session": {},
        "weight_tolerance": float(weight_tolerance),
        "min_trade_usd": float(min_trade_usd),
        "started_at": started.isoformat(),
        "expires_at": (started + timedelta(days=int(max_calendar_days))).isoformat(),
        "disabled_reason": "",
    }
    store.set_runtime_config(CONFIG_KEY, payload, "admin")
    store.audit("CORE_DAILY_VALIDATION_ENABLED", payload, "core_satellite", actor="service:validation")
    return load_core_daily_validation(store, started)


def disable_core_daily_validation(
    store: TradingStore, reason: str = "管理员手动停止", now: datetime | None = None,
) -> CoreDailyValidation:
    def update(current):
        value = dict(current) if isinstance(current, dict) else _default_config()
        value["enabled"] = False
        value["disabled_reason"] = reason
        value["disabled_at"] = _as_utc(now).isoformat()
        return value

    value, _ = store.mutate_runtime_config(CONFIG_KEY, _default_config(), update, "admin")
    store.audit(
        "CORE_DAILY_VALIDATION_DISABLED",
        {"reason": reason, "completed_sessions": value.get("completed_sessions", [])},
        "core_satellite",
     actor="service:validation")
    return load_core_daily_validation(store, now)


def record_core_validation_session(
    store: TradingStore, session_date: str, order_count: int,
    now: datetime | None = None,
) -> CoreDailyValidation:
    session_date = str(session_date)[:10]
    if len(session_date) != 10:
        raise ValueError("交易日格式必须为 YYYY-MM-DD")

    def update(current):
        value = dict(current) if isinstance(current, dict) else _default_config()
        if not value.get("enabled"):
            return value
        sessions = list(dict.fromkeys(str(item) for item in value.get("completed_sessions", [])))
        if session_date not in sessions:
            sessions.append(session_date)
        orders = dict(value.get("orders_by_session", {}))
        orders[session_date] = max(int(orders.get(session_date, 0)), max(0, int(order_count)))
        value["completed_sessions"] = sessions
        value["orders_by_session"] = orders
        value["last_evaluated_at"] = _as_utc(now).isoformat()
        return value

    value, _ = store.mutate_runtime_config(
        CONFIG_KEY, _default_config(), update, "core_satellite_service",
    )
    store.audit(
        "CORE_DAILY_VALIDATION_SESSION",
        {"session_date": session_date, "order_count": max(0, int(order_count)),
         "completed_sessions": value.get("completed_sessions", [])},
        "core_satellite",
     actor="service:validation")
    return load_core_daily_validation(store, now)


def resolve_core_rebalance_settings(
    settings, store: TradingStore, now: datetime | None = None,
) -> EffectiveCoreRebalanceSettings:
    mode = load_core_daily_validation(store, now)
    if mode.active:
        return EffectiveCoreRebalanceSettings(
            frequency="daily",
            weight_tolerance=mode.weight_tolerance,
            min_trade_usd=mode.min_trade_usd,
            validation_mode=True,
            validation_session=f"{len(mode.completed_sessions) + 1}/{mode.target_sessions}",
            formal_frequency=str(settings.core_satellite_rebalance_frequency),
        )
    return EffectiveCoreRebalanceSettings(
        frequency=str(settings.core_satellite_rebalance_frequency),
        weight_tolerance=float(settings.core_satellite_weight_tolerance),
        min_trade_usd=float(settings.core_satellite_min_trade_usd),
        validation_mode=False,
        validation_session="",
        formal_frequency=str(settings.core_satellite_rebalance_frequency),
    )
