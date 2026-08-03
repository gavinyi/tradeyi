from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import time

from .crypto_models import CryptoOrderRequest
from .crypto_protections import (
    crypto_protection_config_from_env,
    entry_protection_reason,
    record_crypto_trade_outcome,
    start_cooldown,
)
from .crypto_volatility import CryptoVolatilityConfig, CryptoVolatilityDecision, decide_crypto_volatility
from .okx_broker import OkxApiError, OkxDemoBroker


@dataclass(frozen=True)
class CryptoExecutionResult:
    symbol: str
    mode: str
    decision: CryptoVolatilityDecision
    order: dict | None = None


def client_order_id(symbol: str, signal_timestamp: int, action: str) -> str:
    digest = hashlib.sha256(f"{symbol}|{signal_timestamp}|{action}".encode()).hexdigest()[:12]
    return f"cq{signal_timestamp:x}{digest}"[:32]


def lifecycle_stage(action: str, order: dict | None = None) -> str:
    if action == "BUY":
        return "opening"
    if action == "SELL_PARTIAL":
        return "reducing"
    if action == "SELL_ALL":
        return "closing"
    if order:
        return "submitted"
    return "monitoring"


def execute_crypto_cycle(
    broker: OkxDemoBroker,
    symbol: str,
    config: CryptoVolatilityConfig,
    mode: str = "SHADOW",
    store=None,
) -> CryptoExecutionResult:
    mode = mode.upper()
    if mode not in {"SHADOW", "OKX_DEMO"}:
        raise ValueError("币策略执行模式只允许 SHADOW 或 OKX_DEMO")
    bars = broker.candles(symbol, limit=300)
    higher_timeframe_bars = (
        broker.candles(
            symbol, bar=config.higher_timeframe_bar,
            limit=min(300, max(config.higher_timeframe_min_bars + 5, 100)),
        )
        if config.higher_timeframe_enabled else None
    )
    strategy_id = "crypto_volatility_spot"
    allocation = None
    if store is not None:
        allocation = next(
            (
                row for row in store.virtual_allocations()
                if row["strategy_id"] == strategy_id and row["symbol"] == symbol
            ),
            None,
        )
    quantity = Decimal(str((allocation or {}).get("quantity", 0) or 0))
    average_cost = Decimal(str((allocation or {}).get("average_cost", 0) or 0))
    last_close = Decimal(str(bars.iloc[-1]["close"]))
    peak_key = f"crypto_peak_price:{symbol}"
    stage_key = f"crypto_partial_stage:{symbol}"
    open_key = f"crypto_opened_at:{symbol}"
    contraction_key = f"crypto_contraction_count:{symbol}"
    peak_price = last_close
    partial_stage, held_hours, contraction_count = 0, 0.0, 0
    if store is not None and hasattr(store, "get_setting"):
        peak_price = Decimal(str(store.get_setting(peak_key, str(last_close)) or last_close))
        peak_price = max(peak_price, last_close)
        store.set_setting(peak_key, str(peak_price))
        partial_stage = int(store.get_setting(stage_key, "0") or 0)
        contraction_count = int(store.get_setting(contraction_key, "0") or 0)
        opened_at = store.get_setting(open_key, "")
        if opened_at:
            try:
                opened = datetime.fromisoformat(opened_at)
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                held_hours = (
                    datetime.now(timezone.utc) - opened.astimezone(timezone.utc)
                ).total_seconds() / 3600
            except ValueError:
                held_hours = 0
    decision = decide_crypto_volatility(
        bars, position_size=quantity,
        average_price=average_cost if average_cost > 0 else None,
        peak_price=peak_price, partial_stage=partial_stage,
        held_hours=held_hours, contraction_count=contraction_count, config=config,
        higher_timeframe_bars=higher_timeframe_bars,
    )
    if store is not None and hasattr(store, "set_setting") and quantity > 0:
        contracted = (
            float(decision.metrics.get("volatility_expansion", 99))
            <= config.contraction_ratio
        )
        store.set_setting(
            contraction_key, str(contraction_count + 1 if contracted else 0),
        )
    if decision.action == "BUY" and store is not None and hasattr(store, "get_setting"):
        blocked = entry_protection_reason(
            store, symbol, decision.target_notional,
            crypto_protection_config_from_env(),
        )
        if blocked:
            decision = CryptoVolatilityDecision(
                "HOLD", f"组合保护阻止开仓：{blocked}",
                metrics=decision.metrics, checks=decision.checks,
            )
    if decision.action not in {"BUY", "SELL_ALL", "SELL_PARTIAL"}:
        return CryptoExecutionResult(symbol, mode, decision)
    if mode == "OKX_DEMO":
        pending = [
            item for item in broker.pending_orders()
            if item.get("instId") == symbol
            and str(item.get("state", "")).lower() in {"live", "partially_filled"}
        ]
        if pending:
            return CryptoExecutionResult(
                symbol, mode,
                CryptoVolatilityDecision(
                    "HOLD", f"已有 {len(pending)} 笔待成交订单，禁止重复提交",
                    metrics=decision.metrics, checks=decision.checks,
                ),
            )
    ticker = broker.ticker(symbol)
    ask = Decimal(str(ticker.get("askPx") or ticker.get("last") or "0"))
    bid = Decimal(str(ticker.get("bidPx") or ticker.get("last") or "0"))
    timestamp = int(ticker.get("ts") or time.time() * 1000)
    if ask <= 0 or bid <= 0:
        raise OkxApiError("欧易盘口不可用：买一或卖一价格为 0")
    spread = (ask - bid) / ((ask + bid) / 2)
    if spread > Decimal("0.005"):
        raise OkxApiError(f"欧易盘口点差过宽：{spread:.2%} > 0.50%")
    notional = min(decision.target_notional, broker.settings.max_order_notional)
    if decision.action == "SELL_ALL":
        size = quantity
    elif decision.action == "SELL_PARTIAL":
        size = quantity * Decimal(str(decision.quantity_fraction))
    else:
        size = Decimal(str(notional)) / ask
    instrument = next(
        (item for item in broker.instruments() if item.inst_id == symbol), None
    )
    if instrument is None:
        raise OkxApiError(f"欧易现货目录中找不到 {symbol}")
    price = instrument.normalize_price(bid if decision.action == "SELL_ALL" else ask)
    size = instrument.normalize_size(size)
    if size < instrument.min_size:
        raise OkxApiError(
            f"计划数量 {size} 小于欧易最小下单量 {instrument.min_size}"
        )
    side = "sell" if decision.action in {"SELL_ALL", "SELL_PARTIAL"} else "buy"
    if mode == "SHADOW":
        if store is None:
            return CryptoExecutionResult(symbol, mode, decision)
        if side == "buy":
            store.set_virtual_allocation(strategy_id, symbol, float(size), float(price))
            record_crypto_trade_outcome(store, symbol, None)
            if hasattr(store, "set_setting"):
                store.set_setting(open_key, datetime.now(timezone.utc).isoformat())
                store.set_setting(stage_key, "0")
        else:
            pnl_ratio = (
                float(price) / float(average_cost) - 1
                if average_cost > 0 else None
            )
            record_crypto_trade_outcome(store, symbol, pnl_ratio)
            remaining = max(Decimal("0"), quantity - size)
            if remaining > 0 and decision.action == "SELL_PARTIAL":
                store.set_virtual_allocation(
                    strategy_id, symbol, float(remaining), float(average_cost),
                )
                if hasattr(store, "set_setting"):
                    store.set_setting(stage_key, str(partial_stage + 1))
            else:
                store.delete_virtual_allocation(strategy_id, symbol)
            if hasattr(store, "set_setting"):
                if decision.action == "SELL_ALL":
                    store.set_setting(peak_key, "0")
                    store.set_setting(open_key, "")
                    store.set_setting(stage_key, "0")
                    start_cooldown(
                        store, symbol,
                        crypto_protection_config_from_env().cooldown_hours,
                        decision.reason,
                    )
        return CryptoExecutionResult(
            symbol, mode, decision,
            {
                "status": "SHADOW_FILLED", "side": side, "price": float(price),
                "quantity": float(size), "notional": float(price * size),
                "lifecycle_stage": lifecycle_stage(decision.action),
            },
        )
    request = CryptoOrderRequest(
        inst_id=symbol, side=side, order_type="limit", size=size, price=price,
        client_order_id=client_order_id(symbol, timestamp, decision.action),
    )
    order = broker.place_spot_order(request)
    order["lifecycle_stage"] = lifecycle_stage(decision.action, order)
    return CryptoExecutionResult(symbol, mode, decision, order)
