"""回归：额度/限额类命名必须显式携带方向语义（10-23 Product 规范）。

背景（本次事故的根因之一）：
函数原名 `submitted_real_notional_since` —— **名字里没有「开仓」**。
于是它的语义被理解成「所有在途单的名义金额」，后来者**不知道它该过滤 `side`**，
把卖出也算进开仓额度 → 「白天开仓用满 → 晚上盈利的平仓单被自己的买入额度挡住」，
10-22 实测连续 9 笔卖出被拒、仓位持有到到期。

Product 据此立规：**额度/限额相关的函数、配置键、审计事件，命名必须显式携带
方向与语义（开仓 / 减仓 / 名义 / 盈亏）**，禁止用无方向名。

本文件把这条规范钉成可执行守卫 —— 否则它只是一句写在 PRD 里的话。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

REAL_TRADING = ROOT / "src" / "futu_quant" / "real_trading.py"

# 无方向的额度名 —— 出现即违规（历史事故名，不得回潮）
DIRECTION_LESS_NAMES = (
    "submitted_real_notional_since",       # 本次事故的原名
    "submitted_notional_since",            # 无方向的通用名（同名不同实现也曾并存）
)

# 允许的额度函数名：必须带 entry / reduction 等方向词
DIRECTION_WORDS = ("entry", "reduction")


def _quota_function_defs(path: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "notional" in node.name
    ]


def test_entry_and_reduction_helpers_are_direction_named():
    """开仓/减仓两个额度函数必须成对且名字带方向词。"""
    from futu_quant import real_trading as rt

    names = [n for n in dir(rt) if "notional_since" in n]
    assert "submitted_real_entry_notional_since" in names, (
        "开仓额度函数必须命名为 ..._entry_...（名字即契约）"
    )
    assert "submitted_real_reduction_notional_since" in names, (
        "减仓额度函数必须命名为 ..._reduction_..."
    )


def test_no_direction_less_notional_name_anywhere():
    """全仓库不得再出现**作为代码标识符**的无方向额度名。

    只查 AST 里的函数定义与属性访问（真实代码），不查注释/文档字符串 ——
    文档里提到旧名（说明它曾是事故根源）是**应该的**，不是违规。
    """
    offenders: list[str] = []
    for base in ("src", "scripts", "tests", "pages"):
        for path in (ROOT / base).rglob("*.py"):
            # 本守卫文件自身含这些名字（作为常量），跳过
            if path.name == Path(__file__).name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = node.name
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                if name in DIRECTION_LESS_NAMES:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert not offenders, f"出现无方向额度名（会重蹈 10-22 覆辙）：{offenders}"


def test_every_notional_function_name_carries_direction():
    """`real_trading.py` 里所有含 notional 的函数名都必须带方向词。"""
    for fn in _quota_function_defs(REAL_TRADING):
        if "since" not in fn.name:
            continue  # 只看「额度统计」类函数
        assert any(word in fn.name for word in DIRECTION_WORDS), (
            f"函数 {fn.name} 是额度统计函数但名字无方向语义 —— "
            f"必须带 {'/'.join(DIRECTION_WORDS)} 之一"
        )


def test_naming_rule_is_documented_in_code():
    """命名规范必须写进代码注释（否则下一个人看不到）。"""
    doc = REAL_TRADING.read_text(encoding="utf-8")
    assert "命名" in doc and ("方向" in doc), (
        "命名规范未落进代码注释 —— 规范必须可被读到，不能只存在于 PRD"
    )


def test_reduction_limits_reuse_risk_limits():
    """减仓额度口径必须复用 RiskLimits（不得第三份实现）。"""
    from futu_quant.real_trading import reduction_notional_limits
    from futu_quant.risk import RiskLimits

    limits = RiskLimits.load()
    order_limit, daily_limit = reduction_notional_limits()
    assert (order_limit, daily_limit) == (
        limits.max_reduction_order_notional, limits.max_daily_reduction_notional,
    ), "减仓额度未与 RiskLimits 同源 —— 会形成第三套实现"
