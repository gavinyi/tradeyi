"""开仓门禁仲裁器守卫（G4，`src/futu_quant/entry_gate.py`）。

对应设计 §D3/§D3.1（`1482791`）与验收 V16/V21/V23：
- 聚合顺序固定：对账锁 → 急停 → 只减仓 → 熔断 → 日亏损 → 预算；
- 多条原因**全量返回**，每条带 `blocked_by`（shadow 期同样记录）；
- **V21**：本模块禁止查权益/快照/券商/库，只接收 `EntrySignals` 数值——静态守卫钉死。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from futu_quant import entry_gate as gate
from futu_quant import risk_breaker as rb

SOURCE = Path(gate.__file__)


def test_entry_signals_fields_are_breaker_only():
    """单一来源（Chief 裁定）：既有闸门原因只来自 `risk_decision.blocked_by`。

    `EntrySignals` 只允许熔断维度字段；若有人把对账锁/急停/日亏损等字段加回来，
    就会出现「同一原因两个来源」的漂移，本断言立即变红。
    """
    import dataclasses

    assert {field.name for field in dataclasses.fields(gate.EntrySignals)} == {
        "breaker_reasons",
        "breaker_unsupported",
    }


def test_decision_reasons_come_first_then_breaker():
    """原因顺序 = `risk_decision.blocked_by` 原序 → 熔断原因（去重）。"""
    decision = SimpleNamespace(
        allowed=False,
        reason="三账不一致",
        blocked_by=(rb.RECONCILIATION_LOCK, rb.ACCOUNT_HALT, rb.STRATEGY_REDUCE_ONLY),
    )
    reasons = gate.resolve_entry_block_reasons(
        gate.EntrySignals(breaker_reasons=(rb.BlockReason(rb.DRAWDOWN_WARN, "回撤 9%"),)),
        decision,
    )
    assert [reason.code for reason in reasons] == [
        rb.RECONCILIATION_LOCK,
        rb.ACCOUNT_HALT,
        rb.STRATEGY_REDUCE_ONLY,
        rb.DRAWDOWN_WARN,
    ]
    assert reasons[0].detail == "三账不一致"


def test_risk_decision_reasons_merged_first_and_deduplicated():
    """Chief 裁定：`risk_decision.blocked_by` 作为第一条原因合并，一次落 audit（V9）。"""
    decision = SimpleNamespace(
        allowed=False, reason="账户急停已开启", blocked_by=(rb.ACCOUNT_HALT,)
    )
    reasons = gate.resolve_entry_block_reasons(
        gate.EntrySignals(
            breaker_reasons=(
                rb.BlockReason(rb.DRAWDOWN_WARN, "回撤 9%"),
                rb.BlockReason(rb.ACCOUNT_HALT, "重复来源的急停"),
            ),
        ),
        decision,
    )
    assert [reason.code for reason in reasons] == [rb.ACCOUNT_HALT, rb.DRAWDOWN_WARN]
    assert reasons[0].detail == "账户急停已开启"


def test_risk_decision_can_be_omitted_for_shadow_path():
    reasons = gate.resolve_entry_block_reasons(
        gate.EntrySignals(breaker_reasons=(rb.BlockReason(rb.DRAWDOWN_ABSOLUTE, "x"),))
    )
    assert [reason.code for reason in reasons] == [rb.DRAWDOWN_ABSOLUTE]


def test_unlabelled_risk_reject_gets_fallback_reason():
    """P0 fail-open 防线：RiskEngine 拒绝了但没带 `blocked_by` → 必须补兜底原因。"""
    decision = SimpleNamespace(
        allowed=False, reason="单笔名义金额超过 $100,000", blocked_by=()
    )
    reasons = gate.resolve_entry_block_reasons(gate.EntrySignals(), decision)
    assert [reason.code for reason in reasons] == [gate.RISK_ENGINE_REJECT]
    assert reasons[0].detail == "单笔名义金额超过 $100,000"


def test_allowed_decision_has_no_fallback_reason():
    decision = SimpleNamespace(allowed=True, reason="风控通过", blocked_by=())
    assert gate.resolve_entry_block_reasons(gate.EntrySignals(), decision) == []


def test_empty_signals_allow_entry():
    assert gate.resolve_entry_block_reasons(gate.EntrySignals()) == []


def test_breaker_reasons_pass_through_with_specific_gate():
    """V23：`blocked_by` 必须能区分具体闸门（8% / 10% / 绝对底线），不许只写「回撤超限」。"""
    reasons = gate.resolve_entry_block_reasons(
        gate.EntrySignals(
            breaker_reasons=(
                rb.BlockReason(rb.DRAWDOWN_STOP, "dd_since_reset=10.2%"),
                rb.BlockReason(rb.DRAWDOWN_ABSOLUTE, "dd_vs_peak=11.0%"),
            )
        )
    )
    assert [reason.code for reason in reasons] == [rb.DRAWDOWN_STOP, rb.DRAWDOWN_ABSOLUTE]
    payload = gate.audit_payload(reasons)
    assert payload["blocked"] is True
    assert payload["blocked_by"] == [rb.DRAWDOWN_STOP, rb.DRAWDOWN_ABSOLUTE]
    assert all("gate" in detail for detail in payload["block_details"])


def test_audit_payload_records_shadow_would_block():
    """shadow 期不真拦，但 audit 也必须留 would_block 证据（S2 零误报要拿得出）。"""
    payload = gate.audit_payload([])
    assert payload == {"blocked_by": [], "block_details": [], "blocked": False}


def test_simulate_enforce_does_not_block_but_audit_records_unsupported_env():
    """Chief 验收：`SIMULATE + enforce` → BUY 照常通过，audit 里有 breaker_unsupported_env。

    `BreakerUnsupportedEnv` 必须被聚合层捕获，**绝不允许冒到 submit_limit**。
    """
    from types import SimpleNamespace

    settings = SimpleNamespace(real_account_id=281756481476169109)
    reasons, unsupported = gate.resolve_breaker_inputs(
        settings, "SIMULATE", (rb.BlockReason(rb.DRAWDOWN_WARN, "不应出现"),)
    )
    assert reasons == (), "不支持的 env 熔断维度不得产生 block 原因"
    assert unsupported == "SIMULATE"

    # BUY 侧照常放行（没有其他原因时）
    signals = gate.EntrySignals(breaker_reasons=reasons, breaker_unsupported=unsupported)
    assert gate.resolve_entry_block_reasons(signals) == []
    payload = gate.audit_payload([], breaker_unsupported=unsupported)
    assert payload["blocked"] is False
    assert payload["breaker_unsupported_env"] == "SIMULATE"
    assert payload["breaker_code"] == "breaker_unsupported_env"
    assert payload["breaker_status"] == "not_enabled"


def test_real_enforce_passes_breaker_reasons_through():
    from types import SimpleNamespace

    settings = SimpleNamespace(real_account_id=281756481476169109)
    reasons, unsupported = gate.resolve_breaker_inputs(
        settings, "REAL", (rb.BlockReason(rb.DRAWDOWN_ABSOLUTE, "dd_vs_peak=10.1%"),)
    )
    assert unsupported == ""
    assert [reason.code for reason in reasons] == [rb.DRAWDOWN_ABSOLUTE]
    blocked = gate.resolve_entry_block_reasons(gate.EntrySignals(breaker_reasons=reasons))
    assert gate.audit_payload(blocked)["blocked_by"] == [rb.DRAWDOWN_ABSOLUTE]


def test_breaker_unsupported_never_escapes_aggregation():
    """静态 + 行为双守卫：聚合层不得把 `BreakerUnsupportedEnv` 抛出去。"""
    import inspect

    source = inspect.getsource(gate)
    assert "except BreakerUnsupportedEnv" in source
    for env in ("SIMULATE", "", "PAPER"):
        from types import SimpleNamespace

        reasons, unsupported = gate.resolve_breaker_inputs(
            SimpleNamespace(real_account_id=None), env
        )
        assert reasons == () and unsupported, f"env={env!r} 未被聚合层显式记录"


FORBIDDEN_TOKENS = (
    "equity_snapshot",
    "total_assets",
    "get_account",
    "account_info",
    "broker.",
    "store.",
    "sqlite3",
    "connection.execute",
)


def _forbidden_hits(source: str) -> list[str]:
    return [token for token in FORBIDDEN_TOKENS if token in source]


def test_v21_entry_gate_has_no_equity_or_broker_access():
    """V21 静态守卫：聚合层出现权益/快照/券商查询即判红（第二处取快照 = 方案 1 失效）。"""
    found = _forbidden_hits(SOURCE.read_text(encoding="utf-8"))
    assert not found, f"entry_gate 出现禁止的取数/判定入口：{found}"


def test_v21_guard_self_check_catches_fake_source():
    """Reviewer 小项 2：守卫自检——含 `store.` / 取快照的假源码必须被判红。"""
    fake_store = "def leak(store):\n    return store.load_risk_state('x')\n"
    assert _forbidden_hits(fake_store), "守卫自检失败：含 store. 的假源码未被判红"
    fake_snapshot = "def leak():\n    return equity_snapshots()\n"
    assert _forbidden_hits(fake_snapshot), "守卫自检失败：取快照的假源码未被判红"
    clean = "def resolve_entry_block_reasons(signals):\n    return []\n"
    assert _forbidden_hits(clean) == []


def test_v21_entry_gate_signature_only_takes_signals():
    import inspect

    signature = inspect.signature(gate.resolve_entry_block_reasons)
    parameters = list(signature.parameters.values())
    assert len(parameters) == 2
    assert parameters[0].name == "signals"
    assert parameters[1].name == "risk_decision"
    assert parameters[0].annotation in ("EntrySignals", gate.EntrySignals)


def test_entry_gate_does_not_implement_sell_path():
    """任何等级都只停新开仓：聚合层不得出现 SELL 判定/分支（文档里的说明文字不算）。"""
    import ast

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    sell_literals = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and str(node.value).upper() == "SELL"
    ]
    assert not sell_literals, f"entry_gate 出现 SELL 常量（第 {sell_literals} 行）"
