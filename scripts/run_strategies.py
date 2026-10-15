from __future__ import annotations

import argparse
import time

import pandas as pd

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.core_satellite import (
    CORE_MAX_WEIGHTS, STRATEGY_NAME, build_rebalance_plan, rebalance_due,
)
from futu_quant.core_validation import (
    record_core_validation_session,
    resolve_core_rebalance_settings,
)
from futu_quant.execution import ExecutionManager
from futu_quant.strategies import STRATEGIES, Signal


def _real_sellable_quantity(broker: FutuPaperBroker, symbol: str) -> tuple[int, str]:
    """Return broker-reported sellable quantity after active sell orders.

    Core Satellite can run in a long-lived service loop.  Its initial position
    snapshot may become stale after fills or manual broker actions, so SELL
    orders are checked again immediately before submission.
    """
    positions = broker.positions(refresh=True)
    orders = broker.orders()
    if positions.empty or "code" not in positions.columns:
        return 0, "富途未返回持仓，无法确认可卖数量"
    rows = positions[positions["code"].astype(str) == symbol]
    if rows.empty:
        return 0, "富途当前没有该标的持仓"
    row = rows.iloc[0]
    raw_sellable = row.get("can_sell_qty", row.get("sellable_qty", row.get("qty", 0)))
    try:
        sellable = float(raw_sellable or 0)
    except (TypeError, ValueError):
        sellable = 0.0
    pending_sell = 0.0
    if not orders.empty and {"code", "trd_side", "qty", "order_status"}.issubset(orders.columns):
        active = orders[orders["order_status"].map(broker.is_cancellable_status)]
        side_text = active["trd_side"].astype(str).str.upper()
        symbol_orders = active[
            (active["code"].astype(str) == symbol) & side_text.str.contains("SELL")
        ]
        pending_sell = float(
            pd.to_numeric(symbol_orders["qty"], errors="coerce").fillna(0).sum()
        ) if not symbol_orders.empty else 0.0
    available = max(0, int(sellable - pending_sell))
    return available, f"富途可卖 {sellable:g}，待成交卖单 {pending_sell:g}，提交前可用 {available:g}"


