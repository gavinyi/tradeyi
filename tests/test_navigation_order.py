"""守卫：主导航顺序 —— 「业务配置」组必须排在**最后**，不占主位。

用户诉求（2026-11-02）：「把业务配置这个功能的位置挪下去，不要占了主要的位置，
用得不是很多的」。

判据（按 `app.py` 里 `navigation` 字典的字面顺序，Streamlit 侧边栏按该顺序渲染）：

1. `业务配置` 必须是最后一组（币量化模块只在 `ENABLE_CRYPTO_MODULE=true` 时动态追加，
   不算在字面顺序里）；
2. 五个业务入口仍必须全部注册（挪位置 ≠ 删入口）；
3. 首页驾驶舱/账户总览仍属第一组，主位保持原样。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

BUSINESS_ENTRIES = (
    "pages/00_today_overview.py",
    "pages/44_strategy_center.py",
    "pages/45_risk_and_limits.py",
    "pages/46_account_and_env.py",
    "pages/47_ops_and_notifications.py",
)


def _navigation_dict() -> ast.Dict:
    tree = ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "navigation" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return node.value
    raise AssertionError("app.py 里找不到 navigation 字典")


def _group_names() -> list[str]:
    names: list[str] = []
    for key in _navigation_dict().keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.append(key.value)
    return names


def _pages_in_group(group: str) -> list[str]:
    nav = _navigation_dict()
    for key, value in zip(nav.keys, nav.values):
        if isinstance(key, ast.Constant) and key.value == group:
            return [
                node.args[0].value
                for node in ast.walk(value)
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "Page"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ]
    return []


def test_business_config_group_is_last():
    names = _group_names()

    assert names[-1] == "业务配置", f"业务配置必须在最后一组，实际顺序：{names}"


def test_business_config_group_is_not_first_anymore():
    names = _group_names()

    assert names[0] == "账户与交易", f"主位应保留给「账户与交易」，实际：{names[0]}"


def test_all_five_business_entries_still_registered():
    pages = _pages_in_group("业务配置")

    for entry in BUSINESS_ENTRIES:
        assert entry in pages, f"挪位置不等于删入口：{entry} 丢了"


def test_business_pages_registered_exactly_once():
    source = APP.read_text(encoding="utf-8")

    for entry in BUSINESS_ENTRIES:
        assert source.count(f'"{entry}"') == 1, f"{entry} 在 app.py 里注册了多次"
