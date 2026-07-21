from __future__ import annotations

import pandas as pd

from .backtest import BacktestResult
from .option_evidence import assess_option_evidence
from .option_lifecycle import parse_option_code


def run_option_event_backtest(
    snapshots: pd.DataFrame,
    code: str,
    initial_cash: float,
    contracts: int = 1,
    commission_per_contract: float = 0.65,
    take_profit: float = 0.50,
    stop_loss: float = 0.35,
) -> BacktestResult:
    """Long-option replay using ask entry, bid exit and intrinsic expiry settlement."""
    contract = parse_option_code(code)
    if contract is None:
        raise ValueError("无法识别期权代码")
    if contracts <= 0 or initial_cash <= 0:
        raise ValueError("初始资金和合约数量必须大于 0")
    required = {"time_key", "bid", "ask", "underlying_price"}
    if snapshots.empty or not required.issubset(snapshots.columns):
        raise ValueError("没有可用的历史期权盘口数据")
    evidence = assess_option_evidence(snapshots, min_samples=5, min_days=1)
    if evidence.status != "SUFFICIENT":
        raise ValueError(f"期权历史证据不足：{evidence.reason}")
    frame = snapshots.copy()
    frame["time_key"] = pd.to_datetime(frame["time_key"], errors="coerce")
    for column in ("bid", "ask", "underlying_price"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["time_key", "bid", "ask"]).sort_values("time_key")
    frame = frame[(frame["bid"] > 0) & (frame["ask"] >= frame["bid"])]
    if "quality_status" in frame:
        frame = frame[frame["quality_status"] == "GOOD"]
    frame = frame.drop_duplicates("time_key")
    if len(frame) < 5:
        raise ValueError(f"有效期权历史样本只有 {len(frame)} 条，至少需要 5 条")

    first = frame.iloc[0]
    entry_price = float(first["ask"])
    entry_fee = float(commission_per_contract) * contracts
    entry_cost = entry_price * 100 * contracts + entry_fee
    if entry_cost > initial_cash:
        raise ValueError(f"初始资金不足：开仓需要 ${entry_cost:,.2f}")
    cash = float(initial_cash) - entry_cost
    held = contracts
    trades = [{
        "date": first["time_key"], "code": code, "side": "BUY",
        "price": entry_price, "quantity": contracts, "commission": entry_fee,
        "cash_after": cash, "reason": "按首个可用卖一价开仓",
    }]
    equity_rows: list[dict] = []
    exit_pnl = 0.0
    for index, row in frame.iterrows():
        bid = float(row["bid"])
        mark_value = bid * 100 * held
        return_ratio = bid / entry_price - 1
        timestamp = row["time_key"]
        reached_expiry = timestamp.date() >= contract.expiration
        reason = None
        if held and return_ratio >= take_profit:
            reason = "达到止盈"
        elif held and return_ratio <= -stop_loss:
            reason = "达到止损"
        elif held and reached_expiry:
            reason = "到期按内在价值结算"
        elif held and index == frame.index[-1]:
            reason = "历史数据结束，按买一价退出"
        if reason:
            if reached_expiry:
                spot = float(row.get("underlying_price", 0) or 0)
                exit_price = (
                    max(0.0, spot - contract.strike)
                    if contract.option_type == "CALL"
                    else max(0.0, contract.strike - spot)
                )
            else:
                exit_price = bid
            exit_fee = float(commission_per_contract) * held
            proceeds = exit_price * 100 * held - exit_fee
            cash += proceeds
            exit_pnl = proceeds - entry_cost
            trades.append({
                "date": timestamp, "code": code, "side": "SELL", "price": exit_price,
                "quantity": held, "commission": exit_fee, "cash_after": cash,
                "pnl": exit_pnl, "reason": reason,
            })
            held = 0
            mark_value = 0
        equity_rows.append({
            "date": timestamp, "equity": cash + mark_value, "cash": cash,
            "position": held, "bid": bid, "ask": float(row["ask"]),
            "underlying_price": float(row.get("underlying_price", 0) or 0),
        })
        if held == 0:
            break
    equity = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trades)
    curve = equity["equity"]
    drawdown = curve / curve.cummax() - 1
    metrics = {
        "initial_cash": float(initial_cash), "final_equity": float(curve.iloc[-1]),
        "total_return": float(curve.iloc[-1] / initial_cash - 1),
        "max_drawdown": float(drawdown.min()), "trade_count": float(len(trade_frame)),
        "closed_trades": 1.0, "win_rate": float(exit_pnl > 0),
        "sample_count": float(len(frame)),
        "spread_cost": round((entry_price - float(first["bid"])) * 100 * contracts, 8),
        "evidence_trading_days": float(evidence.trading_days),
        "evidence_degraded_ratio": float(evidence.degraded_ratio),
        "evidence_average_spread_pct": float(evidence.average_spread_pct),
    }
    return BacktestResult(equity, trade_frame, metrics)
