import pandas as pd
import pytest

from futu_quant.config import Settings, normalize_us_symbol
from futu_quant.core_satellite import (
    allocation_factor, build_rebalance_plan, indicators, max_weights,
    ranked_target_weights, trade_participation,
)
from futu_quant.strategies import Signal, breakout


def test_real_environment_requires_live_safety_gates(monkeypatch):
    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED"):
        Settings.load()


def test_real_environment_loads_only_when_fully_configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456")
    monkeypatch.setenv("REAL_ALLOWED_STRATEGIES", "core_satellite")
    monkeypatch.setenv("REAL_MAX_ORDER_NOTIONAL", "500")
    monkeypatch.setenv("REAL_MAX_DAILY_NOTIONAL", "1000")

    settings = Settings.load()

    assert settings.trading_env == "REAL"
    assert settings.real_account_id == 123456
    assert settings.real_allowed_strategies == ("core_satellite",)


def test_non_us_symbol_is_rejected(monkeypatch):
    monkeypatch.setenv("FUTU_TRADING_ENV", "SIMULATE")
    monkeypatch.setenv("WATCHLIST", "HK.00700")
    with pytest.raises(RuntimeError, match="US"):
        Settings.load()


def test_breakout_buy():
    frame = pd.DataFrame({"close": list(range(1, 22))})
    assert breakout(frame).signal == Signal.BUY


@pytest.mark.parametrize(
    ("raw", "expected"), [("aapl", "US.AAPL"), ("US.tsla", "US.TSLA"), ("brk.b", "US.BRK.B")]
)
def test_normalize_us_symbol(raw, expected):
    assert normalize_us_symbol(raw) == expected


def test_normalize_legacy_berkshire_symbol_keeps_futu_dot():
    assert normalize_us_symbol("BRKB") == "US.BRK.B"


def test_invalid_symbol_is_rejected():
    with pytest.raises(ValueError, match="有效美股代码"):
        normalize_us_symbol("HK.00700")




def core_frame(price: float = 100, sma200_price: float = 90) -> pd.DataFrame:
    values = [sma200_price] * 199 + [price]
    return pd.DataFrame({"close": values})


def trend_frame(start: float = 80, step: float = 0.25, count: int = 220) -> pd.DataFrame:
    return pd.DataFrame({"close": [start + index * step for index in range(count)]})


def volatile_core_frame() -> pd.DataFrame:
    values = []
    price = 100.0
    for index in range(220):
        price *= 1.04 if index % 2 else 0.96
        values.append(price)
    return pd.DataFrame({"close": values})


def test_core_satellite_weights_respect_cap():
    symbols = ("US.SPY", "US.QQQ", "US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN")
    weights = max_weights(symbols, 0.70)
    assert sum(weights.values()) <= 0.70
    assert weights["US.SPY"] == pytest.approx(0.30)
    assert weights["US.AAPL"] <= 0.05


def test_core_satellite_profile_weights_scale_to_unified_budget_cap():
    weights = max_weights(("US.SPY", "US.QQQ", "US.NVDA"), 0.50, "aggressive")

    assert sum(weights.values()) == pytest.approx(0.50)
    assert weights["US.SPY"] == pytest.approx(0.50 * 0.28 / 0.50)
    assert weights["US.NVDA"] == 0


def test_core_satellite_explicit_core_and_satellite_budgets_are_visible_inputs():
    weights = max_weights(
        ("US.SPY", "US.QQQ", "US.NVDA", "US.GOOG"),
        0.85, "balanced", core_budget=0.50, satellite_budget=0.20,
    )

    assert weights["US.SPY"] == pytest.approx(0.30)
    assert weights["US.QQQ"] == pytest.approx(0.15)
    assert weights["US.NVDA"] == pytest.approx(0.10)
    assert weights["US.GOOG"] == pytest.approx(0.10)
    assert sum(weights.values()) <= 0.70


