from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .strategies import STRATEGIES, Signal
from .core_satellite import build_rebalance_plan
from .backtest_metrics import calculate_backtest_metrics


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, float]


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, initial_cash: float) -> dict[str, float]:
    return calculate_backtest_metrics(equity, trades, initial_cash)


def run_core_satellite_backtest(
    frames: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    initial_cash: float,
    max_stock_weight: float = 0.70,
    min_trade_usd: float = 500.0,
    commission: float = 0.0,
    slippage_bps: float = 0.0,
    profile: str = "conservative",
    rebalance_frequency: str = "daily",
    weight_tolerance: float = 0.0,
) -> BacktestResult:
    """Portfolio backtest with close planning and next-open rebalance execution."""
    required = tuple(dict.fromkeys(("US.SPY", *symbols)))
    if any(symbol not in frames for symbol in required):
        raise ValueError("Core Satellite 缺少 SPY 或组合标的历史数据")
    prepared: dict[str, pd.DataFrame] = {}
    common_dates: set[str] | None = None
    for symbol in required:
        frame = frames[symbol].sort_values("time_key").drop_duplicates("time_key").copy()
        frame["date"] = pd.to_datetime(frame["time_key"]).dt.strftime("%Y-%m-%d")
        frame = frame.set_index("date")
        prepared[symbol] = frame
        dates = set(frame.index)
        common_dates = dates if common_dates is None else common_dates & dates
    dates = sorted(common_dates or [])
    if len(dates) < 201:
        raise ValueError("Core Satellite 回测至少需要 201 个共同交易日")

    cash = float(initial_cash)
    holdings = {symbol: 0 for symbol in symbols}
    pending = []
    trades: list[dict] = []
    equity_rows: list[dict] = []
    slip = slippage_bps / 10_000
    previous_risk_on: bool | None = None

    for index, day in enumerate(dates):
        for order in sorted(pending, key=lambda item: item.side != "SELL"):
            open_price = float(prepared[order.symbol].loc[day, "open"])
            price = open_price * (1 - slip if order.side == "SELL" else 1 + slip)
            if order.side == "SELL":
                quantity = min(order.quantity, holdings[order.symbol])
                if quantity:
                    cash += price * quantity - commission
                    holdings[order.symbol] -= quantity
            else:
                quantity = min(order.quantity, int(max(0, (cash - commission) // price)))
                if quantity:
                    cash -= price * quantity + commission
                    holdings[order.symbol] += quantity
            if quantity:
                trades.append(
                    {"date": day, "side": order.side, "code": order.symbol, "price": price,
                     "quantity": quantity, "commission": commission, "cash_after": cash,
                     "target_weight": order.target_weight, "reason": order.reason,
                     "notional": price * quantity}
                )

        close_values = {
            symbol: float(prepared[symbol].loc[day, "close"]) for symbol in symbols
        }
        equity_value = cash + sum(holdings[s] * close_values[s] for s in symbols)
        equity_rows.append(
            {"date": day, "equity": equity_value, "cash": cash,
             "stock_value": equity_value - cash,
             "risk_state": "N/A" if index < 199 else "PENDING"}
        )
        pending = []
        if index >= 199:
            histories = {
                symbol: prepared[symbol].loc[dates[: index + 1]].reset_index()
                for symbol in required
            }
            position_rows = [
                {"code": symbol, "qty": qty, "can_sell_qty": qty,
                 "market_val": qty * close_values[symbol]}
                for symbol, qty in holdings.items() if qty > 0
            ]
            spy_close = histories["US.SPY"]["close"].astype(float)
            risk_on = float(spy_close.iloc[-1]) >= float(spy_close.iloc[-200:].mean())
            risk_changed = previous_risk_on is not None and risk_on != previous_risk_on
            current = pd.Timestamp(day)
            next_day = pd.Timestamp(dates[index + 1]) if index + 1 < len(dates) else None
            if rebalance_frequency == "daily":
                scheduled = True
            elif rebalance_frequency == "weekly":
                scheduled = next_day is None or current.isocalendar().week != next_day.isocalendar().week
            else:
                scheduled = next_day is None or current.month != next_day.month
            if scheduled or risk_changed:
                pending, risk_on = build_rebalance_plan(
                    histories, pd.DataFrame(position_rows), equity_value, symbols,
                    max_stock_weight, min_trade_usd, profile, weight_tolerance,
                )
            previous_risk_on = risk_on
            equity_rows[-1]["risk_state"] = "RISK-ON" if risk_on else "RISK-OFF"

    equity = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trades)
    return BacktestResult(equity, trade_frame, _metrics(equity, trade_frame, initial_cash))


def run_backtest(
    bars: pd.DataFrame,
    strategy_name: str,
    initial_cash: float,
    quantity: int,
    commission: float = 0.0,
    slippage_bps: float = 0.0,
    max_volume_participation: float | None = None,
) -> BacktestResult:
    """Long-only backtest: close signal at T, execution at T+1 open."""
    if strategy_name not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    required = {"time_key", "open", "close"}
    if bars.empty or not required.issubset(bars.columns):
        raise ValueError("回测数据为空或缺少 OHLC 字段")
    frame = bars.sort_values("time_key").reset_index(drop=True).copy()
    if len(frame) < 35:
        raise ValueError("回测数据不足，至少需要 35 个交易日")
    if max_volume_participation is not None and not 0 < max_volume_participation <= 1:
        raise ValueError("成交量参与率必须在 0 到 1 之间")

    cash = float(initial_cash)
    held = 0
    pending = Signal.HOLD
    entry_cost = 0.0
    trade_rows: list[dict] = []
    equity_rows: list[dict] = []
    slip = float(slippage_bps) / 10_000

    for index, row in frame.iterrows():
        if pending == Signal.BUY and held == 0:
            price = float(row["open"]) * (1 + slip)
            affordable = int(max(0, (cash - commission) // price))
            volume_cap = int(quantity)
            if max_volume_participation is not None and "volume" in frame:
                volume_cap = max(0, int(float(row.get("volume", 0) or 0) * max_volume_participation))
            requested_quantity = int(quantity)
            fill_qty = min(requested_quantity, affordable, volume_cap)
            if fill_qty > 0:
                cost = price * fill_qty + commission
                cash -= cost
                held = fill_qty
                entry_cost = cost
                trade_rows.append(
                    {"date": row["time_key"], "side": "BUY", "price": price,
                     "quantity": fill_qty, "commission": commission, "cash_after": cash,
                     "notional": price * fill_qty, "requested_quantity": requested_quantity,
                     "unfilled_quantity": requested_quantity - fill_qty,
                     "fill_status": "FILLED" if fill_qty == requested_quantity else "PARTIAL"}
                )
        elif pending == Signal.SELL and held > 0:
            price = float(row["open"]) * (1 - slip)
            requested_quantity = held
            volume_cap = requested_quantity
            if max_volume_participation is not None and "volume" in frame:
                volume_cap = max(0, int(float(row.get("volume", 0) or 0) * max_volume_participation))
            fill_qty = min(requested_quantity, volume_cap)
            if fill_qty <= 0:
                history = frame.iloc[: index + 1]
                pending = STRATEGIES[strategy_name](history).signal
                close = float(row["close"])
                equity_rows.append(
                    {"date": row["time_key"], "equity": cash + held * close, "cash": cash,
                     "position": held, "close": close, "signal": pending.value}
                )
                continue
            held_before = held
            allocated_entry_cost = entry_cost * fill_qty / held_before
            proceeds = price * fill_qty - commission
            cash += proceeds
            pnl = proceeds - allocated_entry_cost
            trade_rows.append(
                {"date": row["time_key"], "side": "SELL", "price": price,
                 "quantity": fill_qty, "commission": commission, "cash_after": cash, "pnl": pnl,
                 "notional": price * fill_qty, "requested_quantity": requested_quantity,
                 "unfilled_quantity": requested_quantity - fill_qty,
                 "fill_status": "FILLED" if fill_qty == requested_quantity else "PARTIAL"}
            )
            held -= fill_qty
            entry_cost -= allocated_entry_cost

        history = frame.iloc[: index + 1]
        pending = STRATEGIES[strategy_name](history).signal
        close = float(row["close"])
        equity_rows.append(
            {"date": row["time_key"], "equity": cash + held * close, "cash": cash,
             "position": held, "close": close, "signal": pending.value}
        )

    equity = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trade_rows)
    closed = trades[trades["side"] == "SELL"] if not trades.empty else trades
    wins = int((closed.get("pnl", pd.Series(dtype=float)) > 0).sum())
    metrics = _metrics(equity, trades, initial_cash)
    metrics["win_rate"] = float(wins / len(closed)) if len(closed) else 0.0
    return BacktestResult(equity=equity, trades=trades, metrics=metrics)
