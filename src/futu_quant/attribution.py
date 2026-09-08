from __future__ import annotations

from collections import defaultdict, deque
import json

import pandas as pd

from .option_lifecycle import parse_option_code


def _is_futu_us_symbol(symbol: str) -> bool:
    return symbol.startswith("US.")


def _number(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(result) else result


def attribution_report(store, broker) -> tuple[pd.DataFrame, pd.DataFrame]:
    intents = pd.DataFrame(store.intents(limit=50_000))
    fills = (
        intents[intents["filled_quantity"] > 0].sort_values("created_at").copy()
        if not intents.empty and "filled_quantity" in intents else pd.DataFrame()
    )
    allocations = store.virtual_allocations()
    symbols = set(fills["symbol"].astype(str)) if not fills.empty else set()
    symbols.update(str(item["symbol"]) for item in allocations)
    us_symbols = sorted(symbol for symbol in symbols if _is_futu_us_symbol(symbol))
    quotes = broker.snapshots(us_symbols) if us_symbols else pd.DataFrame()
    quote_map = {
        str(row["code"]): row for _, row in quotes.iterrows()
    } if not quotes.empty else {}

    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    realized_by_key: dict[tuple[str, str], float] = defaultdict(float)
    fees_by_key: dict[tuple[str, str], float] = defaultdict(float)
    slippage_by_key: dict[tuple[str, str], float] = defaultdict(float)
    closed_by_key: dict[tuple[str, str], int] = defaultdict(int)
    wins_by_key: dict[tuple[str, str], int] = defaultdict(int)
    for item in allocations:
        quantity = float(item["quantity"])
        if quantity > 0:
            multiplier = 100 if parse_option_code(item["symbol"]) else 1
            lots[(item["strategy_id"], item["symbol"])].append(
                [quantity, float(item["average_cost"]), multiplier]
            )

    for _, fill in fills.iterrows():
        key = (str(fill["strategy_id"]), str(fill["symbol"]))
        metadata = json.loads(fill.get("metadata_json", "{}") or "{}")
        multiplier = float(metadata.get(
            "multiplier", 100 if parse_option_code(key[1]) else 1
        ))
        quantity = float(fill["filled_quantity"])
        price = float(fill["average_fill_price"])
        fees_by_key[key] += float(fill.get("fees", 0) or 0)
        reference = float(metadata.get("reference_price", fill.get("limit_price", price)) or price)
        direction = 1 if fill["side"] == "BUY" else -1
        slippage_by_key[key] += (price - reference) * direction * quantity * multiplier
        if fill["side"] == "BUY":
            lots[key].append([quantity, price, multiplier])
            continue
        remaining, trade_pnl = quantity, 0.0
        while remaining > 0 and lots[key]:
            lot_quantity, lot_price, lot_multiplier = lots[key][0]
            matched = min(remaining, lot_quantity)
            trade_pnl += (price - lot_price) * matched * lot_multiplier
            remaining -= matched
            lot_quantity -= matched
            if lot_quantity <= 0:
                lots[key].popleft()
            else:
                lots[key][0][0] = lot_quantity
        realized_by_key[key] += trade_pnl
        closed_by_key[key] += 1
        wins_by_key[key] += int(trade_pnl > 0)

    keys = set(lots) | set(realized_by_key) | set(fees_by_key)
    detail_rows = []
    for strategy, symbol in sorted(keys):
        key = (strategy, symbol)
        row = quote_map.get(symbol, {})
        last = _number(row.get("last_price"), 0)
        quote_status = (
            "行情已读取" if symbol in quote_map
            else "未请求行情：非美股代码" if not _is_futu_us_symbol(symbol)
            else "行情缺失"
        )
        open_lots = lots[key]
        quantity = sum(float(lot[0]) for lot in open_lots)
        cost = sum(float(lot[0]) * float(lot[1]) * float(lot[2]) for lot in open_lots)
        multiplier = 100 if parse_option_code(symbol) else 1
        market_value = quantity * last * multiplier
        is_option = parse_option_code(symbol) is not None
        detail_rows.append({
            "strategy_id": strategy, "symbol": symbol,
            "asset_class": "OPTION" if is_option else "STOCK_ETF",
            "quantity": quantity, "average_cost": cost / (quantity * multiplier)
            if quantity else 0, "last_price": last, "market_value": market_value,
            "quote_status": quote_status,
            "realized_pnl": realized_by_key[key], "unrealized_pnl": market_value - cost,
            "fees": fees_by_key[key], "slippage_cost": slippage_by_key[key],
            "total_pnl": realized_by_key[key] + market_value - cost - fees_by_key[key],
            "delta_exposure": quantity * multiplier * _number(
                row.get("option_delta"), 0 if is_option else 1
            ) * (_number(row.get("underlying_price"), last) if is_option else last),
            "gamma_exposure": quantity * multiplier * _number(row.get("option_gamma")),
            "vega_exposure": quantity * multiplier * _number(row.get("option_vega")),
            "theta_exposure": quantity * multiplier * _number(row.get("option_theta")),
            "closed_trades": closed_by_key[key], "winning_trades": wins_by_key[key],
        })
    detail = pd.DataFrame(detail_rows)
    if detail.empty:
        return pd.DataFrame(), detail
    summary = detail.groupby("strategy_id", as_index=False).agg(
        realized_pnl=("realized_pnl", "sum"), unrealized_pnl=("unrealized_pnl", "sum"),
        fees=("fees", "sum"), slippage_cost=("slippage_cost", "sum"),
        total_pnl=("total_pnl", "sum"), market_value=("market_value", "sum"),
        delta_exposure=("delta_exposure", "sum"), gamma_exposure=("gamma_exposure", "sum"),
        vega_exposure=("vega_exposure", "sum"), theta_exposure=("theta_exposure", "sum"),
        closed_trades=("closed_trades", "sum"), winning_trades=("winning_trades", "sum"),
    )
    summary["win_rate"] = summary["winning_trades"] / summary["closed_trades"].replace(0, pd.NA)
    summary["win_rate"] = summary["win_rate"].fillna(0.0)
    return summary, detail
