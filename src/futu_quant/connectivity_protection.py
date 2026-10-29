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


# 券商持仓快照的键：断线时**读不到实时持仓**，只能读最后一次成功写入的快照。
# 那正是需要保护的对象 —— 不能因为快照“旧了”就当没有仓位，风控宁可多报。
_BROKER_POSITION_SNAPSHOT_KEYS = {
    "REAL": "positions_real",
    "SIMULATE": "positions_sim",
}

# 策略账本里纳入保护范围的三类策略。其余策略（如 core_satellite 的正股）
# 不在这份白名单内，但**券商侧的正股持仓会被单独统计**，见下方
# `active_protected_stock_positions`。
_PROTECTED_STRATEGIES = {
    "volatility_long_gamma",
    "earnings_gamma",
    "earnings_gamma_manual",
}


def _broker_snapshot_positions(store, trading_env: str) -> tuple[list[dict], str]:
    """读券商持仓快照，返回 ``(持仓列表, 快照时间)``。

    为什么要读快照而不是实时查询：本函数在**预检已失败**时被调用，此刻券商
    连接就是断的，实时查询必然再失败一次。快照是断线前最后一次成功对账的结果，
    恰好就是「断线时手上有哪些仓」的答案。

    `getattr` 防御：单元测试普遍用轻量 fake store，不实现该方法时直接跳过，
    不要求所有替身都补上券商能力。
    """
    reader = getattr(store, "broker_snapshot", None)
    if not callable(reader):
        return [], ""
    key = _BROKER_POSITION_SNAPSHOT_KEYS.get(
        str(trading_env or "").strip().upper(), "positions_real",
    )
    try:
        snapshot = reader(key)
    except Exception:
        return [], ""
    if not snapshot:
        return [], ""
    payload = snapshot.get("payload")
    if not isinstance(payload, list):
        return [], ""
    return payload, str(snapshot.get("captured_at") or "")


def _normalize_quantity(item: dict) -> float:
    try:
        return float(item.get("quantity") or item.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ledger_option_positions(store) -> dict[str, dict]:
    """策略账本（虚拟盘 / 影子盘）里的期权持仓。"""
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
            quantity = _normalize_quantity(item)
            if abs(quantity) <= 1e-9:
                continue
            strategy_id = str(item.get("strategy_id") or "")
            if strategy_id and strategy_id not in _PROTECTED_STRATEGIES:
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
    return rows


def _broker_positions_split(store, trading_env: str) -> tuple[dict, dict, str]:
    """把券商快照拆成 ``(期权, 正股, 快照时间)``。

    券商快照没有 `strategy_id`，无法按策略白名单过滤 —— 这是有意的：
    只要账户里真有仓，就纳入可见范围。断线期间没人真正在意它由哪个策略建的，
    在意的是「有敞口却看不见」。
    """
    options: dict[str, dict] = {}
    stocks: dict[str, dict] = {}
    items, captured_at = _broker_snapshot_positions(store, trading_env)
    today = _today_new_york()
    for item in items:
        symbol = str(item.get("code") or item.get("symbol") or "").strip()
        if not symbol:
            continue
        quantity = _normalize_quantity(item)
        if abs(quantity) <= 1e-9:
            continue
        contract = parse_option_code(symbol)
        if contract is not None:
            if contract.expiration < today:
                continue
            bucket = options
        else:
            bucket = stocks
        existing = bucket.setdefault(symbol, {
            "symbol": symbol,
            "quantity": 0.0,
            "sources": set(),
            "strategy_id": "broker",
        })
        existing["quantity"] += quantity
        existing["sources"].add("broker")
        # 正股带市值/浮亏便于判断风险量级；期权以权利金为主，不强求。
        # 比例字段单独给 4 位：富途的 `pl_ratio` 是小数（-0.1616 = -16.16%），
        # 按金额那样保留 2 位会舍成 -0.16，展示出来正好丢掉一个数量级的信息。
        for field, digits in (
            ("market_val", 2), ("pl_val", 2), ("nominal_price", 2), ("pl_ratio", 4),
        ):
            value = item.get(field)
            if value is None:
                continue
            try:
                existing[field] = round(float(value), digits)
            except (TypeError, ValueError):
                continue
    return options, stocks, captured_at


def _merge_and_sort(buckets: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for bucket in buckets:
        for symbol, item in bucket.items():
            if symbol in merged:
                merged[symbol]["quantity"] += item["quantity"]
                merged[symbol]["sources"] |= item["sources"]
                continue
            merged[symbol] = dict(item, sources=set(item["sources"]))
    result = []
    for item in merged.values():
        normalized = dict(item)
        normalized["sources"] = sorted(normalized["sources"])
        result.append(normalized)
    return sorted(result, key=lambda row: row["symbol"])


def active_protected_option_positions(store, *, trading_env: str = "REAL") -> list[dict]:
    """当前需要实时退出管理的**期权**持仓：策略账本 + 券商实盘，两路合并。"""
    ledger = _ledger_option_positions(store) if _has_ledger(store) else {}
    broker_options, _broker_stocks, _captured = _broker_positions_split(store, trading_env)
    return _merge_and_sort([ledger, broker_options])


def _has_ledger(store) -> bool:
    return callable(getattr(store, "virtual_positions", None)) and callable(
        getattr(store, "shadow_open_positions", None)
    )


def active_protected_stock_positions(store, *, trading_env: str = "REAL") -> list[dict]:
    """券商侧的**正股**持仓。

    与期权分开统计的原因：正股无到期日，断线的危害是「暂时不能止损」而非
    gamma 风险，不需要按期权那套节奏升级重启；但它必须**可见** ——
    2026-10-30 实测实盘长期持有 NVDA 6 股 / GLD 2.43 股，断线保护事件里
    一张都没提到，用户会以为账户是空仓。
    """
    _options, stocks, _captured = _broker_positions_split(store, trading_env)
    return _merge_and_sort([stocks])


def protected_position_summary(store, *, trading_env: str = "REAL") -> dict:
    """持仓保护的完整视图：期权、正股分开计数，附快照时间与来源。"""
    ledger = _ledger_option_positions(store) if _has_ledger(store) else {}
    broker_options, broker_stocks, captured_at = _broker_positions_split(store, trading_env)
    options = _merge_and_sort([ledger, broker_options])
    stocks = _merge_and_sort([broker_stocks])
    sources = sorted({src for item in options for src in item["sources"]})
    return {
        "option_count": len(options),
        "stock_count": len(stocks),
        "options": options,
        "stocks": stocks,
        "option_sources": sources,
        "broker_snapshot_at": captured_at,
    }


def has_active_protected_options(store, *, trading_env: str = "REAL") -> bool:
    return bool(active_protected_option_positions(store, trading_env=trading_env))


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
