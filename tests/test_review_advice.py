from __future__ import annotations

from datetime import date
from pathlib import Path

from futu_quant.store import TradingStore
from futu_quant.strategy_review import (
    build_strategy_review,
    _review_issue_advice,
    review_advice_markdown,
    review_markdown,
)
from futu_quant.trade_diagnostics import diagnostic_window


def test_review_advice_generates_compact_sections():
    store = TradingStore(Path(__file__).resolve().parents[1] / "data" / "test_advice_check.db")
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


def test_review_markdown_contains_advice_section():
    store = TradingStore(Path(__file__).resolve().parents[1] / "data" / "test_advice_check.db")
    review = build_strategy_review(store, diagnostic_window(date(2026, 10, 9)))
    markdown = review_markdown(review)

    assert "## 当日问题与建议" in markdown
    assert "## 阻塞原因" in markdown
    # 建议章节应在阻塞原因之后
    assert markdown.index("## 当日问题与建议") > markdown.index("## 阻塞原因")
