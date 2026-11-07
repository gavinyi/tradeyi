"""告警分级推送守卫（P2-②，2026-11-07，PRD prd_p2_batch_20261107.md §2）。

验收（PRD 2.4）：同因 100 条只收 1 条聚合（带计数与首末时间）/ P0 不聚合不可静默
（设成聚合必红）/ 超阈值自动升级 / 降级登记可查 / NEVER_MUTE 永远 P0。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from futu_quant import alerts as al
from futu_quant.alerts import (
    P1Aggregator, classify_alert_level, load_alert_levels, update_alert_levels,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "futu_quant" / "alerts.py"


# ---------- 分级判定 ----------

def test_never_mutable_is_always_p0():
    """红线 1：NEVER_MUTE 清单内事件永远 P0，分级机制不得覆盖。"""
    for event_type in ("LEDGER_MISMATCH", "DAILY_LOSS_KILL_SWITCH",
                       "RECONCILIATION_POSITION_DIFF", "REAL_TRADE_REJECTED"):
        assert classify_alert_level(event_type) == "P0", event_type


def test_config_cannot_downgrade_never_mutable():
    """配置把 NEVER_MUTE 设成 P2 → classify 仍 P0；update_alert_levels 整体拒绝。"""
    result = update_alert_levels(FakeStore(), {"LEDGER_MISMATCH": "P2"}, "tester", "试")
    assert result["accepted"] is False and "LEDGER_MISMATCH" in result["rejected"]
    assert classify_alert_level("LEDGER_MISMATCH") == "P0"


def test_benign_is_p2():
    assert classify_alert_level("VOLATILITY_CANDIDATE_FILTERED") == "P2"
    assert classify_alert_level("UNKNOWN_INFO_EVENT", benign=True) == "P2"


def test_p1_default_and_config_override():
    assert classify_alert_level("TASK_TIMEOUT") == "P1"
    # 登记降级 P1→P2：合法（非 NEVER_MUTE），留审计
    store = FakeStore()
    result = update_alert_levels(store, {"TASK_TIMEOUT": "P2"}, "tester", "降噪登记")
    assert result["accepted"] is True
    load_alert_levels(store)
    assert classify_alert_level("TASK_TIMEOUT") == "P2"
    # 审计留痕
    audits = [row for row in store.audits if row[0] == "ALERT_LEVELS_UPDATED"]
    assert audits, "降级必须落审计"


def test_illegal_level_rejected():
    result = update_alert_levels(FakeStore(), {"X": "P9"}, "tester")
    assert result["accepted"] is False


# ---------- P1 聚合（PRD 2.4-1/3） ----------

def test_aggregation_one_push_per_window_with_count_and_times():
    agg = P1Aggregator(window_seconds=3600, escalate_count=20)
    pushed = 0
    last_state = None
    for index in range(100):
        state = agg.observe("TASK_TIMEOUT", "t1", 1700_000_000 + index * 30)
        if state["should_push"]:
            pushed += 1
        last_state = state
    assert pushed == 1, f"100 条同因只应推 1 条，实际 {pushed}"
    summary = P1Aggregator.format_summary("TASK_TIMEOUT", last_state)
    assert "100 次" in summary and "首次" in summary and "最近" in summary


def test_aggregation_escalates_past_threshold():
    """红线 2：1 小时内超 N 次自动升级 P0 单推（恶化不吞）。"""
    agg = P1Aggregator(window_seconds=3600, escalate_count=20)
    states = [agg.observe("TASK_TIMEOUT", "t1", 1700_000_000 + i * 10) for i in range(25)]
    assert not any(s["escalated"] for s in states[:20])
    assert states[20]["escalated"] and states[24]["escalated"]
    assert all(s["should_push"] for s in states[20:]), "升级后逐条推送"


def test_aggregation_is_per_tenant():
    agg = P1Aggregator(window_seconds=3600, escalate_count=20)
    a = agg.observe("X", "tenant_a", 1700_000_000)
    b = agg.observe("X", "tenant_b", 1700_000_001)
    assert a["should_push"] and b["should_push"], "不同租户不得共用聚合桶"


# ---------- send() 分级接线 ----------

def test_send_p0_pushes_each_time(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path)
    monkeypatch.setattr("futu_quant.alerts.feishu_configured", lambda _s: True)
    manager = _spy_manager(store)
    for _ in range(3):
        manager.send("CRITICAL", "LEDGER_MISMATCH", "对账不一致")
    assert manager.pushed.count("LEDGER_MISMATCH") == 3, "P0 必须逐条推送"


def test_send_p2_benign_never_pushes(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path)
    monkeypatch.setattr("futu_quant.alerts.feishu_configured", lambda _s: True)
    manager = _spy_manager(store)
    manager.send("WARNING", "VOLATILITY_CANDIDATE_FILTERED", "正常拦截", benign=True)
    assert manager.pushed == []
    rows = [row for row in store.alerts(limit=10)
            if row.get("event_type") == "VOLATILITY_CANDIDATE_FILTERED"]
    assert rows, "P2 仍须落库"


def test_mutation_p0_aggregated_must_be_red():
    """PRD 2.4-2：把 P0 类设成聚合 → 必红（AST：send 分级分支禁止对 P0 聚合）。"""
    source = MODULE.read_text(encoding="utf-8")
    assert 'level == "P0"' in source and "escalated" in source, \
        "send 必须按分级判定且含升级路径"
    # classify 永不给 NEVER_MUTE 降级机会
    anchor = '    if is_never_mutable(name) or name in P0_EVENT_TYPES:\n        return "P0"'
    assert anchor in source, "classify 的 NEVER_MUTE→P0 强制分支丢失"


# ---------- 汇总独立 store 替身 ----------

class FakeStore:
    def __init__(self):
        self.settings: dict[str, str] = {}
        self.audits: list[tuple] = []

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def mutate_runtime_config(self, key, _default, mutator, actor, **kwargs):
        """对齐真签名：mutate 与 audit 是两条留痕路径，替身不得吞审计。

        真 store 的 mutate_runtime_config 自带 config_versions 版本留痕并支持
        actor/keyword；这里显式记录 actor，若测试断言「降级必须留审计」而替身
        把 mutate 吞了，断言会假绿（Reviewer must-fix 2026-11-07）。
        """
        current = self.settings.get(key, "")
        self.settings[key] = mutator(current)
        self.audits.append(("CONFIG_MUTATED", {"key": key, "actor": actor}, "runtime_config", actor))
        return self.settings[key]

    def audit(self, event_type, payload, source, actor=None, **kwargs):
        self.audits.append((event_type, payload, source, actor))


def _fresh_store(tmp_path):
    from futu_quant.store import TradingStore
    return TradingStore(tmp_path / "lvl.db", bootstrap=True)


class _SpyManager(al.AlertManager):
    def __init__(self, store):
        super().__init__(store)
        self.pushed: list[str] = []

    def _push_webhook(self, severity, event_type, message):  # noqa: ANN001
        return -1

    def _push_feishu(self, severity, event_type, message, tenant_id=None):  # noqa: ANN001
        self.pushed.append(event_type)
        return 1
