"""`TradingStore.decisions_for_strategy()`：按策略在 SQL 侧过滤决策日志。

背景（2026-11-04 性能优化）：波动率总览页原先用 `decisions(limit=500)` 拉全局最近
500 行（每行 payload ≈1.1 KB）再在 Python 里按 strategy_id 过滤，两个策略各过滤一遍。
本测试固定住新方法的三条契约：只返回本策略、按 id 倒序、遵守 limit。
"""

from futu_quant.store import TradingStore


def _store(tmp_path) -> TradingStore:
    return TradingStore(tmp_path / "decisions.db")


def test_returns_only_the_requested_strategy(tmp_path):
    store = _store(tmp_path)
    store.decision_if_changed("volatility_scanner", "US.NVDA", "SCAN", "扫描")
    store.decision_if_changed("volatility_long_gamma", "US.META", "BUY", "开仓")
    store.decision_if_changed("volatility_scanner", "US.AMD", "SCAN", "扫描")

    scanner = store.decisions_for_strategy("volatility_scanner")
    assert [row["symbol"] for row in scanner] == ["US.AMD", "US.NVDA"]
    assert {row["strategy_id"] for row in scanner} == {"volatility_scanner"}

    gamma = store.decisions_for_strategy("volatility_long_gamma")
    assert [row["symbol"] for row in gamma] == ["US.META"]


def test_newest_first_and_limit_is_respected(tmp_path):
    store = _store(tmp_path)
    for symbol in ("US.A", "US.B", "US.C"):
        store.decision_if_changed("volatility_scanner", symbol, "SCAN", f"{symbol} 扫描")

    rows = store.decisions_for_strategy("volatility_scanner", limit=2)
    assert [row["symbol"] for row in rows] == ["US.C", "US.B"]
    assert rows[0]["id"] > rows[1]["id"]


def test_unknown_strategy_returns_empty_list(tmp_path):
    store = _store(tmp_path)
    store.decision_if_changed("volatility_scanner", "US.NVDA", "SCAN", "扫描")
    assert store.decisions_for_strategy("core_satellite") == []
