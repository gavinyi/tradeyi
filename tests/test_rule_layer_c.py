"""Layer C（合约级判别，仅 moneyness）验收守卫。

Chief 2026-10-25 裁定「A 变体」：
- **只上 `moneyness` 单特征**（Analyst block bootstrap 0.5526 [0.510,0.623] 显著）；
  gamma_to_theta 0.5048 无信号、DTE 0.5946 CI 含 0.5（组合层幸运排列）→ 都不上；
  设计 §3.2 原方向组合 0.4882 < 0.5 → 判无正信号，不救也不挑方向。
- **flag 默认关闭**：方向是 in-sample 选出来的，**不能当验收证据**；开门条件写死为
  OOS 并列组 AUC ≥ 0.52 且交易日 block bootstrap 下界 > 0.5。
- **作用域 = L2 破并列组**：旧可分辨对（A4）结构上不动，Layer B 不被覆盖。
- **单源**：只吃 CSV/上下文里已导出的 `moneyness` 列，不许读 `last_price` 或重算。
- **缺特征降级**：组内任一行缺 moneyness → 该组回退中立 tie-break，并记原因计数。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for extra in (str(ROOT), str(TESTS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import futu_quant.ml_scorer as ms  # noqa: E402
from futu_quant.ml_scorer import (  # noqa: E402
    _LAYER_C_ENABLED,
    _LAYER_C_FEATURE,
    _LAYER_C_GATE_AUC,
    BuiltinRuleModel,
    _layer_c_order,
)
from test_rule_neutral_tiebreak import (  # noqa: E402
    _load_frame,
    _old_discrete_score,
    _tie_groups,
)

FEATURE_COLS = [
    "symbol_win_rate", "symbol_avg_pnl", "symbol_pnl_trend", "symbol_std_pnl",
    "symbol_max_pnl", "symbol_min_pnl", "symbol_3d_avg_pnl", "symbol_7d_avg_pnl",
    "symbol_trade_count", "days_to_expiry", "expiry_bucket", "is_call", "strike_price",
]


def _scores(frame: pd.DataFrame, monkeypatch=None, enabled: bool | None = None) -> np.ndarray:
    if enabled is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(ms, "_LAYER_C_ENABLED", enabled)
    return BuiltinRuleModel().predict_proba(frame)[:, 1]


# ------------------------------------------------------------------ flag 契约
def test_layer_c_flag_defaults_off_and_is_gated():
    """默认必须关闭；开门条件与「in-sample 不是证据」必须写在源码里。"""
    assert _LAYER_C_ENABLED is False
    assert _LAYER_C_FEATURE == "moneyness"
    assert _LAYER_C_GATE_AUC == 0.52
    source = (ROOT / "src/futu_quant/ml_scorer.py").read_text(encoding="utf-8")
    assert "in-sample" in source
    assert "不能当验收证据" in source
    assert "block bootstrap 下界 > 0.5" in source


def test_layer_c_disabled_output_equals_neutral_tiebreak():
    """flag 关闭时行为与中立 tie-break 完全一致（生产默认路径不变）。"""
    frame = _load_frame()
    model = BuiltinRuleModel()
    scores = model.predict_proba(frame)[:, 1]
    assert model.last_layer_c_diagnostics["layer_c_enabled"] is False
    assert model.last_layer_c_diagnostics["layer_c_groups"] == 0
    # 逐组核对：组内次序必须等于 _fused_tie_order
    for _, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        day_scores = model.predict_proba(day)[:, 1]
        rows = list(day.iterrows())
        for group in _tie_groups(day):
            expected = ms._fused_tie_order([rows[i][1] for i in group], list(range(len(group))))
            order = sorted(group, key=lambda i: day_scores[i])
            expected_indices = [group[local] for local in expected]
            assert order == expected_indices
    assert len(scores) == len(frame)


# ------------------------------------------------------- 开启后的行为（合成数据）
def _synthetic_tie_frame(moneyness=("0.05", "0.10", "0.15")):
    """两合约 score_a 完全相同（symbol_* 全同），只有 moneyness 不同 → L2 并列组。"""
    base = {
        "trade_date": "2026-08-05",
        "symbol_win_rate": 0.6, "symbol_avg_pnl": 10.0, "symbol_pnl_trend": 1.0,
        "symbol_std_pnl": 20.0, "symbol_max_pnl": 30.0, "symbol_min_pnl": -10.0,
        "symbol_3d_avg_pnl": 8.0, "symbol_7d_avg_pnl": 6.0, "symbol_trade_count": 5,
        "underlying_symbol": "US.SPY", "is_call": 1,
        "days_to_expiry": 3, "expiry_bucket": 1,
    }
    rows = []
    for index, value in enumerate(moneyness):
        row = dict(base)
        row.update({"strike_price": 400.0 + index, "moneyness": float(value)})
        rows.append(row)
    return pd.DataFrame(rows)


def test_enabled_layer_c_orders_by_moneyness_desc(monkeypatch):
    frame = _synthetic_tie_frame(("0.05", "0.15", "0.10"))
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", True)
    model = BuiltinRuleModel()
    scores = model.predict_proba(frame)[:, 1]
    # moneyness 高者名次靠前 → 分数更高
    assert scores[1] > scores[2] > scores[0]
    diagnostics = model.last_layer_c_diagnostics
    assert diagnostics["layer_c_enabled"] is True
    assert diagnostics["tie_groups"] == 1
    assert diagnostics["layer_c_groups"] == 1
    assert diagnostics["fallback_missing_feature"] == 0


def test_enabled_layer_c_falls_back_when_feature_missing(monkeypatch):
    """组内任一行缺 moneyness → 整组回退中立 tie-break，并计原因；不伪造、不整批失败。"""
    frame = _synthetic_tie_frame(("0.05", "0.15", "0.10"))
    mutated = frame.copy()
    mutated.loc[1, "moneyness"] = float("nan")
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", True)
    model = BuiltinRuleModel()
    scores = model.predict_proba(mutated)[:, 1]
    diagnostics = model.last_layer_c_diagnostics
    assert diagnostics["fallback_missing_feature"] == 1
    assert diagnostics["layer_c_groups"] == 0

    rows = list(mutated.iterrows())
    expected = ms._fused_tie_order([rows[i][1] for i in range(len(mutated))], list(range(len(mutated))))
    assert [int(i) for i in np.argsort(scores)] == expected


def test_layer_c_does_not_cover_layer_b_ordering(monkeypatch):
    """作用域只在 L2 并列组：Layer B 已能分序的组不归 Layer C 管（不许覆盖 B）。"""
    frame = _synthetic_tie_frame(("0.05", "0.15"))
    mutated = frame.copy()
    # 让 B 层分量不同（max_pnl 差异）→ base 不等 → 不进入 L2 组
    mutated.loc[1, "symbol_max_pnl"] = 60.0
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", True)
    model = BuiltinRuleModel()
    model.predict_proba(mutated)
    diagnostics = model.last_layer_c_diagnostics
    assert diagnostics["tie_groups"] == 0, "B 层可分辨的组不应进入 Layer C 作用域"


def test_layer_c_does_not_read_last_price():
    """单源守卫：Layer C 实现里不许出现 last_price / 重算 moneyness 的调用。"""
    source = (ROOT / "src/futu_quant/ml_scorer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_layer_c_order"
    )
    # 只看**代码标识符**（Name/Attribute），不把 docstring/注释里的说明文字算进来
    identifiers: set[str] = set()
    for node in ast.walk(function_node):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    assert "last_price" not in identifiers, "Layer C 不许读 last_price（二次来源＝skew）"
    assert "moneyness_ratio" not in identifiers, "Layer C 不许自己重算 moneyness（单源）"

    # 行为守卫：只有 last_price、没有 moneyness 列 → 回退而不是重算
    frame = _synthetic_tie_frame(("0.05", "0.15"))
    mutated = frame.drop(columns=["moneyness"])
    mutated["last_price"] = [3.0, 4.0]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", True)
    try:
        model = BuiltinRuleModel()
        model.predict_proba(mutated)
        assert model.last_layer_c_diagnostics["fallback_missing_feature"] == 1
    finally:
        monkeypatch.undo()


# ---------------------------------------------------- A4 逐值 + 真实样本 AUC
def test_layer_c_keeps_old_distinguishable_pairs_identical(monkeypatch):
    """A4 硬版本（`==`）：旧可分辨对的先后，同时开/关 Layer C 时逐值相同。"""
    frame = _load_frame()
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", False)
    before = BuiltinRuleModel().predict_proba(frame)[:, 1]
    monkeypatch.setattr(ms, "_LAYER_C_ENABLED", True)
    after = BuiltinRuleModel().predict_proba(frame)[:, 1]

    compared = 0
    offset = 0
    for _, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        size = len(day)
        day_before = before[offset:offset + size]
        day_after = after[offset:offset + size]
        old = day.apply(_old_discrete_score, axis=1).to_numpy()
        for i in range(size):
            for j in range(i + 1, size):
                if abs(old[i] - old[j]) < 1e-12:
                    continue
                compared += 1
                assert np.sign(day_before[i] - day_before[j]) == np.sign(day_after[i] - day_after[j]), (
                    f"开 Layer C 后旧可分辨对 ({i},{j}) 的先后变了 → A4 不再逐值相等"
                )
        offset += size
    assert compared > 1000


def test_moneyness_in_sample_auc_meets_gate_but_is_not_evidence():
    """in-sample 证据：L2 并列组内 moneyness 高者优先的 AUC 应 ≥ 0.52（Analyst 0.5526 互证）。

    ⚠️ 这是 **in-sample** 数字，只用于确认「方向不是随手拍的」，**不作验收证据**；
    真正的开门条件是 OOS AUC ≥0.52 且 block bootstrap 下界 >0.5。
    """
    frame = _load_frame()
    ok = total = 0
    for _, day in frame.groupby("trade_date"):
        day = day.reset_index(drop=True)
        pnl = day["window_net_pnl"].to_numpy(dtype=float)
        rows = list(day.iterrows())
        for group in _tie_groups(day):
            if any(
                pd.isna(rows[i][1].get("moneyness")) for i in group
            ):
                continue
            ordered = _layer_c_order([rows[i][1] for i in group], list(range(len(group))))
            # 名次越大分数越高 → moneyness 高者名次大；此处转成「分数从高到低」的位置
            positions = {group[local]: -rank for rank, local in enumerate(ordered)}
            for a in range(len(group)):
                for b in range(a + 1, len(group)):
                    i, j = group[a], group[b]
                    if abs(pnl[i] - pnl[j]) < 1e-12:
                        continue
                    winner, loser = (i, j) if pnl[i] > pnl[j] else (j, i)
                    total += 1
                    ok += int(positions[winner] < positions[loser])
    assert total > 1000, f"可检验配对仅 {total}"
    auc = ok / total
    assert auc >= _LAYER_C_GATE_AUC, f"in-sample moneyness AUC {auc:.4f} < 门槛 {_LAYER_C_GATE_AUC}"
