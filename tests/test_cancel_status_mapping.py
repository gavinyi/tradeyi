"""撤单状态映射：券商 `FAILED` 不能一律记成失败（2026-10-27 修复）。

缺陷（实盘 $0.01 探针实测）：我方 5.3s 撤单成功、成交 0 股，但券商侧状态回报 `FAILED`，
本地 `order_intents.status` 直接记 `FAILED` → 「主动撤单」与「券商拒绝」在账上无法区分，
事后既算不出撤单率，也会把主动撤单误当失败订单去排查。

判定只用现有字段（不新增表结构）：
- 已提交 = `order_intents` 存在该 `broker_order_id`（券商受理过）；
- 我方发过撤单 = intent 状态 `CANCEL_REQUESTED`，或审计里有该 intent 的撤单事件；
- 成交股数 = 券商回报的 `dealt_qty`。

三档结论：已提交+0 成交+有撤单 → `CANCELLED`；未提交被拒 → `FAILED`；
部分成交后撤单 → `CANCELLED_PART`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.execution import resolve_cancel_aware_status  # noqa: E402


def test_submitted_zero_fill_with_cancel_becomes_cancelled():
    """已提交 + 成交 0 + 我方发过撤单 → CANCELLED（这就是 2026-10-27 探针单的情形）。"""
    assert resolve_cancel_aware_status(
        "FAILED", filled_quantity=0, submitted=True, cancel_requested=True,
    ) == "CANCELLED"


def test_rejected_before_submit_stays_failed():
    """券商直接拒绝（未提交成功）→ 必须仍是 FAILED，不许洗成主动撤单。"""
    assert resolve_cancel_aware_status(
        "FAILED", filled_quantity=0, submitted=False, cancel_requested=False,
    ) == "FAILED"
    # 极端错配：即使有撤单痕迹，未提交也不能改判
    assert resolve_cancel_aware_status(
        "FAILED", filled_quantity=0, submitted=False, cancel_requested=True,
    ) == "FAILED"


def test_partial_fill_then_cancel_becomes_cancelled_part():
    """部分成交后撤单 → CANCELLED_PART（成交不能被丢掉）。"""
    assert resolve_cancel_aware_status(
        "FAILED", filled_quantity=3, submitted=True, cancel_requested=True,
    ) == "CANCELLED_PART"


def test_submitted_but_no_cancel_attempt_stays_failed():
    """已提交但**我们没发过撤单** → 保持 FAILED（券商侧真失败）。"""
    assert resolve_cancel_aware_status(
        "FAILED", filled_quantity=0, submitted=True, cancel_requested=False,
    ) == "FAILED"


def test_non_failed_statuses_pass_through():
    """本函数只管 FAILED 这一种歧义，其他券商状态原样返回。"""
    for raw in ("FILLED", "CANCELLED", "CANCELLED_PART", "SUBMITTED"):
        assert resolve_cancel_aware_status(
            raw, filled_quantity=1, submitted=True, cancel_requested=True,
        ) == raw


class _FakeStore:
    def __init__(self, intent=None, events=None) -> None:
        self._intent = intent
        self._events = events or []

    def intent_by_broker_order(self, broker_order_id):
        return self._intent

    def audit_events(self, limit=500):
        return list(self._events)


def _manager_with(store):
    from futu_quant.execution import ExecutionManager

    manager = ExecutionManager.__new__(ExecutionManager)  # 只测证据解析，不构造依赖
    manager.store = store
    return manager


def test_evidence_from_intent_cancel_requested():
    """证据来源①：intent 状态为 CANCEL_REQUESTED（cancel_stale_orders 发撤单前写入）。"""
    store = _FakeStore(intent={"id": 7, "status": "CANCEL_REQUESTED"})
    manager = _manager_with(store)
    assert manager.resolve_status_with_cancel_evidence("BROKER-1", "FAILED", 0) == "CANCELLED"


def test_evidence_from_cancel_audit_event():
    """证据来源②：intent 状态已被别的推送路径推到终态，但审计里有该 intent 的撤单事件。"""
    store = _FakeStore(
        intent={"id": 42, "status": "SUBMITTED"},
        events=[
            {"event_type": "STALE_ORDER_CANCELLED",
             "payload_json": json.dumps({"intent_id": 42, "age": 130})},
        ],
    )
    manager = _manager_with(store)
    assert manager.resolve_status_with_cancel_evidence("BROKER-2", "FAILED", 0) == "CANCELLED"


def test_no_intent_means_broker_rejection():
    """找不到 intent（券商没受理过）→ 无证据 → FAILED。"""
    manager = _manager_with(_FakeStore(intent=None))
    assert manager.resolve_status_with_cancel_evidence("BROKER-3", "FAILED", 0) == "FAILED"


def test_evidence_read_failure_is_fail_safe():
    """读库异常 → 按无证据处理（宁可保留 FAILED，也不把券商拒绝洗成撤单）。"""

    class _BrokenStore:
        def intent_by_broker_order(self, broker_order_id):
            raise RuntimeError("db locked")

        def audit_events(self, limit=500):
            raise RuntimeError("db locked")

    manager = _manager_with(_BrokenStore())
    assert manager.resolve_status_with_cancel_evidence("BROKER-4", "FAILED", 0) == "FAILED"
