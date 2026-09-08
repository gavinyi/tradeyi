from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import os
from typing import Iterable

import pandas as pd

from .option_lifecycle import parse_option_code


FILLED_STATUSES = {"FILLED", "FILLED_ALL", "SHADOW_FILLED"}
OPEN_ORDER_MARKERS = ("待", "提交", "未成交", "SUBMITTED", "SUBMIT", "FILLED_PART")


@dataclass(frozen=True)
class VolatilityOrphanPosition:
    code: str
    underlying: str
    quantity: int
    can_sell_quantity: int
    cost_price: float
    nominal_price: float
    market_value: float
    pnl_value: float
    pnl_ratio: float
    strategy_net_quantity: float
    pending_order_count: int
    status: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def option_codes_from_campaigns(campaigns: Iterable[dict]) -> set[str]:
    codes: set[str] = set()
    for campaign in campaigns:
        code = str(campaign.get("code") or "").upper()
        if parse_option_code(code):
            codes.add(code)
        for leg in campaign.get("legs") or []:
            leg_code = str(leg.get("code") or leg.get("symbol") or "").upper()
            if parse_option_code(leg_code):
                codes.add(leg_code)
    return codes


def _number(value: object, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _strategy_net_quantities(store, strategy_id: str, limit: int = 50000) -> dict[str, float]:
    net: dict[str, float] = {}
    for intent in store.intents(limit=limit):
        if str(intent.get("strategy_id")) != strategy_id:
            continue
        status = str(intent.get("status", "")).upper()
        filled = _number(intent.get("filled_quantity"))
        if filled <= 0 and status not in FILLED_STATUSES:
            continue
        quantity = filled if filled > 0 else _number(intent.get("quantity"))
        if quantity <= 0:
            continue
        symbol = str(intent.get("symbol") or "").upper()
        if parse_option_code(symbol) is None:
            continue
        side = str(intent.get("side", "")).upper()
        if side == "BUY":
            net[symbol] = net.get(symbol, 0.0) + quantity
        elif side == "SELL":
            net[symbol] = net.get(symbol, 0.0) - quantity
    return net


def _latest_strategy_buy(store, strategy_id: str, code: str) -> dict | None:
    for intent in store.intents(limit=50000):
        if (
            str(intent.get("strategy_id")) == strategy_id
            and str(intent.get("symbol") or "").upper() == code.upper()
            and str(intent.get("side", "")).upper() == "BUY"
            and _number(intent.get("filled_quantity")) > 0
        ):
            return intent
    return None


def _pending_counts(orders: pd.DataFrame | None, broker) -> dict[str, int]:
    if orders is None or orders.empty or "code" not in orders.columns:
        return {}
    if "order_status" not in orders.columns:
        return {
            str(code).upper(): int(count)
            for code, count in orders["code"].astype(str).str.upper().value_counts().items()
        }
    counts: dict[str, int] = {}
    for _, row in orders.iterrows():
        status = row.get("order_status")
        try:
            active = bool(broker.is_cancellable_status(status))
        except Exception:
            active = any(marker in str(status).upper() for marker in OPEN_ORDER_MARKERS)
        if not active:
            continue
        code = str(row.get("code") or "").upper()
        counts[code] = counts.get(code, 0) + 1
    return counts


def detect_volatility_orphan_positions(
    store,
    positions: pd.DataFrame | None,
    orders: pd.DataFrame | None,
    broker,
    campaigns: Iterable[dict],
    strategy_id: str = "volatility_long_gamma",
) -> list[VolatilityOrphanPosition]:
    """Find broker option positions created by the strategy but no longer managed.

    The detector is intentionally conservative: a broker position is considered
    an orphan only when the same option has a positive net filled quantity in
    this strategy's order-intent ledger. This avoids selling manually created
    option positions simply because their underlying is also monitored.
    """
    if positions is None or positions.empty or "code" not in positions.columns:
        return []
    campaign_codes = option_codes_from_campaigns(campaigns)
    states = store.campaign_states()
    strategy_net = _strategy_net_quantities(store, strategy_id)
    pending = _pending_counts(orders, broker)
    rows: list[VolatilityOrphanPosition] = []
    for _, row in positions.iterrows():
        code = str(row.get("code") or "").upper()
        contract = parse_option_code(code)
        if contract is None:
            continue
        quantity = int(_number(row.get("qty", row.get("quantity", 0))))
        if quantity <= 0:
            continue
        if code in campaign_codes:
            continue
        state = states.get(f"{strategy_id}:{code}") or {}
        if int(_number(state.get("previous_position_quantity"))) > 0:
            continue
        strategy_net_quantity = strategy_net.get(code, 0.0)
        if strategy_net_quantity <= 0:
            continue
        can_sell = int(_number(row.get("can_sell_qty", row.get("sellable_qty", quantity))))
        pending_count = pending.get(code, 0)
        status = (
            "PENDING_EXIT_ORDER" if pending_count else
            "SELLABLE_ORPHAN" if can_sell > 0 else
            "LOCKED_ORPHAN"
        )
        reason = (
            "富途存在由波动率策略买入的期权持仓，但当前策略配置和状态均未接管"
        )
        rows.append(VolatilityOrphanPosition(
            code=code,
            underlying=contract.underlying,
            quantity=quantity,
            can_sell_quantity=can_sell,
            cost_price=_number(row.get("cost_price")),
            nominal_price=_number(row.get("nominal_price", row.get("last_price"))),
            market_value=_number(row.get("market_val", row.get("market_value"))),
            pnl_value=_number(row.get("pl_val", row.get("pnl_value"))),
            pnl_ratio=_number(row.get("pl_ratio", row.get("pnl_ratio"))),
            strategy_net_quantity=strategy_net_quantity,
            pending_order_count=pending_count,
            status=status,
            reason=reason,
        ))
    return rows


def protected_volatility_state_keys(
    store,
    positions: pd.DataFrame | None,
    orders: pd.DataFrame | None,
    broker,
    prefix: str = "volatility_long_gamma:",
) -> set[str]:
    """State keys that must not be pruned while broker risk still exists."""
    protected: set[str] = set()
    if positions is not None and not positions.empty and "code" in positions.columns:
        for _, row in positions.iterrows():
            code = str(row.get("code") or "").upper()
            if parse_option_code(code) and abs(_number(row.get("qty", row.get("quantity")))) > 1e-9:
                protected.add(f"{prefix}{code}")
    if orders is not None and not orders.empty and "code" in orders.columns:
        pending = _pending_counts(orders, broker)
        protected.update(f"{prefix}{code}" for code, count in pending.items() if count > 0)
    for item in store.shadow_open_positions():
        if str(item.get("strategy_id")) == prefix.rstrip(":"):
            code = str(item.get("symbol") or "").upper()
            if parse_option_code(code):
                protected.add(f"{prefix}{code}")
    return protected


def manage_volatility_orphans(
    *,
    broker,
    executor,
    campaigns: list[dict],
    positions: pd.DataFrame,
    orders: pd.DataFrame,
    now: datetime | None = None,
) -> list[dict]:
    """Audit and optionally submit reduce-only exits for unmanaged strategy positions."""
    now = now or datetime.now(timezone.utc)
    store = executor.store
    orphans = detect_volatility_orphan_positions(
        store, positions, orders, broker, campaigns,
    )
    if not orphans:
        store.recover_incident("volatility-orphan-broker-positions")
        return []
    payload = [item.to_dict() for item in orphans]
    message = f"发现 {len(orphans)} 个波动率期权未接管富途持仓，已进入安全处置流程"
    store.record_incident("volatility-orphan-broker-positions", "ERROR", message)
    store.audit("VOLATILITY_ORPHAN_POSITIONS_DETECTED", {"positions": payload}, "volatility_long_gamma")
    policy = store.get_setting(
        "volatility_orphan_position_policy",
        os.getenv("VOLATILITY_ORPHAN_POSITION_POLICY", "ADOPT"),
    ).upper()
    if policy not in {"ADOPT", "CLOSE", "BLOCK"}:
        policy = "ADOPT"
    results: list[dict] = []
    quote_map: dict[str, dict] = {}
    sellable_codes = [item.code for item in orphans if item.status == "SELLABLE_ORPHAN"]
    if sellable_codes:
        try:
            quotes = broker.snapshots(sellable_codes)
            if quotes is not None and not quotes.empty and "code" in quotes.columns:
                quote_map = {
                    str(row.get("code") or "").upper(): row.to_dict()
                    for _, row in quotes.iterrows()
                }
        except Exception as exc:
            store.audit(
                "VOLATILITY_ORPHAN_QUOTE_FAILED",
                {"error": str(exc), "codes": sellable_codes},
                "volatility_long_gamma",
            )
    for item in orphans:
        base = item.to_dict()
        if policy == "ADOPT":
            adopted = adopt_volatility_orphan_position(store, item, campaigns)
            reason = "未接管仓位已自动接管：后续纳入波动率策略止盈、止损、盈利保护和方向退出"
            store.decision("volatility_long_gamma", item.code, "HOLD", reason, {
                **base, "adopted_campaign": adopted,
            })
            results.append({
                **base,
                "action": "ADOPTED",
                "result": reason,
                "campaign": adopted,
            })
            continue
        if policy == "BLOCK":
            reason = "发现未接管仓位；当前策略配置为只提示不处置，等待人工处理"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base)
            results.append({**base, "action": "BLOCKED", "result": reason})
            continue
        if item.pending_order_count:
            reason = "未接管仓位已有待成交退出订单，等待成交回报"
            store.decision("volatility_long_gamma", item.code, "HOLD", reason, base)
            results.append({**base, "action": "WAIT_PENDING", "result": reason})
            continue
        if item.can_sell_quantity <= 0:
            reason = "发现未接管仓位，但当前可卖数量为 0，等待券商解锁"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base)
            results.append({**base, "action": "LOCKED", "result": reason})
            continue
        quote = quote_map.get(item.code, {})
        bid = _number(quote.get("bid_price"))
        reference = bid if bid > 0 else item.nominal_price
        if reference <= 0:
            reason = "发现未接管仓位，但盘口和现价均不可用，无法安全提交退出单"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base)
            results.append({**base, "action": "NO_PRICE", "result": reason})
            continue
        price = max(0.01, round(reference * (0.98 if bid > 0 else 0.90), 2))
        result = executor.submit_limit(
            "volatility_long_gamma",
            f"{now.date().isoformat()}:ORPHAN_EXIT:{item.code}",
            item.code,
            "SELL",
            min(item.quantity, item.can_sell_quantity),
            price,
            {
                "multiplier": 100.0,
                "underlying": item.underlying,
                "urgent": True,
                "reduce_only": True,
                "orphan_recovery": True,
                "reason": item.reason,
                "reference_price": reference,
                "orphan_position": base,
            },
        )
        reason = f"未接管仓位自动提交平仓：{result.message}"
        store.decision("volatility_long_gamma", item.code, "SELL", reason, {
            **base, "exit_price": price, "execution_status": result.status,
            "broker_order_id": result.broker_order_id,
        })
        results.append({
            **base,
            "action": "SELL_SUBMITTED",
            "exit_price": price,
            "result": result.message,
            "broker_order_id": result.broker_order_id,
            "execution_status": result.status,
        })
    store.audit("VOLATILITY_ORPHAN_POSITIONS_HANDLED", {"results": results}, "volatility_long_gamma")
    return results


