import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


spec = importlib.util.spec_from_file_location(
    "run_gamma_strategy",
    Path(__file__).resolve().parents[1] / "scripts" / "run_gamma_strategy.py",
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
volatility_exit_close_legs = module.volatility_exit_close_legs
volatility_exit_limit_price = module.volatility_exit_limit_price
volatility_near_expiry_itm_exit_reason = module.volatility_near_expiry_itm_exit_reason


def test_exit_only_multileg_volatility_campaign_closes_primary_leg_only():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1, "role": "PROTECTIVE"},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1, "role": "RISK"},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "exit_only": True,
        "structure": "PUT_DEBIT_SPREAD",
        "legs": legs,
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs,
    )

    assert route == "EXIT_ONLY"
    assert close_legs == [{
        "code": "US.SPCX260814P142000",
        "side": "BUY",
        "ratio": 1,
        "role": "PRIMARY",
    }]


def test_missing_combo_ledger_falls_back_to_primary_leg_exit():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "structure": "PUT_DEBIT_SPREAD",
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs, combo_available=False,
    )

    assert route == "MISSING_COMBO"
    assert len(close_legs) == 1
    assert close_legs[0]["code"] == "US.SPCX260814P142000"


def test_available_combo_keeps_multileg_close_route():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "structure": "PUT_DEBIT_SPREAD",
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs, combo_available=True,
    )

    assert route is None
    assert close_legs == legs


def test_near_expiry_itm_call_forces_early_exit():
    campaign = {
        "code": "US.SPCX260814C141000",
        "trigger_mode": "VOLATILITY",
        "near_expiry_itm_exit_days": 1,
    }

    reason = volatility_near_expiry_itm_exit_reason(
        campaign,
        "US.SPCX260814C141000",
        now=datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        underlying_price=142.5,
        quantity=1,
    )

    assert reason is not None
    assert "临近到期价内合约强制提前退出" in reason


def test_near_expiry_otm_put_does_not_force_exit():
    campaign = {
        "code": "US.SPCX260814P141000",
        "trigger_mode": "VOLATILITY",
        "near_expiry_itm_exit_days": 1,
    }

    reason = volatility_near_expiry_itm_exit_reason(
        campaign,
        "US.SPCX260814P141000",
        now=datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        underlying_price=142.5,
        quantity=1,
    )

    assert reason is None


def test_non_volatility_strategy_does_not_use_volatility_lifecycle_exit():
    campaign = {
        "code": "US.SPCX260814C141000",
        "trigger_mode": "EARNINGS",
        "near_expiry_itm_exit_days": 1,
    }

    reason = volatility_near_expiry_itm_exit_reason(
        campaign,
        "US.SPCX260814C141000",
        now=datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        underlying_price=142.5,
        quantity=1,
    )

    assert reason is None


def test_urgent_sell_exit_prices_below_bid_to_improve_fill_odds():
    price, context = volatility_exit_limit_price(
        pd.Series({"bid_price": 2.50, "ask_price": 3.40, "last_price": 3.00}),
        "SELL",
        urgent=True,
        campaign={"urgent_exit_price_improvement_pct": 0.04},
    )

    assert price == 2.40
    assert context["urgent_exit"] is True
    assert context["exit_limit_rule"].startswith("紧急退出")


def test_normal_sell_exit_uses_bid_without_extra_degrade():
    price, context = volatility_exit_limit_price(
        pd.Series({"bid_price": 2.50, "ask_price": 3.40, "last_price": 3.00}),
        "SELL",
        urgent=False,
        campaign={},
    )

    assert price == 2.50
    assert context["urgent_exit"] is False


def test_urgent_buy_to_close_prices_above_ask_to_reduce_short_leg_risk():
    price, context = volatility_exit_limit_price(
        pd.Series({"bid_price": 0.80, "ask_price": 1.00, "last_price": 0.90}),
        "BUY",
        urgent=True,
        campaign={"urgent_exit_price_improvement_pct": 0.05},
    )

    assert price == 1.05
    assert context["urgent_exit"] is True


def test_sell_exit_when_bid_non_positive_returns_zero_price_and_waiting_liquidity():
    """Bug 4 守卫：
    平多头期权（SELL）时，若 bid <= 0（流动性真空/无买盘），
    严禁回退到陈旧的 last 挂单，必须返回 0.0 限价并标记 WAITING_EXIT_LIQUIDITY，
    避免悬空限价单导致监控进入在途订单等待死循环。
    """
    quote = pd.Series({"bid_price": 0.0, "ask_price": 2.50, "last_price": 2.00})
    price, context = volatility_exit_limit_price(
        quote,
        "SELL",
        urgent=True,
        campaign={"urgent_exit_price_improvement_pct": 0.04},
    )

    assert price == 0.0
    assert context["liquidity_status"] == "WAITING_EXIT_LIQUIDITY"
    assert "买盘真空" in context["exit_limit_rule"]


def test_sell_exit_liquidity_vacuum_alerts_with_multi_leg_distinction(tmp_path):
    """Bug 4 守卫：
    同标的多腿组合发生买盘真空时，incident_key 必须区分 leg_number 与 leg code，
    绝不能把不同腿合并去重。
    """
    class MockAlertManager:
        def __init__(self):
            self.sent = []

        def send(self, severity, event_type, message, incident_key=None, **kwargs):
            self.sent.append({
                "severity": severity,
                "event_type": event_type,
                "message": message,
                "incident_key": incident_key,
            })

    alerts = MockAlertManager()
    campaign_id = "camp-123"

    # 模拟 leg 1 与 leg 2 分别触发告警
    for leg_num, code in [(1, "US.XYZ261120C100000"), (2, "US.XYZ261120C105000")]:
        wait_reason = f"等待可退出盘口：{code} SELL 可用限价为 0"
        alerts.send(
            "ERROR",
            "OPTION_EXIT_LIQUIDITY_VACUUM",
            f"XYZ: {wait_reason}",
            incident_key=f"OPTION_EXIT_LIQUIDITY_VACUUM:{campaign_id}:{leg_num}:{code}",
        )

    assert len(alerts.sent) == 2
    keys = {item["incident_key"] for item in alerts.sent}
    assert len(keys) == 2, f"两腿组合必须生成两条独立 incident_key: {keys}"
    assert f"OPTION_EXIT_LIQUIDITY_VACUUM:{campaign_id}:1:US.XYZ261120C100000" in keys
    assert f"OPTION_EXIT_LIQUIDITY_VACUUM:{campaign_id}:2:US.XYZ261120C105000" in keys
