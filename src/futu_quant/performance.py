from __future__ import annotations

from collections import defaultdict, deque
import json

import pandas as pd

from .risk import OPTION_PATTERN
from zoneinfo import ZoneInfo
import math

from .option_lifecycle import parse_option_code


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
        loader = getattr(broker, "snapshots_tolerant", None)
        quotes = loader(open_symbols) if loader is not None else broker.snapshots(open_symbols)
        prices = quotes.set_index("code")["last_price"].astype(float).to_dict() if not quotes.empty else {}
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


def performance_buckets(store, *, period: str = "daily", start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Aggregate canonical fills by New York trading day/week/month."""
    if period not in {"daily", "weekly", "monthly"}:
        raise ValueError("period must be daily, weekly or monthly")
    frame = pd.DataFrame(store.fills(start=start, end=end))
    if frame.empty:
        return pd.DataFrame()
    frame["filled_at"] = pd.to_datetime(frame["filled_at"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["filled_at"]).copy()
    local = frame["filled_at"].dt.tz_convert(ZoneInfo("America/New_York"))
    if period == "daily":
        frame["期间"] = local.dt.strftime("%Y-%m-%d")
    elif period == "weekly":
        frame["期间"] = local.dt.to_period("W-MON").astype(str)
    else:
        frame["期间"] = local.dt.strftime("%Y-%m")
    frame["成交金额"] = frame["quantity"] * frame["price"] * frame["multiplier"]
    frame["手续费"] = frame["fee_total"].astype(float)
    frame["价差成本"] = frame["spread_cost"].astype(float)
    frame["价格冲击"] = frame["price_impact_cost"].astype(float)
    frame["方向"] = frame["side"].map({"BUY": 1, "SELL": -1}).fillna(0)
    frame["现金流"] = -frame["方向"] * frame["成交金额"]
    # FIFO realized P&L is assigned to the period in which the closing fill occurs.
    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    realized = defaultdict(float)
    for row in frame.sort_values(["filled_at", "id"]).itertuples():
        key = (str(row.strategy_id), str(row.symbol))
        qty, price, multiplier = float(row.quantity), float(row.price), float(row.multiplier)
        if str(row.side).upper() == "BUY":
            lots[key].append([qty, price, multiplier])
            continue
        remaining = qty
        while remaining > 0 and lots[key]:
            lot_qty, lot_price, lot_multiplier = lots[key][0]
            matched = min(remaining, lot_qty)
            realized[(str(row.期间), str(row.strategy_id))] += (
                price - lot_price
            ) * matched * min(multiplier, lot_multiplier)
            remaining -= matched
            lot_qty -= matched
            if lot_qty <= 0:
                lots[key].popleft()
            else:
                lots[key][0][0] = lot_qty
    result = frame.groupby(["期间", "strategy_id"], as_index=False).agg(
        成交笔数=("id", "count"), 成交金额=("成交金额", "sum"),
        手续费=("手续费", "sum"), 价差成本=("价差成本", "sum"),
        价格冲击=("价格冲击", "sum"), 净现金流=("现金流", "sum"),
        行情覆盖率=("mid_price", lambda s: float(s.notna().mean())),
    )
    result["已实现盈亏"] = [realized[(str(p), str(s))] for p, s in zip(result["期间"], result["strategy_id"])]
    result["可观测交易成本"] = result["手续费"] + result["价差成本"] + result["价格冲击"]
    result["净收益"] = result["已实现盈亏"] - result["可观测交易成本"]
    result["说明"] = "已实现盈亏按 FIFO；未平仓浮盈需结合期末行情估值"
    return result.sort_values(["期间", "strategy_id"]).reset_index(drop=True)


def _period_label(value: pd.Timestamp, period: str) -> str:
    local = value.tz_convert(ZoneInfo("America/New_York"))
    if period == "daily":
        return local.strftime("%Y-%m-%d")
    if period == "weekly":
        return str(local.to_period("W-MON"))
    return local.strftime("%Y-%m")


def _period_metrics(equity: pd.DataFrame) -> dict[str, float]:
    if equity.empty or "total_assets" not in equity:
        return {"TWR": 0.0, "最大回撤": 0.0, "波动率": 0.0, "Sharpe": 0.0, "Sortino": 0.0}
    values = pd.to_numeric(equity["total_assets"], errors="coerce").dropna()
    returns = values.pct_change().dropna()
    if returns.empty:
        return {"TWR": 0.0, "最大回撤": 0.0, "波动率": 0.0, "Sharpe": 0.0, "Sortino": 0.0}
    curve = (1 + returns).cumprod()
    drawdown = curve / curve.cummax() - 1
    downside = returns[returns < 0]
    volatility = float(returns.std(ddof=1) or 0)
    downside_dev = float(downside.std(ddof=1) or 0)
    return {
        "TWR": float(curve.iloc[-1] - 1),
        "最大回撤": float(drawdown.min()),
        "波动率": volatility,
        "Sharpe": float(returns.mean() / volatility * math.sqrt(252)) if volatility else 0.0,
        "Sortino": float(returns.mean() / downside_dev * math.sqrt(252)) if downside_dev else 0.0,
    }


def performance_dashboard_data(store, broker=None, *, period: str = "daily") -> dict[str, pd.DataFrame | dict]:
    """Return mature-dashboard datasets: fills, costs, equity risk, and coverage."""
    buckets = performance_buckets(store, period=period)
    fills = pd.DataFrame(store.fills())
    if not fills.empty:
        fills["期权结构"] = fills["symbol"].map(
            lambda code: (parse_option_code(str(code)).option_type if parse_option_code(str(code)) else "股票/ETF")
        )
        fills["标的"] = fills["symbol"].map(
            lambda code: parse_option_code(str(code)).underlying if parse_option_code(str(code)) else str(code)
        )
    equity = pd.DataFrame(store.equity_history(limit=5_000))
    if not equity.empty:
        equity["created_at"] = pd.to_datetime(equity["created_at"], utc=True, errors="coerce")
        equity = equity.dropna(subset=["created_at"])
        equity["期间"] = equity["created_at"].map(lambda value: _period_label(value, period))
    metrics = _period_metrics(equity)
    metrics["费用覆盖率"] = float(fills["mid_price"].notna().mean()) if not fills.empty else 0.0
    return {"buckets": buckets, "fills": fills, "equity": equity, "metrics": metrics}


def strategy_attribution(store, broker) -> pd.DataFrame:
    _, detail, _ = performance_report(store, broker)
    return detail
