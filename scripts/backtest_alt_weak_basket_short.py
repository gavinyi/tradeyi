from __future__ import annotations

import argparse

import pandas as pd

from futu_quant.crypto_alt_basket_short import (
    AltWeakBasketShortConfig,
    rank_alt_weakness,
    run_alt_weak_basket_short_backtest,
)
from futu_quant.okx_broker import OkxDemoBroker
from futu_quant.okx_config import OkxSettings


def _symbols(text: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in text.split(",") if item.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="回测山寨币弱势篮子做空策略")
    parser.add_argument("--limit", type=int, default=720, help="1H K线数量")
    parser.add_argument("--cash", type=float, default=10_000, help="初始资金")
    parser.add_argument(
        "--candidates",
        default="SOL-USDT-SWAP,DOGE-USDT-SWAP,XRP-USDT-SWAP,ADA-USDT-SWAP,AVAX-USDT-SWAP,LINK-USDT-SWAP",
        help="候选山寨币合约，逗号分隔",
    )
    parser.add_argument("--basket-size", type=int, default=3)
    parser.add_argument("--lookback", type=int, default=72)
    parser.add_argument("--rebalance", type=int, default=6)
    parser.add_argument("--min-weakness", type=float, default=0.06)
    parser.add_argument("--market-max-return", type=float, default=0.015)
    parser.add_argument("--market-short-max-return", type=float, default=0.005)
    parser.add_argument("--candidate-min-short-return", type=float, default=-0.08)
    parser.add_argument("--candidate-max-short-return", type=float, default=0.0)
    parser.add_argument("--candidate-min-lookback-return", type=float, default=-0.005)
    parser.add_argument("--max-hold", type=int, default=12)
    args = parser.parse_args()

    settings = OkxSettings.from_env()
    broker = OkxDemoBroker(settings)
    config = AltWeakBasketShortConfig(
        candidate_symbols=_symbols(args.candidates),
        basket_size=args.basket_size,
        lookback_bars=args.lookback,
        rebalance_bars=args.rebalance,
        min_relative_weakness=args.min_weakness,
        market_max_return=args.market_max_return,
        market_short_max_return=args.market_short_max_return,
        candidate_min_short_return=args.candidate_min_short_return,
        candidate_max_short_return=args.candidate_max_short_return,
        candidate_min_lookback_return=args.candidate_min_lookback_return,
        max_hold_bars=args.max_hold,
    )
    symbols = tuple(dict.fromkeys((*config.market_symbols, *config.candidate_symbols)))
    data = {}
    for symbol in symbols:
        print(f"读取 {symbol} 1H K线...")
        data[symbol] = broker.historical_candles(symbol, bar="1H", limit=args.limit)

    result = run_alt_weak_basket_short_backtest(data, args.cash, config)
    ranking = rank_alt_weakness(data, config)
    metrics = result.metrics
    print("\n=== 山寨币弱势篮子做空回测 ===")
    print(f"样本: {args.limit} 根 1H K线")
    print(f"总收益: {metrics['total_return']:.2%}")
    print(f"BTC/ETH基准: {metrics['benchmark_return']:.2%}")
    print(f"超额收益: {metrics['excess_return']:.2%}")
    print(f"最大回撤: {metrics['max_drawdown']:.2%}")
    print(f"交易记录: {metrics['trade_count']:.0f}")
    print(f"已平仓: {metrics['closed_trades']:.0f}")
    print(f"胜率: {metrics['win_rate']:.2%}")
    print(f"盈利因子: {metrics['profit_factor']:.2f}")
    print(f"篮子轮数: {metrics.get('basket_count', 0):.0f}")
    print(f"未平仓数量: {metrics.get('open_positions', 0):.0f}")
    print(
        "参数: "
        f"篮子={config.basket_size}, 相对弱势≥{config.min_relative_weakness:.2%}, "
        f"长市场≤{config.market_max_return:.2%}, "
        f"短市场≤{config.market_short_max_return:.2%}, "
        f"候选短周期={config.candidate_min_short_return:.2%}"
        f"~{config.candidate_max_short_return:.2%}, "
        f"最长持有={config.max_hold_bars}根"
    )

    if not result.trades.empty:
        trades = result.trades.copy()
        trades["时间"] = pd.to_datetime(
            trades["timestamp"], unit="ms", utc=True
        ).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M")
        display_columns = [
            "时间", "side", "symbol", "basket_id", "price", "quantity",
            "notional", "pnl", "pnl_ratio", "reason",
        ]
        available = [column for column in display_columns if column in trades]
        print("\n=== 最近交易 ===")
        print(trades[available].tail(20).to_string(index=False))

        closed = trades[trades["side"].eq("CLOSE_SHORT")].copy()
        if not closed.empty:
            summary = closed.groupby("symbol").agg(
                平仓次数=("pnl", "size"),
                总盈亏=("pnl", "sum"),
                平均收益率=("pnl_ratio", "mean"),
                胜率=("pnl", lambda values: float((values > 0).mean())),
            )
            print("\n=== 按标的汇总 ===")
            print(summary.sort_values("总盈亏", ascending=False).to_string())

    if not ranking.empty:
        print("\n=== 当前相对弱势排序 ===")
        print(
            ranking[
                [
                    "symbol", "own_return", "short_return", "market_return",
                    "relative_strength", "weakness", "ema_downtrend",
                ]
            ]
            .head(10)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
