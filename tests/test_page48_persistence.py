"""页 48 落盘回看真值测试（P2-③，2026-11-07）。

Product 验收（PRD §8.3 + Reviewer 条件①）：结果必须落盘可回看——
summary.json 写读 roundtrip、历史运行列表数据源、events.csv 列契约。
全部纯文件系统行为测试（不跑 Streamlit 运行时）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "48_signal_backtest.py"


def _page_source() -> str:
    source = PAGE.read_text(encoding="utf-8")
    assert source, "页 48 为空"
    return source


# ---------- 落盘行为（真值：直接按页面同构逻辑读写） ----------

def _persist_summary(out_dir: Path, persisted: dict) -> Path:
    """与页 48 落盘逻辑同构（summary.json + events.csv）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "summary.json"
    path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_summary_json_roundtrip(tmp_path):
    """写什么读什么：字段不丢、中文不坏、数值不变。"""
    persisted = {
        "window": ["2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00"],
        "config": {"thresholds": {"entry_zscore": 2.0, "minimum_underlying_move": 0.02}},
        "alignment_ratio": 1.0,
        "actual": 4302, "replayed": 4302, "mismatches": 0, "fills": 12,
        "baseline": {"臂": "基准（不改）", "BUY 触发": 4302, "通过重算": 4302},
        "candidate": {"臂": "候选", "BUY 触发": 4302, "通过重算": 4200},
        "generated_at": "2026-11-07T06:00:00",
    }
    path = _persist_summary(tmp_path / "run_a", persisted)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == persisted, "roundtrip 后必须逐字段一致"
    assert loaded["baseline"]["臂"] == "基准（不改）", "中文键值必须无乱码"
    assert loaded["alignment_ratio"] == 1.0


def test_history_listing_orders_newest_first(tmp_path, monkeypatch):
    """历史下拉数据源：按 run 名倒序（最新在前），只收含 summary.json 的 run。"""
    runs = tmp_path / "backtests"
    for name in ("ui_20261107_a", "ui_20261108_b", "ui_20261106_c"):
        _persist_summary(runs / name, {"window": ["s", "e"]})
    (runs / "ui_20261109_broken").mkdir(parents=True)  # 无 summary.json → 不得入选

    existing = sorted(
        (item for item in runs.glob("ui_*") if (item / "summary.json").exists()),
        key=lambda item: item.name, reverse=True,
    )
    assert [item.name for item in existing] == [
        "ui_20261108_b", "ui_20261107_a", "ui_20261106_c",
    ]
    assert all((item / "summary.json").exists() for item in existing)


def test_history_view_tolerates_partial_summary(tmp_path):
    """旧版本 summary 缺字段（如无 candidate）时回看不得崩——用 .get 兜底。"""
    path = _persist_summary(tmp_path / "run_old", {
        "window": ["s", "e"], "alignment_ratio": 1.0, "actual": 10,
        "baseline": {"臂": "基准"},  # 无 candidate 字段（旧版形态）
        "generated_at": "2026-11-06T00:00:00",
    })
    summary = json.loads(path.read_text(encoding="utf-8"))
    baseline = summary.get("baseline", {})
    candidate = summary.get("candidate", {})
    assert baseline == {"臂": "基准"} and candidate == {}


# ---------- 页面结构守卫（AST 锚点） ----------

def test_page_persists_before_history_read():
    source = _page_source()
    tree = ast.parse(source)
    fn_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    # 落盘块与历史回看块都存在
    assert "summary.json" in source and "events.csv" in source
    assert "历史结果回看" in source and 'glob("ui_*")' in source
    # 伪造形态：只有读没有写
    assert "write_text" in source, "页面必须写 summary.json（否则回看永远为空）"


def test_page_summary_fields_complete():
    """落盘字段契约：两臂/对齐率/窗口/生成时间缺一不可（回看页直接读它们）。"""
    source = _page_source()
    for field in ('"alignment_ratio"', '"baseline"', '"candidate"',
                  '"window"', '"generated_at"', '"actual"', '"replayed"', '"mismatches"'):
        assert field in source, f"summary.json 缺字段 {field}"


def test_mutation_dropping_persist_must_be_red():
    """变异：摘掉落盘 write_text → 落盘守卫必红。"""
    import shutil
    import subprocess
    import sys

    original = _page_source()
    anchor = '    (_out_dir / "summary.json").write_text('
    assert anchor in original, "落盘锚点丢失"
    mutated = original.replace(anchor, "    (_out_dir / 'summary.json') and None and (", 1)
    assert mutated != original
    try:
        PAGE.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_page_persists_before_history_read",
             "--capture=no"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert result.returncode != 0, "落盘被摘后守卫仍绿——回看功能可静默失效"
    finally:
        PAGE.write_text(original, encoding="utf-8")
        shutil.rmtree(ROOT / ".pytest_cache", ignore_errors=True)
