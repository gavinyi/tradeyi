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


def test_sell_exit_liquidity_vacuum_alerts_with_multi_leg_distinction():
    """Bug 4 守卫：
    同标的多腿组合发生买盘真空时，incident_key 必须调用生产函数 _exit_liquidity_incident_key，
    即使合约代码相同，只要 leg_number 不同，生成的 incident_key 必须独立解耦，绝不能被合并去重。
    """
    _exit_liquidity_incident_key = getattr(module, "_exit_liquidity_incident_key", None)
    assert _exit_liquidity_incident_key is not None, "生产代码必须提供 _exit_liquidity_incident_key 函数"

    campaign_id = "camp-123"
    code = "US.XYZ261120C100000"

    # 同一合约代码，不同 leg_number（如多腿拆分执行）
    key_leg1 = _exit_liquidity_incident_key(campaign_id, 1, code)
    key_leg2 = _exit_liquidity_incident_key(campaign_id, 2, code)

    assert key_leg1 != key_leg2, f"同一合约代码的不同腿不能生成相同 incident_key: {key_leg1}"
    assert key_leg1 == f"OPTION_EXIT_LIQUIDITY_VACUUM:{campaign_id}:1:{code}"
    assert key_leg2 == f"OPTION_EXIT_LIQUIDITY_VACUUM:{campaign_id}:2:{code}"
