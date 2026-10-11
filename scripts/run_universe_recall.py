"""
周度全市场期权成交量 Top 50 粗排召回入口脚本
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.universe.recall_scanner import RecallScanner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run weekly option universe recall")
    parser.add_argument("--week-start", type=str, default=None, help="Week identifier YYYY-MM-DD")
    args = parser.parse_args()

    ny_now = datetime.now(ZoneInfo("America/New_York"))
    week_start = args.week_start or ny_now.date().isoformat()

    settings = Settings.load()
    broker = FutuPaperBroker(settings)
    store = TradingStore()

    scanner = RecallScanner(broker=broker, store=store)
    candidates = scanner.run_weekly_recall(week_start)

    print(f"=== 周度全市场期权流动性召回完成 [{week_start}] ===")
    print(f"筛选合格标的数: {len(candidates)}")
    for idx, c in enumerate(candidates[:15], 1):
        print(f"  {idx:2d}. {c.symbol:<10} ({c.underlying_name:<16}) | 行业: {c.industry:<15} | 日均期权量: {c.option_daily_volume:>8,.0f} | 平值价差: {c.median_spread_pct:.1%}")


if __name__ == "__main__":
    main()
