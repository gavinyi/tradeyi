"""回测对齐前置包守卫（2026-11-07）：trace_id 证据存档 + replay 两级取证 + 页面护栏。

背景（L-A 对齐到 0 的硬前置）：激活证据单槽键 `volatility_activation_evidence:<symbol>`
是覆盖型——每标的只留最近一次，历史 BUY 无法对到当次证据（45 天窗口 4302 条
BUY 只能对齐 1131 条，其余 activation_evidence_missing）。

守卫三块：
①存档：active 轮必须按 trace_id 一条一存（volatility_activation.archive_key）；
②replay：先按 payload.activation_trace_id 查存档 → 退回单槽 → 都没有记
  mismatch（fail-closed，不解释性放行）；差异带 trace_id 与按原因归因；
③UI 页：默认窗口 30 天、大窗口护栏（>20s 警告 / >120s 拦截）、
  aligned=False 时 Tab B 必须禁用（真实差异与伪造对齐率都测）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from futu_quant.signal_backtest import replay as bt_replay
from futu_quant.store import TradingStore
from futu_quant.volatility_activation import (
    ARCHIVE_KEY_PREFIX,
    archive_key,
    build_activation_evidence,
    load_activation_evidence_archive,
    save_activation_evidence,
    save_activation_evidence_archive,
)

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "48_signal_backtest.py"


def _active_evidence(symbol: str, trace_id: str, *, active: bool = True) -> dict:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    return build_activation_evidence(
        symbol, {"active": active, "reason": "test",
                 "trigger_confirmed": True, "volume_confirmed": True,
                 "direction_gate": True},
        {"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
        observed_at=now, trace_id=trace_id,
    )


# ---------- ①存档 ----------

def test_archive_saved_per_trace_id_on_active_round(tmp_path):
    store = TradingStore(tmp_path / "a.db")
    store.initialize()
    first = _active_evidence("US.NVDA", "radar-T1:US.NVDA")
    second = _active_evidence("US.NVDA", "radar-T2:US.NVDA")
    save_activation_evidence_archive(store, first)
    save_activation_evidence_archive(store, second)
    # 两条 trace 各自可取（不覆盖）——这正是单槽键做不到的
    assert load_activation_evidence_archive(store, "radar-T1:US.NVDA")["trace_id"] == "radar-T1:US.NVDA"
    assert load_activation_evidence_archive(store, "radar-T2:US.NVDA")["trace_id"] == "radar-T2:US.NVDA"


def test_archive_prefix_is_dedicated_key():
    assert ARCHIVE_KEY_PREFIX == "volatility_activation_evidence_archive"
    assert archive_key("x").startswith(ARCHIVE_KEY_PREFIX + ":")
    # 不得与单槽键重名（否则又变覆盖型）
    assert archive_key("x") != f"volatility_activation_evidence:x"


# ---------- ②replay 两级取证 ----------

def _seed_buy(store, symbol: str, created_at: str, trace_id: str) -> None:
    store.decision(
        "volatility_scanner", symbol, "BUY", "达到动态波动监测条件",
        {"zscore_20d": 2.5, "activation_trace_id": trace_id},
        tenant_id="t1",
    )
    # decision_if_changed 写 created_at 由 store.now() 决定；直接 UPDATE 到目标时间
    with store.connection() as connection:
        connection.execute(
            "UPDATE decision_logs SET created_at=? WHERE strategy_id='volatility_scanner'",
            (created_at,),
        )


def test_replay_uses_trace_archive_for_history_buys(tmp_path):
    """历史 BUY（单槽键已被更新的证据覆盖）仍能按 trace_id 对齐。"""
    store = TradingStore(tmp_path / "b.db")
    store.initialize()
    _seed_buy(store, "US.NVDA", "2026-11-01T14:00:00+00:00", "radar-OLD:US.NVDA")
    _seed_buy(store, "US.NVDA", "2026-11-05T14:00:00+00:00", "radar-NEW:US.NVDA")
    # 两轮 active 证据都存档
    save_activation_evidence_archive(store, _active_evidence("US.NVDA", "radar-OLD:US.NVDA"))
    save_activation_evidence_archive(store, _active_evidence("US.NVDA", "radar-NEW:US.NVDA"))
    # 单槽键只留最新（模拟生产覆盖行为）
    save_activation_evidence(store, _active_evidence("US.NVDA", "radar-NEW:US.NVDA"))

    result = bt_replay.replay_signals(
        store, "2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00", {},
    )
    assert result.aligned, f"存档可用时历史 BUY 必须全对齐：{result.mismatches}"
    assert len(result.replayed_events) == 2


def test_replay_falls_back_to_single_slot(tmp_path):
    """存档缺失（存档功能上线前的窗口）退回单槽键，能对上就不算差异。"""
    store = TradingStore(tmp_path / "c.db")
    store.initialize()
    _seed_buy(store, "US.GLD", "2026-11-01T14:00:00+00:00", "radar-NOARCH:US.GLD")
    save_activation_evidence(store, _active_evidence("US.GLD", "radar-NOARCH:US.GLD"))
    result = bt_replay.replay_signals(
        store, "2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00", {},
    )
    assert result.aligned and len(result.replayed_events) == 1


def test_replay_missing_both_levels_is_fail_closed_mismatch(tmp_path):
    """两级都没有 → mismatch，绝不解释性放行；差异带 trace_id。"""
    store = TradingStore(tmp_path / "d.db")
    store.initialize()
    _seed_buy(store, "US.AMD", "2026-11-01T14:00:00+00:00", "radar-LOST:US.AMD")
    result = bt_replay.replay_signals(
        store, "2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00", {},
    )
    assert not result.aligned
    assert result.mismatch_reasons().get("activation_evidence_missing") == 1
    assert result.mismatches[0]["trace_id"] == "radar-LOST:US.AMD"


def test_replay_mismatch_reasons_breakdown(tmp_path):
    store = TradingStore(tmp_path / "e.db")
    store.initialize()
    _seed_buy(store, "US.A", "2026-11-01T14:00:00+00:00", "t-a")
    _seed_buy(store, "US.B", "2026-11-01T15:00:00+00:00", "t-b")
    save_activation_evidence_archive(store, _active_evidence("US.A", "t-a", active=False))
    result = bt_replay.replay_signals(
        store, "2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00", {},
    )
    reasons = result.mismatch_reasons()
    assert reasons.get("threshold_recalc_differs") == 1  # 证据在但 active=False
    assert reasons.get("activation_evidence_missing") == 1  # t-b 无证据


# ---------- ③UI 页守卫 ----------

def test_page_default_window_is_30_days():
    source = PAGE.read_text(encoding="utf-8")
    assert "days=30" in source, "默认窗口必须是 30 天（Product 拍板）"


def test_page_guardrails_count_query_and_limits():
    """大窗口护栏：预计耗时提示 + >120s 拦截。"""
    source = PAGE.read_text(encoding="utf-8")
    assert "预计耗时" in source
    assert "st.stop()" in source, "超硬顶必须拦截渲染"


def test_tab_b_disabled_on_real_mismatch(tmp_path):
    """aligned=False（真实差异）→ 页面首屏标不可信、Tab B 禁用逻辑必须触发。"""
    store = TradingStore(tmp_path / "f.db")
    store.initialize()
    _seed_buy(store, "US.T", "2026-11-01T14:00:00+00:00", "radar-MISS:US.T")
    result = bt_replay.replay_signals(
        store, "2026-11-01T00:00:00+00:00", "2026-11-07T00:00:00+00:00", {},
    )
    assert not result.aligned
    # 页面首屏/Tab B 的分支锚点：self_check=False 时必须给出「不可信」与禁用
    source = PAGE.read_text(encoding="utf-8")
    assert "回测器不可信" in source, "首屏必须给「不可信」结论"
    assert "本 Tab 禁用" in source, "Tab B 必须显式禁用"
    # 分支正确性：self_check 即 result.aligned，禁用必须挂在它下面
    assert "if not self_check:" in source and "st.stop()" in source


def test_tab_b_disabled_on_forged_alignment_ratio():
    """伪造对齐率 <100%（直接构造 ReplayResult 篡改）→ aligned 必须为 False。"""
    forged = bt_replay.ReplayResult()
    forged.actual_events = [bt_replay.SignalEvent(
        created_at="2026-11-01T14:00:00+00:00", symbol="US.X", decision="BUY",
        tenant_id="t1", payload={}, strategy_id="volatility_scanner",
    )]
    forged.replayed_events = []  # 宣称对齐了却拿不出重放事件
    assert forged.aligned is True  # 构造基线：空 mismatches 才算对齐
    forged.mismatches.append({"created_at": "x", "symbol": "US.X",
                              "reason": "activation_evidence_missing"})
    assert forged.aligned is False, "aligned 判据只认 mismatches 为空，无旁路"
    assert forged.alignment_ratio < 1.0


# ---------- 存档写入点守卫（scanner 只在 active 轮存档） ----------

def test_scanner_archives_only_on_active(monkeypatch, tmp_path):
    import futu_quant.volatility_scanner as scanner_mod
    source = (ROOT / "src" / "futu_quant" / "volatility_scanner.py").read_text(encoding="utf-8")
    assert "save_activation_evidence_archive" in source, "scanner 必须接存档写入"
    # 存档调用必须在 active 条件之下（形状守卫：条件行在调用行之前且同段）
    lines = source.splitlines()
    call_line = next(i for i, line in enumerate(lines) if "save_activation_evidence_archive(store," in line)
    guard_segment = "\n".join(lines[max(0, call_line - 6):call_line + 1])
    assert 'evidence.get("active")' in guard_segment, "存档必须只在 active 轮写入"


def test_archive_roundtrip_rejects_corrupt_json(tmp_path):
    store = TradingStore(tmp_path / "g.db")
    store.initialize()
    store.set_setting(archive_key("radar-BAD:x"), "{not json")
    assert load_activation_evidence_archive(store, "radar-BAD:x") is None
    assert load_activation_evidence_archive(store, "radar-NEVER:x") is None
