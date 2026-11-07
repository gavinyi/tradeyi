"""回测引擎定向测试：信号重放对齐 + 分层撮合 + 确定性（PRD §2/§4/§6）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from futu_quant.signal_backtest import matcher as bt_matcher
from futu_quant.signal_backtest import replay as bt_replay
from futu_quant.store import TradingStore

WINDOW_START = "2026-11-06T00:00:00+00:00"
WINDOW_END = "2026-11-07T00:00:00+00:00"


def _store(tmp_path) -> TradingStore:
    return TradingStore(tmp_path / "bt.db", bootstrap=True)


def _seed_active_evidence(store, symbol: str, trace_id: str) -> None:
    """按 trace_id 存档一份 active 证据（2026-11-07 新口径：replay 只认证据，
    缺失即 fail-closed mismatch——测试种子必须与生产写入方同构）。"""
    from datetime import datetime, timezone

    from futu_quant.volatility_activation import (
        build_activation_evidence, save_activation_evidence_archive,
    )

    evidence = build_activation_evidence(
        symbol,
        {"active": True, "reason": "达到动态波动监测条件", "trigger_confirmed": True,
         "volume_confirmed": True, "direction_gate": True},
        {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
        observed_at=datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc),
        trace_id=trace_id,
    )
    save_activation_evidence_archive(store, evidence)


def _seed_decision(store, symbol: str, created: str, *, z: float, move: float,
                   decision: str = "BUY", tenant: str = "futu_real_master",
                   payload: dict | None = None) -> None:
    body = payload if payload is not None else {
        "regime": {"zscore_20d": z, "intraday_return": move},
        "checks": {},
    }
    store.decision(
        "volatility_scanner", symbol, decision, "达到动态波动监测条件", body,
        tenant_id=tenant,
    )
    # decision() 内部用 self.now() 生成 created_at，这里直接改写为指定时间，
    # 保证重放窗口与确定性可控（测试库专属操作，生产无此路径）。
    with store.connection() as connection:
        connection.execute(
            "UPDATE decision_logs SET created_at=? WHERE rowid = "
            "(SELECT MAX(rowid) FROM decision_logs)",
            (created,),
        )
        connection.commit()


def test_replay_aligns_actual_and_replayed_events(tmp_path):
    """L-A 对齐：窗口内 BUY 逐条复现，无 payload 缺失即零 mismatch。"""
    store = _store(tmp_path)
    _seed_decision(store, "US.AMD", "2026-11-06T14:00:00+00:00", z=2.5, move=0.05,
                   payload={"zscore_20d": 2.5, "activation_trace_id": "radar-AMD"})
    _seed_decision(store, "US.QQQ", "2026-11-06T15:00:00+00:00", z=2.4, move=0.04,
                   payload={"zscore_20d": 2.4, "activation_trace_id": "radar-QQQ"})
    _seed_decision(store, "US.NVDA", "2026-11-06T16:00:00+00:00", z=2.2, move=0.01,
                   decision="HOLD")  # 非 BUY，不进对齐集

    result = bt_replay.replay_signals(
        store, WINDOW_START, WINDOW_END,
        {"thresholds": {"entry_zscore": 2.3, "minimum_underlying_move": 0.02}},
    )
    assert len(result.actual_events) == 2
    assert result.aligned, f"mismatches={result.mismatches}"
    assert result.alignment_ratio == pytest.approx(1.0)
    assert [event.symbol for event in result.replayed_events] == ["US.AMD", "US.QQQ"]


def test_replay_flags_threshold_recalc_difference(tmp_path):
    """阈值重算不一致 → 必须显式 mismatch（回放器不吞差异）。

    种子带 trace_id 存档证据但 active=False（激活不成立）——按 2026-11-07
    两级取证口径，证据在但三联/active 不一致归 `threshold_recalc_differs`；
    证据完全缺失归 `activation_evidence_missing`，两因不得互相覆盖。
    """
    store = _store(tmp_path)
    _seed_decision(store, "US.AMD", "2026-11-06T14:00:00+00:00", z=2.0, move=0.03,
                   payload={"zscore_20d": 2.0, "activation_trace_id": "radar-INACTIVE"})
    from datetime import datetime, timezone

    from futu_quant.volatility_activation import (
        build_activation_evidence, save_activation_evidence_archive,
    )
    save_activation_evidence_archive(store, build_activation_evidence(
        "US.AMD",
        {"active": False, "reason": "未达动态波动监测条件", "trigger_confirmed": False,
         "volume_confirmed": False, "direction_gate": False},
        {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
        observed_at=datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc),
        trace_id="radar-INACTIVE",
    ))
    result = bt_replay.replay_signals(
        store, WINDOW_START, WINDOW_END,
        {"thresholds": {"entry_zscore": 2.3, "minimum_underlying_move": 0.02}},
    )
    assert not result.aligned
    assert result.mismatches[0]["reason"] == "threshold_recalc_differs"


def test_log_integrity_reports_span_and_gaps(tmp_path):
    """报告头第 4 行：数据跨度 + 窗口内小时缺口如实列出。"""
    store = _store(tmp_path)
    _seed_decision(store, "US.AMD", "2026-11-06T14:10:00+00:00", z=2.5, move=0.05)
    _seed_decision(store, "US.QQQ", "2026-11-06T18:10:00+00:00", z=2.5, move=0.05)
    integrity = bt_replay.audit_log_integrity(store, WINDOW_START, WINDOW_END)
    assert integrity["span_start"].startswith("2026-11-06T14:10")
    assert integrity["span_end"].startswith("2026-11-06T18:10")
    hour_buckets = {gap["hour_bucket"] for gap in integrity["gaps"]}
    assert any("2026-11-06T15" in bucket for bucket in hour_buckets), (
        "14 点与 18 点之间缺失的小时桶必须出现在缺口清单"
    )


def _quote(tier: str, *, ask: float | None = None, premium: float | None = None):
    return bt_matcher.PriceQuote(
        bid_price=ask - 0.1 if ask else None,
        ask_price=ask,
        recorded_premium=premium,
        tier=tier,
    )


def test_match_trade_uses_ask_for_l1():
    signal = replace(bt_replay.SignalEvent(
        created_at="2026-11-06T14:00:00+00:00", symbol="US.AMD",
        decision="BUY", tenant_id="t", payload={},
    ), symbol="US.AMD")
    trade = bt_matcher.match_trade(signal, _quote("L1", ask=2.50))
    assert trade.price_tier == "L1"
    assert trade.fill_price == pytest.approx(2.51)  # ask + 1 tick
    assert trade.premium_notional == pytest.approx(251.0)


def test_match_trade_uses_recorded_premium_for_l2():
    signal = replace(bt_replay.SignalEvent(
        created_at="2026-11-06T14:00:00+00:00", symbol="US.SOFI",
        decision="BUY", tenant_id="t", payload={},
    ), symbol="US.SOFI")
    trade = bt_matcher.match_trade(signal, _quote("L2", premium=0.56))
    assert trade.price_tier == "L2"
    assert trade.fill_price == pytest.approx(0.57)


def test_match_trade_refuses_to_invent_l3_price():
    """L3 无价格依据 → 拒绝编造撮合价（报告层计 L3 占比，引擎不造数）。"""
    signal = replace(bt_replay.SignalEvent(
        created_at="2026-11-06T14:00:00+00:00", symbol="US.XXX",
        decision="BUY", tenant_id="t", payload={},
    ), symbol="US.XXX")
    with pytest.raises(ValueError, match="L3"):
        bt_matcher.match_trade(signal, _quote("L3"))


def test_tier_shares_and_fee():
    trades = [
        bt_matcher.match_trade(
            replace(bt_replay.SignalEvent("t", f"US.A{i}", "BUY", "t", {}),
                    symbol=f"US.A{i}"),
            _quote("L1", ask=2.0),
        )
        for i in range(3)
    ] + [
        bt_matcher.match_trade(
            replace(bt_replay.SignalEvent("t", "US.B", "BUY", "t", {}),
                    symbol="US.B"),
            _quote("L2", premium=1.0),
        )
    ]
    shares = bt_matcher.tier_shares(trades)
    assert shares == {"L1": pytest.approx(0.75), "L2": pytest.approx(0.25), "L3": 0.0}
    fee = bt_matcher.fee_per_trade(2.0, 1)
    assert fee > 0


def test_replay_is_deterministic(tmp_path):
    """同输入两跑逐字节一致（PRD 硬门槛 4）。"""
    store = _store(tmp_path)
    _seed_decision(store, "US.AMD", "2026-11-06T14:00:00+00:00", z=2.5, move=0.05)
    config = {"thresholds": {"entry_zscore": 2.3, "minimum_underlying_move": 0.02}}
    first = bt_replay.replay_signals(store, WINDOW_START, WINDOW_END, config)
    second = bt_replay.replay_signals(store, WINDOW_START, WINDOW_END, config)
    assert json.dumps(first.replayed_events, default=vars, sort_keys=True) == json.dumps(
        second.replayed_events, default=vars, sort_keys=True
    )
    assert json.dumps(first.mismatches, sort_keys=True) == json.dumps(
        second.mismatches, sort_keys=True
    )


def test_read_only_store_is_enforced(tmp_path):
    """只读铁律：read_only store 无写路径可用（引擎不可能写库）。"""
    db = tmp_path / "ro.db"
    TradingStore(db, bootstrap=True)
    store = TradingStore(db, read_only=True)
    assert store.read_only is True
    with pytest.raises(Exception):
        store.set_setting("backtest_should_not_write", "1")
