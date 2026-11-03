"""G 方案（2026-11-04）：买不起**只报警、不停机**。

真实事故链：权益 $6,640、容量线 $398，AMD 单张 $1,740（`needed_equity=$29,000`）。
连续 3 轮 `PREMIUM_OVER_CAPACITY` → 自动暂停 → 而解除暂停只发生在「出现装得下的候选」，
于是「账户小 → 永久停机 → 只能人工解」的死循环，两天内被人工解了两次。

新口径：
- 买不起 → 升级 ERROR 告警（每天最多一条），**不写 `volatility_new_entries_paused`、不写 marker**；
- 人工/故障暂停语义完全不变，且永不被这条链路改写；
- 容量预检本身不放宽：买不起的候选照旧被拒（`premium > line` 直接筛掉）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from futu_quant.capacity_precheck import (
    MARKER_KEY,
    REASON_PREMIUM,
    CapacityPrecheck,
    record_selection_round,
)
from futu_quant.store import TradingStore
from futu_quant.volatility_pause import (
    set_volatility_new_entries_paused,
    volatility_new_entries_paused_for_tenant,
)

TENANT = "futu_real_master"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _bought_out() -> CapacityPrecheck:
    return CapacityPrecheck(
        equity=6640.73, line=398.44, reasons={REASON_PREMIUM: 2}, cheapest_premium=1740.0,
    )


def _exhausted_alerts(store) -> list:
    return [row for row in store.alerts(limit=200) if "VOLATILITY_CAPACITY_EXHAUSTED" in str(row)]


# --------------------------------------------------------------------------- #
# 只报警，不停机
# --------------------------------------------------------------------------- #


def test_ten_buyout_rounds_never_pause_but_do_alert(tmp_path):
    """Reviewer 证据要求：连续 10 轮 buyout **不得**写 `paused=true`，但必须有 ERROR。"""
    store = TradingStore(tmp_path / "alert_only.db")
    for _ in range(10):
        state = record_selection_round(store, TENANT, _bought_out())

    assert state["streak"] == 10
    assert state["action"] == "insufficient"
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT), "") == ""
    events = [
        row for row in store.operational_events(limit=200)
        if str(row.get("event_type")) == "VOLATILITY_CAPACITY_EXHAUSTED"
    ]
    assert events, "不暂停不等于不告警：必须有 ERROR 事件留痕"


def test_error_alert_is_at_most_one_per_day(tmp_path):
    store = TradingStore(tmp_path / "alert_only.db")
    for _ in range(10):
        record_selection_round(store, TENANT, _bought_out())

    assert len(_exhausted_alerts(store)) == 1, "每轮都告警会把人吵死；按天去重"


def test_manual_pause_is_never_touched_or_re_alerted(tmp_path):
    """人工/故障暂停：买不起链路一个字都不写，也不重复告警。"""
    store = TradingStore(tmp_path / "alert_only.db")
    set_volatility_new_entries_paused(store, TENANT, True)
    before = len(_exhausted_alerts(store))

    for _ in range(10):
        state = record_selection_round(store, TENANT, _bought_out())

    assert state["paused"] is True
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT), "") == ""
    assert len(_exhausted_alerts(store)) == before, "人工暂停期间不应再刷「容量耗尽」告警"


# --------------------------------------------------------------------------- #
# 预检本身不得放宽：买不起的候选照旧被拒
# --------------------------------------------------------------------------- #


def test_buyout_candidates_are_still_rejected_by_the_precheck():
    from futu_quant.capacity_precheck import filter_candidates, minimum_premium

    line = 398.44
    row = {"bid_price": 17.35, "ask_price": 17.40}  # $1,740 一张
    assert minimum_premium(row) == pytest.approx(1740.0)
    assert minimum_premium(row) > line
    result = filter_candidates(
        pd.DataFrame([row]), equity=6640.73, spot=250.0, loss_limit=line,
    )
    assert result.empty, "G 方案只改停机行为，绝不放宽容量预检"


# --------------------------------------------------------------------------- #
# 结构守卫：实盘「先按容量筛、再选约」，且只对 real 生效
# --------------------------------------------------------------------------- #


def _parse_roll() -> tuple[ast.AST, str]:
    path = REPO_ROOT / "src" / "futu_quant" / "volatility_roll.py"
    return ast.parse(path.read_text(encoding="utf-8")), path.read_text(encoding="utf-8")


def _calls_in(node: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            target = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if target == name:
                found.append(child)
    return found


def test_real_capacity_precheck_runs_before_candidate_selection():
    """`_apply_real_capacity_precheck` 必须早于 `eligible.iloc[0]`（选约）。

    变异守卫：把预检挪到选约之后（或删掉），这里立刻红 —— 那正是
    「选中最贵那张、再判 $0 张」的模拟盘形态，实盘不能出现。
    """
    tree, _source = _parse_roll()

    roll = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "roll_campaign"
        ),
        None,
    )
    assert roll is not None, "没找到 roll_campaign，守卫脚本需更新"

    precheck_lines = [
        call.lineno
        for node in ast.walk(roll)
        if isinstance(node, ast.If)
        and _calls_in(node.test, "is_real_account")
        for call in _calls_in(node, "_apply_real_capacity_precheck")
    ]
    assert precheck_lines, "实盘容量预检必须位于 `if is_real_account(broker)` 分支内"

    # 只看「从候选/合格集合里取第 0 张」——快照 `.iloc[0]` 不是选约。
    selection_lines = [
        node.lineno for node in ast.walk(roll)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "iloc"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id in {"eligible", "candidates"}
    ]
    assert selection_lines, "没找到选约行（eligible.iloc[...]），守卫脚本需更新"
    assert min(precheck_lines) < min(selection_lines), (
        "容量预检必须在选约之前：否则会先选中最贵那张再判 0 张"
    )
