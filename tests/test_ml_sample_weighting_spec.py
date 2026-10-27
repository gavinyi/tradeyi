"""ML v3.2 样本收益加权：必须与规格公式一致。

规格：`docs/ml_v3_2_convexity_and_odds_tuning_spec.md:46` → ``w_i = ln(1 + |window_net_pnl_i|)``

2026-10-27 修：
- 去掉未记录在规格里的 `* 10` 放大系数（它在对数内部放大小额盈亏，压缩大肉单的相对优势）
- 保留显式零点护栏 `+ 1.0`（纯公式在 pnl=0 时给 w=0，会丢掉「不赚不亏」的负样本）
- `abs` 与规格一致 —— Reviewer 提的「取 abs 导致盈亏同权」不成立，规格原文就是绝对值

变异可红：把 `sample_weights_from_pnl` 改回 `1 + log1p(abs(pnl) * 10)`，
`test_big_winner_dominates_small_winner` 会转红。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml_train_symbol_scorer_v3.py"
_spec = importlib.util.spec_from_file_location("ml_train_v3_under_test", _SCRIPT)
ml_train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml_train)


def test_flat_sample_keeps_positive_weight():
    """盈亏为 0 的样本不能被丢掉（权重必须 > 0，而不是 0）。"""
    weights = ml_train.sample_weights_from_pnl(pd.Series([0.0, 0.0]))
    assert (weights > 0).all()
    assert weights.iloc[0] == 1.0


def test_weight_is_monotonic_in_absolute_pnl():
    weights = ml_train.sample_weights_from_pnl(pd.Series([0.0, 10.0, 100.0, 500.0]))
    assert list(weights) == sorted(weights), "权重必须随 |盈亏| 单调递增"


def test_weight_ignores_sign():
    """规格用绝对值：+300 与 -300 权重必须相同。"""
    weights = ml_train.sample_weights_from_pnl(pd.Series([300.0, -300.0]))
    assert weights.iloc[0] == weights.iloc[1]


def test_big_winner_dominates_small_winner():
    """大肉单（+500）相对小单（+10）的优势必须显著，符合规格「5~8 倍」目标量级。

    去掉 `* 10` 之前，这个比值被压到 ~1.7 倍；改回规格公式后应达到 2 倍以上。
    """
    weights = ml_train.sample_weights_from_pnl(pd.Series([10.0, 500.0]))
    ratio = weights.iloc[1] / weights.iloc[0]
    assert ratio > 2.0, f"大肉单相对权重只有 {ratio:.2f} 倍，规格意图未达成"


def test_matches_spec_formula():
    pnl = pd.Series([0.0, 5.0, -120.0, 480.0])
    expected = 1.0 + np.log1p(pnl.abs())
    got = ml_train.sample_weights_from_pnl(pnl)
    assert np.allclose(got.to_numpy(), expected.to_numpy())
