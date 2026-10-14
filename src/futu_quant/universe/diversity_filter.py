"""
模块 4: 行业配额与相关性打散重排器 (Diversity & Re-ranking Filter)
"""
from __future__ import annotations

import logging
import pandas as pd

from .recall_scanner import INDUSTRY_MAPPING

logger = logging.getLogger(__name__)


class DiversityFilter:
    """MMR 贪心打散与行业配额重排器"""

    def __init__(
        self,
        max_satellites: int = 5,
        correlation_threshold: float = 0.70,
        lambda_tradeoff: float = 0.65,
    ):
        self.max_satellites = max_satellites
        self.correlation_threshold = correlation_threshold
        self.lambda_tradeoff = lambda_tradeoff

    def resolve_capacity(self, candidates: pd.DataFrame) -> int:
        """根据当前市场优质黑马的充沛程度自适应决定外援池容量 (5 ~ 10只)。"""
        if candidates.empty or "ml_score" not in candidates.columns:
            return self.max_satellites
        high_score_count = int((candidates["ml_score"] >= 0.75).sum())
        if high_score_count >= 8:
            return 10
        if high_score_count >= 5:
            return 8
        return max(5, self.max_satellites)

    def select_diverse_portfolio(
        self,
        ranked_df: pd.DataFrame,
        corr_matrix: pd.DataFrame | None = None,
        industry_map: dict[str, str] | None = None,
        adaptive_capacity: bool = True,
    ) -> list[str]:
        """
        实施 MMR 打散算法与行业配额约束:
        1. 优质标的爆发时自适应动态扩容至 5 ~ 10 只;
        2. 同一细分行业最多 1 个外援席位 (INDEX_ETF 除外);
        3. 两两 60 日收益率相关系数严格 <= 0.70;
        4. 输出最终攻守兼备的外援名单。
        """
        if ranked_df.empty or "symbol" not in ranked_df.columns:
            return []

        mapping = industry_map or INDUSTRY_MAPPING
        selected: list[str] = []
        selected_industries: set[str] = set()

        candidates = ranked_df.copy()
        if "ml_score" not in candidates.columns:
            candidates["ml_score"] = 0.50

        effective_capacity = self.resolve_capacity(candidates) if adaptive_capacity else self.max_satellites

        # 遍历候选标的
        for _, row in candidates.iterrows():
            if len(selected) >= effective_capacity:
                break

            symbol = str(row["symbol"]).strip().upper()
            industry = mapping.get(symbol, "OTHER")

            # 1. 行业配额检查 (避免例如 4 只全为芯片股发生踩踏)
            if industry != "INDEX_ETF" and industry in selected_industries:
                logger.debug("标的 %s 因行业 %s 已有配额被跳过", symbol, industry)
                continue

            # 2. 相关性打散检查
            conflict = False
            if corr_matrix is not None and not corr_matrix.empty and symbol in corr_matrix.index:
                for existing in selected:
                    if existing in corr_matrix.columns:
                        corr = float(corr_matrix.loc[symbol, existing])
                        if corr > self.correlation_threshold:
                            conflict = True
                            logger.debug("标的 %s 与已有标的 %s 相关性 %.2f 过高被跳过", symbol, existing, corr)
                            break
            if conflict:
                continue

            selected.append(symbol)
            if industry != "INDEX_ETF":
                selected_industries.add(industry)

        return selected
