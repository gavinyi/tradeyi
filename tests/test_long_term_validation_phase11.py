from __future__ import annotations

import pytest

from futu_quant.long_term_validation import (
    equity_horizon_metrics, long_term_validation_cycle, manually_restore_paper,
    realized_trade_ledger, stability_scores, stratified_execution_quality,
    strategy_horizon_metrics,
)
from futu_quant.store import TradingStore


def filled_intent(store, key, side, price, reference=100):
    intent, _ = store.create_intent(
        "alpha", f"{key}:PAPER", "US.NVDA", side, 1, reference,
        {"reference_price": reference},
    )
    store.update_shadow_intent(intent["id"], "FILLED", 1, price, 1)


def test_horizon_metrics_and_realized_attribution(tmp_path):
    store = TradingStore(tmp_path / "long.db")
    store.record_equity(100_000, 50_000, 50_000)
    store.record_equity(101_000, 50_000, 51_000)
    filled_intent(store, "buy", "BUY", 100)
    filled_intent(store, "sell", "SELL", 110, 109)

    assert equity_horizon_metrics(store).iloc[0]["收益率"] == pytest.approx(0.01)
    ledger = realized_trade_ledger(store)
    assert ledger.iloc[0]["净盈亏"] == 8
    assert not strategy_horizon_metrics(store).empty
    assert not stratified_execution_quality(store).empty


def test_stability_and_long_term_cycle_are_explainable(tmp_path):
    store = TradingStore(tmp_path / "score.db")
    for index in range(20):
        filled_intent(store, str(index), "BUY", 100)
    scores = stability_scores(store)
    assert scores.iloc[0]["稳定性评分"] >= 70
    result = long_term_validation_cycle(store)
    assert "governance" in result
    assert "只允许" in result["governance"]


def test_manual_restore_requires_shadow_and_reason(tmp_path):
    store = TradingStore(tmp_path / "restore.db")
    store.set_setting("strategy_execution_mode:alpha", "SHADOW")
    try:
        manually_restore_paper(
            store, "alpha", "admin", "", tenant_id="futu_paper_sandbox",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("empty reason must fail")
    # P1-6：恢复必须显式给租户，禁止跨租户无差别解禁
    try:
        manually_restore_paper(store, "alpha", "admin", "人工复核通过")
    except TypeError:
        pass
    else:
        raise AssertionError("缺少 tenant_id 必须报错（跨租户无差别恢复是红线）")
    # 恢复 = 删掉覆盖键（回到原配置），不是写 PAPER：实盘账户上 PAPER 会自动提成 REAL
    legacy_key = "strategy_execution_mode:alpha"
    store.set_setting("strategy_execution_mode:futu_paper_sandbox:alpha", "SHADOW")
    manually_restore_paper(
        store, "alpha", "admin", "人工复核通过", tenant_id="futu_paper_sandbox",
    )
    assert store.get_setting("strategy_execution_mode:futu_paper_sandbox:alpha") == ""
    assert store.get_setting(legacy_key) == ""
