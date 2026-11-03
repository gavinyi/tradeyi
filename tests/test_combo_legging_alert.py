"""组合「长短腿不同步」告警与审计（2026-11-03）。

背景：Futu 只有单腿 `place_order`（broker.py:725），价差是**逐腿提交**的
（combo_execution.py `_submit_leg`）。买腿先成交、卖腿才在保护成立后提交，
所以「长腿已成交、短腿没成交」是一个**真实存在的中间态**：此刻的组合敞口
是**已成交腿的全额成本**，而不是价差的净支出（$150 净支出背后可能是 $650 敞口）。

本文件的断言围绕三件事：
1. 当轮就报（不等回滚完成），并且**不可静音**；
2. payload 给的是**真实数字**（成交价、已成交腿成本、未成交腿、合计敞口、预算线、裸奔时长）；
3. 全成交 / 全未成交时**不许误报**（否则告警会变成噪音而被忽略）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.alerts import is_never_mutable
from futu_quant.combo_execution import ComboExecutionManager, create_combo
from futu_quant.store import TradingStore


def vertical_legs():
    return [
        {"role": "PROTECTIVE", "symbol": "US.NVDA260717C220000", "side": "BUY",
         "quantity": 1, "limit_price": 6.5},
        {"role": "RISK", "symbol": "US.NVDA260717C210000", "side": "SELL",
         "quantity": 1, "limit_price": 5.0},
    ]


def combo_config(**overrides):
    config = {
        "strategy_id": "volatility_long_gamma", "name": "测试看涨价差",
        "underlying": "US.NVDA", "structure": "VERTICAL",
        "max_net_debit": 2.0, "min_net_credit": 0,
        "assembly_timeout_seconds": 300, "naked_timeout_seconds": 15,
        "auto_rollback": True,
    }
    config.update(overrides)
    return config


class FakeBroker:
    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol, "bid_price": 0.95, "ask_price": 1.05,
            "option_volume": 1000, "option_open_interest": 5000,
        } for symbol in symbols])

    def cancel_order(self, order_id):
        return pd.DataFrame([{"order_id": order_id}])

    def positions(self, refresh=False):
        return pd.DataFrame(columns=["code", "qty"])


class FakeExecutor:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def submit_limit(self, strategy, key, symbol, side, quantity, price, metadata):
        intent, _ = self.store.create_intent(strategy, key, symbol, side, quantity, price, metadata)
        self.store.update_intent(intent["id"], "SUBMITTED", broker_order_id=f"paper-{intent['id']}")
        self.calls.append((symbol, side, quantity, price, metadata, intent["id"]))
        return SimpleNamespace(intent_id=intent["id"], status="SUBMITTED", message="模拟订单已提交")


def fill(store, intent_id, price=None):
    intent = store.intent(intent_id)
    store.update_shadow_intent(
        intent_id, "FILLED", intent["quantity"], price or intent["limit_price"], 0,
    )


def _legging_events(store, combo_id):
    return [
        event for event in store.option_combo_events(combo_id, limit=200)
        if event["event_type"] == "COMBO_LEGGING_EXPOSURE"
    ]


def _legging_alerts(store):
    return [
        row for row in store.alerts(limit=200)
        if row.get("event_type") == "COMBO_LEGGING_EXPOSURE"
    ]


def _details(store, combo_id):
    import json

    event = _legging_events(store, combo_id)[0]
    raw = event.get("details_json") or event.get("details") or "{}"
    return json.loads(raw) if isinstance(raw, str) else raw


def _open_with_filled_long(tmp_path, **config):
    """长腿按 $6.20 成交（≠ 限价 6.5），随后提交短腿但**不成交**。"""
    store = TradingStore(tmp_path / "legging.db", bootstrap=True)
    combo_id = create_combo(store, combo_config(**config), vertical_legs())
    executor = FakeExecutor(store)
    manager = ComboExecutionManager(FakeBroker(), store, executor)
    manager.start(combo_id)
    assert len(executor.calls) == 1, "应当先只提交买腿"
    fill(store, executor.calls[-1][5], price=6.20)
    manager.advance(combo_id)
    assert len(executor.calls) == 2, "买腿成交后才提交卖腿"
    return store, manager, combo_id, executor


def test_legging_exposure_alert_is_never_mutable():
    # 不可静音：被静音就等于「敞口没人知道」，与 10-23 静默拦截同类。
    assert is_never_mutable("COMBO_LEGGING_EXPOSURE") is True


def test_long_filled_short_failed_alerts_in_the_same_round(tmp_path):
    store, manager, combo_id, executor = _open_with_filled_long(tmp_path)
    # 短腿被券商拒绝：改 **intent** 状态（腿状态每轮由 `_refresh_legs` 从 intent 回填）
    store.update_intent(executor.calls[-1][5], "REJECTED", error="broker rejected")

    manager.advance(combo_id)

    alerts = _legging_alerts(store)
    assert alerts, "长短腿不同步必须当轮告警"
    assert str(alerts[0]["severity"]).upper() == "CRITICAL"
    assert "COMBO_LEGGING_EXPOSURE" in str(alerts[0])
    # 告警发生在回滚之前：状态已进入回滚，而告警已经存在
    assert store.option_combo(combo_id)["status"] == "ROLLBACK_PENDING"


def test_payload_carries_recomputed_exposure_from_fill_price(tmp_path):
    store, manager, combo_id, executor = _open_with_filled_long(tmp_path)
    store.update_intent(executor.calls[-1][5], "REJECTED", error="broker rejected")

    manager.advance(combo_id)

    payload = _details(store, combo_id)
    # 真实敞口用**成交价**重算：1 张 × 6.20 × 100 = $620
    # （不是限价 6.5 → $650，也不是 ask）
    assert payload["exposure_usd"] == pytest.approx(620.0)
    assert payload["filled_legs"][0]["fill_price"] == pytest.approx(6.20)
    assert payload["filled_legs"][0]["cost_usd"] == pytest.approx(620.0)
    assert [item["symbol"] for item in payload["unfilled_legs"]] == ["US.NVDA260717C210000"]
    assert "budget_usd" in payload and "budget_ratio" in payload
    assert payload["legging_seconds"] is not None
    assert payload["trigger"]


def test_budget_line_derived_from_risk_budget_not_hardcoded(tmp_path, monkeypatch):
    store, manager, combo_id, executor = _open_with_filled_long(tmp_path)
    store.update_intent(executor.calls[-1][5], "REJECTED", error="broker rejected")
    # 预算线与 6% 压力损失同源：权益 × max_loss_ratio，取不到就 None（绝不编数字）
    monkeypatch.setattr(store, "equity_history", lambda **_: [{"total_assets": 6606.0}])
    monkeypatch.setattr(store, "strategy_risk_budget", lambda *_: {
        "strategy_id": "volatility_long_gamma", "max_loss_ratio": 0.06,
        "capital_ratio": 0.5, "option_premium_ratio": 0.25, "enabled": True,
    })

    manager.advance(combo_id)

    payload = _details(store, combo_id)
    assert payload["budget_ratio"] == pytest.approx(0.06)
    assert payload["budget_usd"] == pytest.approx(396.36, abs=0.5)
    assert payload["over_budget"] is True   # $620 敞口 > $396 预算


def test_budget_unknown_does_not_invent_numbers(tmp_path, monkeypatch):
    store, manager, combo_id, executor = _open_with_filled_long(tmp_path)
    store.update_intent(executor.calls[-1][5], "REJECTED", error="broker rejected")
    monkeypatch.setattr(store, "equity_history", lambda **_: [])
    monkeypatch.setattr(store, "strategy_risk_budget", lambda *_: None)

    manager.advance(combo_id)

    payload = _details(store, combo_id)
    assert payload["budget_usd"] is None
    assert payload["over_budget"] is None
    assert payload["exposure_usd"] == pytest.approx(620.0)


def test_no_alert_when_no_leg_filled(tmp_path):
    store = TradingStore(tmp_path / "legging.db", bootstrap=True)
    combo_id = create_combo(store, combo_config(), vertical_legs())
    executor = FakeExecutor(store)
    manager = ComboExecutionManager(FakeBroker(), store, executor)
    manager.start(combo_id)
    store.update_intent(executor.calls[-1][5], "REJECTED", error="rejected")

    manager.advance(combo_id)

    assert not _legging_alerts(store)
    assert store.option_combo(combo_id)["status"] == "FAILED"


def test_no_alert_when_all_legs_filled(tmp_path):
    store = TradingStore(tmp_path / "legging.db", bootstrap=True)
    combo_id = create_combo(store, combo_config(), vertical_legs())
    executor = FakeExecutor(store)
    manager = ComboExecutionManager(FakeBroker(), store, executor)
    manager.start(combo_id)
    fill(store, executor.calls[-1][5])
    manager.advance(combo_id)
    fill(store, executor.calls[-1][5])
    manager.advance(combo_id)

    assert store.option_combo(combo_id)["status"] == "OPEN"
    assert not _legging_alerts(store)


def test_assembly_timeout_also_alerts(tmp_path):
    # 超时同样必须当轮喊：此时长腿已经在场，敞口是实时的。
    store, manager, combo_id, _executor = _open_with_filled_long(tmp_path)
    # 把装配超时改成 0（测试内直接改行；`update_option_combo` 不开放该字段）
    with store.connection() as conn:
        conn.execute(
            "UPDATE option_combos SET assembly_timeout_seconds=0 WHERE combo_id=?",
            (combo_id,),
        )
        conn.commit()

    manager.advance(combo_id)

    payload = _details(store, combo_id)
    assert payload["trigger"] == "装配超时"
    assert _legging_alerts(store)
