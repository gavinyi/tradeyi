"""正常拦截前缀守卫（P2-③ 复盘文案扫尾，2026-11-07）。

口径（10-22 白名单定案 + 用户拍板）：正常拦截类告警统一【正常拦截】前缀、
不推飞书群（落库可见），与真故障分开。前缀只在 alerts.mark_benign 一处定义。
"""

from __future__ import annotations

from pathlib import Path

from futu_quant.alerts import BENIGN_PREFIX, mark_benign

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "src" / "futu_quant" / "volatility_scanner.py"


def test_mark_benign_prefixes_and_is_idempotent():
    assert mark_benign("测试消息") == f"{BENIGN_PREFIX}测试消息"
    assert mark_benign(f"{BENIGN_PREFIX}已带前缀") == f"{BENIGN_PREFIX}已带前缀"


def test_benign_send_does_not_push_feishu(monkeypatch, tmp_path):
    """benign=True：不推飞书（feishu_delivered=-1），但仍落库。"""
    from futu_quant.alerts import AlertManager
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "a.db", bootstrap=True)
    pushed: list[tuple] = []

    class SpyManager(AlertManager):
        def _push_webhook(self, severity, event_type, message):  # noqa: ANN001
            return -1

        def _push_feishu(self, severity, event_type, message, tenant_id=None):  # noqa: ANN001
            pushed.append((event_type, message))
            return 1

    manager = SpyManager(store)
    manager.send("WARNING", "VOLATILITY_CANDIDATE_FILTERED", "测试拦截", benign=True)
    assert pushed == [], "benign 事件不得推飞书"
    rows = [row for row in store.alerts(limit=20) if row.get("event_type") == "VOLATILITY_CANDIDATE_FILTERED"]
    assert rows, "benign 事件必须仍落库"
    assert rows[0]["message"].startswith(BENIGN_PREFIX)


def test_non_benign_error_still_pushes(monkeypatch, tmp_path):
    """不加 benign 的 ERROR 仍走默认推送（降噪不得波及真故障）。"""
    from futu_quant.alerts import AlertManager
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "b.db", bootstrap=True)
    monkeypatch.setattr("futu_quant.alerts.feishu_configured", lambda _s: True)
    pushed: list[str] = []

    class SpyManager(AlertManager):
        def _push_webhook(self, severity, event_type, message):  # noqa: ANN001
            return -1

        def _push_feishu(self, severity, event_type, message, tenant_id=None):  # noqa: ANN001
            pushed.append(event_type)
            return 1

    SpyManager(store).send("ERROR", "LEDGER_MISMATCH", "真故障测试")
    assert "LEDGER_MISMATCH" in pushed, "真故障推送被误伤"


def test_scanner_call_site_uses_benign_flag():
    """雷达候选拦截调用点必须显式 benign=True（不许手写前缀）。"""
    source = SCANNER.read_text(encoding="utf-8")
    assert "benign=True" in source, "候选拦截调用点未接 benign 标志"
    assert 'f"{BENIGN_PREFIX}' not in source and "【正常拦截】" not in source, \
        "调用点禁止手写前缀（口径必须集中 mark_benign）"


def test_benign_never_covers_never_mutable_semantics():
    """benign 前缀只是文案与降噪；不可静音清单语义不受影响（判据不混用）。"""
    from futu_quant.alerts import is_never_mutable
    assert is_never_mutable("LEDGER_MISMATCH")
    assert is_never_mutable("RECONCILIATION_POSITION_DIFF")
    assert not is_never_mutable("VOLATILITY_CANDIDATE_FILTERED")
