from __future__ import annotations

import dataclasses
from datetime import date

from futu_quant.store import TradingStore
from futu_quant.strategy_review import (
    build_strategy_review,
    _review_issue_advice,
    review_advice_markdown,
    review_markdown,
)
from futu_quant.trade_diagnostics import diagnostic_window


def test_review_advice_generates_compact_sections(tmp_path):
    store = TradingStore(tmp_path / "advice_check.db")
    review = build_strategy_review(store, diagnostic_window(date(2026, 10, 9)))
    issues, advice = _review_issue_advice(review)
    markdown = review_advice_markdown(review)

    assert "## 当日问题与建议" in markdown
    assert "### 问题" in markdown
    assert "### 建议" in markdown
    assert len(issues) <= 5
    assert len(advice) <= 5
    assert issues, "应至少有一条问题"
    assert advice, "应至少有一条建议"


def test_review_markdown_contains_advice_section(tmp_path):
    store = TradingStore(tmp_path / "advice_check.db")
    review = build_strategy_review(store, diagnostic_window(date(2026, 10, 9)))
    markdown = review_markdown(review)

    assert "## 当日问题与建议" in markdown
    assert "## 阻塞原因" in markdown
    # 建议章节应在阻塞原因之后
    assert markdown.index("## 当日问题与建议") > markdown.index("## 阻塞原因")


def test_review_markdown_survives_none_table_fields(tmp_path):
    """05:15 定时任务健壮性家族防回归：所有表格字段为 None 时
    review_markdown 必须完整生成（table() 对 None 兜底「无记录。」），
    绝不让复盘报告链路因单个数据段缺失而整体崩溃。"""
    store = TradingStore(tmp_path / "none_fields.db")
    review = build_strategy_review(store, diagnostic_window(date(2026, 10, 9)))

    # 将全部表格型字段替换为 None，模拟上游数据段缺失
    overrides = {
        f.name: None for f in dataclasses.fields(review)
        if f.name in {
            "orders", "option_pnl_breakdown", "option_lifecycle",
            "quality_scores", "action_items", "evidence_alignment",
            "option_quality", "core_quality",
        }
    }
    degraded = dataclasses.replace(review, **overrides)

    markdown = review_markdown(degraded)

    assert "# 策略复盘报告" in markdown
    assert "## 一句话结论" in markdown
    assert "无记录。" in markdown  # None 段落兜底文案生效


def test_review_advice_survives_thin_action_items(tmp_path):
    """本族回归核心用例（Reviewer 点名「瘦表最能防住」）：
    action_items 有行但缺『优先级排序』列时，_review_issue_advice 的
    排序路径必须走列存在性检查 + 派生兜底，绝不 KeyError。"""
    import pandas as pd

    store = TradingStore(tmp_path / "thin_actions.db")
    review = build_strategy_review(store, diagnostic_window(date(2026, 10, 9)))

    # 变体 a：只有「优先级」列（无「优先级排序」「对象」）→ 触发优先级派生路径
    thin_a = pd.DataFrame([
        {"优先级": "高", "建议动作": "检查波动率状态机"},
        {"优先级": "低", "建议动作": "观察盘口质量"},
        {"优先级": "中", "建议动作": "复核对账差异"},
        {"优先级": "高", "建议动作": "清理过期合约"},
    ])
    degraded_a = dataclasses.replace(review, action_items=thin_a)
    issues_a, advice_a = _review_issue_advice(degraded_a)  # 不抛 KeyError 即通过
    assert advice_a, "瘦表（仅优先级列）仍应产出建议动作"

    # 变体 b：完全无关列集（既无优先级也无对象）→ 触发 9 兜底 + head 路径
    thin_b = pd.DataFrame([
        {"任意列": "x"}, {"任意列": "y"}, {"任意列": "z"},
    ])
    degraded_b = dataclasses.replace(review, action_items=thin_b)
    issues_b, advice_b = _review_issue_advice(degraded_b)  # 不抛 KeyError 即通过
    assert issues_b, "瘦表（无关列）仍应产出问题清单"
