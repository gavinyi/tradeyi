from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    signal: Signal
    reason: str
    price: float


def sma_cross(frame: pd.DataFrame, fast: int = 10, slow: int = 30) -> StrategyResult:
    close = frame["close"].astype(float)
    fast_ma, slow_ma = close.rolling(fast).mean(), close.rolling(slow).mean()
    if len(close) < slow + 1 or pd.isna(slow_ma.iloc[-1]):
        return StrategyResult("SMA Cross", Signal.HOLD, "not enough bars", float(close.iloc[-1]))
    crossed_up = fast_ma.iloc[-2] <= slow_ma.iloc[-2] and fast_ma.iloc[-1] > slow_ma.iloc[-1]
    crossed_down = fast_ma.iloc[-2] >= slow_ma.iloc[-2] and fast_ma.iloc[-1] < slow_ma.iloc[-1]
    signal = Signal.BUY if crossed_up else Signal.SELL if crossed_down else Signal.HOLD
    return StrategyResult("SMA Cross", signal, f"MA{fast}={fast_ma.iloc[-1]:.2f}, MA{slow}={slow_ma.iloc[-1]:.2f}", float(close.iloc[-1]))


def rsi_reversion(frame: pd.DataFrame, period: int = 14) -> StrategyResult:
    close = frame["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    value = float(rsi.iloc[-1])
    signal = Signal.BUY if value < 30 else Signal.SELL if value > 70 else Signal.HOLD
    return StrategyResult("RSI Reversion", signal, f"RSI{period}={value:.2f}", float(close.iloc[-1]))


def breakout(frame: pd.DataFrame, lookback: int = 20) -> StrategyResult:
    close = frame["close"].astype(float)
    prior = close.iloc[-lookback - 1 : -1]
    price = float(close.iloc[-1])
    signal = Signal.BUY if price > prior.max() else Signal.SELL if price < prior.min() else Signal.HOLD
    return StrategyResult("20-day Breakout", signal, f"range={prior.min():.2f}–{prior.max():.2f}", price)


STRATEGIES = {"SMA Cross": sma_cross, "RSI Reversion": rsi_reversion, "20-day Breakout": breakout}

