from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.config import Settings
from futu_quant.data_quality import option_quote_freshness_for_exit


def test_option_exit_quality_healthy_book_passes():
    # 模拟盘口健康：买一 3.68、卖一 3.71、last 3.70，价差 0.8%
    row = pd.Series({
        "bid_price": 3.68,
        "ask_price": 3.71,
        "last_price": 3.70,
        "update_time": "2026-10-20 10:00:00",  # 成交时间即使久远
    })
    ok, reason, spread = option_quote_freshness_for_exit(row, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is True
    assert "双边盘口健康可用" in reason
    assert spread < 1.0


def test_option_exit_quality_profit_taking_rally_passes():
    # 止盈场景：期权价格上涨，买一 $6.50 高于 last $5.00（有利滑点，严禁被 abs 误拦）
    row_profit = pd.Series({
        "bid_price": 6.50,
        "ask_price": 6.60,
        "last_price": 5.00,
    })
    ok, reason, _ = option_quote_freshness_for_exit(row_profit, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is True
    assert "双边盘口健康可用" in reason


def test_option_exit_quality_reasonable_stop_loss_drop_passes():
    # 正常硬止损场景：期权价格由 $5.00 下跌至 $3.77（下跌 24.6% 触发 -25% 硬止损）
    # 双边盘口健康，且下行偏离 24.6% <= 40.0%，必须顺畅放行平仓，严禁锁死仓位！
    row_stop_loss = pd.Series({
        "bid_price": 3.77,
        "ask_price": 3.82,
        "last_price": 5.00,
    })
    ok, reason, _ = option_quote_freshness_for_exit(row_stop_loss, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is True
    assert "双边盘口健康可用" in reason


def test_option_exit_quality_extreme_flash_crash_blocks():
    # 极端暴跌闪崩抽单场景：last=$5.00，买一被恶意砸到 $2.50（下行暴跌 50.0% > 40.0%）
    row_crash = pd.Series({
        "bid_price": 2.50,
        "ask_price": 2.55,
        "last_price": 5.00,
    })
    ok, reason, _ = option_quote_freshness_for_exit(row_crash, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is False
    assert "防极端暴跌抽单保护拦截" in reason


def test_option_exit_quality_wide_spread_blocks():
    # 模拟盘口价差过大：买一 3.00、卖一 4.00（价差 28.6% > 20%）
    row = pd.Series({
        "bid_price": 3.00,
        "ask_price": 4.00,
        "last_price": 3.50,
    })
    ok, reason, spread = option_quote_freshness_for_exit(row, maximum_spread_pct=20.0)
    assert ok is False
    assert "盘口价差过大" in reason


def test_option_exit_quality_invalid_or_crossed_book_blocks():
    # 买一为 0
    row_zero = pd.Series({"bid_price": 0.0, "ask_price": 1.0, "last_price": 1.0})
    ok, reason, _ = option_quote_freshness_for_exit(row_zero)
    assert ok is False
    assert "盘口不可用" in reason

    # 盘口倒挂
    row_cross = pd.Series({"bid_price": 5.0, "ask_price": 4.0, "last_price": 4.5})
    ok, reason, _ = option_quote_freshness_for_exit(row_cross)
    assert ok is False
    assert "盘口倒挂" in reason


def test_option_exit_settings_bounds(monkeypatch):
    monkeypatch.setenv("OPTION_EXIT_MAX_SPREAD_PCT", "99.0")
    with pytest.raises(RuntimeError, match="OPTION_EXIT_MAX_SPREAD_PCT"):
        Settings.load()

    monkeypatch.setenv("OPTION_EXIT_MAX_SPREAD_PCT", "20.0")
    monkeypatch.setenv("OPTION_EXIT_PRICE_DEVIATION_ANCHOR_PCT", "0.95")
    with pytest.raises(RuntimeError, match="OPTION_EXIT_PRICE_DEVIATION_ANCHOR_PCT"):
        Settings.load()


def test_option_exit_quality_nan_and_none_resilience():
    # NaN bid/ask 不得误判为通过
    row_nan_bid = pd.Series({"bid_price": float("nan"), "ask_price": 4.0, "last_price": 4.0})
    ok, reason, _ = option_quote_freshness_for_exit(row_nan_bid)
    assert ok is False

    row_nan_ask = pd.Series({"bid_price": 3.8, "ask_price": float("nan"), "last_price": 3.8})
    ok, reason, _ = option_quote_freshness_for_exit(row_nan_ask)
    assert ok is False

    # None 或缺失键测试
    row_none = pd.Series({"bid_price": None, "ask_price": None})
    ok, reason, _ = option_quote_freshness_for_exit(row_none)
    assert ok is False
    assert "盘口不可用" in reason


def test_option_exit_quality_exact_boundary():
    # 偏离锚精准边界：last = 10.0, bid = 6.0 (下行偏离 40.0% <= 40%) -> Pass
    row_anchor_pass = pd.Series({"bid_price": 6.0, "ask_price": 6.2, "last_price": 10.0})
    ok, _, _ = option_quote_freshness_for_exit(row_anchor_pass, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is True

    # 偏离锚超界：last = 10.0, bid = 5.98 (下行偏离 40.2% > 40%) -> Fail
    row_anchor_fail = pd.Series({"bid_price": 5.98, "ask_price": 6.1, "last_price": 10.0})
    ok, reason, _ = option_quote_freshness_for_exit(row_anchor_fail, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is False
    assert "防极端暴跌抽单保护拦截" in reason


def test_option_exit_quality_last_price_zero_fallback():
    # 当期权无今日成交（last=0 或 None），只要双边盘口健康且无倒挂，不应因偏离锚除零崩溃
    row_no_last = pd.Series({"bid_price": 2.50, "ask_price": 2.60, "last_price": 0.0})
    ok, reason, spread = option_quote_freshness_for_exit(row_no_last, maximum_spread_pct=20.0, price_deviation_anchor_pct=0.40)
    assert ok is True
    assert "双边盘口健康可用" in reason
    assert spread < 5.0
