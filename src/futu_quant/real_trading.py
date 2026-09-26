from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from .config import Settings
from .risk import ny_trading_day_start_utc
from .store import TradingStore


SESSION_KEY = "real_trading_session"
OPTION_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$")


@dataclass(frozen=True)
class RealTradeDecision:
    allowed: bool
    reason: str
    details: dict[str, Any]


def contract_multiplier(symbol: str, metadata_multiplier: Any = None) -> float:
    if metadata_multiplier is not None:
        try:
            value = float(metadata_multiplier)
            return value if value > 0 else 1.0
        except (TypeError, ValueError):
            pass
    return 100.0 if OPTION_PATTERN.match(symbol) else 1.0


def confirmation_code(settings: Settings, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone()
    account_tail = str(settings.real_account_id or "0000")[-4:]
    return f"REAL-{current:%Y%m%d}-{account_tail}"


def _parse_session(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_session(store: TradingStore) -> dict[str, Any]:
    return _parse_session(store.get_setting(SESSION_KEY, "{}"))


def session_status(
    store: TradingStore, settings: Settings, now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    session = load_session(store)
    if settings.trading_env != "REAL":
        return False, "当前交易环境不是 REAL", session
    if not settings.real_trading_enabled:
        return False, "REAL_TRADING_ENABLED 未开启", session
    if not settings.real_account_id:
        return False, "未配置 REAL_ACCOUNT_ID", session
    if settings.real_require_manual_confirm:
        expires_at = str(session.get("expires_at") or "")
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            return False, "尚未完成实盘人工确认会话", session
        current = now or datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if current > expires:
            return False, "实盘人工确认会话已过期", session
        if str(session.get("account_id") or "") != str(settings.real_account_id or ""):
            return False, "确认会话账户与 REAL_ACCOUNT_ID 不一致", session
    return True, "实盘确认会话有效", session


def enable_session(
    store: TradingStore,
    settings: Settings,
    code: str,
    minutes: int = 30,
    actor: str = "streamlit_admin",
    now: datetime | None = None,
) -> tuple[bool, str]:
    if settings.trading_env != "REAL":
        return False, "请先把 FUTU_TRADING_ENV 切换为 REAL 并保存"
    if not settings.real_trading_enabled:
        return False, "请先开启 REAL_TRADING_ENABLED"
    if not settings.real_account_id:
        return False, "请先配置 REAL_ACCOUNT_ID"
    expected = confirmation_code(settings, now)
    if str(code).strip() != expected:
        return False, f"确认码不正确，应输入 {expected}"
    current = now or datetime.now(timezone.utc)
    expires = current + timedelta(minutes=max(1, min(240, int(minutes))))
    payload = {
        "account_id": settings.real_account_id,
        "confirmed_at": current.isoformat(),
        "expires_at": expires.isoformat(),
        "actor": actor,
    }
    store.set_setting(SESSION_KEY, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    store.audit("REAL_TRADING_SESSION_ENABLED", payload)
    return True, f"实盘确认会话已开启，到期时间 {expires.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"


def disable_session(store: TradingStore, actor: str = "streamlit_admin") -> None:
    store.set_setting(SESSION_KEY, "{}")
    store.audit("REAL_TRADING_SESSION_DISABLED", {"actor": actor})


def submitted_real_notional_since(store: TradingStore, iso_timestamp: str,
                                  exclude_intent_id: int | None = None) -> float:
    total = 0.0
    for row in store.intents(limit=50_000):
        if exclude_intent_id is not None and row.get("id") == exclude_intent_id:
            continue
        if str(row.get("created_at") or "") < iso_timestamp:
            continue
        # Unconfirmed broker outcomes may already consume live buying power.
        if str(row.get("status")) not in {"SUBMITTING", "SUBMITTED", "UNKNOWN", "FILLED", "FILLED_PART", "CANCEL_REQUESTED"}:
            continue
        metadata = _parse_session(row.get("metadata_json"))
        if metadata.get("execution_mode") != "REAL" and not str(row.get("intent_key", "")).endswith(":REAL"):
            continue
        multiplier = contract_multiplier(str(row.get("symbol", "")), metadata.get("multiplier"))
        total += abs(float(row.get("quantity") or 0) * float(row.get("limit_price") or 0) * multiplier)
    return total


def check_real_trade(
    store: TradingStore,
    settings: Settings,
    strategy_id: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
    exclude_intent_id: int | None = None,
) -> RealTradeDecision:
    metadata = metadata or {}
    if settings.trading_env != "REAL":
        return RealTradeDecision(False, "券商连接环境不是 REAL", {})
    if not settings.real_trading_enabled:
        return RealTradeDecision(False, "实盘总开关未开启", {})
    if strategy_id not in settings.real_allowed_strategies:
        return RealTradeDecision(False, f"策略 {strategy_id} 不在实盘白名单", {})
    active, reason, session = session_status(store, settings, now)
    if not active:
        return RealTradeDecision(False, reason, {"session": session})
    multiplier = contract_multiplier(symbol, metadata.get("multiplier"))
    notional = abs(float(quantity) * float(price) * multiplier)
    if notional > settings.real_max_order_notional:
        return RealTradeDecision(
            False,
            f"实盘单笔名义金额 ${notional:,.2f} 超过上限 ${settings.real_max_order_notional:,.2f}",
            {"notional": notional},
        )
    day_start = ny_trading_day_start_utc(now)
    used = submitted_real_notional_since(store, day_start.isoformat(), exclude_intent_id)
    if used + notional > settings.real_max_daily_notional:
        return RealTradeDecision(
            False,
            f"实盘当日累计名义金额将超过 ${settings.real_max_daily_notional:,.2f}",
            {"notional": notional, "used_today": used},
        )
    return RealTradeDecision(True, "实盘安全闸门通过", {"notional": notional, "used_today": used})
