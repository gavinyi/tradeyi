"""replay 第三级取证守卫（2026-11-07 生产归因，Ops 报告 replay_alignment_attribution）。

三级链：trace_id 存档 → 单槽键 → decision_logs 直读（历史窗口 BUY 行自身即
当时判定的原始记录）。三级全 miss 才 fail-closed。不放宽判据：直读级要求
payload 触发依据齐备（zscore/move 非空），且只认信号层 BUY。
"""

from __future__ import annotations

import json

from futu_quant.signal_backtest import replay as bt_replay

WINDOW = ("2026-11-04T00:00:00+00:00", "2026-11-07T00:00:00+00:00")


class FakeStore:
    """带窗口直读的最小替身（模拟历史形态库：无存档、无单槽）。"""

    def __init__(self, rows):
        self.rows = rows
        self.settings: dict[str, str] = {}

    def decisions_for_strategy(self, strategy_id, limit=500):
        return [r for r in self.rows if r["strategy_id"] == strategy_id][:limit]

    def decisions_in_window(self, strategy_id, ws, we, *, decision=None, limit=200_000):
        out = [r for r in self.rows
               if r["strategy_id"] == strategy_id and ws <= r["created_at"] <= we]
        if decision:
            out = [r for r in out if r["decision"].upper() == decision.upper()]
        return out[:limit]

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def _row(symbol, created, *, zscore=2.6, move=0.05, regime=None,
         payload_overrides: dict | None = None):
    payload = {"activation_trace_id": ""} if regime is None else {"regime": regime}
    if regime is None:
        payload.update({"zscore_20d": zscore, "intraday_return": move})
    if payload_overrides:
        payload.update(payload_overrides)
    return {
        "strategy_id": "volatility_scanner", "symbol": symbol, "decision": "BUY",
        "created_at": created, "tenant_id": "t1",
        "payload_json": json.dumps(payload),
    }


def test_third_level_direct_read_aligns_history_buys():
    """历史形态（无存档/无单槽、payload 顶层带触发依据）→ 直读级全对齐。"""
    store = FakeStore([
        _row("US.AMD", "2026-11-05T14:00:00+00:00"),
        _row("US.QQQ", "2026-11-06T15:00:00+00:00"),
    ])
    result = bt_replay.replay_signals(store, *WINDOW, {})
    assert result.aligned, result.mismatches
    assert len(result.replayed_events) == 2


def test_regime_shaped_payload_also_hits_third_level():
    """payload 走 regime 子对象的历史形态（引擎测试夹具形态）同样命中直读级。"""
    store = FakeStore([
        _row("US.NVDA", "2026-11-05T14:00:00+00:00",
             regime={"zscore_20d": 2.4, "intraday_return": 0.03, "checks": {}}),
    ])
    result = bt_replay.replay_signals(store, *WINDOW, {})
    assert result.aligned, result.mismatches


def test_third_level_requires_trigger_evidence_fail_closed():
    """payload 无任何触发依据（zscore/move 双缺）→ 三级全 miss → mismatch（不放宽）。"""
    store = FakeStore([
        {"strategy_id": "volatility_scanner", "symbol": "US.BARE", "decision": "BUY",
         "created_at": "2026-11-05T14:00:00+00:00", "tenant_id": "t1",
         "payload_json": json.dumps({"activation_trace_id": ""})},
    ])
    result = bt_replay.replay_signals(store, *WINDOW, {})
    assert not result.aligned
    assert result.mismatch_reasons().get("activation_evidence_missing") == 1


def test_archive_level_still_wins_over_direct_read():
    """存档在 → 仍走第一级（权威证据复验，直读只是兜底不覆盖它）。

    正确形态：BUY 行声明 trace_id，存档按该 trace_id 落键且 active=False ——
    第一级必须以其为准判 threshold_recalc_differs，直读级不得洗绿。
    """
    store = FakeStore([
        _row("US.AMD", "2026-11-05T14:00:00+00:00",
             payload_overrides={"activation_trace_id": "radar-REAL:US.AMD"}),
    ])
    from futu_quant.volatility_activation import (
        build_activation_evidence, save_activation_evidence_archive,
    )
    from datetime import datetime, timezone
    save_activation_evidence_archive(store, build_activation_evidence(
        "US.AMD", {"active": False, "reason": "x", "trigger_confirmed": False,
                   "volume_confirmed": False, "direction_gate": False},
        {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
        observed_at=datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc),
        trace_id="radar-REAL:US.AMD",
    ))
    result = bt_replay.replay_signals(store, *WINDOW, {})
    # 存档说 active=False → threshold_recalc_differs（直读级不会把它洗成 aligned）
    assert result.mismatch_reasons().get("threshold_recalc_differs") == 1


def test_declared_trace_id_without_archive_payload_self_consistent_aligns():
    """悬空声明（存档 11-06 上线前窗口的 1,211 条形态）：声明 trace_id 但两级
    miss 且 payload 自洽 → 直读复验对齐（Chief 2026-11-07 裁定放宽）。

    与洗绿缺口的本质区别：这里**无任何证据可查**，BUY 行自身是原始判定；
    存档有权威反证的形态（上一条守卫）仍判差异，不在此放宽范围。
    """
    store = FakeStore([
        _row("US.LOST", "2026-11-05T14:00:00+00:00",
             payload_overrides={"activation_trace_id": "radar-GONE:US.LOST"}),
    ])
    result = bt_replay.replay_signals(store, *WINDOW, {})
    assert result.aligned, result.mismatches
    assert len(result.replayed_events) == 1


def test_declared_trace_id_bare_payload_is_dangling_mismatch():
    """声明了 trace_id 且 payload 也无自洽依据 → 取证链彻底断，
    落 dangling_declaration 档（fail-closed 不放宽，供分档统计）。"""
    store = FakeStore([
        {"strategy_id": "volatility_scanner", "symbol": "US.BARE2", "decision": "BUY",
         "created_at": "2026-11-05T14:00:00+00:00", "tenant_id": "t1",
         "payload_json": json.dumps({"activation_trace_id": "radar-X:US.BARE2"})},
    ])
    result = bt_replay.replay_signals(store, *WINDOW, {})
    assert not result.aligned
    row = result.mismatches[0]
    assert row["reason"] == "activation_evidence_missing"
    assert row.get("dangling_declaration") is True


def test_window_query_efficiency_no_full_scan():
    """SQL 侧窗口过滤：直读方法必须带窗口条件（AST 守卫）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "store.py"
           ).read_text(encoding="utf-8")
    assert "decisions_in_window" in src
    assert "created_at>=?" in src and "created_at<=?" in src
