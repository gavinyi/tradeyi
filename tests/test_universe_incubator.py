"""
测试模块 2: 隔离沙盒影子演练与微观特征提取器 (Sandbox Incubator)
"""
from __future__ import annotations

from pathlib import Path
from futu_quant.store import TradingStore
from futu_quant.universe.incubator_engine import IncubatorEngine


def test_incubator_signal_and_trade_aggregation():
    db_path = Path("data/test_incubator.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        engine = IncubatorEngine(store=store)

        # 1. 模拟记录 3 次突破信号
        engine.record_simulated_signal("US.PLTR", z_score=2.3, volume_ratio=1.5, spread_pct=0.02)
        engine.record_simulated_signal("US.PLTR", z_score=2.5, volume_ratio=1.8, spread_pct=0.018)
        engine.record_simulated_signal("US.PLTR", z_score=2.1, volume_ratio=1.3, spread_pct=0.022)

        data = store.incubator_symbol("US.PLTR")
        assert data is not None
        assert data["signal_triggers"] == 3

        # 2. 模拟 2 胜 1 负的虚拟交易
        engine.update_simulated_trade("US.PLTR", is_win=True, pnl_usd=150.0, return_ratio=0.15, slippage_pct=0.015, volume_retention=0.75)
        engine.update_simulated_trade("US.PLTR", is_win=True, pnl_usd=200.0, return_ratio=0.20, slippage_pct=0.018, volume_retention=0.80)
        engine.update_simulated_trade("US.PLTR", is_win=False, pnl_usd=-80.0, return_ratio=-0.08, slippage_pct=0.020, volume_retention=0.65)

        updated = store.incubator_symbol("US.PLTR")
        assert updated["simulated_wins"] == 2
        assert updated["simulated_losses"] == 1
        assert round(updated["simulated_pnl_usd"], 2) == 270.0
        assert updated["simulated_max_dd"] < 0.25

        # 3. 模拟满 10 日且高分后晋级门槛评估
        updated["latest_ml_score"] = 0.82
        store.upsert_incubator_metrics("US.PLTR", updated)

        eligible = engine.evaluate_daily_metrics("US.PLTR", current_days=10)
        assert eligible is True

        final_data = store.incubator_symbol("US.PLTR")
        assert final_data["promotion_eligible"] == 1

        # 4. 若回撤超过 25%，应一票否决
        final_data["simulated_max_dd"] = 0.28
        store.upsert_incubator_metrics("US.PLTR", final_data)
        eligible_fail = engine.evaluate_daily_metrics("US.PLTR", current_days=10)
        assert eligible_fail is False
    finally:
        db_path.unlink(missing_ok=True)
