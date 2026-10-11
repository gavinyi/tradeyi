"""
测试模块 1: 全市场流动性 Top 50 粗排召回器 (Recall Scanner)
"""
from __future__ import annotations

from pathlib import Path
from futu_quant.store import TradingStore
from futu_quant.universe.recall_scanner import RecallScanner


def test_recall_filters_core_symbols_and_negative_screen():
    db_path = Path("data/test_recall.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        scanner = RecallScanner(store=store)

        mock_candidates = [
            # 1. 正常高流动性标的 (应入选)
            {"symbol": "US.PLTR", "option_daily_volume": 200000, "open_interest": 800000, "stock_turnover_usd": 1500000000, "median_spread_pct": 0.02},
            {"symbol": "US.ARM", "option_daily_volume": 120000, "open_interest": 500000, "stock_turnover_usd": 1000000000, "median_spread_pct": 0.025},
            # 2. 已在 Core 13 只底仓中 (应排除)
            {"symbol": "US.NVDA", "option_daily_volume": 1000000, "open_interest": 4000000, "stock_turnover_usd": 10000000000, "median_spread_pct": 0.01},
            {"symbol": "US.COIN", "option_daily_volume": 300000, "open_interest": 1000000, "stock_turnover_usd": 2000000000, "median_spread_pct": 0.02},
            # 3. 黑名单失血标的 (应排除)
            {"symbol": "US.TSLA", "option_daily_volume": 800000, "open_interest": 3000000, "stock_turnover_usd": 7000000000, "median_spread_pct": 0.02},
            {"symbol": "US.SPCX", "option_daily_volume": 10000, "open_interest": 20000, "stock_turnover_usd": 30000000, "median_spread_pct": 0.15},
            {"symbol": "US.KO", "option_daily_volume": 40000, "open_interest": 200000, "stock_turnover_usd": 300000000, "median_spread_pct": 0.035},
            # 4. 价差过大或成交量不足 (应排除)
            {"symbol": "US.SMALL", "option_daily_volume": 20000, "open_interest": 50000, "stock_turnover_usd": 600000000, "median_spread_pct": 0.02},
            {"symbol": "US.WIDE", "option_daily_volume": 80000, "open_interest": 300000, "stock_turnover_usd": 800000000, "median_spread_pct": 0.045},
        ]

        filtered = scanner.filter_candidates(mock_candidates)
        symbols = [c.symbol for c in filtered]

        assert "US.PLTR" in symbols
        assert "US.ARM" in symbols
        assert "US.NVDA" not in symbols  # 底仓排除
        assert "US.COIN" not in symbols  # 底仓排除
        assert "US.TSLA" not in symbols  # 黑名单排除
        assert "US.SPCX" not in symbols  # 黑名单排除
        assert "US.KO" not in symbols    # 黑名单排除
        assert "US.SMALL" not in symbols # 量不足排除
        assert "US.WIDE" not in symbols  # 价差过大排除
    finally:
        db_path.unlink(missing_ok=True)


def test_weekly_recall_persists_to_store():
    db_path = Path("data/test_recall_persist.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        scanner = RecallScanner(store=store)

        candidates = scanner.run_weekly_recall("2026-10-12")
        assert len(candidates) > 0

        saved = store.candidate_symbols("2026-10-12")
        assert len(saved) == len(candidates)

        events = store.universe_lifecycle_events()
        assert any(e["event_type"] == "RECALL_COMPLETED" for e in events)
    finally:
        db_path.unlink(missing_ok=True)
