"""UI 优化包守卫（2026-11-07 五项一包）。

①假横幅修复（Ops 坐实：生产全库 `recovery_blocked*` / `strategy_reconciliation_disabled*`
  **没有一个 true**，横幅却弹了——`ui.py:473-476` 用 `settings_with_prefix` 取键名
  后**没有重新按键名重读值**，而是在推导式里引用了外层变量 `tid`/`acc` 做租户过滤，
  键名过滤与值判定混在一起：只要租户键存在（哪怕值 false）就可能误判）。
  契约：数据全 false 时横幅必须不弹；判定=键名与值两层都过才进 disabled_strats。
②复盘战报窗口标注（`feishu_reporter.dispatch_pending_review` 标题带统计窗口）。
③实盘文案修正（「实盘尚未启用」→「实盘 0 成交（N 次开仓尝试被风控拦截）」口径）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "futu_quant" / "ui.py"
REPORTER = ROOT / "src" / "futu_quant" / "feishu_reporter.py"


def _tree(path: Path) -> ast.AST:
    text = path.read_text(encoding="utf-8")
    assert text, f"{path} 为空（守卫读不到输入不能放行）"
    return ast.parse(text)


def test_banner_disabled_strats_filters_by_value_not_keyname_leak():
    """①假横幅根因守卫：`settings_with_prefix` 的键值对必须按值过滤。

    生产数据（只读实证）：8 个 `strategy_reconciliation_disabled:*` 键全部
    `false`、4 个 `recovery_blocked*` 全部 `false`——横幅却弹了。修复后
    推导式必须先取 `(key, value)` 再判 `value == "true"`，禁止只用键名列表。
    """
    source = UI.read_text(encoding="utf-8")
    tree = _tree(UI)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "render_sidebar"
    )
    seg = ast.get_source_segment(source, fn)
    assert "settings_with_prefix(" in seg, "横幅判定不再读前缀键，语义漂移"
    # 修复形态：从 prefix 结果里解包 (键, 值)，再按值过滤——不允许只在
    # 键名推导里做租户过滤而丢失值判定。
    assert ".items()" in source or ".items()" in seg or "_flag_true(" in seg, (
        "横幅判定必须遍历 (key, value) 对并按 value=='true' 过滤"
        "（假横幅根因：只拿键名列表，值判定丢失）"
    )


def test_banner_must_not_fire_when_all_false():
    """行为守卫：全 false 数据下横幅判定函数返回空列表（不弹）。"""
    from futu_quant.ui import _reconciliation_banner_rows

    flags = {
        "strategy_reconciliation_disabled:volatility_long_gamma": "false",
        "strategy_reconciliation_disabled:futu_paper_sandbox:core_satellite": "false",
        "recovery_blocked": "false",
        "recovery_blocked:20170229": "false",
    }
    rows = _reconciliation_banner_rows(
        flags, tenant_id="futu_paper_sandbox", account_id="20170229",
    )
    assert rows == [], f"全 false 数据不得触发横幅：{rows}"


def test_banner_fires_only_on_true_value():
    """真 true 才弹，且带策略名明细。"""
    from futu_quant.ui import _reconciliation_banner_rows

    flags = {
        "strategy_reconciliation_disabled:volatility_long_gamma": "false",
        "strategy_reconciliation_disabled:futu_paper_sandbox:core_satellite": "true",
        "recovery_blocked": "false",
    }
    rows = _reconciliation_banner_rows(
        flags, tenant_id="futu_paper_sandbox", account_id="20170229",
    )
    assert len(rows) == 1
    assert "core_satellite" in rows[0]


def test_review_report_title_carries_window():
    """②复盘战报标题带统计窗口（美股日 vs 自然日误解的解药）。"""
    source = REPORTER.read_text(encoding="utf-8")
    assert "窗口" in source, "复盘战报标题缺统计窗口标注"


def test_review_report_real_copy_not_misleading():
    """③实盘文案：「实盘尚未启用」必须改为带拦截计数的口径。"""
    source = REPORTER.read_text(encoding="utf-8")
    assert "尚未启用实盘交易" not in source, "误导文案仍在"
    assert "风控拦截" in source, "实盘 0 成交文案必须带拦截计数口径"


def test_mutation_removing_banner_judgment_must_be_red():
    """变异：摘掉 `_flag_true` 值判定（退回键名/存在性判断）→ 行为守卫必须红。

    旧假横幅根因是「键存在就弹」；变异模拟该回归形态：任何出现的键都算 true。
    """
    import shutil
    import subprocess
    import sys

    original = UI.read_text(encoding="utf-8")
    anchor = (
        '        return str(flags.get(key, "")).strip().lower() == "true"'
    )
    assert anchor in original, "值判定锚点丢失，无法构造变异"
    mutated = original.replace(anchor, "        return key in flags")
    assert mutated != original
    try:
        UI.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_banner_must_not_fire_when_all_false"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "值判定被摘掉后守卫仍绿 —— 假横幅可复发"
    finally:
        UI.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
