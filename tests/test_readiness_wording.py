"""回测大屏「越权准入结论」文案守卫（Chief 2026-10-25 裁定，P0）。

问题：`pages/42` 曾用 `st.success` 渲染 `🟢 满足实盘接入要求 (Recommended for Production)`
+「可安全接入实盘/稳定增量 Alpha」——这是拿**单次回测**（无置信区间、无样本量门槛、
不走预写阈值）替页面下准入结论。用户看到满屏绿就会以为可以切实盘。

真实判定：Analyst `switch_decision.md` 按预写阈值 → 灰区不切（AUC 5% 分位 0.4847<0.50、
中位 0.5565<0.58）；复测条件见 `data/qa/ml_v32_enable_runbook.md`「切/不切判定」。

本守卫要求：
1. 源码不得再出现越权措辞（`Recommended for Production` / `可安全接入实盘` / `稳定的增量 Alpha` …）；
2. 摘要必须是中性「本次回测结果（单次评估，不构成准入结论）」；
3. 必须显示样本量（笔数 / **交易日去重数**）与 runbook 准入参考；
4. 页面不得再用 `st.success` / `st.error` 渲染该结论（绿勾=背书，必须中性 `st.info`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.ml_backtest_comparator import run_ml_vs_rule_comparison  # noqa: E402

FORBIDDEN_PHRASES = (
    "Recommended for Production",
    "可安全接入实盘",
    "稳定的增量 Alpha",
    "满足实盘接入要求",
    "建议维持规则打分 (Keep Rule-Based)",
)
SCAN_GLOBS = ("src/futu_quant/*.py", "pages/*.py", "scripts/*.py")
PAGE = ROOT / "pages/42_ml_model_status.py"
CSV_CANDIDATES = (
    ROOT / "data/ml_training/volatility_trades_v3_latest.csv",
    Path(r"E:\Thisyi\trade\data\ml_training\volatility_trades_v3_latest.csv"),
)


def _assert_no_forbidden(text: str) -> None:
    hits = [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]
    assert not hits, (
        f"源码出现越权准入措辞 {hits}：单次回测不得替页面下准入结论"
        "（准入看 data/qa/ml_v32_enable_runbook.md 预写阈值）"
    )


def _csv_path() -> Path:
    for candidate in CSV_CANDIDATES:
        if candidate.exists():
            return candidate
    pytest.fail(
        "找不到 volatility_trades_v3_latest.csv（本 worktree 与生产目录都没有）；"
        "重导：python scripts/ml_export_training_data_v3.py "
        "--output data/ml_training/volatility_trades_v3_latest.csv --db data/trading.db"
    )


def _verdict_block() -> str:
    """抽出页面里渲染 verdict 的那段源码（从 `rep = res.readiness_report` 起）。"""
    source = PAGE.read_text(encoding="utf-8")
    marker = "rep = res.readiness_report"
    assert marker in source, "页面找不到 verdict 渲染入口（结构变了，守卫需同步更新）"
    tail = source.split(marker, 1)[1]
    return marker + tail[:1200]


def _assert_neutral_render(block: str) -> None:
    assert "st.info(" in block, "verdict 必须用中性信息框渲染"
    assert "st.success(" not in block, "verdict 不得用 st.success 渲染（绿勾=替页面背书）"
    assert "st.error(" not in block, "verdict 不得用 st.error 渲染（红叉=替页面下反向结论）"


# ------------------------------------------------------------- 源码扫描
def test_no_admission_verdict_phrases_anywhere():
    offenders = []
    for pattern in SCAN_GLOBS:
        for path in ROOT.glob(pattern):
            text = path.read_text(encoding="utf-8")
            hits = [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]
            if hits:
                offenders.append((str(path.relative_to(ROOT)), hits))
    assert not offenders, f"越权准入措辞仍在：{offenders}"


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_mutation_reintroduced_phrase_must_be_red(phrase):
    with pytest.raises(AssertionError):
        _assert_no_forbidden(f"somewhere {phrase} in code")


# ------------------------------------------------------- comparator 输出契约
def test_comparator_verdict_is_neutral_and_shows_sample_metrics():
    path = _csv_path()
    frame = pd.read_csv(path)
    dates = frame["trade_date"].astype(str) if "trade_date" in frame.columns else None
    result = run_ml_vs_rule_comparison(data_path=path)
    rep = result.readiness_report

    title = rep["verdict_title"]
    assert "本次回测结果" in title
    assert "单次评估" in title and "不构成准入结论" in title
    assert rep["evaluation_scope"] == "single_run_backtest"

    # 样本量：笔数 + **交易日去重数**（不是行数/不是笔数）
    assert rep["sample_trades"] == len(frame)
    assert dates is not None
    assert rep["sample_days"] == int(dates.nunique())
    assert rep["sample_days"] < rep["sample_trades"], "交易日必须是去重后的日期数，不是笔数"

    desc = rep["verdict_desc"]
    assert "笔" in desc and "交易日" in desc
    assert "不作准入判定" in desc
    assert "ml_v32_enable_runbook.md" in rep["admission_reference"]
    assert "切/不切判定" in rep["admission_reference"]
    assert "ml_v32_enable_runbook.md" in desc


def test_page_renders_verdict_without_green_check():
    block = _verdict_block()
    _assert_neutral_render(block)
    assert "sample_trades" in block and "sample_days" in block, (
        "页面必须显示样本量（笔数 / 交易日去重数）"
    )
    assert "admission_reference" in block


def test_mutation_page_green_check_must_be_red():
    block = _verdict_block()
    mutated = block.replace("st.info(", "st.success(", 1)
    with pytest.raises(AssertionError):
        _assert_neutral_render(mutated)
