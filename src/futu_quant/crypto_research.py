from __future__ import annotations

import numpy as np
import pandas as pd

from .crypto_backtest import run_crypto_volatility_backtest


def crypto_walk_forward(
    bars: pd.DataFrame, initial_cash: float, config,
    train_bars: int = 144, test_bars: int = 72,
) -> dict:
    minimum = train_bars + test_bars
    if len(bars) < minimum:
        raise ValueError(f"Walk-forward 至少需要 {minimum} 根 K 线")
    windows = []
    start = 0
    while start + minimum <= len(bars):
        test = bars.iloc[start + train_bars : start + minimum].copy()
        warmup = bars.iloc[start : start + train_bars].copy()
        result = run_crypto_volatility_backtest(
            pd.concat([warmup, test], ignore_index=True), initial_cash, config,
        )
        windows.append({
            "训练开始": int(warmup.iloc[0]["timestamp"]),
            "测试结束": int(test.iloc[-1]["timestamp"]),
            "样本外收益": result.metrics["total_return"],
            "最大回撤": result.metrics["max_drawdown"],
            "交易次数": result.metrics["trade_count"],
        })
        start += test_bars
    frame = pd.DataFrame(windows)
    return {
        "windows": frame,
        "passed": bool(
            not frame.empty
            and (frame["样本外收益"] > 0).mean() >= .5
            and frame["最大回撤"].min() >= -.30
        ),
    }


def crypto_monte_carlo(equity: pd.DataFrame, simulations: int = 500, seed: int = 42) -> dict:
    returns = equity["equity"].astype(float).pct_change().dropna().to_numpy()
    if len(returns) < 20:
        raise ValueError("Monte Carlo 至少需要 20 个权益收益样本")
    rng = np.random.default_rng(seed)
    terminal, drawdowns = [], []
    for _ in range(max(10, simulations)):
        sampled = rng.choice(returns, len(returns), replace=True)
        path = np.cumprod(1 + sampled)
        terminal.append(float(path[-1] - 1))
        drawdowns.append(float((path / np.maximum.accumulate(path) - 1).min()))
    return {
        "收益中位数": float(np.median(terminal)),
        "收益5%分位": float(np.quantile(terminal, .05)),
        "最大回撤95%压力": float(np.quantile(drawdowns, .05)),
        "亏损概率": float(np.mean(np.array(terminal) < 0)),
        "模拟次数": int(max(10, simulations)),
    }


def crypto_lookahead_audit(result) -> dict:
    if result.trades.empty:
        return {"passed": True, "violations": 0, "reason": "没有交易，未发现前视成交"}
    violations = int(
        (result.trades["timestamp"].astype(float)
         <= result.trades["signal_timestamp"].astype(float)).sum()
    )
    return {
        "passed": violations == 0, "violations": violations,
        "reason": "全部订单均在信号后的下一根 K 线成交" if not violations else "发现同K线成交",
    }

