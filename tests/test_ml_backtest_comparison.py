from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from futu_quant import ml_backtest_comparator as comparator
from futu_quant.ml_backtest_comparator import (
    BacktestComparisonResult,
    run_ml_vs_rule_comparison,
)


def _require_default_trades_data() -> None:
    """默认回测数据是 **gitignored 数据产物**：缺失时 skip 并写明原因与期望路径。

    为什么必须 skip 而不是失败、更不许 xfail：
    - 失败会让「fresh worktree / 没有数据的环境」报假红，混淆「代码坏了」与「数据没到」；
    - xfail 更糟：它把「数据没到」伪装成「预期失败」，等数据真的坏了也看不出来。

    路径取自 `ml_backtest_comparator.DEFAULT_DATA_PATH`（**动态读模块属性**，不在这里复制一份），
    数据产物换位置时这条守卫跟着走，不会自己漂。
    """
    path = pathlib.Path(comparator.DEFAULT_DATA_PATH)
    if not path.exists():
        pytest.skip(
            f"缺少回测数据产物：{path}（gitignored，只在带数据的验证环境可跑）。"
            "缺失时按 skip 处理，禁止用 xfail 掩盖；请在带 data/ml_training 副本的环境重跑。"
        )


def test_ml_vs_rule_comparison_full_pipeline(tmp_path):
    _require_default_trades_data()
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
    _require_default_trades_data()
    res = run_ml_vs_rule_comparison(
        start_date="2026-07-01",
        end_date="2026-10-24",
        initial_capital=5000.0,
        top_k=2,
    )
    assert not res.equity_series.empty
    assert res.equity_series.iloc[0]["规则基准净值"] == 5000.0


def test_ml_vs_rule_comparison_too_few_samples(tmp_path):
    # 构造少量样本报错（自带 temp 数据，不依赖 gitignored 产物）
    mini_csv = tmp_path / "mini.csv"
    pd.DataFrame({"created_at": ["2026-10-01"], "symbol": ["US.AAPL"]}).to_csv(mini_csv, index=False)
    with pytest.raises(ValueError, match="有效样本过少"):
        run_ml_vs_rule_comparison(data_path=mini_csv)


# ---------------------------------------------------------------- 守卫：skip 的语义
def test_missing_artifact_skips_with_explicit_path_and_reason(monkeypatch, tmp_path):
    """缺数据时必须 skip，且消息里带**期望路径**与「禁止 xfail」的理由。"""
    fake = tmp_path / "not_there" / "volatility_trades_v3_latest.csv"
    monkeypatch.setattr(comparator, "DEFAULT_DATA_PATH", fake)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _require_default_trades_data()

    message = str(excinfo.value)
    assert str(fake) in message, "skip 消息必须写明期望路径，否则排查要翻代码"
    assert "xfail" in message, "必须写清为什么不能用 xfail 掩盖"


def test_file_does_not_use_xfail_to_mask_missing_data():
    """禁止用 xfail 掩盖缺数据（Chief 2026-11-05 明确要求）。

    断言里的关键字**拆开拼**：直接把标记名写进源码会让守卫命中自己（自指假红）。
    """
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    marker = "pytest.mark." + "xfail"
    call = "xfail" + "("
    assert marker not in source
    assert call not in source
