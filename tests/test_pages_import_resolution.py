"""守卫：`pages/*.py` 的模块级 `futu_quant` 导入必须真实存在。

背景（2026-11-01 生产事故）：`pages/00_today_overview.py` 写了
`from futu_quant.ui_store import ui_store`，但 **`ui_store.py` 从未被创建** ——
静态巡检 `scripts/check_streamlit_pages.py` 只做语法/AST 检查，不做导入解析，
于是「页面在重启后一打开就 ImportError」这种阻断，在任何测试里都不会红。

本守卫按 import 语义逐条解析：模块必须存在、被导入的名字必须存在。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

PAGES_DIR = Path(__file__).resolve().parents[1] / "pages"


def _page_files() -> list[Path]:
    return sorted(PAGES_DIR.glob("*.py"))


def _module_level_futu_quant_imports(path: Path) -> list[tuple[str, list[str], int]]:
    """取模块级（非函数体内）的 `from futu_quant... import ...`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, list[str], int]] = []
    for node in tree.body:  # 只扫模块顶层，函数内延迟导入不阻断页面启动
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("futu_quant"):
            names = [alias.name for alias in node.names if alias.name != "*"]
            if names:
                found.append((node.module or "", names, node.lineno))
    return found


def test_pages_module_level_futu_quant_imports_resolve():
    """任何页面顶层导入的 futu_quant 模块与符号都必须存在。"""
    missing: list[str] = []
    for path in _page_files():
        for module_name, names, lineno in _module_level_futu_quant_imports(path):
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - 缺失/导入期异常都要报出来
                missing.append(f"{path.name}:{lineno} 无法导入模块 {module_name}（{exc}）")
                continue
            for name in names:
                if not hasattr(module, name):
                    missing.append(
                        f"{path.name}:{lineno} {module_name} 里没有符号 {name}"
                    )
    assert not missing, "页面顶层导入无法解析，页面一打开就会崩：\n" + "\n".join(missing)


def test_today_overview_has_no_ui_store_dependency():
    """回归：`ui_store` 从未存在过，页面不得再引用它。"""
    path = PAGES_DIR / "00_today_overview.py"
    source = path.read_text(encoding="utf-8")
    assert "ui_store" not in source, "`futu_quant.ui_store` 模块不存在，引用它会导致页面启动失败"
