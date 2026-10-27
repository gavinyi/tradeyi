"""P1-11 批 1：按策略维度的净值快照（**只写不读**，观测用）。

设计：``data/qa/p1_11_observability_design.md`` §2（批 1 = 迁移 21 + 新表 + 写入）。

批 1 边界（刻意收窄）：
- 只写 ``strategy_equity_snapshots``，不接页面、不做回填、不算 ``attribution_diff``；
- ``unrealized_pnl`` 在**没有标记价**时写 NULL（``attribution_status='NO_MARK'``），
  **绝不写 0 冒充**（批 1 尚无统一标记价入口，故凡有持仓的策略一律 NO_MARK）；
- 观察期 3 天，失败只落 DEGRADED 事件，连续 3 次升级 ERROR，**绝不打断**权益快照任务。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BUCKET_SECONDS_SETTING = "STRATEGY_EQUITY_SNAPSHOT_SECONDS"
DEFAULT_BUCKET_SECONDS = 300
NY = ZoneInfo("America/New_York")
CLOSE_HOUR = 16
FAILURE_STREAK_LIMIT = 3

_failure_streak: dict[str, int] = {}


def bucket_seconds(settings=None) -> int:
    """写入粒度（秒）；<=0 表示关闭（返回 0）。"""
    raw = None
    if settings is not None:
        raw = getattr(settings, "strategy_equity_snapshot_seconds", None)
    try:
        value = int(raw) if raw is not None else DEFAULT_BUCKET_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_BUCKET_SECONDS
    return max(0, value)


def bucket_start(now: datetime, seconds: int = DEFAULT_BUCKET_SECONDS) -> datetime:
    """桶起点（UTC）：按 ``seconds`` 向下取整，保证同桶重写走 UPSERT。"""
    step = max(int(seconds), 1)
    moment = now.astimezone(timezone.utc)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step, tz=timezone.utc)


def close_bucket_start(now: datetime) -> datetime | None:
    """当日收盘桶起点（NY 16:00 换成 UTC）；未到收盘返回 None。"""
    ny_now = now.astimezone(NY)
    if ny_now.hour < CLOSE_HOUR:
        return None
    close_local = ny_now.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return close_local.astimezone(timezone.utc)


def fifo_realized_pnl(fills: list[dict]) -> dict[str, float]:
    """按策略算 FIFO 已实现盈亏（含费用）。

    ``fills`` 需按时间升序；返回 ``{strategy_id: realized_pnl}``。
    与 ``performance.py`` 的 FIFO 口径一致（同一套「先进先出、费用计入」）。
    """
    lots: dict[tuple[str, str], list[list[float]]] = {}
    realized: dict[str, float] = {}
    for row in fills or []:
        strategy_id = str(row.get("strategy_id") or "")
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").upper()
        try:
            qty = float(row.get("quantity") or 0)
            price = float(row.get("price") or 0)
            multiplier = float(row.get("multiplier") or 1) or 1.0
            fee = float(row.get("fee_total") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        book = lots.setdefault((strategy_id, symbol), [])
        if side in {"BUY", "BUY_TO_OPEN"} or not book:
            book.append([qty, price * multiplier])
            realized.setdefault(strategy_id, 0.0)
            if side not in {"BUY", "BUY_TO_OPEN"}:
                # 无对应买入的卖出：不臆造成本，直接计入进项（留痕由 attribution_status 兜底）
                realized[strategy_id] += qty * price * multiplier - fee
            continue
        remaining = qty
        cost = 0.0
        while remaining > 1e-9 and book:
            lot = book[0]
            take = min(lot[0], remaining)
            cost += take * lot[1]
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                book.pop(0)
        realized[strategy_id] = realized.get(strategy_id, 0.0) + (
            qty * price * multiplier - cost - fee
        )
    return realized


def group_positions(allocations: list[dict]) -> dict[str, list[dict]]:
    """把 ``virtual_allocations`` 按策略归组，丢弃零持仓。"""
    grouped: dict[str, list[dict]] = {}
    for row in allocations or []:
        try:
            qty = float(row.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if abs(qty) < 1e-9:
            continue
        strategy_id = str(row.get("strategy_id") or "")
        grouped.setdefault(strategy_id, []).append(
            {"symbol": row.get("symbol"), "quantity": qty},
        )
    return grouped


def build_rows(
    *,
    tenant_id: str,
    bucket: str,
    as_of: datetime,
    realized: dict[str, float],
    positions: dict[str, list[dict]],
    total_assets: float | None,
    now: datetime,
) -> list[dict]:
    """组装待写入行；策略集合 = 有已实现或有持仓。"""
    strategies = sorted({*realized.keys(), *positions.keys()})
    rows: list[dict] = []
    for strategy_id in strategies:
        holds = positions.get(strategy_id, [])
        has_marks = False  # 批 1 无统一标记价入口 → 有持仓一律 NO_MARK
        if not holds:
            unrealized = 0.0
            status = "COMPLETE"
        elif has_marks:
            unrealized = None
            status = "PARTIAL"
        else:
            unrealized = None
            status = "NO_MARK"
        rows.append({
            "tenant_id": str(tenant_id),
            "strategy_id": strategy_id,
            "bucket": bucket,
            "as_of": as_of.astimezone(timezone.utc).isoformat(),
            "realized_pnl": float(realized.get(strategy_id, 0.0)),
            "unrealized_pnl": unrealized,
            "attribution_status": status,
            "net_exposure": 0.0,
            "market_value": 0.0,
            "position_count": len(holds),
            "account_total_assets": (
                None if total_assets is None else float(total_assets)
            ),
            "attribution_diff": None,
            "source": "cycle",
            "created_at": now.astimezone(timezone.utc).isoformat(),
        })
    return rows


def snapshot_strategy_equity(
    store, alerts, *, tenant_id: str, total_assets: float | None,
    now: datetime, settings=None,
) -> int:
    """写一批策略净值快照；**任何异常都不得抛出**（观测失败不影响交易）。

    返回实际写入行数；0 表示关闭或未写入。
    """
    tenant_key = str(tenant_id or "").strip()
    if not tenant_key:
        _note_failure(store, "", "tenant_id_missing")
        return 0
    seconds = bucket_seconds(settings)
    if seconds <= 0:
        return 0
    try:
        fills = store.fill_ledger_rows(tenant_key)
        allocations = store.virtual_allocations_for_tenant(tenant_key)
        rows = build_rows(
            tenant_id=tenant_key,
            bucket="5m",
            as_of=bucket_start(now, seconds),
            realized=fifo_realized_pnl(fills),
            positions=group_positions(allocations),
            total_assets=total_assets,
            now=now,
        )
        close_at = close_bucket_start(now)
        if close_at is not None:
            rows.extend(build_rows(
                tenant_id=tenant_key,
                bucket="close",
                as_of=close_at,
                realized=fifo_realized_pnl(fills),
                positions=group_positions(allocations),
                total_assets=total_assets,
                now=now,
            ))
        for row in rows:
            store.save_strategy_equity_snapshot(row)
        _failure_streak[tenant_key] = 0
        store.heartbeat(
            f"strategy_equity:{tenant_key}", "HEALTHY",
            f"策略净值快照 {len(rows)} 行（bucket={rows[0]['bucket'] if rows else '-'}）",
        )
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - 观测写入失败绝不影响权益快照任务
        _note_failure(store, tenant_key, f"{type(exc).__name__}: {exc}")
        return 0


def _note_failure(store, tenant_id: str, reason: str) -> None:
    """失败留痕：DEGRADED，连续 3 次升级 ERROR（仍然不抛）。"""
    key = str(tenant_id or "-")
    streak = _failure_streak.get(key, 0) + 1
    _failure_streak[key] = streak
    level = "ERROR" if streak >= FAILURE_STREAK_LIMIT else "DEGRADED"
    try:
        store.operational_event(
            "strategy_equity", "STRATEGY_EQUITY_SNAPSHOT_FAILED", level,
            {"tenant_id": tenant_id, "reason": reason, "streak": streak},
        )
        store.heartbeat(
            f"strategy_equity:{key}", "DEGRADED", f"策略净值快照失败：{reason}",
        )
    except Exception:  # noqa: BLE001
        # silent-ok: 留痕失败不得反向打断调用方（下一轮会再尝试）
        pass
