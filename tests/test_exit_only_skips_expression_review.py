"""回归：`exit_only` campaign 不得被表达复核改写合约（P0-2）。

背景（10-22 META 事故根因）：
被接管的 `exit_only` campaign 只管理退出。但表达复核（`should_review_expression`）
与到期滚动（`needs_expiry_roll`）对它们照常跑，`roll_campaign()` 按「当前最佳表达」
重选行权价并把 `code` 覆写 —— 于是 campaign 指向一个**不存在的合约**，
真实持仓瞬间失去归属，而止损评估只遍历 campaign 列表 → 仓位进入无保护窗口。

修复：两个门槛都排除 `exit_only`。本文件锁定该不变量。
"""
from __future__ import annotations

import ast
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "scripts" / "run_volatility_strategy.py"


def _run_once_source() -> str:
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_once")
    return "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])


def test_expression_review_skips_exit_only_campaigns():
    """表达复核门槛必须排除 exit_only。"""
    body = _run_once_source()
    assert "not campaign.get(\"exit_only\")" in body, (
        "表达复核未排除 exit_only —— 接管后的在险持仓会被改写合约号，失去归属"
    )


def test_expiry_roll_skips_exit_only_campaigns():
    """到期滚动门槛必须排除 exit_only（到期由退出逻辑处理，不靠改写成别的合约）。"""
    body = _run_once_source()
    assert "not campaign.get(\"exit_only\")\n" in body or \
           'not campaign.get("exit_only")' in body, "到期滚动未排除 exit_only"
    # 确认 needs_expiry_roll 的定义里带上了 exit_only 排除
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_once")
    seg = "\n".join(TARGET.read_text(encoding="utf-8").splitlines()[fn.lineno - 1 : fn.end_lineno])
    idx = seg.index("needs_expiry_roll =")
    block = seg[idx : idx + 200]
    assert "exit_only" in block, "needs_expiry_roll 未排除 exit_only"


def test_shared_retain_guard_still_wired():
    """回归：共享护栏仍被两条路径使用（P0-2 的另一半）。"""
    worker = TARGET.read_text(encoding="utf-8")
    scanner = Path("src/futu_quant/volatility_scanner.py").read_text(encoding="utf-8")
    assert "retain_campaign_if_at_risk" in worker, "worker 路径未使用共享护栏"
    assert "retain_campaign_if_at_risk" in scanner, "雷达路径未使用共享护栏"


def test_exit_only_skip_is_not_silent():
    """exit_only 跳过必须留 WARN —— 静默 return 曾让真实持仓裸奔（Ops 10-22 排查）。"""
    body = _run_once_source()
    assert "EXIT_ONLY_CAMPAIGN_SKIPPED" in body, (
        "exit_only campaign 被跳过时没有任何事件 —— 合约身份被覆写后仓位无人管理，日志里无声"
    )
    assert '"WARNING"' in body, "跳过事件必须是 WARNING 级别"
    # 必须能区分「券商仍持有该 code（危险）」与「正常跳过」
    assert "broker_holds_code" in body, "跳过事件未标出券商是否仍持有该合约"
    assert "legs" in body and "broker_positions" in body, "跳过事件未带 legs/持仓快照，无法定位"
