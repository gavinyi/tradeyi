"""回归：止损限额口径唯一且显式（P1-2 / P1-3）。

背景：
`volatility_hard_loss_exit_reason` 的函数默认是 `max_loss_usd=100.0`，而
`run_gamma_strategy` 的回落是 `campaign.get("max_position_loss_usd", 300)` ——
**同一件事两个数，靠「键不存在」决定用哪个**，属静默分歧（配置漂移）。

修复：
1. 函数默认值统一为 300 / 0.25（与生效值一致）；
2. 下单前强制校验 campaign 必须**显式**带 `max_position_loss_usd` 与
   `max_position_loss_ratio`，缺省即**拒绝开仓并记 ERROR**，不再静默兜底。

本文件锁定这两条。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from futu_quant.volatility_optimizer import volatility_hard_loss_exit_reason

GAMMA = Path(__file__).resolve().parents[1] / "scripts" / "run_gamma_strategy.py"


def test_function_default_matches_effective_value():
    """函数默认值必须与 campaign 回落值一致（消除 100/300 分歧）。"""
    sig = inspect.signature(volatility_hard_loss_exit_reason)
    assert sig.parameters["max_loss_usd"].default == 300.0, (
        "函数默认值与生效值不一致 —— 静默分歧会重新出现"
    )
    assert sig.parameters["max_loss_ratio"].default == 0.25


def test_entry_requires_explicit_stop_limits():
    """下单前必须校验 campaign 显式带止损限额，缺省即拒绝开仓。"""
    src = GAMMA.read_text(encoding="utf-8")
    assert "VOLATILITY_STOP_LIMIT_MISSING" in src, "缺少缺省拒绝开仓的校验门"
    assert "max_position_loss_usd" in src and "max_position_loss_ratio" in src
    # 必须是「拒绝开仓」而不是静默回落
    assert "已拒绝开仓（禁止静默兜底）" in src, "缺省不得静默兜底"


def test_missing_limit_raises_error_alert():
    """缺限额时必须记 ERROR 告警（不能只是 BLOCKED 就沉默）。"""
    src = GAMMA.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_campaign")
    body = ast.dump(fn)
    assert "VOLATILITY_STOP_LIMIT_MISSING" in body
    # 告警以 executor.alerts.send(...) 调用，AST 中表现为 'alerts' + 'send' 两个属性名
    assert "alerts" in body and "send" in body, "缺限额未发 ERROR 告警"
    # 且告警严重级别必须是 ERROR
    assert '"ERROR"' in body or "'ERROR'" in body, "缺限额告警未使用 ERROR 级别"
