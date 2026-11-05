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
    """score_symbol 返回 None（缺必需特征 NA）且零结算时回退 0.50 中性分，不抛异常。"""
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


def _seed_incubator(store: TradingStore, symbol: str, wins: int, losses: int,
                    pnl: float, dd: float = 0.0) -> None:
    store.upsert_incubator_metrics(symbol, {
        "symbol": symbol,
        "observation_days": 10,
        "signal_triggers": wins + losses + 1,
        "simulated_wins": wins,
        "simulated_losses": losses,
        "simulated_pnl_usd": pnl,
        "simulated_max_dd": dd,
        "avg_slippage_pct": 0.015,
        "volume_retention_ratio": 0.75,
        "latest_ml_score": 0.50,
        "promotion_eligible": 0,
    })


def test_single_settlement_breaks_the_0_5_plateau():
    """R3 根因修复：1 笔结算也必须用赛马实绩重算，不再恒 0.50。

    生产实测：shadow_incubator 8/8 行恒 0.50——v3 模型对股票 symbol 必然
    MissingFeatureError → base_score=0.5，而旧门槛 total>=3 让 1-2 笔结算
    的标的永远走不到赛马分支。
    """
    db_path = Path("data/test_ranking_r3.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        # 1 胜 0 负 +$48：赛马分 = 1.0*0.7 + (0.5+0.48)*0.3 = 0.994 →
        # final = 0.5*0.3 + 0.994*0.7 ≈ 0.8458，明显偏离 0.50
        _seed_incubator(store, "US.WIN1", wins=1, losses=0, pnl=48.0)
        # 0 胜 1 负 -$6.6：赛马分 = 0*0.7 + 0.434*0.3 ≈ 0.1302 →
        # final ≈ 0.15 + 0.091 ≈ 0.2412，同样偏离 0.50
        _seed_incubator(store, "US.LOSS1", wins=0, losses=1, pnl=-6.64)
        pipeline = RankingPipeline(store=store)
        df = pipeline.rank_candidates(["US.WIN1", "US.LOSS1"])
        scores = dict(zip(df["symbol"], df["ml_score"]))
        assert scores["US.WIN1"] != 0.50, "1 笔结算的胜者仍卡在模型先验 0.50"
        assert scores["US.LOSS1"] != 0.50, "1 笔结算的败者仍卡在模型先验 0.50"
        assert scores["US.WIN1"] > scores["US.LOSS1"]
        assert scores["US.WIN1"] > 0.75, "1 胜 +$48 应显著高于中性（旧门槛下恒 0.50）"
        assert scores["US.LOSS1"] < 0.50
    finally:
        db_path.unlink(missing_ok=True)


def test_zero_settlement_stays_neutral():
    """零结算且模型 NA：无实绩可算，保持 0.50 中性分（不编造差异）。"""
    db_path = Path("data/test_ranking_r3_zero.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        _seed_incubator(store, "US.IDLE", wins=0, losses=0, pnl=0.0)
        pipeline = RankingPipeline(store=store)
        df = pipeline.rank_candidates(["US.IDLE"])
        assert df.iloc[0]["ml_score"] == 0.50
    finally:
        db_path.unlink(missing_ok=True)


def test_mutation_three_settlement_threshold_must_be_red():
    """变异守卫：把赛马门槛改回 total>=3，1 笔结算用例必须红（防回退）。"""
    import shutil
    import subprocess
    import sys

    pipeline_path = Path("src/futu_quant/universe/ranking_pipeline.py")
    original = pipeline_path.read_text(encoding="utf-8")
    assert "if total >= 1:" in original, "R3 门槛语义丢失，无法构造变异"
    mutated = original.replace("if total >= 1:", "if total >= 3:")
    assert mutated != original
    db_path = Path("data/test_ranking_r3_mut.db")
    try:
        pipeline_path.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_single_settlement_breaks_the_0_5_plateau"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            "门槛变异后 1 笔结算用例仍绿 —— 守卫失效（R3 修复可被静默回退）"
        )
    finally:
        pipeline_path.write_text(original, encoding="utf-8")
        db_path.unlink(missing_ok=True)
        shutil.rmtree(".pytest_cache", ignore_errors=True)
