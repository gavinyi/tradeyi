from __future__ import annotations

import pandas as pd

from futu_quant.crypto_alt_basket_short import (
    AltWeakBasketShortConfig,
    rank_alt_weakness,
    run_alt_weak_basket_short_backtest,
)


def _bars(changes: list[float], start: float = 100.0) -> pd.DataFrame:
    price = start
    rows = []
    for index, change in enumerate(changes):
        open_price = price
        price *= 1 + change
        rows.append({
            "timestamp": index * 3_600_000,
            "open": open_price,
            "high": max(open_price, price) * 1.003,
            "low": min(open_price, price) * 0.997,
            "close": price,
            "volume": 1000,
            "confirm": True,
        })
    return pd.DataFrame(rows)


def test_rank_alt_weakness_sorts_by_relative_underperformance():
    changes = [0.0] + [0.001] * 80
    data = {
        "BTC-USDT-SWAP": _bars(changes),
        "ETH-USDT-SWAP": _bars(changes),
        "SOL-USDT-SWAP": _bars([0.0] + [-0.002] * 80),
        "DOGE-USDT-SWAP": _bars([0.0] + [-0.004] * 80),
    }
    ranking = rank_alt_weakness(
        data,
        AltWeakBasketShortConfig(
            candidate_symbols=("SOL-USDT-SWAP", "DOGE-USDT-SWAP"),
            lookback_bars=48,
        ),
    )
    assert ranking.iloc[0]["symbol"] == "DOGE-USDT-SWAP"
    assert ranking.iloc[0]["relative_strength"] < ranking.iloc[1]["relative_strength"]


def test_alt_weak_basket_opens_and_closes_profitable_short():
    market = [0.0] + [-0.0005] * 90 + [0.0] * 30
    weak_then_more_weak = [0.0] + [-0.003] * 90 + [-0.004] * 30
    data = {
        "BTC-USDT-SWAP": _bars(market),
        "ETH-USDT-SWAP": _bars(market),
        "DOGE-USDT-SWAP": _bars(weak_then_more_weak),
        "XRP-USDT-SWAP": _bars(weak_then_more_weak),
    }
    result = run_alt_weak_basket_short_backtest(
        data,
        10_000,
        AltWeakBasketShortConfig(
            candidate_symbols=("DOGE-USDT-SWAP", "XRP-USDT-SWAP"),
            lookback_bars=48,
            rebalance_bars=4,
            basket_size=2,
            take_profit=0.025,
            max_hold_bars=48,
        ),
    )
    closes = result.trades[result.trades["side"].eq("CLOSE_SHORT")]
    assert len(closes) >= 1
    assert result.metrics["total_return"] > 0


def test_alt_weak_basket_blocks_short_when_majors_are_strong():
    data = {
        "BTC-USDT-SWAP": _bars([0.0] + [0.004] * 100),
        "ETH-USDT-SWAP": _bars([0.0] + [0.004] * 100),
        "DOGE-USDT-SWAP": _bars([0.0] + [-0.003] * 100),
    }
    result = run_alt_weak_basket_short_backtest(
        data,
        10_000,
        AltWeakBasketShortConfig(
            candidate_symbols=("DOGE-USDT-SWAP",),
            lookback_bars=48,
            market_max_return=0.01,
        ),
    )
    assert result.trades.empty
    assert result.metrics["total_return"] == 0
