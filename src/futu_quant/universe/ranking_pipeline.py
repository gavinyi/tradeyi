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
                # MLSymbolScorer 只有单数的 score_symbol(symbol, broker, store)，
                # 返回 float | None（None=缺必需特征，需回退规则/平滑默认）。
                ml_score = self.scorer.score_symbol(symbol, broker, self.store)
                if ml_score is not None:
                    base_score = float(ml_score)
        except Exception as e:
            logger.warning("标的 %s 调用模型打分异常，使用平滑默认值: %s", symbol, e)
            base_score = 0.50

        # 至少三笔真实虚拟结算后，以赛马实绩为主（60%），模型先验为辅（40%）。
        # 这使长期固定 0.5 的缺模型场景也能凭可审计胜负/PnL 形成差异化评分。
        wins = int(incubator.get("simulated_wins", 0))
        losses = int(incubator.get("simulated_losses", 0))
        total = wins + losses
        if total >= 3:
            win_rate = wins / total
            max_dd = float(incubator.get("simulated_max_dd", 0.0))
            pnl = float(incubator.get("simulated_pnl_usd", 0.0))
            pnl_score = max(0.0, min(1.0, 0.50 + pnl / 100.0))
            race_score = win_rate * 0.7 + pnl_score * 0.3
            dd_penalty = max(0.0, max_dd - 0.15) * 0.50
            final_score = max(0.05, min(0.98, base_score * 0.3 + race_score * 0.7 - dd_penalty))
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
