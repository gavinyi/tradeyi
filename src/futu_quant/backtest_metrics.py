from __future__ import annotations

import math

import pandas as pd


def _safe_float(value: float) -> float:
    return float(value) if math.isfinite(float(value)) else 0.0


def _drawdown_duration(drawdown: pd.Series) -> int:
    longest = current = 0
    for value in drawdown:
        if float(value) < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _exposure(equity: pd.DataFrame) -> pd.Series:
    curve = equity["equity"].astype(float).replace(0, pd.NA)
    if "stock_value" in equity:
        return (equity["stock_value"].astype(float).abs() / curve).fillna(0)
    if {"position", "close"}.issubset(equity.columns):
        return (
            equity["position"].astype(float).abs() * equity["close"].astype(float) / curve
        ).fillna(0)
    return pd.Series(0.0, index=equity.index)


def calculate_backtest_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
    benchmark: pd.Series | None = None,
    periods_per_year: int = 252,
) -> dict[str, float]:
    if equity.empty or "equity" not in equity:
        raise ValueError("权益曲线为空或缺少 equity 字段")
    curve = equity["equity"].astype(float).reset_index(drop=True)
    returns = curve.pct_change().dropna()
    drawdown = curve / curve.cummax() - 1
    periods = max(len(curve) - 1, 1)
    total_return = float(curve.iloc[-1] / float(initial_cash) - 1)
    years = periods / periods_per_year
    cagr = float((curve.iloc[-1] / float(initial_cash)) ** (1 / years) - 1) if years > 0 and curve.iloc[-1] > 0 else -1.0
    annual_volatility = float(returns.std(ddof=0) * math.sqrt(periods_per_year)) if len(returns) else 0.0
    mean_return = float(returns.mean()) if len(returns) else 0.0
    sharpe = mean_return / float(returns.std(ddof=0)) * math.sqrt(periods_per_year) if len(returns) and float(returns.std(ddof=0)) > 0 else 0.0
    downside = returns[returns < 0]
    downside_deviation = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
    sortino = mean_return / downside_deviation * math.sqrt(periods_per_year) if downside_deviation > 0 else 0.0
    maximum_drawdown = float(drawdown.min())
    calmar = cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else 0.0

    if not trades.empty and "pnl" in trades:
        pnl_series = pd.to_numeric(trades["pnl"], errors="coerce")
        closed = trades[pnl_series.notna()].copy()
        pnls = pnl_series[pnl_series.notna()]
    elif not trades.empty and "side" in trades:
        closed = trades[trades["side"].astype(str).eq("SELL")].copy()
        pnls = pd.to_numeric(
            closed.get("pnl", pd.Series(dtype=float)), errors="coerce"
        ).dropna()
    else:
        closed = pd.DataFrame()
        pnls = pd.Series(dtype=float)
    gains = float(pnls[pnls > 0].sum())
    losses = abs(float(pnls[pnls < 0].sum()))
    profit_factor = gains / losses if losses > 0 else gains if gains > 0 else 0.0
    expectancy = float(pnls.mean()) if len(pnls) else 0.0
    win_rate = float((pnls > 0).mean()) if len(pnls) else 0.0
    if not trades.empty:
        if "notional" in trades:
            notionals = pd.to_numeric(trades["notional"], errors="coerce").fillna(0).abs()
        elif {"price", "quantity"}.issubset(trades.columns):
            notionals = (
                pd.to_numeric(trades["price"], errors="coerce").fillna(0)
                * pd.to_numeric(trades["quantity"], errors="coerce").fillna(0)
            ).abs()
        else:
            notionals = pd.Series(dtype=float)
    else:
        notionals = pd.Series(dtype=float)
    turnover = float(notionals.sum() / curve.mean()) if curve.mean() else 0.0
    exposure = _exposure(equity)

    metrics = {
        "initial_cash": float(initial_cash),
        "final_equity": float(curve.iloc[-1]),
        "total_return": total_return,
        "cagr": _safe_float(cagr),
        "annual_volatility": _safe_float(annual_volatility),
        "sharpe": _safe_float(sharpe),
        "sortino": _safe_float(sortino),
        "calmar": _safe_float(calmar),
        "max_drawdown": maximum_drawdown,
        "max_drawdown_duration": float(_drawdown_duration(drawdown)),
        "trade_count": float(len(trades)),
        "closed_trades": float(len(closed)),
        "win_rate": win_rate,
        "profit_factor": _safe_float(profit_factor),
        "expectancy": expectancy,
        "turnover": turnover,
        "average_exposure": float(exposure.mean()),
        "maximum_exposure": float(exposure.max()),
        "benchmark_return": 0.0,
        "excess_return": total_return,
        "beta": 0.0,
        "alpha": 0.0,
        "up_capture": 0.0,
        "down_capture": 0.0,
    }
    if benchmark is not None and len(benchmark) >= 2:
        benchmark_curve = pd.to_numeric(pd.Series(benchmark).reset_index(drop=True), errors="coerce")
        size = min(len(curve), len(benchmark_curve))
        benchmark_curve = benchmark_curve.iloc[-size:].reset_index(drop=True)
        strategy_curve = curve.iloc[-size:].reset_index(drop=True)
        strategy_returns = strategy_curve.pct_change()
        benchmark_returns = benchmark_curve.pct_change()
        aligned = pd.DataFrame({"strategy": strategy_returns, "benchmark": benchmark_returns}).dropna()
        benchmark_return = float(benchmark_curve.iloc[-1] / benchmark_curve.iloc[0] - 1)
        variance = float(aligned["benchmark"].var(ddof=0)) if len(aligned) else 0.0
        beta = float(aligned.cov(ddof=0).loc["strategy", "benchmark"] / variance) if variance > 0 else 0.0
        alpha = float((aligned["strategy"].mean() - beta * aligned["benchmark"].mean()) * periods_per_year) if len(aligned) else 0.0
        up = aligned[aligned["benchmark"] > 0]
        down = aligned[aligned["benchmark"] < 0]
        metrics.update(
            {
                "benchmark_return": benchmark_return,
                "excess_return": total_return - benchmark_return,
                "beta": _safe_float(beta),
                "alpha": _safe_float(alpha),
                "up_capture": _safe_float(up["strategy"].sum() / up["benchmark"].sum()) if len(up) and up["benchmark"].sum() else 0.0,
                "down_capture": _safe_float(down["strategy"].sum() / down["benchmark"].sum()) if len(down) and down["benchmark"].sum() else 0.0,
            }
        )
    return metrics
