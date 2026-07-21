from __future__ import annotations

import pandas as pd

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.volatility_optimizer import volatility_regime


SYMBOLS = ("US.AAPL", "US.NVDA", "US.AMD", "US.TSLA", "US.META", "US.SPY", "US.QQQ")


def main() -> None:
    broker = FutuPaperBroker(Settings.load())
    summaries = []
    for symbol in SYMBOLS:
        bars = broker.candles_range(symbol, "2024-01-01", "2026-07-15").sort_values("time_key").reset_index(drop=True)
        trades = []
        signals = 0
        for index in range(60, len(bars) - 1):
            history = bars.loc[:index].copy()
            current = float(history.iloc[-1]["close"])
            previous = float(history.iloc[-2]["close"])
            regime = volatility_regime({"last_price": current, "prev_close_price": previous}, history, 2.0)
            if not regime["active"]:
                continue
            signals += 1
            next_return = float(bars.iloc[index + 1]["close"]) / current - 1
            direction = 1 if regime["direction_score"] > 0 else -1
            # Defined-risk option proxy: directional 3x exposure with production loss/profit caps.
            trades.append(max(-.40, min(.50, direction * next_return * 3)))
        series = pd.Series(trades, dtype=float)
        curve = (1 + series).cumprod()
        drawdown = curve / curve.cummax() - 1 if len(curve) else pd.Series(dtype=float)
        summaries.append({
            "标的": symbol, "有效信号": signals, "胜率": float((series > 0).mean()) if len(series) else 0,
            "平均单次代理收益": float(series.mean()) if len(series) else 0,
            "中位代理收益": float(series.median()) if len(series) else 0,
            "最大回撤": float(drawdown.min()) if len(drawdown) else 0,
            "累计代理收益": float(curve.iloc[-1] - 1) if len(curve) else 0,
        })
    result = pd.DataFrame(summaries).sort_values(["平均单次代理收益", "胜率"], ascending=False)
    print(result.to_string(index=False))
    print("\n说明：只用正股日线验证动态状态与方向，不代表历史期权实际收益；未包含历史IV、价差和盘中路径。")


if __name__ == "__main__":
    main()