def run_once(
    broker: FutuPaperBroker, strategy_name: str, executor: ExecutionManager | None = None,
    execution_strategy_id: str | None = None,
) -> None:
    settings = broker.settings
    executor = executor or ExecutionManager(broker)
    executor.reconcile()
    positions = broker.positions()
    if strategy_name == STRATEGY_NAME:
        effective = resolve_core_rebalance_settings(settings, executor.store)
        info = broker.account_info()
        if info.empty:
            raise RuntimeError("Account information is empty")
        account_value = float(info.iloc[0]["total_assets"])
        core_budget = executor.store.strategy_risk_budget("core_satellite")
        core_capital_ratio = (
            float(core_budget["capital_ratio"])
            if core_budget and core_budget.get("enabled") else 0.0
        )
        if core_budget and not core_budget.get("enabled"):
            executor.store.decision(
                "core_satellite", None, "BLOCKED", "组合资金分配器已停用 Core Satellite",
            )
            return
        effective_stock_cap = min(
            settings.core_satellite_max_stock_weight,
            core_capital_ratio if core_budget else settings.core_satellite_max_stock_weight,
        )
        symbols = tuple(dict.fromkeys(settings.core_satellite_symbols))
        required_list = list(dict.fromkeys(("US.SPY", *symbols)))
        if settings.core_satellite_cash_yield_enabled:
            required_list.append(settings.core_satellite_cash_yield_symbol)
        required = tuple(dict.fromkeys(required_list))
        frames = {symbol: broker.candles(symbol, count=260) for symbol in required}
        managed_positions = (
            positions[positions["code"].isin(symbols) & (positions["qty"] != 0)]
            if not positions.empty else positions
        )
        held_symbols = set(managed_positions["code"].astype(str)) if not managed_positions.empty else set()
        # A negative stock position is an external/short exposure, not a
        # missing bootstrap position. Do not repeatedly submit a new BUY while
        # the broker/local ownership of that short is unresolved.
        negative_symbols = {
            str(row["code"])
            for _, row in managed_positions.iterrows()
            if float(row.get("qty", 0) or 0) < 0
        }
        missing_symbols = set(symbols) - held_symbols
        if negative_symbols:
            blocked = ",".join(sorted(negative_symbols))
            reason = f"检测到负仓位 {blocked}，禁止自动建仓，等待券商/本地账本对账"
            print(f"Core Satellite blocked: {reason}")
            executor.store.decision(
                "core_satellite", None, "BLOCKED", reason,
                {"negative_symbols": sorted(negative_symbols)},
            )
            return
        if missing_symbols:
            today_orders = broker.orders()
            pending_managed = (
                today_orders[
                    today_orders["code"].isin(missing_symbols)
                    & today_orders["order_status"].map(broker.is_cancellable_status)
                ]
                if not today_orders.empty else today_orders
            )
            if pending_managed.empty:
                due, schedule_reason = True, (
                    "initial portfolio bootstrap: " + ",".join(sorted(missing_symbols))
                )
            else:
                print("Core Satellite skipped: initial orders are still pending")
                executor.store.decision(
                    "core_satellite", None, "HOLD", "初始订单仍待成交"
                )
                return
        else:
            due, schedule_reason = rebalance_due(
                frames["US.SPY"], effective.frequency
            )
        satellite_only = False
        if not due:
            if (
                settings.core_satellite_satellite_daily_scan
                and effective.frequency != "daily"
            ):
                due = True
                satellite_only = True
                schedule_reason = (
                    f"Satellite 每日机会扫描；正式 Core 调仓未到期：{schedule_reason}"
                )
            else:
                print(f"Core Satellite skipped: not due ({schedule_reason})")
                executor.store.decision(
                    "core_satellite", None, "NOT_DUE", schedule_reason
                )
                return
        print(f"Rebalance trigger: {schedule_reason}")
        if effective.validation_mode:
            print(
                "Core Satellite daily validation: "
                f"session={effective.validation_session}, "
                f"tolerance={effective.weight_tolerance:.2%}, "
                f"minimum=${effective.min_trade_usd:,.0f}"
            )
        orders, risk_on = build_rebalance_plan(
            frames, positions, account_value, symbols,
            effective_stock_cap,
            effective.min_trade_usd,
            settings.core_satellite_profile,
            effective.weight_tolerance,
            settings.core_satellite_cash_buffer,
            settings.core_satellite_max_single_rebalance_pct,
            settings.core_satellite_high_volatility_threshold,
            settings.core_satellite_high_volatility_trade_factor,
            settings.core_satellite_max_order_notional,
            settings.core_satellite_core_budget,
            settings.core_satellite_satellite_budget,
            settings.core_satellite_satellite_min_score,
            satellite_only,
            settings.core_satellite_max_target_change,
            settings.core_satellite_cash_yield_enabled,
            settings.core_satellite_cash_yield_symbol,
        )
        signal_date = str(frames["US.SPY"].iloc[-1]["time_key"])[:10]
        validation_payload = {
            "validation_mode": effective.validation_mode,
            "validation_session": effective.validation_session,
            "formal_frequency": effective.formal_frequency,
            "effective_frequency": effective.frequency,
            "effective_weight_tolerance": effective.weight_tolerance,
            "effective_min_trade_usd": effective.min_trade_usd,
            "core_capital_ratio": core_capital_ratio if core_budget else None,
            "effective_stock_cap": effective_stock_cap,
            "cash_buffer": settings.core_satellite_cash_buffer,
            "core_budget": settings.core_satellite_core_budget,
            "satellite_budget": settings.core_satellite_satellite_budget,
            "max_single_rebalance_pct": settings.core_satellite_max_single_rebalance_pct,
            "max_order_notional": settings.core_satellite_max_order_notional,
            "high_volatility_threshold": settings.core_satellite_high_volatility_threshold,
            "high_volatility_trade_factor": settings.core_satellite_high_volatility_trade_factor,
            "satellite_daily_scan": settings.core_satellite_satellite_daily_scan,
            "satellite_min_score": settings.core_satellite_satellite_min_score,
            "satellite_only": satellite_only,
            "max_target_change": settings.core_satellite_max_target_change,
        }
        if effective.validation_mode:
            record_core_validation_session(
                executor.store, signal_date, order_count=len(orders),
            )
        print(f"{STRATEGY_NAME}: {'RISK-ON' if risk_on else 'RISK-OFF'}")
        if not orders:
            print("No rebalance order reached the minimum trade value")
            executor.store.decision(
                "core_satellite", None, "HOLD",
                "Satellite 每日扫描未发现可交易机会" if satellite_only
                else "没有达到最低交易金额的调仓指令",
                {"risk_on": risk_on, **validation_payload},
            )
        planned_reduction_delta = sum(
            float(order.order_notional) for order in orders if order.side == "SELL"
        )
        for order in orders:
            print(
                f"{order.symbol} {order.side} @ {order.price:.2f} qty={order.quantity} "
                f"({order.current_weight:.2%} -> {order.target_weight:.2%}; "
                f"{order.reason}; {order.throttle_reason}; "
                f"batch {order.execution_slice}/{order.total_slices})"
            )
            if settings.enable_paper_orders:
                actual_quantity = int(order.quantity)
                sellable_reason = ""
                if order.side == "SELL":
                    actual_quantity, sellable_reason = _real_sellable_quantity(
                        broker, order.symbol,
                    )
                    actual_quantity = min(int(order.quantity), actual_quantity)
                    if actual_quantity <= 0:
                        reason = f"提交前跳过卖出：{sellable_reason}"
                        print(f"{order.symbol} SELL skipped ({reason})")
                        executor.store.decision(
                            "core_satellite", order.symbol, "HOLD", reason,
                            {
                                "planned_quantity": order.quantity,
                                "pre_submit_sellable_quantity": actual_quantity,
                                **validation_payload,
                            },
                        )
                        continue
                intent_mode = (
                    "daily-validation" if effective.validation_mode
                    else ("satellite-daily" if satellite_only else "formal")
                )
                print(
                    executor.submit_limit(
                        "core_satellite",
                        f"{signal_date}:{intent_mode}:{settings.core_satellite_profile}:"
                        f"{order.symbol}:{order.side}:slice{order.execution_slice}",
                        order.symbol, order.side, actual_quantity, order.price,
                        {
                            "profile": settings.core_satellite_profile,
                            "reason": order.reason,
                            "trade_reason": order.reason,
                            "throttle_reason": order.throttle_reason,
                            "desired_quantity": order.desired_quantity,
                            "desired_notional": order.desired_notional,
                            "planned_quantity": order.quantity,
                            "submitted_quantity": actual_quantity,
                            "pre_submit_sellable_reason": sellable_reason,
                            "planned_notional": order.order_notional,
                            "execution_slice": order.execution_slice,
                            "total_slices": order.total_slices,
                            "rebalance_batch": True,
                            "rebalance_reduction_delta": planned_reduction_delta,
                            "current_weight": order.current_weight,
                            "target_weight": order.target_weight,
                            "portfolio_bucket": (
                                "CORE" if order.symbol in CORE_MAX_WEIGHTS else "SATELLITE"
                            ),
                            "bucket_budget": (
                                settings.core_satellite_core_budget
                                if order.symbol in CORE_MAX_WEIGHTS else
                                settings.core_satellite_satellite_budget
                            ),
                            "cash_buffer": settings.core_satellite_cash_buffer,
                            **validation_payload,
                        },
                    )
                )
        if orders and not settings.enable_paper_orders:
            print("Observation only: ENABLE_PAPER_ORDERS=false; no orders submitted")
        return
    held = set(positions.loc[positions["qty"] > 0, "code"]) if not positions.empty else set()
    strategy_id = execution_strategy_id or strategy_name
    for symbol in settings.watchlist:
        order_quantity = settings.quantity_for(symbol)
        candles = broker.candles(symbol)
        result = STRATEGIES[strategy_name](candles)
        signal_date = str(candles.iloc[-1]["time_key"])[:10]
        print(
            f"{symbol} {result.strategy}: {result.signal.value} @ {result.price:.2f} "
            f"qty={order_quantity} ({result.reason})"
        )
        executor.store.decision(
            strategy_id, symbol, result.signal.value, result.reason,
            {"price": result.price, "quantity": order_quantity},
        )
        if result.signal == Signal.BUY and symbol not in held:
            print(executor.submit_limit(
                strategy_id, f"{signal_date}:{symbol}:BUY", symbol, "BUY",
                order_quantity, result.price, {"reason": result.reason},
            ))
        elif result.signal == Signal.SELL and symbol in held:
            owned = int(positions.loc[positions["code"] == symbol, "qty"].iloc[0])
            print(executor.submit_limit(
                strategy_id, f"{signal_date}:{symbol}:SELL", symbol, "SELL",
                min(order_quantity, owned), result.price, {"reason": result.reason},
            ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Futu US paper strategy")
    parser.add_argument("--strategy", choices=(*STRATEGIES, STRATEGY_NAME), default="SMA Cross")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    broker = FutuPaperBroker(Settings.load())
    executor = ExecutionManager(broker)
    while True:
        run_once(broker, args.strategy, executor)
        if not args.loop:
            break
        time.sleep(broker.settings.interval_seconds)


if __name__ == "__main__":
    main()