def adopt_volatility_orphan_position(
    store,
    item: VolatilityOrphanPosition,
    campaigns: list[dict],
) -> dict:
    """Create a defensive exit-only campaign/state for an unmanaged broker position."""
    from .config import upsert_campaign

    contract = parse_option_code(item.code)
    template = next(
        (campaign for campaign in campaigns if str(campaign.get("underlying", "")).upper() == item.underlying.upper()),
        {},
    )
    latest_buy = _latest_strategy_buy(store, "volatility_long_gamma", item.code) or {}
    opened_at = str(latest_buy.get("created_at") or datetime.now(timezone.utc).isoformat())
    cost = item.cost_price or _number(latest_buy.get("average_fill_price")) or item.nominal_price
    peak = max(item.nominal_price, cost, _number(template.get("peak")))
    expiry = contract.expiration.isoformat() if contract else str(template.get("strike_time", ""))
    adopted = {
        **template,
        "campaign_id": f"orphan:{item.code}",
        "code": item.code,
        "underlying": item.underlying,
        "option_type": contract.option_type if contract else template.get("option_type", "CALL"),
        "strike_time": expiry,
        "event_date": datetime.now(timezone.utc).date().isoformat(),
        "event_session": template.get("event_session", "盘中"),
        "trigger_mode": "VOLATILITY",
        "structure": "LONG_OPTION",
        "legs": [{"code": item.code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "enabled": True,
        "exit_only": True,
        "orphan_adopted": True,
        "orphan_adopted_at": datetime.now(timezone.utc).isoformat(),
        "orphan_adopted_reason": item.reason,
        "contracts_per_stage": int(template.get("contracts_per_stage", 1) or 1),
        "max_stages": int(template.get("max_stages", 1) or 1),
        "max_premium_per_stage": float(template.get("max_premium_per_stage", 3000) or 3000),
        "profit_target": float(template.get("profit_target", .50) or .50),
        "stop_loss": float(template.get("stop_loss", .25) or .25),
        "trailing_stop": float(template.get("trailing_stop", .25) or .25),
        "entry_hours": int(template.get("entry_hours", 6) or 6),
        "max_hold_hours": float(template.get("max_hold_hours", 24) or 24),
    }
    upsert_campaign("VOLATILITY_CAMPAIGNS", adopted)
    state_key = f"volatility_long_gamma:{item.code}"
    states = store.campaign_states()
    state = dict(states.get(state_key) or {})
    state.update({
        "previous_position_quantity": int(item.quantity),
        "position_opened_at": opened_at,
        "peak": peak,
        "entry_iv_actual": float(state.get("entry_iv_actual", template.get("entry_iv", 0)) or 0),
        "entry_gamma_theta_efficiency_actual": float(
            state.get(
                "entry_gamma_theta_efficiency_actual",
                template.get("entry_gamma_theta_efficiency", 0),
            ) or 0
        ),
        "signal_active": bool(state.get("signal_active", True)),
        "regime_was_active": bool(state.get("regime_was_active", True)),
        "orphan_adopted": True,
        "orphan_adopted_at": adopted["orphan_adopted_at"],
        "orphan_cost_price": item.cost_price,
        "orphan_nominal_price": item.nominal_price,
    })
    store.save_campaign_states({state_key: state})
    store.audit("VOLATILITY_ORPHAN_POSITION_ADOPTED", {
        "code": item.code,
        "underlying": item.underlying,
        "quantity": item.quantity,
        "cost_price": item.cost_price,
        "nominal_price": item.nominal_price,
        "pnl_value": item.pnl_value,
        "pnl_ratio": item.pnl_ratio,
        "campaign": adopted,
    }, "volatility_long_gamma")
    return adopted


def orphan_rows_for_display(
    store,
    positions: pd.DataFrame,
    orders: pd.DataFrame,
    broker,
    campaigns: list[dict],
) -> list[dict]:
    return [item.to_dict() for item in detect_volatility_orphan_positions(
        store, positions, orders, broker, campaigns,
    )]
