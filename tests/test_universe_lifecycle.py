"""
测试模块 5: 晋级准入、迟滞缓冲与秒踢仲裁器 (Lifecycle Manager)
"""
from __future__ import annotations

from pathlib import Path
from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS
from futu_quant.store import TradingStore
from futu_quant.universe.lifecycle_manager import LifecycleManager


def test_promotion_and_eviction_lifecycle():
    db_path = Path("data/test_lifecycle.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        manager = LifecycleManager(store=store)

        # 录入沙盒达标数据
        store.upsert_incubator_metrics("US.PLTR", {
            "symbol": "US.PLTR",
            "observation_days": 10,
            "promotion_eligible": 1,
            "latest_ml_score": 0.88,
        })
        store.upsert_incubator_metrics("US.ARM", {
            "symbol": "US.ARM",
            "observation_days": 10,
            "promotion_eligible": 1,
            "latest_ml_score": 0.85,
        })

        # 1. 执行晋级评估
        promoted = manager.evaluate_promotions(
            ["US.PLTR", "US.ARM"],
            candidate_scores={"US.PLTR": 0.88, "US.ARM": 0.85},
        )
        assert "US.PLTR" in promoted
        assert "US.ARM" in promoted

        active_promoted = store.active_promoted_symbols()
        assert len(active_promoted) == 2

        # 2. 验证同步至运行时标的池 (核心 13 只 + 外援 2 只)
        merged = manager.sync_to_runtime_universe()
        assert len(merged) == len(DEFAULT_VOLATILITY_SYMBOLS) + 2
        assert "US.PLTR" in merged
        assert "US.ARM" in merged
        for core in DEFAULT_VOLATILITY_SYMBOLS:
            assert core in merged  # 底仓分毫不动

        # 3. 模拟触发秒踢红线 (连续止损)
        evicted = manager.evaluate_evictions(hard_stop_symbols={"US.PLTR"})
        assert "US.PLTR" in evicted

        # 4. 再次同步，US.PLTR 应被安全清除，恢复底仓 + ARM
        merged_after = manager.sync_to_runtime_universe()
        assert "US.PLTR" not in merged_after
        assert "US.ARM" in merged_after
    finally:
        db_path.unlink(missing_ok=True)
