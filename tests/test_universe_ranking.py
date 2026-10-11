"""
测试模块 3: 机器学习精排多维特征提取与模型打分器 (Ranking Pipeline)
"""
from __future__ import annotations

from pathlib import Path
from futu_quant.store import TradingStore
from futu_quant.universe.ranking_pipeline import RankingPipeline


def test_ranking_pipeline_scores_and_ranks():
    db_path = Path("data/test_ranking.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        pipeline = RankingPipeline(store=store)

        # 录入两只标的沙盒数据，US.PLTR (胜率75%) vs US.WEAK (胜率20%)
        store.upsert_incubator_metrics("US.PLTR", {
            "symbol": "US.PLTR",
            "observation_days": 10,
            "signal_triggers": 4,
            "simulated_wins": 3,
            "simulated_losses": 1,
            "simulated_pnl_usd": 400.0,
            "simulated_max_dd": 0.08,
            "avg_slippage_pct": 0.015,
            "volume_retention_ratio": 0.75,
            "latest_ml_score": 0.85,
            "promotion_eligible": 1,
        })
        store.upsert_incubator_metrics("US.WEAK", {
            "symbol": "US.WEAK",
            "observation_days": 10,
            "signal_triggers": 5,
            "simulated_wins": 1,
            "simulated_losses": 4,
            "simulated_pnl_usd": -250.0,
            "simulated_max_dd": 0.22,
            "avg_slippage_pct": 0.035,
            "volume_retention_ratio": 0.50,
            "latest_ml_score": 0.35,
            "promotion_eligible": 0,
        })

        ranked_df = pipeline.rank_candidates(["US.PLTR", "US.WEAK"])
        assert len(ranked_df) == 2
        assert ranked_df.iloc[0]["symbol"] == "US.PLTR"
        assert ranked_df.iloc[0]["ml_score"] > ranked_df.iloc[1]["ml_score"]
    finally:
        db_path.unlink(missing_ok=True)
