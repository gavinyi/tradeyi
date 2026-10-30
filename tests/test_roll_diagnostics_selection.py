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


def test_same_primary_leg_different_structures_is_reported_not_silently_deduped():
    """同一主腿的多种表达必须**标注结构**，不能当成重复行删掉。

    10-30 实盘榜单里 US.COIN261120P145000 出现两行（exp 0.1895 与 0.0505），
    质量分都是 83.71 —— 看起来像「数据写重了」。真实原因是 `_package()` 把
    组合的 `code` 设成了**主腿代码**：一行是 LONG_OPTION，另一行是
    PUT_DEBIT_SPREAD，同一主腿、两种表达、各自的预期收益。

    组合本身是逐腿下单的（`option_combo_legs` 每条腿各有 intent_id），
    所以 code 重名不代表执行漏腿；但展示必须能区分，否则复盘会误判成数据损坏。
    """
    frame = pd.DataFrame([
        {
            "code": "US.COIN261120P140000", "structure": "LONG_OPTION",
            "structure_label": "单腿买方期权", "volatility_eligible": True,
            "volatility_filter_reason": "全部条件通过", "volatility_quality_score": 77.75,
            "model_expected_return": 0.2151, "structure_expected_return": 0.2151,
            "iv_rv": 0.82, "iv_rv_limit": 1.8, "premium_per_stage": 935,
        },
        {
            "code": "US.COIN261120P145000", "structure": "LONG_OPTION",
            "structure_label": "单腿买方期权", "volatility_eligible": True,
            "volatility_filter_reason": "全部条件通过", "volatility_quality_score": 83.71,
            "model_expected_return": 0.1895, "structure_expected_return": 0.1895,
            "iv_rv": 0.84, "iv_rv_limit": 1.8, "premium_per_stage": 980,
        },
        {
            "code": "US.COIN261120P145000", "structure": "PUT_DEBIT_SPREAD",
            "structure_label": "认沽借方价差", "volatility_eligible": True,
            "volatility_filter_reason": "全部条件通过", "volatility_quality_score": 83.71,
            "model_expected_return": 0.0505, "structure_expected_return": 0.0505,
            "iv_rv": 0.84, "iv_rv_limit": 1.8, "premium_per_stage": 420,
        },
    ])

    diagnostics = _roll_diagnostics(
        frame, REGIME, "已自动滚动", selected_code="US.COIN261120P140000",
    )

    top = diagnostics["top_candidates"]
    structures = {item["code"]: item["structure"] for item in top}
    assert structures["US.COIN261120P140000"] == "LONG_OPTION"
    # 两行同 code 的行都在榜单里，且结构可区分（**不是**简单去重）
    p145 = [item for item in top if item["code"] == "US.COIN261120P145000"]
    assert len(p145) == 2, "同主腿的两行都应保留 —— 去重会隐藏「价差表达」这个选项"
    assert {item["structure"] for item in p145} == {"LONG_OPTION", "PUT_DEBIT_SPREAD"}
    assert all(item["structure_label"] for item in top), "每行都要有可读的结构名"
    assert diagnostics["multi_expression_codes"] == ["US.COIN261120P145000"], (
        "必须显式告诉调用方「这个 code 有多行」，否则页面看起来像重复 bug"
    )
