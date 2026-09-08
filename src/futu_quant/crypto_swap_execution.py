from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import time

from .crypto_swap import CryptoSwapDecision, decide_crypto_swap
from .crypto_swap_managed_futures import (
    decide_crypto_swap_managed_futures,
    managed_futures_config_from_env,
)
from .crypto_swap_models import CryptoSwapOrderRequest
from .crypto_swap_risk import (
    crypto_swap_risk_config_from_env,
    swap_entry_block_reason,
)
from .crypto_swap_watchlist import crypto_swap_radar_config_from_env
from .okx_broker import OkxApiError


@dataclass(frozen=True)
class CryptoSwapExecutionResult:
    symbol: str
    mode: str
    decision: CryptoSwapDecision
    order: dict | None = None


def _client_id(symbol: str, timestamp: int, action: str) -> str:
    digest = hashlib.sha256(f"{symbol}|{timestamp}|{action}".encode()).hexdigest()[:12]
    return f"cs{timestamp:x}{digest}"[:32]


def _held_hours(store, symbol: str) -> float:
    if not store:
        return 0.0
    opened_at = store.get_setting(f"crypto_swap_opened_at:{symbol}", "")
    if not opened_at:
        return 0.0
    try:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds() / 3600
    except ValueError:
        return 0.0


def _liquidation_buffer(platform_rows: list[dict], last_price: float) -> float | None:
    buffers = []
    for row in platform_rows:
        liquidation = float(row.get("liqPx", 0) or 0)
        mark = float(row.get("markPx", 0) or 0) or last_price
        if liquidation > 0 and mark > 0:
            buffers.append(abs(mark - liquidation) / mark)
    return min(buffers) if buffers else None


