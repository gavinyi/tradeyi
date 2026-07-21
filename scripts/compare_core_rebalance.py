import pandas as pd

from futu_quant.backtest import run_core_satellite_backtest
from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings


settings = Settings.load()
broker = FutuPaperBroker(settings)
symbols = settings.watchlist
required = tuple(dict.fromkeys(("US.SPY", *symbols)))
frames = {
    symbol: broker.candles_range(symbol, "2018-01-01", "2026-07-14")
    for symbol in required
}
rows = []
for name, frequency, tolerance, minimum in [
    ("调整前-每日", "daily", 0.0, 500),
    ("调整后-每周", "weekly", 0.02, 1000),
    ("备选-每月", "monthly", 0.02, 1000),
]:
    result = run_core_satellite_backtest(
        frames, symbols, 100_000, min_trade_usd=minimum, slippage_bps=2,
        profile="aggressive", rebalance_frequency=frequency, weight_tolerance=tolerance,
    )
    equity = result.equity.copy()
    equity["date"] = pd.to_datetime(equity["date"])
    equity = equity.set_index("date")
    full = equity["equity"]
    test = equity.loc[equity.index >= "2023-01-01", "equity"]
    rows.append(
        {
            "方案": name, "全期收益": full.iloc[-1] / full.iloc[0] - 1,
            "全期回撤": (full / full.cummax() - 1).min(),
            "2023后收益": test.iloc[-1] / test.iloc[0] - 1,
            "2023后回撤": (test / test.cummax() - 1).min(),
            "交易次数": len(result.trades),
            "平均仓位": (equity["stock_value"] / equity["equity"]).mean(),
        }
    )
print(pd.DataFrame(rows).to_string(index=False))
