"""
测试 Core Satellite 策略防高频追涨杀跌与迟滞保护 (Anti-Whipsaw & Retention Guard Test Suite)
"""
from __future__ import annotations

import pandas as pd
from futu_quant.core_satellite import allocation_factor, build_rebalance_plan


def test_held_satellite_retained_when_score_dips_near_threshold():
    """
    测试：在仓标的当评分稍微从 5.0 回落到 3.5 时，只要趋势在 200 日线上方，
    必须保持基础维持仓位 (0.40)，绝不能因为低于新开仓门槛就直接归零触发甩卖！
    """
    stats = {
        "price": 220.0,
        "sma200": 180.0,  # 远高于 200 日线，长期趋势健康
        "sma20": 218.0,
        "drawdown60": -0.05,
        "momentum20": -0.01,
        "momentum60": -0.01,
        "volatility": 0.02,
        "extension200": 0.22,
    }
    benchmark_stats = {
        "price": 580.0,
        "sma200": 550.0,
        "momentum20": 0.05,
        "momentum60": 0.08,
    }

    # 1. 未持仓标的：评分低于严格门槛 4.0 时，不建仓 (factor = 0.0)
    factor_unheld, reason_unheld = allocation_factor(
        stats, "satellite", risk_on=True, base_factor=0.80,
        benchmark_stats=benchmark_stats, satellite_min_score=4.0,
        currently_held=False,
    )
    assert factor_unheld == 0.0
    assert "等待更强入场信号" in reason_unheld

    # 2. 已经在仓标的：必须有底仓维稳保护 (factor > 0)
    factor_held, reason_held = allocation_factor(
        stats, "satellite", risk_on=True, base_factor=0.80,
        benchmark_stats=benchmark_stats, satellite_min_score=4.0,
        currently_held=True,
    )
    assert factor_held > 0.0
    assert "在仓底仓" in reason_held or "维持" in reason_held


def test_satellite_daily_scan_does_not_trim_healthy_holdings():
    """
    测试：在非正式调仓日的每日机会扫描 (satellite_only=True) 下，
    只要在仓标的未发生均线破位，绝对不执行倒手减仓，避免磨损手续费！
    """
    # 构造假数据
    dates = pd.date_range("2026-01-01", periods=260)
    spy_df = pd.DataFrame({
        "time_key": dates.strftime("%Y-%m-%d"),
        "close": [500.0 + i * 0.3 for i in range(260)],
    })
    nvda_df = pd.DataFrame({
        "time_key": dates.strftime("%Y-%m-%d"),
        "close": [150.0 + i * 0.3 for i in range(260)],
    })
    frames = {"US.SPY": spy_df, "US.NVDA": nvda_df}

    # 假设当前已经持有 100 股 NVDA (市值 $22,800，占比 ~4.5%)
    positions = pd.DataFrame([{
        "code": "US.NVDA",
        "qty": 100.0,
        "can_sell_qty": 100.0,
        "market_val": 22800.0,
    }])
    account_value = 500000.0

    # 执行每日扫描 (satellite_only = True)
    orders, _ = build_rebalance_plan(
        frames=frames,
        positions=positions,
        account_value=account_value,
        symbols=("US.NVDA",),
        satellite_only=True,
        satellite_min_score=4.0,
    )

    # 验证：绝不应该产生 SELL 减仓订单！
    sell_orders = [o for o in orders if o.symbol == "US.NVDA" and o.side == "SELL"]
    assert len(sell_orders) == 0, f"非正式调仓日不应产生减仓单：{sell_orders}"
