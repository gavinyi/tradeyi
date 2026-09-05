"""
机器学习标的评分器 - V3版本

使用训练好的模型为标的打分，预测其未来盈利概率
集成新的ml_scorer模块，支持期权特征解析
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .ml_scorer import (
    load_ml_model,
    predict_symbol_score,
    batch_predict_scores,
)

if TYPE_CHECKING:
    from .broker import FutuPaperBroker
    from .store import TradingStore

logger = logging.getLogger(__name__)


class MLSymbolScorer:
    """机器学习标的评分器 V3"""
    
    def __init__(
        self, 
        model_path: str | None = None,
        threshold: float | None = None,
        enabled: bool | None = None
    ):
        """
        初始化评分器
        
        Args:
            model_path: 模型文件路径，默认从环境变量 ML_MODEL_PATH 读取
            threshold: 预测阈值，默认从环境变量 ML_THRESHOLD 读取 (0.5)
            enabled: 是否启用ML评分，默认从环境变量 ML_ENABLED 读取 (true)
        """
        # 从环境变量读取默认值（兼容 .env 配置）
        import os
        if model_path is None:
            model_path = os.getenv("ML_MODEL_PATH", "data/ml_models/volatility_scorer_v3.pkl")
        if threshold is None:
            threshold = float(os.getenv("ML_THRESHOLD", "0.5"))
        if enabled is None:
            enabled = os.getenv("ML_ENABLED", "true").lower() == "true"
        
        self.model_path = Path(model_path)
        self.threshold = threshold
        self.model = None
        self.feature_cols = None
        self.enabled = enabled
        
        if not self.enabled:
            logger.info("⚠️  ML评分已禁用 (ML_ENABLED=false)")
            return
        
        try:
            self.load_model()
        except FileNotFoundError:
            logger.warning(f"⚠️  ML模型未找到: {model_path}")
            logger.warning("   将跳过机器学习评分，使用规则打分")
            self.enabled = False
        except Exception as e:
            logger.warning(f"⚠️  ML模型加载失败: {e}")
            logger.warning("   将跳过机器学习评分，使用规则打分")
            self.enabled = False
    
    def load_model(self):
        """加载训练好的模型"""
        self.model, self.feature_cols = load_ml_model(str(self.model_path))
        logger.info(f"✅ ML模型已加载: {self.model_path}")
        logger.info(f"   特征数: {len(self.feature_cols)}")
        logger.info(f"   阈值: {self.threshold}")
    
    def score_symbol(
        self,
        symbol: str,
        broker: FutuPaperBroker,
        store: TradingStore,
        trade_time: datetime | None = None
    ) -> float:
        """
        为单个标的打分
        
        Args:
            symbol: 标的代码（如US.AAPL240119C175000）
            broker: 经纪商接口（保留兼容性，当前未使用）
            store: 数据存储接口
            trade_time: 交易时间，默认当前时间
        
        Returns:
            0-1之间的分数，越高越可能盈利
        """
        if not self.enabled or self.model is None:
            return 0.5  # 默认中等分数
        
        try:
            score = predict_symbol_score(
                symbol,
                self.model,
                self.feature_cols,
                store,
                trade_time
            )
            return score
        except Exception as e:
            logger.error(f"ML评分失败 {symbol}: {e}")
            return 0.0  # 失败时返回最低分
    
    def rank_symbols(
        self,
        symbols: list[str],
        broker: FutuPaperBroker,
        store: TradingStore,
        trade_time: datetime | None = None
    ) -> pd.DataFrame:
        """
        对标的列表进行排名
        
        Returns:
            DataFrame with columns: [symbol, ml_score, ml_rank]
        """
        if not self.enabled or self.model is None:
            # 未启用时返回默认分数
            return pd.DataFrame([
                {'symbol': s, 'ml_score': 0.5, 'ml_rank': 'MEDIUM'}
                for s in symbols
            ])
        
        try:
            # 批量预测
            scores = batch_predict_scores(
                symbols,
                self.model,
                self.feature_cols,
                store,
                trade_time
            )
            
            # 构建结果
            results = []
            for symbol, score in scores.items():
                rank = 'HIGH' if score >= 0.7 else 'MEDIUM' if score >= 0.5 else 'LOW'
                results.append({
                    'symbol': symbol,
                    'ml_score': score,
                    'ml_rank': rank
                })
            
            df = pd.DataFrame(results).sort_values('ml_score', ascending=False)
            return df
            
        except Exception as e:
            logger.error(f"批量排名失败: {e}")
            return pd.DataFrame([
                {'symbol': s, 'ml_score': 0.0, 'ml_rank': 'LOW'}
                for s in symbols
            ])
    
    def filter_symbols(
        self,
        symbols: list[str],
        broker: FutuPaperBroker,
        store: TradingStore,
        min_score: float | None = None,
        trade_time: datetime | None = None
    ) -> list[str]:
        """
        过滤出高分标的
        
        Args:
            symbols: 候选标的列表
            broker: 经纪商接口
            store: 数据存储接口
            min_score: 最低分数阈值，默认使用self.threshold
            trade_time: 交易时间
        
        Returns:
            过滤后的标的列表（按分数降序）
        """
        if not self.enabled or self.model is None:
            logger.info("ML评分未启用，返回全部候选标的")
            return symbols
        
        if min_score is None:
            min_score = self.threshold
        
        try:
            ranked = self.rank_symbols(symbols, broker, store, trade_time)
            filtered = ranked[ranked['ml_score'] >= min_score]
            
            logger.info(f"ML筛选: {len(symbols)} → {len(filtered)} (阈值={min_score:.2f})")
            
            # 记录过滤详情
            for _, row in ranked.iterrows():
                symbol = row['symbol']
                score = row['ml_score']
                passed = score >= min_score
                status = "✅通过" if passed else "❌过滤"
                logger.info(f"  {status} {symbol}: ML分数={score:.3f}")
            
            return filtered['symbol'].tolist()
            
        except Exception as e:
            logger.error(f"ML过滤失败: {e}，返回全部候选")
            return symbols


def allocate_capital_by_ml_score(
    symbols: list[str],
    ml_scores: dict[str, float],
    total_capital: float,
    min_allocation: float = 0.02,  # 最小2%
    max_allocation: float = 0.15   # 最大15%
) -> dict[str, float]:
    """
    根据ML分数动态分配资金
    
    高分标的获得更多资金，低分标的减少资金
    
    Args:
        symbols: 标的列表
        ml_scores: 标的分数字典 {symbol: score}
        total_capital: 总资金
        min_allocation: 最小分配比例
        max_allocation: 最大分配比例
    
    Returns:
        {symbol: allocated_capital} 字典
    """
    if not symbols or not ml_scores:
        return {}
    
    # 归一化分数
    scores = [ml_scores.get(s, 0.5) for s in symbols]
    total_score = sum(scores)
    
    if total_score == 0:
        # 均分
        equal_share = total_capital / len(symbols)
        return {s: equal_share for s in symbols}
    
    allocations = {}
    for symbol, score in zip(symbols, scores):
        # 按分数比例分配
        raw_allocation = (score / total_score) * total_capital
        
        # 限制在min/max范围内
        min_amount = min_allocation * total_capital
        max_amount = max_allocation * total_capital
        
        final_allocation = max(min_amount, min(max_amount, raw_allocation))
        allocations[symbol] = final_allocation
    
    # 归一化到总资金
    total_allocated = sum(allocations.values())
    if total_allocated > 0:
        scale_factor = total_capital / total_allocated
        allocations = {s: amt * scale_factor for s, amt in allocations.items()}
    
    return allocations
