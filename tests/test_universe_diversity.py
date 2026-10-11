"""
测试模块 4: 行业配额与相关性打散重排器 (Diversity Filter)
"""
from __future__ import annotations

import pandas as pd
from futu_quant.universe.diversity_filter import DiversityFilter


def test_diversity_filter_enforces_industry_and_correlation_limits():
    filter_engine = DiversityFilter(max_satellites=3, correlation_threshold=0.70)

    # 模拟打分：4 只半导体（ARM, MU, QCOM, INTC）以及 1 只 AI 软件（PLTR）、1 只加密（MSTR）
    ranked_df = pd.DataFrame([
        {"symbol": "US.ARM", "ml_score": 0.90},   # 半导体 1
        {"symbol": "US.MU", "ml_score": 0.88},    # 半导体 2 (应因行业配额跳过)
        {"symbol": "US.PLTR", "ml_score": 0.85},  # AI 软件 (应入选)
        {"symbol": "US.MSTR", "ml_score": 0.82},  # 加密 (应入选)
        {"symbol": "US.QCOM", "ml_score": 0.80},  # 半导体 3 (已满)
    ])

    industry_map = {
        "US.ARM": "SEMICONDUCTOR",
        "US.MU": "SEMICONDUCTOR",
        "US.PLTR": "AI_BIGDATA",
        "US.MSTR": "CRYPTO",
        "US.QCOM": "SEMICONDUCTOR",
    }

    # 模拟相关性矩阵：ARM 与 MU 相关性高达 0.85，与 PLTR 0.40
    corr_data = {
        "US.ARM": [1.0, 0.85, 0.40, 0.35, 0.80],
        "US.MU": [0.85, 1.0, 0.42, 0.30, 0.82],
        "US.PLTR": [0.40, 0.42, 1.0, 0.55, 0.45],
        "US.MSTR": [0.35, 0.30, 0.55, 1.0, 0.38],
        "US.QCOM": [0.80, 0.82, 0.45, 0.38, 1.0],
    }
    symbols = ["US.ARM", "US.MU", "US.PLTR", "US.MSTR", "US.QCOM"]
    corr_matrix = pd.DataFrame(corr_data, index=symbols)

    selected = filter_engine.select_diverse_portfolio(ranked_df, corr_matrix, industry_map)

    # 只能有 1 只半导体，以及不同行业的另外 2 只
    assert len(selected) == 3
    assert selected[0] == "US.ARM"
    assert "US.MU" not in selected   # 同行业被剔除
    assert "US.PLTR" in selected    # 入选
    assert "US.MSTR" in selected    # 入选
