from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.ml_backtest_comparator import (
    BacktestComparisonResult,
    run_ml_vs_rule_comparison,
)


def test_ml_vs_rule_comparison_full_pipeline(tmp_path):
    # 使用实际历史训练数据跑对照比对
    result = run_ml_vs_rule_comparison(
        initial_capital=10000.0,
        top_k=3,
    )

    assert isinstance(result, BacktestComparisonResult)

    # 1. 验证双曲线净值序列
    eq = result.equity_series
    assert not eq.empty
    assert "规则基准净值" in eq.columns
    assert "ML增强净值" in eq.columns
    assert eq.iloc[0]["规则基准净值"] == 10000.0
    assert eq.iloc[0]["ML增强净值"] == 10000.0

    # 2. 验证绩效指标字典字段
    for m in (result.rule_metrics, result.ml_metrics):
        assert "total_return_pct" in m
        assert "win_rate" in m
        assert "profit_loss_ratio" in m
        assert "max_drawdown_pct" in m
        assert "trades_count" in m
        assert m["trades_count"] > 0

    # 3. 验证五档分层分析
    q = result.quintile_returns
    assert not q.empty
    assert len(q) == 5
    assert "Q1 (最高)" in q["quintile"].astype(str).values
    assert "Q5 (最低)" in q["quintile"].astype(str).values

    # 4. 验证生产准入报告
    rep = result.readiness_report
    assert "is_ready" in rep
    assert "verdict_title" in rep
    assert "checklist" in rep
    assert len(rep["checklist"]) >= 4

    # 5. 验证选品分歧归因明细
    assert isinstance(result.divergence_records, list)


def test_ml_vs_rule_comparison_window_filter():
    res = run_ml_vs_rule_comparison(
        start_date="2026-07-01",
        end_date="2026-10-24",
        initial_capital=5000.0,
        top_k=2,
    )
    assert not res.equity_series.empty
    assert res.equity_series.iloc[0]["规则基准净值"] == 5000.0


def test_ml_vs_rule_comparison_too_few_samples(tmp_path):
    # 构造少量样本报错
    mini_csv = tmp_path / "mini.csv"
    pd.DataFrame({"created_at": ["2026-10-01"], "symbol": ["US.AAPL"]}).to_csv(mini_csv, index=False)
    with pytest.raises(ValueError, match="有效样本过少"):
        run_ml_vs_rule_comparison(data_path=mini_csv)
