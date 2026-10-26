from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from zoneinfo import ZoneInfo

from .fee_model import estimate_fees
from .data_quality import option_quote_activity
from .market_calendar import USMarketCalendar
from .quote_liveness import BookActivityTracker


OPEN_STATUSES = {"SHADOW", "SHADOW_PART"}
OPTION_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$")

# 影子撮合每个周期都看同一批合约，用「距上次盘口变化秒数」当盘口年龄
# （成交时间在期权上是成交驱动的，不能当盘口新鲜度；2026-10-26）。
_BOOK_ACTIVITY = BookActivityTracker()


def _shadow_reprice_target(
    intent: dict, quote: dict, max_slippage_pct: float, step_pct: float,
) -> float | None:
    side = str(intent.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        return None
    current = float(quote.get("ask_price" if side == "BUY" else "bid_price", 0) or 0)
    if current <= 0:
        return None
    reference = float(json.loads(intent.get("metadata_json", "{}") or "{}").get(
        "reference_price", intent.get("limit_price", current),
    ) or current)
    if side == "BUY":
        ceiling = reference * (1 + max_slippage_pct)
        target = round(min(current * (1 + step_pct), ceiling), 2)
        return target if target > float(intent.get("limit_price", 0) or 0) else None
    floor = reference * (1 - max_slippage_pct)
    target = round(max(current * (1 - step_pct), floor), 2)
    return target if target < float(intent.get("limit_price", 0) or 0) else None


def match_shadow_orders(store, broker, now: datetime | None = None) -> int:
    """Deterministically match Shadow limits against executable bid/ask quotes."""
    matched = 0
    participation = min(1.0, max(0.01, float(os.getenv("SHADOW_PARTICIPATION_RATE", "0.25"))))
    timeout = max(60, int(os.getenv("SHADOW_ORDER_TIMEOUT_SECONDS", "900")))
    max_reprice_attempts = max(0, int(os.getenv("SHADOW_MAX_REPRICE_ATTEMPTS", "3")))
    reprice_step_pct = max(0.0001, float(os.getenv("SHADOW_REPRICE_STEP_PCT", "0.0075")))
    max_slippage_pct = max(0.0001, float(os.getenv("SHADOW_MAX_SLIPPAGE_PCT", "0.12")))
    now = now or datetime.now(timezone.utc)
    for intent in store.intents(limit=5000):
        if intent["status"] not in OPEN_STATUSES:
            continue
        anchor = intent.get("last_reprice_at") or intent["created_at"]
        age = (now - datetime.fromisoformat(anchor)).total_seconds()
        if age >= timeout:
            try:
                quote = broker.snapshots([intent["symbol"]]).iloc[0].to_dict()
            except Exception:
                store.update_intent(intent["id"], "SHADOW_EXPIRED", error="虚拟订单超时未成交")
                continue
            if bool(OPTION_PATTERN.match(intent["symbol"])):
                ny_now = now.astimezone(ZoneInfo("America/New_York"))
                if not USMarketCalendar().session(ny_now).regular_open(ny_now):
                    store.update_intent(intent["id"], "SHADOW_EXPIRED", error="虚拟订单超时未成交")
                    continue
                fresh, _, _, _ = option_quote_activity(
                    quote, ny_now,
                    book_active_seconds=_BOOK_ACTIVITY.observe(intent["symbol"], quote, ny_now),
                    fresh_seconds=180, maximum_seconds=900,
                )
                if not fresh:
                    store.update_intent(intent["id"], "SHADOW_EXPIRED", error="虚拟订单超时未成交")
                    continue
            try:
                book = broker.order_book(intent["symbol"], depth=10)
                bids, asks = book.get("Bid", []), book.get("Ask", [])
                if bids:
                    quote["bid_price"], quote["bid_vol"] = float(bids[0][0]), float(bids[0][1])
                    quote["bid_depth_volume"] = sum(float(level[1]) for level in bids)
                if asks:
                    quote["ask_price"], quote["ask_vol"] = float(asks[0][0]), float(asks[0][1])
                    quote["ask_depth_volume"] = sum(float(level[1]) for level in asks)
            except Exception:
                quote["order_book_fallback"] = True
            is_option = bool(OPTION_PATTERN.match(intent["symbol"]))
            if int(intent.get("reprice_count", 0)) < max_reprice_attempts:
                target = _shadow_reprice_target(intent, quote, max_slippage_pct, reprice_step_pct)
                if target is not None:
                    store.record_reprice(intent["id"], target)
                    store.audit(
                        "SHADOW_REPRICE",
                        {
                            "intent_id": intent["id"],
                            "symbol": intent["symbol"],
                            "old_limit": float(intent["limit_price"]),
                            "new_limit": target,
                            "reprice_count": int(intent.get("reprice_count", 0)) + 1,
                        },
                        intent["strategy_id"],
                     actor="service:shadow_matching")
                    intent["limit_price"] = target
                    intent["last_reprice_at"] = now.isoformat()
                    age = 0.0
                else:
                    store.update_intent(intent["id"], "SHADOW_EXPIRED", error="虚拟订单超时未成交")
                    continue
            else:
                store.update_intent(intent["id"], "SHADOW_EXPIRED", error="虚拟订单超时未成交")
                continue
        else:
            try:
                quote = broker.snapshots([intent["symbol"]]).iloc[0].to_dict()
            except Exception:
                continue
            is_option = bool(OPTION_PATTERN.match(intent["symbol"]))
            if is_option:
                ny_now = now.astimezone(ZoneInfo("America/New_York"))
                if not USMarketCalendar().session(ny_now).regular_open(ny_now):
                    continue
                fresh, _, _, _ = option_quote_activity(
                    quote, ny_now,
                    book_active_seconds=_BOOK_ACTIVITY.observe(intent["symbol"], quote, ny_now),
                    fresh_seconds=180, maximum_seconds=900,
                )
                if not fresh:
                    continue
        try:
            book = broker.order_book(intent["symbol"], depth=10)
            bids, asks = book.get("Bid", []), book.get("Ask", [])
            if bids:
                quote["bid_price"], quote["bid_vol"] = float(bids[0][0]), float(bids[0][1])
                quote["bid_depth_volume"] = sum(float(level[1]) for level in bids)
            if asks:
                quote["ask_price"], quote["ask_vol"] = float(asks[0][0]), float(asks[0][1])
                quote["ask_depth_volume"] = sum(float(level[1]) for level in asks)
        except Exception:
            quote["order_book_fallback"] = True
        side = intent["side"]
        try:
            metadata = json.loads(intent.get("metadata_json", "{}") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        urgent_exit = side == "SELL" and bool(metadata.get("urgent"))
        executable = float(quote.get("ask_price" if side == "BUY" else "bid_price", 0) or 0)
        volume = float(quote.get("ask_vol" if side == "BUY" else "bid_vol", 0) or 0)
        limit = float(intent["limit_price"])
        touches = executable > 0 and (
            urgent_exit or (executable <= limit if side == "BUY" else executable >= limit)
        )
        bid, ask = float(quote.get("bid_price", 0) or 0), float(quote.get("ask_price", 0) or 0)
        spread = (ask - bid) / ((ask + bid) / 2) if bid > 0 and ask >= bid else 1.0
        ny = now.astimezone(ZoneInfo("America/New_York"))
        regular = (ny.hour > 9 or (ny.hour == 9 and ny.minute >= 30)) and ny.hour < 16
        session_factor = 1.0 if regular else float(os.getenv("SHADOW_EXTENDED_HOURS_LIQUIDITY", "0.35"))
        option_factor = 1.0
        if is_option:
            open_interest = float(quote.get("option_open_interest", 0) or 0)
            option_factor = min(1.0, max(0.1, open_interest / 500))
        penetration = abs(limit - executable) / max(executable, 0.01)
        fill_probability = min(1.0, (0.30 + penetration * 100) * session_factor * option_factor /
                               max(1.0, spread / 0.01))
        draw = int(hashlib.sha256(f"{intent['id']}:{now:%Y%m%d%H%M}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        crosses = touches and (urgent_exit or penetration >= 0.001 or draw <= fill_probability)
        remaining = float(intent["quantity"]) - float(intent.get("filled_quantity", 0) or 0)
        if not crosses or remaining <= 0:
            continue
        available = max(1.0, volume * participation * session_factor * option_factor) if volume > 0 else remaining
        fill_qty = min(remaining, available)
        previous_qty = float(intent.get("filled_quantity", 0) or 0)
        previous_avg = float(intent.get("average_fill_price", 0) or 0)
        total_qty = previous_qty + fill_qty
        average = (previous_avg * previous_qty + executable * fill_qty) / total_qty
        fill_fee = estimate_fees(intent["symbol"], side, fill_qty, executable)
        fees = float(intent.get("fees", 0) or 0) + fill_fee
        status = "SHADOW_FILLED" if total_qty >= float(intent["quantity"]) else "SHADOW_PART"
        store.update_shadow_intent(intent["id"], status, total_qty, average, fees)
        quote.update({"model_spread": spread, "model_probability": fill_probability,
                      "model_draw": draw, "model_session": "RTH" if regular else "EXTENDED"})
        store.record_shadow_fill(intent["id"], fill_qty, executable, fill_fee, quote)
        store.audit("SHADOW_FILL", {"intent_id": intent["id"], "quantity": fill_qty,
                                    "price": executable, "status": status}, intent["strategy_id"], actor="service:shadow_matching")
        matched += 1
    return matched