def execute_crypto_swap_cycle(broker, symbol, config, mode="SHADOW", store=None):
    mode = str(mode).upper()
    if mode not in {"SHADOW", "OKX_DEMO"}:
        raise ValueError("合约执行模式只允许 SHADOW 或 OKX_DEMO")
    strategy_id = "crypto_volatility_swap"
    platform_rows = []
    allocation = next((
        row for row in (store.virtual_allocations() if store else [])
        if row["strategy_id"] == strategy_id and row["symbol"] == symbol
    ), None)
    contracts = float((allocation or {}).get("quantity", 0) or 0)
    average_price = float((allocation or {}).get("average_cost", 0) or 0)
    if mode == "OKX_DEMO":
        platform_rows = [
            row for row in broker.positions("SWAP")
            if row.get("instId") == symbol and float(row.get("pos", 0) or 0) != 0
        ]
        contracts = sum(float(row.get("pos", 0) or 0) for row in platform_rows)
        average_price = (
            sum(
                abs(float(row.get("pos", 0) or 0)) * float(row.get("avgPx", 0) or 0)
                for row in platform_rows
            ) / sum(abs(float(row.get("pos", 0) or 0)) for row in platform_rows)
            if platform_rows else 0.0
        )
        if store is not None:
            if contracts:
                store.set_virtual_allocation(
                    strategy_id, symbol, contracts, average_price,
                )
            else:
                store.delete_virtual_allocation(strategy_id, symbol)
    bars = broker.candles(symbol, limit=300)
    higher_timeframe_bars = (
        broker.candles(
            symbol, bar=config.higher_timeframe_bar,
            limit=min(300, max(config.higher_timeframe_min_bars + 5, 100)),
        )
        if config.higher_timeframe_enabled else None
    )
    last = float(bars.iloc[-1]["close"])
    liquidation_buffer = _liquidation_buffer(platform_rows, last)
    peak_key = f"crypto_swap_peak_pnl:{symbol}"
    stage_key = f"crypto_swap_partial_stage:{symbol}"
    recovery_layer_key = f"crypto_swap_recovery_layer:{symbol}"
    contraction_key = f"crypto_swap_contraction_count:{symbol}"
    previous_peak = float(store.get_setting(peak_key, "0") or 0) if store else 0
    partial_stage = int(store.get_setting(stage_key, "0") or 0) if store else 0
    recovery_layer = int(store.get_setting(recovery_layer_key, "0") or 0) if store else 0
    contraction_count = int(store.get_setting(contraction_key, "0") or 0) if store else 0
    funding_row = broker.funding_rate(symbol) if hasattr(broker, "funding_rate") else {}
    funding_rate = float(funding_row.get("fundingRate", 0) or 0)
    current_pnl = (
        last / average_price - 1 if contracts > 0
        else average_price / last - 1 if contracts < 0 and average_price > 0 else 0
    )
    peak_pnl = max(previous_peak, current_pnl)
    if store and contracts:
        store.set_setting(peak_key, str(peak_pnl))
    mf_config = managed_futures_config_from_env()
    if mf_config.engine == "MANAGED_FUTURES":
        daily_bars = broker.candles(symbol, bar="1D", limit=220)
        benchmark_bars = (
            broker.candles(mf_config.benchmark_symbol, limit=300)
            if symbol != mf_config.benchmark_symbol else bars
        )
        decision = decide_crypto_swap_managed_futures(
            symbol, bars, contracts, average_price, config, mf_config,
            peak_pnl=peak_pnl, partial_stage=partial_stage,
            held_hours=_held_hours(store, symbol),
            recovery_layer=recovery_layer,
            funding_rate=funding_rate,
            liquidation_buffer=liquidation_buffer,
            higher_timeframe_bars=higher_timeframe_bars,
            daily_bars=daily_bars,
            benchmark_bars=benchmark_bars,
        )
    else:
        decision = decide_crypto_swap(
            bars, contracts, average_price, config, peak_pnl=peak_pnl,
            partial_stage=partial_stage, held_hours=_held_hours(store, symbol),
            contraction_count=contraction_count, funding_rate=funding_rate,
            liquidation_buffer=liquidation_buffer,
            higher_timeframe_bars=higher_timeframe_bars,
        )
    if store and contracts:
        contracted = float(decision.metrics.get("volatility_expansion", 99)) <= config.contraction_ratio
        store.set_setting(contraction_key, str(contraction_count + 1 if contracted else 0))
    if decision.action in {"OPEN_LONG", "OPEN_SHORT"}:
        radar_risk = crypto_swap_radar_config_from_env()
        funding = funding_rate
        quote = broker.ticker(symbol)
        bid_check = float(quote.get("bidPx", 0) or 0)
        ask_check = float(quote.get("askPx", 0) or 0)
        mid_check = (bid_check + ask_check) / 2 if bid_check > 0 and ask_check > 0 else 0
        spread = (
            (ask_check - bid_check) / mid_check
            if mid_check > 0 and ask_check >= bid_check else 1.0
        )
        if abs(funding) > radar_risk.max_abs_funding_rate:
            decision = CryptoSwapDecision(
                "HOLD",
                f"资金费率风险保护：当前 {funding:.4%}，"
                f"绝对值上限 {radar_risk.max_abs_funding_rate:.4%}",
                metrics={**decision.metrics, "funding_rate": funding},
                checks=decision.checks,
            )
        elif spread > radar_risk.max_spread_ratio:
            decision = CryptoSwapDecision(
                "HOLD",
                f"盘口价差风险保护：当前 {spread:.3%}，"
                f"上限 {radar_risk.max_spread_ratio:.3%}",
                metrics={**decision.metrics, "spread_ratio": spread},
                checks=decision.checks,
            )
    if decision.action in {"OPEN_LONG", "OPEN_SHORT"} and store is not None:
        planned_margin = (
            float(decision.target_notional) / float(config.leverage)
            if config.leverage > 0 and decision.target_notional > 0
            else float(config.margin_budget)
        )
        blocked = swap_entry_block_reason(
            store, symbol, planned_margin, config.leverage,
            crypto_swap_risk_config_from_env(),
        )
        if blocked:
            decision = CryptoSwapDecision(
                "HOLD", f"合约风险保护阻止开仓：{blocked}",
                metrics=decision.metrics, checks=decision.checks,
            )
    if decision.action == "HOLD":
        return CryptoSwapExecutionResult(symbol, mode, decision)
    if mode == "OKX_DEMO":
        pending = [
            row for row in broker.pending_orders("SWAP")
            if row.get("instId") == symbol
            and str(row.get("state", "")).lower() in {"live", "partially_filled"}
        ]
        if pending:
            return CryptoSwapExecutionResult(
                symbol, mode,
                CryptoSwapDecision(
                    "HOLD", f"已有 {len(pending)} 笔合约待成交订单，禁止重复提交",
                    metrics=decision.metrics, checks=decision.checks,
                ),
            )
    ticker = broker.ticker(symbol)
    ask = Decimal(str(ticker.get("askPx") or ticker.get("last") or "0"))
    bid = Decimal(str(ticker.get("bidPx") or ticker.get("last") or "0"))
    if ask <= 0 or bid <= 0:
        raise OkxApiError("欧易永续合约盘口不可用")
    instrument = next((item for item in broker.swap_instruments() if item.inst_id == symbol), None)
    if instrument is None:
        raise OkxApiError(f"欧易永续合约目录中找不到 {symbol}")
    closing = decision.action.startswith("CLOSE_")
    price = instrument.normalize_price(
        bid if decision.action in {"OPEN_SHORT", "CLOSE_LONG"} else ask
    )
    if closing:
        order_contracts = instrument.normalize_contracts(
            Decimal(str(abs(contracts) * float(decision.quantity_fraction)))
        )
    else:
        raw = Decimal(str(decision.target_notional)) / (price * instrument.contract_value)
        order_contracts = instrument.normalize_contracts(raw)
    if order_contracts < instrument.min_size:
        raise OkxApiError(f"计划张数 {order_contracts} 小于最小张数 {instrument.min_size}")
    side = "buy" if decision.action in {"OPEN_LONG", "CLOSE_SHORT"} else "sell"
    signed = order_contracts if decision.action == "OPEN_LONG" else -order_contracts
    if mode == "SHADOW":
        if store is None:
            return CryptoSwapExecutionResult(symbol, mode, decision)
        if closing:
            if float(decision.quantity_fraction) < 1.0 and abs(contracts) > float(order_contracts):
                remaining = abs(contracts) - float(order_contracts)
                signed_remaining = remaining if contracts > 0 else -remaining
                store.set_virtual_allocation(strategy_id, symbol, signed_remaining, average_price)
                store.set_setting(stage_key, str(partial_stage + 1))
            else:
                store.delete_virtual_allocation(strategy_id, symbol)
                store.set_setting(peak_key, "0")
                store.set_setting(stage_key, "0")
                store.set_setting(recovery_layer_key, "0")
                store.set_setting(contraction_key, "0")
                store.set_setting(f"crypto_swap_margin:{symbol}", "0")
        else:
            existing_same_direction = (
                contracts
                and ((contracts > 0 and signed > 0) or (contracts < 0 and signed < 0))
            )
            if existing_same_direction:
                new_quantity = float(contracts) + float(signed)
                new_average = (
                    abs(float(contracts)) * float(average_price)
                    + abs(float(signed)) * float(price)
                ) / abs(new_quantity)
                store.set_virtual_allocation(
                    strategy_id, symbol, new_quantity, new_average,
                )
            else:
                store.set_virtual_allocation(strategy_id, symbol, float(signed), float(price))
            store.set_setting(f"crypto_swap_opened_at:{symbol}", datetime.now(timezone.utc).isoformat())
            store.set_setting(stage_key, "0")
            if decision.metrics.get("next_recovery_layer") is not None:
                store.set_setting(
                    recovery_layer_key, str(int(decision.metrics["next_recovery_layer"]))
                )
            else:
                store.set_setting(recovery_layer_key, "0")
            previous_margin = float(store.get_setting(f"crypto_swap_margin:{symbol}", "0") or 0)
            added_margin = (
                float(decision.target_notional) / float(config.leverage)
                if config.leverage > 0 and decision.target_notional > 0
                else float(config.margin_budget)
            )
            store.set_setting(
                f"crypto_swap_margin:{symbol}", str(previous_margin + added_margin),
            )
        return CryptoSwapExecutionResult(symbol, mode, decision, {
            "status": "SHADOW_FILLED", "side": side,
            "contracts": float(order_contracts), "price": float(price),
            "reduce_only": closing,
            "notional": float(order_contracts * price * instrument.contract_value),
            "lifecycle_stage": "reducing" if closing and decision.quantity_fraction < 1 else "closing" if closing else "opening",
        })
    timestamp = int(ticker.get("ts") or time.time() * 1000)
    request = CryptoSwapOrderRequest(
        symbol, side, order_contracts, price, closing, decision.action,
        client_order_id=_client_id(symbol, timestamp, decision.action),
    )
    order = broker.place_swap_order(request)
    order["lifecycle_stage"] = "reducing" if closing and decision.quantity_fraction < 1 else "closing" if closing else "opening"
    return CryptoSwapExecutionResult(symbol, mode, decision, order)
