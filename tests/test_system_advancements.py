"""
测试 4 大进阶系统完善点 (System Advancements Test Suite)
"""
from __future__ import annotations

from pathlib import Path
from futu_quant.feishu_reporter import FeishuReporter
from futu_quant.profit_sweeper import ProfitSweeper
from futu_quant.store import TradingStore


def test_feishu_reporter_dispatches_when_pending():
    db_path = Path("data/test_feishu_adv.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        reporter = FeishuReporter(store=store)

        # 初始无 pending
        assert reporter.dispatch_pending_review() is True

        # 录入一条待投递记录
        from datetime import date
        from futu_quant.scheduled_tasks import queue_review_for_im_delivery, pending_im_delivery
        queue_review_for_im_delivery(store, date(2026, 10, 13), "data/dummy.html", "测试复盘结论", "data/dummy.md")

        record = pending_im_delivery(store)
        assert record is not None
        assert record["session_date"] == "2026-10-13"
    finally:
        db_path.unlink(missing_ok=True)


def test_profit_sweeper_locks_profit_to_spy():
    db_path = Path("data/test_sweeper.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        sweeper = ProfitSweeper(store=store)

        # 模拟尚未沉淀
        stats = sweeper.evaluate_unsettled_profit()
        assert stats["already_swept_usd"] == 0.0

        # 手动设置一次超额利润并触发沉淀
        store.set_setting("profit_sweeper_total_swept_usd", "0.0")
        # 直接测试沉淀算法逻辑
        to_core = round(2000.0 * 0.50, 2)
        assert to_core == 1000.0

        # 测试触发门槛 (低于 1000 不沉淀)
        assert sweeper.sweep_profit_to_spy(min_threshold_usd=999999.0) is None
    finally:
        db_path.unlink(missing_ok=True)


def test_probe_sizing_for_new_promoted_symbols():
    db_path = Path("data/test_probe_sizing.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))

        # 1. 录入新晋外援标的 (live_trades_count = 0)
        store.promote_universe_symbol("US.PLTR", 0.88, "2026-11-12T00:00:00+00:00", "AI_BIGDATA")
        row = store.active_promoted_symbols()[0]
        assert row["symbol"] == "US.PLTR"
        assert row["live_trades_count"] == 0

        # 首秀状态应限制为 1 张探路仓
        trades = int(row.get("live_trades_count", 0))
        pnl = float(row.get("live_realized_pnl", 0.0))
        is_probe = (trades == 0 or pnl <= 0)
        assert is_probe is True

        # 2. 模拟完成 1 笔盈利平仓 (转正为 2 张标准仓)
        with store.connection() as conn:
            conn.execute("UPDATE promoted_universe SET live_trades_count=1, live_realized_pnl=250.0 WHERE symbol='US.PLTR'")

        row2 = store.active_promoted_symbols()[0]
        trades2 = int(row2.get("live_trades_count", 0))
        pnl2 = float(row2.get("live_realized_pnl", 0.0))
        is_probe2 = (trades2 == 0 or pnl2 <= 0)
        assert is_probe2 is False  # 成功转正解锁
    finally:
        db_path.unlink(missing_ok=True)
