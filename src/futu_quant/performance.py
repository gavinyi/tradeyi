from __future__ import annotations

from collections import defaultdict, deque
import json

import pandas as pd

from .risk import OPTION_PATTERN


def equity_frame(store) -> pd.DataFrame:
    equity = pd.DataFrame(store.equity_history())
    if not equity.empty:
        equity["峰值"] = equity["total_assets"].cummax()
        equity["回撤"] = equity["total_assets"] / equity["峰值"] - 1
    return equity


def performance_report(store, broker) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    intents = pd.DataFrame(store.intents(limit=10_000))
    if intents.empty or "filled_quantity" not in intents:
        return pd.DataFrame(), pd.DataFrame(), equity_frame(store)
    fills = intents[intents["filled_quantity"] > 0].sort_values("created_at").copy()
    fills = fills[~fills["strategy_id"].astype(str).str.startswith("migration_")]
    if fills.empty:
        return pd.DataFrame(), pd.DataFrame(), equity_frame(store)
    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    realized: dict[str, list[float]] = defaultdict(list)
    fees: dict[str, float] = defaultdict(float)

    for _, fill in fills.iterrows():
        key = (fill["strategy_id"], fill["symbol"])
        metadata = json.loads(fill["metadata_json"])
        multiplier = float(metadata.get(
            "multiplier", 100 if OPTION_PATTERN.match(fill["symbol"]) else 1
        ))
        quantity = float(fill["filled_quantity"])
        price = float(fill["average_fill_price"])
        fees[fill["strategy_id"]] += float(fill.get("fees", 0) or 0)
        if fill["side"] == "BUY":
            lots[key].append([quantity, price, multiplier])
            continue
        remaining = quantity
        trade_pnl = 0.0
        while remaining > 0 and lots[key]:
            lot_qty, lot_price, lot_multiplier = lots[key][0]
            matched = min(remaining, lot_qty)
            trade_pnl += (price - lot_price) * matched * lot_multiplier
            remaining -= matched
            lot_qty -= matched
            if lot_qty <= 0:
                lots[key].popleft()
            else:
                lots[key][0][0] = lot_qty
        realized[fill["strategy_id"]].append(trade_pnl)

    open_symbols = sorted({
        symbol for (_strategy, symbol), open_lots in lots.items() if open_lots
    })
    if open_symbols:
        quotes = broker.snapshots(open_symbols)
        prices = quotes.set_index("code")["last_price"].astype(float).to_dict()
    else:
        prices = {}

    detail_rows = []
    for (strategy, symbol), open_lots in lots.items():
        if not open_lots:
            continue
        last = float(prices.get(symbol, 0))
        quantity = sum(lot[0] for lot in open_lots)
        cost = sum(lot[0] * lot[1] * lot[2] for lot in open_lots)
        market_value = sum(lot[0] * last * lot[2] for lot in open_lots)
        detail_rows.append({
            "策略": strategy, "代码": symbol, "净持仓": quantity,
            "成本金额": cost, "最新价": last, "持仓市值": market_value,
            "未实现盈亏": market_value - cost,
        })
    detail = pd.DataFrame(detail_rows)
    strategies = sorted(set(fills["strategy_id"]))
    summary_rows = []
    for strategy in strategies:
        trades = realized[strategy]
        unrealized = float(detail[detail["策略"] == strategy]["未实现盈亏"].sum()) if not detail.empty else 0
        gross_profit = sum(value for value in trades if value > 0)
        gross_loss = abs(sum(value for value in trades if value < 0))
        realized_pnl = sum(trades) - fees[strategy]
        summary_rows.append({
            "策略": strategy, "已平仓交易": len(trades),
            "胜率": sum(value > 0 for value in trades) / len(trades) if trades else 0,
            "盈亏比": gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0),
            "费用": fees[strategy], "已实现盈亏": realized_pnl,
            "未实现盈亏": unrealized, "总盈亏": realized_pnl + unrealized,
        })
    return pd.DataFrame(summary_rows), detail, equity_frame(store)


def strategy_attribution(store, broker) -> pd.DataFrame:
    _, detail, _ = performance_report(store, broker)
    return detail
