"""候选榜单必须能解释「为什么选这张」（2026-10-30 复盘）。

实盘质疑：候选榜里 P145000 质量分 **83.71**，P140000 只有 **77.75**，
但滚动结果选了 P140000，而且榜单顺序「既非升序也非降序」。

真相：选择键是**预期收益**（`structure_expected_return` 降序，质量分为
tiebreaker），而榜单只展示了质量分 —— 于是看起来像乱序，也解释不了选择。
两个键都在表里，但页面只显示了一个，用户无从判断。

修复后：榜单按**真实选择键**排序、标出 `selected`，并在 payload 写明
`selection_basis` / `selection_key`。本文件锁死这条一致性。
"""
from __future__ import annotations

import pandas as pd

from futu_quant.volatility_roll import _roll_diagnostics

REGIME = {"active": True, "strong_signal_override": False, "reason": "动态波动激活"}


def _frame(**extra) -> pd.DataFrame:
    rows = [
        {
            "code": "US.COIN261120P145000",
            "volatility_eligible": True,
            "volatility_filter_reason": "全部条件通过",
            "volatility_quality_score": 83.71,
            "model_expected_return": 0.18,
            "structure_expected_return": 0.18,
            "iv_rv": 1.2, "iv_rv_limit": 1.5, "premium_per_stage": 900,
        },
        {
            "code": "US.COIN261120P140000",
            "volatility_eligible": True,
            "volatility_filter_reason": "全部条件通过",
            "volatility_quality_score": 77.75,
            "model_expected_return": 0.31,
            "structure_expected_return": 0.31,
            "iv_rv": 1.1, "iv_rv_limit": 1.5, "premium_per_stage": 915,
        },
    ]
    frame = pd.DataFrame(rows)
    for key, values in extra.items():
        frame[key] = values
    return frame


def test_top_candidates_sorted_by_actual_selection_key():
    """榜单第一必须是被选中的那张 —— 选择键是预期收益，不是质量分。"""
    frame = _frame()
    diagnostics = _roll_diagnostics(
        frame, REGIME, "已自动滚动",
        selected_code="US.COIN261120P140000",
    )

    top = diagnostics["top_candidates"]
    assert [item["code"] for item in top] == [
        "US.COIN261120P140000", "US.COIN261120P145000",
    ], "榜单没有按选择键（预期收益）排序 —— 用户会看到「低分的反而中选」"
    assert top[0]["selected"] is True
    assert top[0]["rank"] == 1
    # 质量分仍要展示（它是 tiebreaker，也是用户看惯的指标），但不能冒充选择键
    assert top[1]["score"] == 83.71
    assert top[0]["expected_return"] == 0.31
    assert "预期收益" in diagnostics["selection_basis"]
    assert diagnostics["selection_key"] == "structure_expected_return"
    assert diagnostics["selected_code"] == "US.COIN261120P140000"


def test_ml_reranking_changes_selection_basis_and_order():
    """开了 ML 重排时，榜单必须跟着换成 ml_adjusted_score 排序。"""
    frame = _frame(
        ml_adjusted_score=[95.0, 70.0],
        ml_score=[0.9, 0.4],
    )
    diagnostics = _roll_diagnostics(
        frame, REGIME, "已自动滚动",
        selected_code="US.COIN261120P145000",
    )

    top = diagnostics["top_candidates"]
    assert top[0]["code"] == "US.COIN261120P145000"
    assert top[0]["selected"] is True
    assert "ML" in diagnostics["selection_basis"]
    assert diagnostics["selection_key"] == "ml_adjusted_score"


def test_diagnostics_without_selection_keeps_all_flags_false():
    """早退分支（没有选中项）也要能出榜，且不得伪造 selected。"""
    diagnostics = _roll_diagnostics(_frame(), REGIME, "候选质量过滤后没有可评分的新合约")

    assert all(item["selected"] is False for item in diagnostics["top_candidates"])
    assert diagnostics["message"]
