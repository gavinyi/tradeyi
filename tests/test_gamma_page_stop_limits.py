"""P1-2 回归：财报 Gamma 页面必须能配置并保存「单笔止损限额」。

背景（`docs/fail_closed_design_rules.md` 第 3、4 条）：
执行侧对 `max_position_loss_ratio` / `max_position_loss_usd` 是 **缺省即拒绝开仓**
（`VOLATILITY_STOP_LIMIT_MISSING`，`scripts/run_gamma_strategy.py:1383`）。
如果页面不提供输入项、保存时不写入 campaign，那么从「财报 Gamma 策略」页保存的每一条
策略都会**永久开不了仓**，而且用户看不到原因 —— 这就是「配置错误不可见」。

本文件锁定三件事，用 AST 而不是字符串匹配，避免注释/文案造成假绿：
1. 页面存在两个数值输入项（默认值与生效口径一致 0.25 / 300.0）；
2. campaign 字面量里**显式**带上这两个键，且取值为该输入变量的 `float(...)` 转换；
3. 两者都来自同一处 UI 变量，禁止硬编码常量绕过用户配置。
"""
from __future__ import annotations

import ast
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "5_earnings_gamma_strategy.py"
RATIO_KEY = "max_position_loss_ratio"
USD_KEY = "max_position_loss_usd"


def _tree() -> ast.Module:
    return ast.parse(PAGE.read_text(encoding="utf-8"))


def _number_input_calls(tree: ast.Module, label_keyword: str) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "number_input"):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and label_keyword in str(first.value):
            calls.append(node)
    return calls


def _campaign_literal(tree: ast.Module) -> ast.Dict:
    """找到 campaign 字面量：唯一同时含 `campaign_id` 与 `code` 键的 dict。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
        if {"campaign_id", "code"} <= keys:
            return node
    raise AssertionError("未找到 campaign 字面量（campaign_id + code），页面保存路径可能被改写")


def _input_default(call: ast.Call) -> float:
    """number_input 的第 4 个位置参数是 value（默认值）。"""
    assert len(call.args) >= 4, "number_input 未提供默认值，界面显示值会与生效口径脱节"
    default = call.args[3]
    assert isinstance(default, ast.Constant), "默认值必须是常量，否则无法核对口径"
    return float(default.value)


def test_page_exposes_both_stop_limit_inputs():
    tree = _tree()
    ratio_calls = _number_input_calls(tree, "单笔相对止损")
    usd_calls = _number_input_calls(tree, "单笔绝对止损")
    assert len(ratio_calls) == 1, f"相对止损输入项应唯一存在，实际 {len(ratio_calls)} 处"
    assert len(usd_calls) == 1, f"绝对止损输入项应唯一存在，实际 {len(usd_calls)} 处"
    # 默认值必须与执行侧生效口径一致，否则用户看到的数字会误导
    assert _input_default(ratio_calls[0]) == 0.25, "相对止损默认值必须与生效值 0.25 一致"
    assert _input_default(usd_calls[0]) == 300.0, "绝对止损默认值必须与生效值 300.0 一致"


def test_campaign_literal_persists_both_limits():
    literal = _campaign_literal(_tree())
    mapping: dict[str, ast.expr] = {}
    for key, value in zip(literal.keys, literal.values):
        if isinstance(key, ast.Constant):
            mapping[str(key.value)] = value

    for key in (RATIO_KEY, USD_KEY):
        assert key in mapping, f"保存 campaign 时未显式写入 {key} —— 该策略会被开仓门永久拒绝"
        value = mapping[key]
        assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
            and value.func.id == "float", f"{key} 必须写成 float(<输入变量>)，禁止硬编码常量"
        assert value.args and isinstance(value.args[0], ast.Name), (
            f"{key} 必须取自页面输入变量，硬编码会让界面配置形同虚设"
        )

    # 两个键必须取自**两个不同的**输入变量，否则一个输入项被覆盖成另一个
    ratio_var = mapping[RATIO_KEY].args[0].id
    usd_var = mapping[USD_KEY].args[0].id
    assert ratio_var != usd_var, f"两个限额写入了同一个变量 {ratio_var}，其中一项输入被忽略"


def test_saved_limits_are_the_ui_variables():
    """输入端与保存端必须闭环：campaign 里的变量就是 number_input 赋值的变量。"""
    tree = _tree()
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "number_input":
                assigned.update(t.id for t in node.targets if isinstance(t, ast.Name))

    mapping = {
        str(key.value): value
        for key, value in zip(_campaign_literal(tree).keys, _campaign_literal(tree).values)
        if isinstance(key, ast.Constant)
    }
    for key in (RATIO_KEY, USD_KEY):
        var = mapping[key].args[0].id
        assert var in assigned, f"{key} 取自 {var}，但该变量不是页面 number_input 的赋值目标"
