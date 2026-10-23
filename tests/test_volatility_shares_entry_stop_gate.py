"""P1-3 回归：波动率开仓必须**复用**共享 `run_campaign` 的单笔止损限额门。

事实（核过代码，不是假设）：
- `scripts/run_volatility_strategy.py:30/32` 从 `run_gamma_strategy` 导入 `run_campaign`，
  `:284` 在 `execute_campaign()` 里调用它 —— 波动率与财报事件**共用同一个执行函数**；
- 单笔止损限额门 `_stop_limit_problems(campaign)` 定义在 `run_gamma_strategy.py:347`，
  调用点在 `:1383`，位于 `run_campaign` 内 `if action == "BUY":` 的开仓分支。

所以 P1-3 的正解是**证明这条复用路径成立**，而不是在波动率脚本里再写第二份门禁
（`fail_closed_design_rules.md` 第 5 条：一个概念只能有一份实现）。

本文件守住三件事：
1. 波动率脚本确实调用共享 `run_campaign`（复用而非旁路）；
2. 波动率脚本**没有**自己实现限额判定（一旦有人加第二份，这里立刻红）；
3. 共享门禁在 `run_campaign` 的开仓分支内、且**早于**下单调用 —— 位置对了才有拦截力。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GAMMA = ROOT / "scripts" / "run_gamma_strategy.py"
VOL = ROOT / "scripts" / "run_volatility_strategy.py"
GATE_EVENT = "VOLATILITY_STOP_LIMIT_MISSING"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name} —— 结构被改写，守卫需要同步复核")


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def test_volatility_runner_reuses_shared_run_campaign():
    """波动率执行必须走共享 `run_campaign`，不得自己起一条执行链。"""
    tree = _tree(VOL)
    run_once = _function(tree, "run_once")
    calls = _calls_named(run_once, "run_campaign")
    assert calls, (
        "run_once 内没有调用共享 run_campaign —— 波动率开仓绕过了财报/波动率共用的执行与限额门"
    )
    # 导入端必须来自共享模块，否则「同名不同物」也会让上面的断言假绿
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "run_campaign" in imported, "run_campaign 不是从共享模块导入的，可能被本地重新定义"


def test_volatility_runner_does_not_reimplement_the_gate():
    """P1-3 只能有一份实现：波动率脚本不得出现自己的限额门。"""
    source = VOL.read_text(encoding="utf-8")
    tree = _tree(VOL)
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_stop_limit_problems" not in defined, (
        "波动率脚本自行实现了 _stop_limit_problems —— 同一概念出现两份实现，口径迟早漂移"
    )
    # 也不许直接内联判定：门禁事件只能由共享函数产生
    assert GATE_EVENT not in source, (
        f"波动率脚本内联了 {GATE_EVENT} 判定 —— 请复用 run_campaign 的共享门禁，不要写第二份"
    )


def test_shared_gate_runs_inside_buy_branch_before_order_submission():
    """共享门禁必须在 `action == \"BUY\"` 的开仓分支内，且早于下单调用。"""
    tree = _tree(GAMMA)
    run_campaign = _function(tree, "run_campaign")

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(run_campaign):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    gates = _calls_named(run_campaign, "_stop_limit_problems")
    assert gates, "run_campaign 内没有调用 _stop_limit_problems —— 开仓缺省止损限额将不再拦截"

    gate = gates[0]
    # 祖先链里必须存在 `if action == "BUY":`（字符串比较的字面量形态由代码决定）
    chain, walker = [], gate
    while walker in parents:
        walker = parents[walker]
        chain.append(walker)
    buy_branches = [
        node for node in chain
        if isinstance(node, ast.If)
        and any(
            isinstance(sub, ast.Compare)
            and any(isinstance(c, ast.Constant) and c.value == "BUY" for c in sub.comparators)
            for sub in ast.walk(node.test)
        )
    ]
    assert buy_branches, "止损限额门不在 action == 'BUY' 的开仓分支内 —— 卖出/管理路径会被误拦或漏拦"

    # 早于下单：门禁行号必须小于 run_campaign 内任何 submit_limit 调用的行号
    submissions = [
        node for node in ast.walk(run_campaign)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit_limit"
    ]
    assert submissions, "run_campaign 内未找到下单调用，结构变化需重新复核守卫"
    first_submit = min(node.lineno for node in submissions)
    assert gate.lineno < first_submit, (
        f"限额门（:{gate.lineno}）晚于下单（:{first_submit}）—— 拦截发生在下单之后等于没有拦截"
    )


def test_loaded_module_really_binds_the_shared_gate():
    """运行时证据：载入后的 ``run_campaign`` 代码对象真的引用同一个门禁全局名。

    与上面的 AST 断言互补 —— AST 说明「源码里写了调用」，这里说明「import 之后
    这个调用绑定到的是本模块的那一个函数」，而不是被同名变量遮蔽成空操作。
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import run_gamma_strategy as rg  # noqa: PLC0415

    assert "_stop_limit_problems" in rg.run_campaign.__code__.co_names, (
        "载入后的 run_campaign 代码对象里没有 _stop_limit_problems —— 门禁在运行时被遮蔽/摘除"
    )
    # 波动率 Worker 持有的必须是同一个函数对象，不能是复制出来的副本
    import run_volatility_strategy as rv  # noqa: PLC0415

    assert not hasattr(rv, "_stop_limit_problems"), (
        "波动率脚本握有自己的 _stop_limit_problems 副本 —— 两份实现"
    )
    assert rv.run_campaign is rg.run_campaign, (
        "波动率脚本用的 run_campaign 与共享模块不是同一个对象 —— 存在第二条执行链"
    )
