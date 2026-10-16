from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
import math
from typing import Any

import pandas as pd

from .option_lifecycle import parse_option_code


OPTION_MULTIPLIER = 100.0
FILLED_STATUSES = {"FILLED", "FILLED_ALL", "FILLED_PART", "SHADOW_FILLED", "SHADOW_PART"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _quote_price(row: pd.Series | dict | None) -> float | None:
    if row is None:
        return None
    for column in ("last_price", "price", "nominal_price"):
        value = _number(row.get(column), 0)
        if value > 0:
            return value
    bid = _number(row.get("bid_price"), 0)
    ask = _number(row.get("ask_price"), 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return bid or ask or None


@dataclass
class OptionLot:
    lot_id: str
    strategy_id: str
    symbol: str
    underlying: str
    option_type: str
    strike: float
    expiration: str
    open_time: Any
    open_time_source: str
    open_quantity: float
    remaining_quantity: float
    open_price: float
    open_fees_remaining: float
    metadata: dict = field(default_factory=dict)
    closed_quantity: float = 0.0
    close_notional: float = 0.0
    close_fees: float = 0.0
    close_time: Any = None
    close_time_source: str = ""
    realized_pnl: float = 0.0

    @property
    def average_close_price(self) -> float | None:
        if self.closed_quantity <= 0:
            return None
        return self.close_notional / (self.closed_quantity * OPTION_MULTIPLIER)


def _fetch_quote_map(broker, symbols: list[str]) -> tuple[dict[str, dict], str]:
    if broker is None or not symbols:
        return {}, "未刷新行情"
    try:
        snapshots = broker.snapshots(symbols)
    except Exception as exc:  # broker adapters raise different concrete exceptions in tests/runtime
        return {}, f"行情读取失败：{exc}"
    if snapshots is None or snapshots.empty:
        return {}, "行情为空"
    return {
        str(row.get("code")): dict(row)
        for _, row in snapshots.iterrows()
        if str(row.get("code", "")).startswith("US.")
    }, "行情已刷新"


def _shadow_fill_times(store, limit: int) -> dict[int, str]:
    """Return the first Shadow fill timestamp for each intent.

    The PnL ledger should reflect when a lot actually got filled. For Shadow
    orders, `order_intents.created_at` is only the order creation time; the
    virtual fill can happen minutes later after liquidity/reprice checks.
    """
    try:
        rows = store.shadow_fills(limit=limit)
    except Exception:
        return {}
    first_by_intent: dict[int, str] = {}
    for row in rows:
        try:
            intent_id = int(row["intent_id"])
        except (TypeError, ValueError, KeyError):
            continue
        created_at = str(row.get("created_at") or "")
        if not created_at:
            continue
        previous = first_by_intent.get(intent_id)
        if previous is None or created_at < previous:
            first_by_intent[intent_id] = created_at
    return first_by_intent


def _broker_fill_times(store, limit: int) -> dict[int, str]:
    """Return the first broker-confirmed fill timestamp for each intent.

    `order_intents.last_broker_update` is a mutable reconciliation timestamp.
    It can move every time OpenD is polled, which makes historical PnL windows
    look empty after a later sync. The order state event is append-only, so it
    is the stable timestamp we want for review and PnL attribution.
    """
    try:
        rows = store.order_state_events(limit=limit * 2)
    except Exception:
        return {}
    first_by_intent: dict[int, str] = {}
    for row in rows:
        try:
            intent_id = int(row["intent_id"])
            filled_quantity = float(row.get("filled_quantity") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        status = str(row.get("new_status") or "").upper()
        created_at = str(row.get("created_at") or "")
        if filled_quantity <= 0 or status not in FILLED_STATUSES or not created_at:
            continue
        previous = first_by_intent.get(intent_id)
        if previous is None or created_at < previous:
            first_by_intent[intent_id] = created_at
    return first_by_intent


def _fill_time(
    order: pd.Series,
    shadow_times: dict[int, str],
    broker_times: dict[int, str],
) -> tuple[Any, str]:
    try:
        intent_id = int(order["id"])
    except (TypeError, ValueError, KeyError):
        intent_id = -1
    if intent_id in shadow_times:
        return shadow_times[intent_id], "Shadow 成交时间"
    if intent_id in broker_times:
        return broker_times[intent_id], "富途首次成交时间"
    if str(order.get("status", "")).upper() in FILLED_STATUSES:
        return order.get("created_at"), "订单创建时间"
    return order.get("created_at"), "订单创建时间"


def build_option_pnl_ledger(store, broker=None, limit: int = 50_000, env: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Build a per-option FIFO PnL ledger from order intents and Shadow fills.

    Returns:
        ledger: one row per option opening lot with realized/unrealized PnL.
        orders: every option order intent, including rejected/expired/debug history.
        quote_status: short human-readable quote refresh status.
    """
    intents = pd.DataFrame(store.intents(limit=limit))
    if intents.empty:
        return pd.DataFrame(), pd.DataFrame(), "没有订单记录"

    intents["parsed_contract"] = intents["symbol"].map(parse_option_code)
    option_orders = intents[intents["parsed_contract"].notna()].copy()
    if option_orders.empty:
        return pd.DataFrame(), pd.DataFrame(), "没有期权订单"

    # 环境隔离：支持按 REAL 或 SIMULATE 严格过滤期权台账
    if env:
        target_env = env.upper()
        is_real_mask = option_orders["intent_key"].astype(str).str.contains(":REAL", case=False, na=False) | option_orders["status"].astype(str).str.startswith("REAL")
        if target_env == "REAL":
            option_orders = option_orders[is_real_mask].copy()
            if option_orders.empty:
                return pd.DataFrame(), pd.DataFrame(), "暂无实盘期权订单"
        elif target_env in {"SIMULATE", "PAPER"}:
            option_orders = option_orders[~is_real_mask].copy()
            if option_orders.empty:
                return pd.DataFrame(), pd.DataFrame(), "暂无模拟盘期权订单"

    option_orders["metadata"] = option_orders["metadata_json"].map(_metadata)
    option_orders["成交数量"] = pd.to_numeric(
        option_orders.get("filled_quantity", 0), errors="coerce"
    ).fillna(0.0)
    option_orders["成交均价"] = pd.to_numeric(
        option_orders.get("average_fill_price", 0), errors="coerce"
    ).fillna(0.0)
    option_orders["费用"] = pd.to_numeric(option_orders.get("fees", 0), errors="coerce").fillna(0.0)
    option_orders["filled"] = (
        option_orders["成交数量"].gt(0)
        & option_orders["成交均价"].gt(0)
        & option_orders["status"].astype(str).isin(FILLED_STATUSES | {"SUBMITTED"})
    )

    symbols = sorted(option_orders["symbol"].astype(str).unique())
    quote_map, quote_status = _fetch_quote_map(broker, symbols)
    shadow_times = _shadow_fill_times(store, limit)
    broker_times = _broker_fill_times(store, limit)

    lots_by_key: dict[tuple[str, str], deque[OptionLot]] = defaultdict(deque)
    all_lots: list[OptionLot] = []
    orphan_closes: list[dict] = []

    fills = option_orders[option_orders["filled"]].sort_values(["created_at", "id"]).copy()
    for _, order in fills.iterrows():
        contract = order["parsed_contract"]
        strategy_id = str(order["strategy_id"])
        symbol = str(order["symbol"])
        key = (strategy_id, symbol)
        quantity = _number(order["成交数量"])
        price = _number(order["成交均价"])
        fees = _number(order["费用"])
        if quantity <= 0 or price <= 0:
            continue

        if str(order["side"]).upper() == "BUY":
            open_time, open_time_source = _fill_time(order, shadow_times, broker_times)
            lot = OptionLot(
                lot_id=f"{int(order['id'])}-{symbol}",
                strategy_id=strategy_id,
                symbol=symbol,
                underlying=contract.underlying,
                option_type=contract.option_type,
                strike=contract.strike,
                expiration=contract.expiration.isoformat(),
                open_time=open_time,
                open_time_source=open_time_source,
                open_quantity=quantity,
                remaining_quantity=quantity,
                open_price=price,
                open_fees_remaining=fees,
                metadata=order["metadata"],
            )
            lots_by_key[key].append(lot)
            all_lots.append(lot)
            continue

        remaining = quantity
        sell_fee_remaining = fees
        close_time, close_time_source = _fill_time(order, shadow_times, broker_times)
        while remaining > 0 and lots_by_key[key]:
            lot = lots_by_key[key][0]
            matched = min(remaining, lot.remaining_quantity)
            open_fee_alloc = (
                lot.open_fees_remaining * matched / lot.remaining_quantity
                if lot.remaining_quantity > 0 else 0.0
            )
            sell_fee_alloc = sell_fee_remaining * matched / remaining if remaining > 0 else 0.0
            lot.realized_pnl += (price - lot.open_price) * matched * OPTION_MULTIPLIER
            lot.realized_pnl -= open_fee_alloc + sell_fee_alloc
            lot.closed_quantity += matched
            lot.close_notional += price * matched * OPTION_MULTIPLIER
            lot.close_fees += sell_fee_alloc
            lot.open_fees_remaining -= open_fee_alloc
            lot.remaining_quantity -= matched
            lot.close_time = close_time
            lot.close_time_source = close_time_source

            remaining -= matched
            sell_fee_remaining -= sell_fee_alloc
            if lot.remaining_quantity <= 1e-9:
                lots_by_key[key].popleft()

        if remaining > 0:
            orphan_closes.append({
                "lot_id": f"{int(order['id'])}-{symbol}-SELL",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "underlying": contract.underlying,
                "option_type": contract.option_type,
                "strike": contract.strike,
                "expiration": contract.expiration.isoformat(),
                "open_time": None,
                "open_time_source": "",
                "close_time": close_time,
                "close_time_source": close_time_source,
                "open_quantity": 0.0,
                "closed_quantity": remaining,
                "remaining_quantity": 0.0,
                "open_price": None,
                "avg_close_price": price,
                "last_price": None,
                "realized_pnl": -sell_fee_remaining,
                "unrealized_pnl": 0.0,
                "total_pnl": -sell_fee_remaining,
                "fees": sell_fee_remaining,
                "return_ratio": None,
                "status": "未匹配开仓的卖出",
                "reason": order["metadata"].get("reason", ""),
                "entry_tier": order["metadata"].get("entry_tier", ""),
                "intent_key": order.get("intent_key", ""),
            })

    rows: list[dict] = []
    for lot in all_lots:
        quote_price = _quote_price(quote_map.get(lot.symbol))
        if lot.remaining_quantity > 0 and quote_price is not None:
            unrealized = (
                (quote_price - lot.open_price) * lot.remaining_quantity * OPTION_MULTIPLIER
                - lot.open_fees_remaining
            )
        elif lot.remaining_quantity > 0:
            unrealized = None
        else:
            unrealized = 0.0
        total_pnl = lot.realized_pnl + (unrealized if unrealized is not None else 0.0)
        cost_basis = lot.open_quantity * lot.open_price * OPTION_MULTIPLIER
        fees = lot.close_fees + lot.open_fees_remaining
        if lot.closed_quantity > 0 and lot.remaining_quantity == 0:
            status = "已平仓"
        elif lot.closed_quantity > 0:
            status = "部分平仓"
        else:
            status = "持仓中"
        rows.append({
            "lot_id": lot.lot_id,
            "strategy_id": lot.strategy_id,
            "underlying": lot.underlying,
            "symbol": lot.symbol,
            "option_type": lot.option_type,
            "strike": lot.strike,
            "expiration": lot.expiration,
            "open_time": lot.open_time,
            "open_time_source": lot.open_time_source,
            "close_time": lot.close_time,
            "close_time_source": lot.close_time_source,
            "open_quantity": lot.open_quantity,
            "closed_quantity": lot.closed_quantity,
            "remaining_quantity": lot.remaining_quantity,
            "open_price": lot.open_price,
            "avg_close_price": lot.average_close_price,
            "last_price": quote_price,
            "realized_pnl": lot.realized_pnl,
            "unrealized_pnl": unrealized,
            "total_pnl": total_pnl,
            "fees": fees,
            "return_ratio": total_pnl / cost_basis if cost_basis else None,
            "status": status if unrealized is not None or lot.remaining_quantity == 0 else "持仓中（行情缺失）",
            "reason": lot.metadata.get("reason", ""),
            "entry_tier": lot.metadata.get("entry_tier", ""),
            "effective_premium_budget": lot.metadata.get("effective_premium_budget"),
            "trigger_mode": lot.metadata.get("trigger_mode", ""),
            "intent_key": lot.metadata.get("intent_key", ""),
        })
    rows.extend(orphan_closes)

    ledger = pd.DataFrame(rows)
    if not ledger.empty:
        ledger = ledger.sort_values(["open_time", "close_time", "symbol"], ascending=[False, False, True])

    orders = option_orders.drop(columns=["parsed_contract", "metadata", "filled"], errors="ignore").copy()
    for contract_field in ("underlying", "option_type", "strike", "expiration"):
        orders[contract_field] = option_orders["parsed_contract"].map(
            lambda contract, name=contract_field: getattr(contract, name)
        )
    orders["expiration"] = orders["expiration"].astype(str)
    orders["order_amount"] = orders["quantity"].astype(float) * orders["limit_price"].astype(float) * OPTION_MULTIPLIER
    orders["filled_amount"] = orders["成交数量"] * orders["成交均价"] * OPTION_MULTIPLIER
    orders["reason"] = option_orders["metadata"].map(lambda item: item.get("reason", ""))
    return ledger, orders, quote_status


def option_pnl_summary(ledger: pd.DataFrame) -> dict[str, float]:
    if ledger.empty:
        return {
            "open_contracts": 0, "closed_lots": 0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "total_pnl": 0.0, "wins": 0, "losses": 0,
        }
    unrealized = pd.to_numeric(ledger["unrealized_pnl"], errors="coerce").fillna(0.0)
    total = pd.to_numeric(ledger["total_pnl"], errors="coerce").fillna(0.0)
    closed = ledger[pd.to_numeric(ledger["remaining_quantity"], errors="coerce").fillna(0.0) <= 0]
    return {
        "open_contracts": float(pd.to_numeric(ledger["remaining_quantity"], errors="coerce").fillna(0.0).sum()),
        "closed_lots": float(len(closed)),
        "realized_pnl": float(pd.to_numeric(ledger["realized_pnl"], errors="coerce").fillna(0.0).sum()),
        "unrealized_pnl": float(unrealized.sum()),
        "total_pnl": float(total.sum()),
        "wins": float((total > 0).sum()),
        "losses": float((total < 0).sum()),
    }
