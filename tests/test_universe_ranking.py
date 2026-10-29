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


class _FakeMLScorer:
    """替换 MLSymbolScorer：记录 score_symbol 调用，返回固定结果。

    回归背景：ranking_pipeline 曾错误调用不存在的 score_symbols(list,...)，
    异常被吞后 ML 先验恒为 0.50 平滑默认值（接口自 378d4ee 引入即不匹配）。
    """

    enabled = True

    def __init__(self, result):
        self.result = result
        self.calls: list[str] = []

    def score_symbol(self, symbol, broker, store):
        self.calls.append(symbol)
        return self.result


def _make_pipeline(store, result) -> tuple[RankingPipeline, _FakeMLScorer]:
    pipeline = RankingPipeline(store=store)
    fake = _FakeMLScorer(result)
    pipeline.scorer = fake
    return pipeline, fake


def test_pipeline_uses_singular_score_symbol():
    """模型有分时按单数 score_symbol 接口取值，不再走异常兜底。"""
    db_path = Path("data/test_ranking_call.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        pipeline, fake = _make_pipeline(store, result=0.82)
        df = pipeline.rank_candidates(["US.AAPL"], broker=object())
        assert fake.calls == ["US.AAPL"]
        assert df.iloc[0]["ml_score"] == 0.82
    finally:
        db_path.unlink(missing_ok=True)


def test_pipeline_na_score_falls_back_to_neutral():
    """score_symbol 返回 None（缺必需特征 NA）时回退 0.50 中性分，不抛异常。"""
    db_path = Path("data/test_ranking_na.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        pipeline, fake = _make_pipeline(store, result=None)
        df = pipeline.rank_candidates(["US.NVDA"], broker=object())
        assert fake.calls == ["US.NVDA"]
        assert df.iloc[0]["ml_score"] == 0.50
    finally:
        db_path.unlink(missing_ok=True)
