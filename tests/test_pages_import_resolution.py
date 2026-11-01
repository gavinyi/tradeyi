"""守卫：`pages/*.py` 的模块级 `futu_quant` 导入必须真实存在。

背景（2026-11-01 生产事故）：`pages/00_today_overview.py` 写了
`from futu_quant.ui_store import ui_store`，但 **`ui_store.py` 从未被创建**；
同一页还把 `get_tenant_broker` 从 `futu_quant.broker` 导入（它实际在 `ui.py`）。
`scripts/check_streamlit_pages.py` 只做语法/AST 巡检、不做导入解析，
所以「页面一打开就 ImportError」这类阻断在任何测试里都不会红。

**实现口径（重要）**：本守卫**纯静态**，不 `import` 任何 `futu_quant` 模块 ——
只读源码 AST 判断「模块文件存在 + 符号在顶层被定义/导入」。

为什么不 import：`futu_quant` 的部分模块在导入期就会拉起重依赖
（`futu`、`exchange_calendars` 等可选运行依赖）。用 import 做校验会让守卫
在没装可选依赖的环境（例如只装了 pytest 的精简 venv）**误红**，
从而掩盖真实缺陷 —— 这与本守卫要解决的问题正好相反。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = REPO_ROOT / "pages"
SRC_DIR = REPO_ROOT / "src"


def _page_files() -> list[Path]:
    return sorted(PAGES_DIR.glob("*.py"))


def _module_level_futu_quant_imports(path: Path) -> list[tuple[str, list[str], int]]:
    """取模块级（非函数体内）的 `from futu_quant... import ...`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, list[str], int]] = []
    for node in tree.body:  # 只扫模块顶层；函数内延迟导入不阻断页面启动
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("futu_quant"):
            names = [alias.name for alias in node.names if alias.name != "*"]
            if names:
                found.append((node.module or "", names, node.lineno))
    return found


def _module_file(module_name: str) -> Path | None:
    """把 `futu_quant.a.b` 映射到源码文件（不 import）。"""
    parts = module_name.split(".")
    base = SRC_DIR.joinpath(*parts)
    as_file = base.with_suffix(".py")
    if as_file.is_file():
        return as_file
    as_pkg = base / "__init__.py"
    if as_pkg.is_file():
        return as_pkg
    return None


def _top_level_names(path: Path) -> tuple[set[str], bool]:
    """模块顶层被定义/导入的名字集合，以及是否存在 `import *`（无法静态枚举）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    has_star_import = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple):
                    names.update(
                        element.id for element in target.elts if isinstance(element, ast.Name)
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star_import = True
                else:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):  # 形如 if TYPE_CHECKING: 的再导出
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(child.name)
                elif isinstance(child, ast.ImportFrom):
                    for alias in child.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
    return names, has_star_import


def test_pages_module_level_futu_quant_imports_resolve():
    """任何页面顶层导入的 futu_quant 模块与符号都必须存在（纯静态，不 import）。"""
    problems: list[str] = []
    for path in _page_files():
        for module_name, names, lineno in _module_level_futu_quant_imports(path):
            module_file = _module_file(module_name)
            if module_file is None:
                problems.append(f"{path.name}:{lineno} 模块不存在：{module_name}")
                continue
            defined, has_star_import = _top_level_names(module_file)
            if has_star_import:
                # 存在 `import *` 时无法静态枚举，只保证模块本身存在
                continue
            for name in names:
                if name not in defined:
                    problems.append(
                        f"{path.name}:{lineno} {module_name} 顶层没有符号 {name}"
                    )
    assert not problems, "页面顶层导入无法解析，页面一打开就会崩：\n" + "\n".join(problems)


def test_today_overview_has_no_ui_store_dependency():
    """回归：`ui_store` 从未存在过，页面不得再引用它。"""
    path = PAGES_DIR / "00_today_overview.py"
    source = path.read_text(encoding="utf-8")
    assert "ui_store" not in source, "`futu_quant.ui_store` 模块不存在，引用它会导致页面启动失败"


def test_guard_would_catch_a_missing_module_and_symbol():
    """自检：守卫本身必须能红（否则它只是装饰）。"""
    assert _module_file("futu_quant.ui_store") is None, "ui_store 若被创建，本自检需换样例"

    defined, _ = _top_level_names(_module_file("futu_quant.broker"))
    assert "get_tenant_broker" not in defined, "broker 里不该有这个符号（它在 ui.py）"

    ui_defined, _ = _top_level_names(_module_file("futu_quant.ui"))
    assert "get_tenant_broker" in ui_defined


def test_guard_passes_without_optional_runtime_dependencies(tmp_path):
    """硬要求（Chief 2026-11-01）：未装 `futu` / `exchange_calendars` 时也必须绿。

    做法：起子进程，用 meta_path 拦截器把可选运行依赖设为不可导入，再跑守卫本体。
    `futu_quant` 里不少模块在导入期就拉这些依赖 —— 只要守卫还在 import 任何
    `futu_quant` 模块，这里就会红。
    """
    import subprocess
    import sys

    runner = tmp_path / "run_guard_static.py"
    runner.write_text(
        "import sys, importlib.abc\n"
        "BLOCKED = {'futu', 'exchange_calendars'}\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in BLOCKED:\n"
        "            raise ImportError('blocked optional dep: ' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        f"sys.path.insert(0, r'{Path(__file__).resolve().parent}')\n"
        "import test_pages_import_resolution as guard\n"
        "guard.test_pages_module_level_futu_quant_imports_resolve()\n"
        "guard.test_today_overview_has_no_ui_store_dependency()\n"
        "guard.test_guard_would_catch_a_missing_module_and_symbol()\n"
        "print('STATIC-GUARD-OK')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(runner)], capture_output=True, text=True, timeout=120,
    )

    assert completed.returncode == 0, (
        "守卫在缺少可选运行依赖时误红：\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "STATIC-GUARD-OK" in completed.stdout
