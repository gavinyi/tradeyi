"""paper 租户候选拦截可观测性（P1 小包 2026-11-05）。

背景：实盘下单前拦截有 `LIVE_MAX_ORDER_NOTIONAL` audit；模拟盘候选被
入场过滤器拒绝时走 `VOLATILITY_AUTO_CAMPAIGN_REJECTED` audit +
`decision_logs.CANDIDATE_BLOCKED`，数据完整但**UI 拦截面板不展示**——
用户看不到「模拟盘为什么没下单」（用户原话追问 2026-11-05 23:08）。

契约钉死：
1. 总览页选品漏斗区必须渲染「候选拦截明细（含租户）」块，并带 `租户` 列
   （实盘/模拟盘中文标签），否则 paper 拦截又变不可见；
2. 租户标签映射必须覆盖 `futu_paper_sandbox` 与 `futu_real_master`；
3. 数据来源必须是 `decision_logs.CANDIDATE_BLOCKED`（scanner_decisions
   已含 tenant_id 列，store.decision() 落库时带租户）。
"""

from __future__ import annotations

import ast
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "30_volatility_strategy_overview.py"


def _tree() -> ast.Module:
    text = PAGE.read_text(encoding="utf-8")
    assert text, "总览页源码为空（守卫读不到输入不能放行）"
    return ast.parse(text)


def _find_guard(tree: ast.AST) -> ast.If:
    """定位选品漏斗 tab（overview_tabs[1]）的 if 守卫。"""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "open"
            and ast.unparse(node.test.value) == "overview_tabs[1]"
        ):
            return node
    raise AssertionError("找不到 overview_tabs[1]（选品漏斗）的 if 守卫")


def test_blocked_details_block_exists_with_tenant_column():
    tree = _tree()
    guard = _find_guard(tree)
    guarded_ids = {id(node) for node in ast.walk(guard)}
    assert "候选拦截明细" in PAGE.read_text(encoding="utf-8"), (
        "选品漏斗区缺少「候选拦截明细」块 —— paper 拦截又不可见了"
    )
    # 租户列映射必须出现在漏斗守卫体内（不是文件任意处）
    mapped = [
        node for node in ast.walk(guard)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(k, ast.Constant) and k.value == "futu_paper_sandbox"
            for k in node.keys
        )
    ]
    assert mapped, "拦截明细缺 paper/real 租户中文标签映射"
    assert all(id(node) in guarded_ids for node in mapped), (
        "租户映射在守卫体外，漏斗 tab 未打开时也会执行"
    )


def test_blocked_rows_come_from_scanner_decisions():
    """拦截明细必须取自 scanner_decisions（decision_logs.CANDIDATE_BLOCKED 落库链）。"""
    tree = _tree()
    guard = _find_guard(tree)
    names = {
        node.id
        for node in ast.walk(guard)
        if isinstance(node, ast.Name)
    }
    assert "scanner_decisions" in names, (
        "拦截明细未读取 scanner_decisions（应来自 decision_logs CANDIDATE_BLOCKED）"
    )


def test_mutation_removing_tenant_labels_must_be_red():
    """变异守卫：删掉租户映射（paper 又不可见）时，本文件守卫必须红。"""
    import shutil
    import subprocess
    import sys

    original = PAGE.read_text(encoding="utf-8")
    anchor = '"futu_paper_sandbox": "模拟盘 (paper)",'
    assert anchor in original, "paper 标签锚点丢失，无法构造变异"
    mutated = original.replace(anchor, "").replace(
        '"futu_real_master": "实盘 (real)",', ""
    )
    assert mutated != original
    try:
        PAGE.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(Path(__file__))],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "删掉租户标签后守卫仍绿 —— paper 拦截可再次静默"
    finally:
        PAGE.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
