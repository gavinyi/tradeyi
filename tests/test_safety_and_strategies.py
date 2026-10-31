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


def test_core_satellite_daily_opportunity_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("FUTU_TRADING_ENV", "SIMULATE")
    monkeypatch.setenv("WATCHLIST", "US.SPY,US.NVDA")
    monkeypatch.setenv("CORE_SAT_SATELLITE_DAILY_SCAN", "true")
    monkeypatch.setenv("CORE_SAT_SATELLITE_MIN_SCORE", "4.5")
    monkeypatch.setenv("CORE_SAT_MAX_TARGET_CHANGE", "0.06")

    settings = Settings.load()

    assert settings.core_satellite_satellite_daily_scan is True
    assert settings.core_satellite_satellite_min_score == pytest.approx(4.5)
    assert settings.core_satellite_max_target_change == pytest.approx(0.06)


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


def test_core_satellite_satellite_score_gate_can_be_tightened():
    satellite = indicators(trend_frame(start=70, step=0.45))
    spy = indicators(trend_frame(start=80, step=0.20))

    factor, reason = allocation_factor(
        satellite, "satellite", True, 0.80, spy, satellite_min_score=6.5
    )

    assert factor == 0
    assert "每日机会门槛" in reason


def test_core_satellite_satellite_only_does_not_touch_core_positions():
    frames = {
        "US.SPY": trend_frame(start=80, step=0.20),
        "US.QQQ": trend_frame(start=90, step=0.20),
        "US.NVDA": trend_frame(start=50, step=0.80),
    }
    positions = pd.DataFrame(
        [{"code": "US.SPY", "qty": 100, "can_sell_qty": 100, "market_val": 12_380}]
    )

    plan, risk_on = build_rebalance_plan(
        frames, positions, 100_000, ("US.SPY", "US.QQQ", "US.NVDA"),
        min_trade_usd=0, core_budget=0.40, satellite_budget=0.25,
        satellite_min_score=4.0, satellite_only=True,
    )

    assert risk_on
    assert plan
    assert {order.symbol for order in plan} == {"US.NVDA"}


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
        max_target_change=0.20,
    )
    spy_order = next(order for order in plan if order.symbol == "US.SPY")

    assert risk_on
    assert spy_order.desired_quantity == 200
    assert spy_order.quantity == 100
    assert spy_order.order_notional == pytest.approx(10_000)
    assert spy_order.total_slices == 2


def test_core_satellite_high_volatility_reduces_trade_participation():
    stats = {"realized_vol20": 0.04}
    factor, reason = trade_participation(stats, 0.80, 0.025, 0.30)

    assert factor == pytest.approx(0.30)
    assert "高于阈值" in reason


def test_core_satellite_sgov_excluded_from_stock_market_value_and_sell_first():
    """Bug 1 守卫：
    1. 现金理财 SGOV 市值不计入 total_market_value，不能吞噬股票买入预算；
    2. 生成的再平衡订单必须 SELL 排在 BUY 前面，保障先卖后买资金到位。
    """
    frames = {
        "US.SPY": core_frame(),
        "US.AAPL": trend_frame(start=100, step=1.0),
        "US.MSFT": core_frame(price=100.0),
        "US.SGOV": core_frame(price=100.0),
    }
    # 账户总值 100_000，持有 SGOV 70_000（达到 70% 股票预算上限），持有 MSFT 10_000
    positions = pd.DataFrame([
        {"code": "US.SGOV", "qty": 700, "can_sell_qty": 700, "market_val": 70_000},
        {"code": "US.MSFT", "qty": 100, "can_sell_qty": 100, "market_val": 10_000},
    ])
    # symbols 包含 SPY, AAPL, MSFT；由于 AAPL 强动能目标增持(BUY)，MSFT target_weight 为 0 目标清仓(SELL)
    plan, risk_on = build_rebalance_plan(
        frames, positions, 100_000, ("US.SPY", "US.AAPL", "US.MSFT"),
        min_trade_usd=100, cash_yield_enabled=True, cash_yield_symbol="US.SGOV",
    )
    assert risk_on
    # 1. 验证 SGOV 未吃光 AAPL/SPY 的买入预算（若 SGOV 计入总市值，buy_budget = 70k - 70k - 10k = 0，无法买入 AAPL）
    buys = [o for o in plan if o.side == "BUY" and o.symbol != "US.SGOV"]
    assert len(buys) > 0, "SGOV 不应计入股票市值导致股票买入预算被压成 0"

    # 2. 验证订单顺序：所有 SELL 必须排在 BUY 之前（先卖后买）
    sides = [o.side for o in plan]
    assert "SELL" in sides and "BUY" in sides, f"用例设计需同时产生买卖单，实际: {sides}"
    first_buy_idx = sides.index("BUY")
    last_sell_idx = len(sides) - 1 - sides[::-1].index("SELL")
    assert last_sell_idx < first_buy_idx, f"所有 SELL 必须排在 BUY 之前：实际顺序 {sides}"


