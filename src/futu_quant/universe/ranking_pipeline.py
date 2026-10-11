"""
模块 3: 机器学习精排多维特征提取与模型打分器 (Ranking Pipeline)
"""
from __future__ import annotations

import logging
import os
import pandas as pd

from futu_quant.ml_symbol_scorer import MLSymbolScorer
from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)


class RankingPipeline:
    """机器学习精排评分流水线"""

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float = 0.50,
        store: TradingStore | None = None,
    ):
        self.store = store or TradingStore()
        path = model_path or os.getenv("ML_MODEL_PATH", "data/ml_models/volatility_scorer_v3.pkl")
        self.scorer = MLSymbolScorer(model_path=path, threshold=threshold, enabled=True)

    def extract_features(self, symbol: str, broker=None) -> dict[str, float]:
        """构建对齐 V3 评分器的特征字典，并融合沙盒微观微结构数据。"""
        incubator = self.store.incubator_symbol(symbol) or {}
        wins = int(incubator.get("simulated_wins", 0))
        losses = int(incubator.get("simulated_losses", 0))
        total_trades = wins + losses
        win_rate = wins / total_trades if total_trades > 0 else 0.50

        pnl = float(incubator.get("simulated_pnl_usd", 0.0))
        slippage = float(incubator.get("avg_slippage_pct", 0.025))
        retention = float(incubator.get("volume_retention_ratio", 0.70))

        # 基础特征与沙盒衍生特征
        return {
            "symbol": symbol,
            "win_rate": win_rate,
            "simulated_pnl": pnl,
            "avg_slippage": slippage,
            "volume_retention": retention,
            "observation_days": int(incubator.get("observation_days", 0)),
        }

    def score_symbol(self, symbol: str, broker=None) -> float:
        """为单一标的打分，返回 0.0 ~ 1.0 的预测分。"""
        incubator = self.store.incubator_symbol(symbol) or {}
        # 若已有沙盒数据，综合基础评分与沙盒胜率表现
        base_score = 0.50
        try:
            if broker and self.scorer.enabled:
                score_dict = self.scorer.score_symbols([symbol], broker, self.store)
                base_score = float(score_dict.get(symbol, 0.50))
        except Exception as e:
            logger.warning("标的 %s 调用模型打分异常，使用平滑默认值: %s", symbol, e)
            base_score = 0.50

        # 沙盒胜率与回撤加权微调:
        # 胜率每高于 50% 额外奖励最高 0.15，回撤大于 15% 实施惩罚
        wins = int(incubator.get("simulated_wins", 0))
        losses = int(incubator.get("simulated_losses", 0))
        total = wins + losses
        if total >= 3:
            win_rate = wins / total
            max_dd = float(incubator.get("simulated_max_dd", 0.0))
            score_adj = (win_rate - 0.50) * 0.30 - max(0.0, max_dd - 0.15) * 0.50
            final_score = max(0.05, min(0.98, base_score * 0.7 + (0.50 + score_adj) * 0.3))
        else:
            final_score = base_score

        # 更新回写至 shadow_incubator
        if incubator:
            incubator["latest_ml_score"] = final_score
            self.store.upsert_incubator_metrics(symbol, incubator)

        return final_score

    def rank_candidates(self, symbols: list[str], broker=None) -> pd.DataFrame:
        """对候选标的批量打分并降序输出精排榜单。"""
        results = []
        for symbol in symbols:
            score = self.score_symbol(symbol, broker=broker)
            rank = "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.50 else "LOW"
            results.append({
                "symbol": symbol,
                "ml_score": round(score, 4),
                "ml_rank": rank,
            })

        df = pd.DataFrame(results)
        if not df.empty:
            df = df.sort_values("ml_score", ascending=False).reset_index(drop=True)
        return df
