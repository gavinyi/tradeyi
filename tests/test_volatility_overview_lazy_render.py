"""静态守卫：波动率总览页必须保持「按需渲染」。

2026-11-04 性能优化把 8 个 tab 与导出区改成惰性执行，靠的是 Streamlit 的状态化容器：
只有 `on_change="rerun"`（或回调）时框架才会给 tab/expander 填 `.open`，而
`if tab.open:` 才是真正跳过整块内容的开关。一旦有人把这层守卫改掉或删掉，页面会
静默退回「每次 rerun 全量渲染 8 个 tab + 5 个导出文件」，性能问题无声复活，
所以用 AST 静态检查钉死，不依赖 Streamlit 运行时。
"""

from __future__ import annotations

import ast
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "30_volatility_strategy_overview.py"
TAB_COUNT = 8


def _tree() -> ast.Module:
    return ast.parse(PAGE.read_text(encoding="utf-8"))


def _calls(tree: ast.AST, dotted: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == dotted
    ]


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def test_tabs_are_stateful_so_open_exists():
    # 页面里有两处 st.tabs：外层 8 个主 tab，以及「当前持仓」里的 5 个内嵌 tab。
    calls = [
        call for call in _calls(_tree(), "st.tabs")
        if isinstance(call.args[0], ast.List)
        and any(ast.literal_eval(element) == "雷达流水线" for element in call.args[0].elts)
    ]
    assert len(calls) == 1, "找不到外层 8-tab 容器"
    kwargs = _kwargs(calls[0])
    assert "on_change" in kwargs, "st.tabs 没有 on_change 时 tab 不会有 .open"
    assert ast.literal_eval(kwargs["on_change"]) == "rerun"
    assert ast.literal_eval(kwargs["key"]) == "overview_tab"


def test_every_tab_body_is_guarded_by_open():
    tree = _tree()
    guards: dict[str, ast.If] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Attribute) and test.attr == "open":
            guards[ast.unparse(test.value)] = node

    for index in range(TAB_COUNT):
        target = f"overview_tabs[{index}]"
        assert target in guards, f"{target} 的内容没有被 if {target}.open: 包住（会全量渲染）"
        body_with = [
            item for item in guards[target].body
            if isinstance(item, ast.With)
            and ast.unparse(item.items[0].context_expr) == target
        ]
        assert body_with, f"{target} 的守卫里缺少 with {target}:"


def test_export_downloads_are_built_only_when_expander_open():
    tree = _tree()
    expanders = [
        call for call in _calls(tree, "st.expander")
        if call.args and ast.literal_eval(call.args[0]) == "导出当前复盘数据"
    ]
    assert len(expanders) == 1, "导出区应当是唯一的『导出当前复盘数据』expander"
    kwargs = _kwargs(expanders[0])
    assert ast.literal_eval(kwargs["on_change"]) == "rerun"

    guards = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "open"
        and ast.unparse(node.test.value) == "export_panel"
    ]
    assert len(guards) == 1, "缺少 if export_panel.open: 守卫"
    guard_ids = {id(node) for node in ast.walk(guards[0])}

    downloads = _calls(tree, "st.download_button")
    assert len(downloads) == 5, f"导出按钮应当是 5 个，实际 {len(downloads)}"
    outside = [node for node in downloads if id(node) not in guard_ids]
    assert not outside, "有下载按钮在 if export_panel.open: 之外，会每轮 rerun 都生成 CSV/Markdown"
