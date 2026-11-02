"""守卫：主导航顺序 —— 高频入口别沉底，「业务配置」别占主位。

用户诉求（2026-11-02）：
1. 「把业务配置这个功能的位置挪下去，不要占了主要的位置，用得不是很多的」；
2. 但「今日总览」是最常看的那个（Reviewer 指出：它原先在业务配置组里，跟着一起沉底了）。

因此本守卫锁三条：

- **今日总览在第一组最前**（高频、每天第一眼看）；
- **「业务配置」是最后一组**，且只放 4 个真的「配置」入口；
- **今日总览不得出现在「业务配置」组里** —— 下次有人塞回去，本守恒也是红的。

`币量化` 组只在 `ENABLE_CRYPTO_MODULE=true` 时动态追加，不参与字面顺序断言。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

OVERVIEW = "pages/00_today_overview.py"
CONFIG_ENTRIES = (
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


def _groups() -> list[tuple[str, ast.expr]]:
    nav = _navigation_dict()
    return [
        (key.value, value)
        for key, value in zip(nav.keys, nav.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    ]


def _group_names() -> list[str]:
    return [name for name, _ in _groups()]


def _pages_in_group(group: str) -> list[str]:
    for name, value in _groups():
        if name != group:
            continue
        pages: list[str] = []
        for node in ast.walk(value):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "Page"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                pages.append(node.args[0].value)
        return pages
    return []


def test_business_config_group_is_last():
    names = _group_names()

    assert names[-1] == "业务配置", f"业务配置必须在最后一组，实际顺序：{names}"


def test_first_group_is_accounts_and_trading():
    names = _group_names()

    assert names[0] == "账户与交易", f"主位应是「账户与交易」，实际：{names[0]}"


def test_today_overview_is_first_entry_of_first_group():
    names = _group_names()
    pages = _pages_in_group(names[0])

    assert pages and pages[0] == OVERVIEW, (
        f"今日总览必须放在第一组最前（每天第一眼看），实际第一组顺序：{pages[:3]}"
    )


def test_today_overview_is_not_in_business_config_group():
    """Reviewer 2026-11-02：总览跟着业务配置沉底就是这次要修的问题，不许回退。"""
    assert OVERVIEW not in _pages_in_group("业务配置")


def test_business_config_group_holds_only_the_four_config_entries():
    pages = _pages_in_group("业务配置")

    assert pages == list(CONFIG_ENTRIES), f"业务配置组内容变了：{pages}"


def test_every_entry_registered_exactly_once():
    source = APP.read_text(encoding="utf-8")

    for entry in (OVERVIEW, *CONFIG_ENTRIES):
        assert source.count(f'"{entry}"') == 1, f"{entry} 在 app.py 里注册了多次"
