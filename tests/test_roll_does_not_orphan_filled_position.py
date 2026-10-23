"""回归：滚动不得孤立已成交仓位（21:49 接管空窗的根因修复）。

背景（10-22 实盘事故）：META 看跌 21:39:21 成交，但滚动判定用的是**本轮开头**
的持仓快照（`run_volatility_strategy.run_once` 只取一次），成交发生在快照之后
→ 旧合约被判成「无持仓」而删除 → 已成交仓位成孤儿，止损系统看不到它，
直到 22:01 硬止损以 $366 超调平仓。

修复（三条，本文件锁定防回归）：
1. 滚动前重取持仓/委托快照；
2. 「保留旧合约退出维护」的判定额外纳入成交账本（`has_recorded_fill`），
   覆盖券商快照尚未反映成交的窗口；
3. 表达复核的门槛同样排除「已有成交记录」的合约。
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "scripts" / "run_volatility_strategy.py"


def _lines() -> list[tuple[int, str]]:
    return list(enumerate(TARGET.read_text(encoding="utf-8").splitlines(), start=1))


def _run_once_span(lines: list[tuple[int, str]]) -> tuple[int, int]:
    """返回 run_once 函数体的行号区间 (start, end)。"""
    start = next(i for i, t in lines if re.match(r"\s*def run_once\(", t))
    end = len(lines)
    for i, t in lines[start + 1:]:
        if re.match(r"^def |^class ", t):
            end = i - 1
            break
    return start, end


def _body(lines: list[tuple[int, str]], span: tuple[int, int]) -> str:
    lo, hi = span
    return "\n".join(t for i, t in lines if lo < i <= hi)


def test_roll_is_guarded_by_fresh_position_snapshot():
    """滚动判定前必须重取券商持仓快照，不能沿用本轮开头的旧快照。"""
    lines = _lines()
    span = _run_once_span(lines)
    body = _body(lines, span)

    assert "broker.positions()" in body, "滚动前未重取持仓快照（陈旧快照会把已成交仓位判成无持仓）"
    assert "broker.orders()" in body, "滚动前未重取委托快照"

    refresh_line = next(i for i, t in lines if "positions = broker.positions()" in t and i > span[0])
    gate_line = next(i for i, t in lines if "should_review_expression" in t and i > span[0])
    assert refresh_line < gate_line, "持仓快照重取必须发生在滚动判定之前"


def test_old_campaign_retention_considers_fill_ledger():
    """旧合约仍有成交时必须保留为退出维护配置，不得直接删除。"""
    lines = _lines()
    span = _run_once_span(lines)
    body = _body(lines, span)

    assert "has_recorded_fill(old_code)" in body, (
        "旧合约保留判定未纳入成交账本 —— 券商快照滞后时仍会把已成交合约判成无持仓并删除"
    )


def test_review_gate_excludes_codes_with_fills():
    """表达复核门槛必须排除「已有成交记录」的合约。"""
    lines = _lines()
    span = _run_once_span(lines)
    body = _body(lines, span)

    assert "not has_recorded_fill(current_code)" in body, (
        "表达复核门槛未排除已有成交的合约 —— 刚成交的合约仍会被滚动替换"
    )


def test_has_recorded_fill_reads_fill_ledger_and_filters_nonzero():
    """has_recorded_fill 必须真的读成交账本，且只认非零成交。"""
    src = TARGET.read_text(encoding="utf-8")
    assert "def has_recorded_fill(" in src, "has_recorded_fill 未定义"
    assert "store.fills(" in src, "has_recorded_fill 未读成交账本"
    assert 'row.get("quantity")' in src, "has_recorded_fill 未按成交数量过滤（零成交不应视为有持仓）"


def test_has_recorded_fill_degrades_safely_on_store_error():
    """账本读取失败时必须回落为 False（宁可滚动，不可因异常卡死整个循环）。"""
    src = TARGET.read_text(encoding="utf-8")
    # 取 has_recorded_fill 的函数体做局部校验
    start = src.index("def has_recorded_fill(")
    end = src.index("\n    # Perform all held-position exits", start)
    block = src[start:end]
    assert "except Exception" in block, "has_recorded_fill 未捕获账本读取异常"
    assert "return False" in block, "has_recorded_fill 异常时必须回落 False（True 会把所有合约锁死）"