def test_core_satellite_sgov_insufficient_sellable_blocks_over_cash_buy():
    """Bug 1 追加守卫（Reviewer 实盘风险防线）：
    1. 当 SGOV can_sell_qty 为 0 或不足且现金不足时，不可超买（buy_budget 严格受限于可变现现金）；
    2. 符号比对不因大小写或前缀（如 'sgov' vs 'US.SGOV'）失效；
    3. SGOV 充足时，买入总额不得超过可用现金与 SGOV 可变现额之和。
    """
    frames = {
        "US.SPY": core_frame(),
        "US.AAPL": trend_frame(start=100, step=1.0),
        "US.SGOV": core_frame(price=100.0),
    }
    # 场景 A：持仓 70_000 的 SGOV，但由于冻结或 T+0 在途等原因 can_sell_qty=0，且账户现金已占用完
    # 账户净值 100_000，股票市值 30_000，SGOV 70_000，可用现金为 0
    positions_locked = pd.DataFrame([
        {"code": "sgov", "qty": 700, "can_sell_qty": 0, "market_val": 70_000},
        {"code": "US.OTHER", "qty": 300, "can_sell_qty": 300, "market_val": 30_000},
    ])
    plan_locked, _ = build_rebalance_plan(
        frames, positions_locked, 100_000, ("US.SPY", "US.AAPL"),
        min_trade_usd=100, cash_yield_enabled=True, cash_yield_symbol="US.SGOV",
    )
    buys_locked = [o for o in plan_locked if o.side == "BUY" and o.symbol != "US.SGOV"]
    assert len(buys_locked) == 0, "SGOV 不可卖且无可用现金时，必须阻断 BUY 避免券商拒单或动用保证金"

    # 场景 B：SGOV 充分可卖 (can_sell_qty=950)，初始可用现金仅 5_000 (100k - 95k SGOV)
    # 调仓产生 9_785 买单，买入额超过可用现金且使现金低于预留 buffer 5% (5_000)，
    # 必须断言生成 SGOV 赎回腿（SELL）且赎回金额补足差额！
    positions_sellable = pd.DataFrame([
        {"code": "US.SGOV", "qty": 950, "can_sell_qty": 950, "market_val": 95_000},
    ])
    plan_sellable, _ = build_rebalance_plan(
        frames, positions_sellable, 100_000, ("US.SPY", "US.AAPL"),
        min_trade_usd=100, cash_yield_enabled=True, cash_yield_symbol="US.SGOV",
        cash_buffer=0.05,
    )
    buys_sellable = [o for o in plan_sellable if o.side == "BUY" and o.symbol != "US.SGOV"]
    total_buy_notional = sum(o.order_notional for o in buys_sellable)
    assert total_buy_notional > 5_000, f"买入总额必须大于初始可用现金 5000 以触发赎回，实际: {total_buy_notional}"

    sgov_sells = [o for o in plan_sellable if o.side == "SELL" and "SGOV" in o.symbol]
    assert len(sgov_sells) > 0, "买入额超过可用现金时，必须生成 SGOV SELL 赎回腿，不能因现金计算错误被跳过！"
    redeem_notional = sum(o.order_notional for o in sgov_sells)
    # 验证最终保留现金达标：5_000 - total_buy_notional + redeem_notional >= buffer 5_000
    assert 5_000 - total_buy_notional + redeem_notional >= 5_000 - 1e-4, "赎回额必须补足现金缓冲缺口"
    # 并且 SELL 严格先于 BUY
    sides = [o.side for o in plan_sellable]
    assert sides.index("SELL") < sides.index("BUY"), f"赎回腿必须排在买单前：{sides}"


def test_core_satellite_sgov_missing_price_fails_closed_zero_sellable_value():
    """Bug 1 追加守卫（假价拦截）：
    当 SGOV 缺少真实行情且无真实持仓市值时，价格必须 fail-closed 归零，
    绝不可默认 100.0 假价放宽 buy_budget 导致超额买入。
    """
    frames_no_sgov = {
        "US.SPY": core_frame(),
        "US.AAPL": trend_frame(start=100, step=1.0),
    }
    # 账户净值 100_000，已有股票 95_000，可用现金仅 5_000（恰好等于 cash_buffer 5%）
    # 持有 700 股 SGOV，但在缺少行情/价格时，可变现额必须为 0，不能假定 100.0 算出 70_000 的可变现流动性来放行买入
    positions = pd.DataFrame([
        {"code": "sgov", "qty": 700, "can_sell_qty": 700, "market_val": 0.0},
        {"code": "US.OTHER", "qty": 950, "can_sell_qty": 950, "market_val": 95_000},
    ])
    plan, _ = build_rebalance_plan(
        frames_no_sgov, positions, 100_000, ("US.SPY", "US.AAPL"),
        min_trade_usd=100, cash_yield_enabled=True, cash_yield_symbol="US.SGOV",
        cash_buffer=0.05,
    )
    buys = [o for o in plan if o.side == "BUY" and o.symbol != "US.SGOV"]
    assert len(buys) == 0, "缺少真实 SGOV 价格时其可变现额必须 fail-closed 归零，不可凭空扩充买入预算"