def test_core_satellite_ranked_targets_tilt_instead_of_equal_weighting():
    frames = {
        "US.SPY": trend_frame(start=80, step=0.20),
        "US.QQQ": trend_frame(start=90, step=0.15),
        "US.NVDA": trend_frame(start=50, step=0.80),
        "US.AAPL": trend_frame(start=95, step=0.12),
    }
    stats = {symbol: indicators(frame) for symbol, frame in frames.items()}

    weights, reasons = ranked_target_weights(
        tuple(frames), stats, stats["US.SPY"], True, 0.65, "balanced",
        core_budget=0.40, satellite_budget=0.25,
    )

    assert weights["US.NVDA"] > weights["US.AAPL"]
    assert weights["US.NVDA"] > 0
    assert "相对SPY强势" in reasons["US.NVDA"]


def test_core_satellite_classifies_ivv_and_schd_as_core_assets():
    frames = {
        "US.SPY": trend_frame(start=80, step=0.20),
        "US.IVV": trend_frame(start=80, step=0.19),
        "US.SCHD": trend_frame(start=70, step=0.08),
        "US.NVDA": trend_frame(start=50, step=0.80),
    }
    stats = {symbol: indicators(frame) for symbol, frame in frames.items()}

    weights, reasons = ranked_target_weights(
        tuple(frames), stats, stats["US.SPY"], True, 0.65, "balanced",
        core_budget=0.40, satellite_budget=0.25,
    )

    assert weights["US.IVV"] > 0
    assert weights["US.SCHD"] > 0
    assert reasons["US.IVV"].startswith("Core 质量评分")


def test_core_satellite_broken_satellite_trend_exits():
    stats = {"price": 80, "sma20": 90, "sma200": 100, "drawdown60": -0.2,
             "extension200": -0.2, "momentum20": -0.05, "momentum60": -0.10}
    factor, _ = allocation_factor(stats, "satellite", True)
    assert factor == 0


def test_core_satellite_satellite_requires_relative_strength():
    weak = indicators(core_frame(price=100, sma200_price=99))
    spy = indicators(trend_frame(start=80, step=0.30))

    factor, reason = allocation_factor(weak, "satellite", True, benchmark_stats=spy)

    assert factor == 0
    assert "评分" in reason


def test_core_satellite_satellite_scores_into_position():
    satellite = indicators(trend_frame(start=70, step=0.45))
    spy = indicators(trend_frame(start=80, step=0.20))

    factor, reason = allocation_factor(satellite, "satellite", True, 0.80, spy)

    assert factor >= 0.35
    assert "相对SPY强势" in reason


def test_core_satellite_plan_never_buys_over_total_stock_cap():
    frames = {"US.SPY": core_frame(), "US.AAPL": core_frame()}
    positions = pd.DataFrame(
        [{"code": "US.OTHER", "qty": 700, "can_sell_qty": 700, "market_val": 70_000}]
    )
    plan, risk_on = build_rebalance_plan(
        frames, positions, 100_000, ("US.SPY", "US.AAPL"), min_trade_usd=0
    )
    assert risk_on
    assert not [order for order in plan if order.side == "BUY"]


def test_core_satellite_slices_large_rebalance_and_records_full_target():
    frames = {"US.SPY": core_frame(), "US.NVDA": trend_frame(start=70, step=0.45)}
    plan, risk_on = build_rebalance_plan(
        frames, pd.DataFrame(), 100_000, ("US.SPY", "US.NVDA"),
        min_trade_usd=0, max_single_rebalance_pct=0.50, max_order_notional=10_000,
    )
    spy_order = next(order for order in plan if order.symbol == "US.SPY")

    assert risk_on
    assert spy_order.desired_quantity == 250
    assert spy_order.quantity == 100
    assert spy_order.order_notional == pytest.approx(10_000)
    assert spy_order.total_slices == 3


def test_core_satellite_high_volatility_reduces_trade_participation():
    stats = {"realized_vol20": 0.04}
    factor, reason = trade_participation(stats, 0.80, 0.025, 0.30)

    assert factor == pytest.approx(0.30)
    assert "高于阈值" in reason
