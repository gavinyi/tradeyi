from __future__ import annotations

import pandas as pd
import pytest

from futu_quant.combo_analytics import (
    build_template, capacity_assessment, combo_market_snapshot, expiry_payoff,
    rank_vertical_candidates, roll_symbol, run_combo_backtest,
)
from futu_quant.replay import run_combo_resilience_drill


LEGS = [
    {"symbol": "US.NVDA260724C220000", "side": "BUY", "quantity": 1, "limit_price": 1.0},
    {"symbol": "US.NVDA260724C210000", "side": "SELL", "quantity": 1, "limit_price": 1.5},
]


def test_combo_snapshot_aggregates_net_price_greeks_and_capacity():
    quotes = pd.DataFrame([
        {"code": LEGS[0]["symbol"], "bid_price": 0.95, "ask_price": 1.05,
         "option_delta": 0.3, "option_gamma": 0.02, "option_theta": -0.04,
         "option_vega": 0.08, "option_volume": 1000, "option_open_interest": 5000},
        {"code": LEGS[1]["symbol"], "bid_price": 1.45, "ask_price": 1.55,
         "option_delta": 0.5, "option_gamma": 0.03, "option_theta": -0.05,
         "option_vega": 0.10, "option_volume": 1200, "option_open_interest": 6000},
    ])

    detail, package = combo_market_snapshot(LEGS, quotes)
    capacity = capacity_assessment(detail)

    assert package["mid_net_debit"] == -0.5
    assert round(package["net_delta"], 6) == -20
    assert capacity["allowed"]
    assert capacity["capacity"] == 50


def test_expiry_payoff_has_bounded_vertical_spread_risk():
    payoff = expiry_payoff(LEGS, 180, 240)

    assert payoff["组合到期盈亏"].min() == pytest.approx(-950)
    assert payoff["组合到期盈亏"].max() == pytest.approx(50)


def test_combo_backtest_uses_ask_bid_and_shared_cash():
    times = pd.date_range("2026-07-20 14:30", periods=6, freq="min", tz="UTC")
    histories = {
        LEGS[0]["symbol"]: pd.DataFrame({
            "time_key": times, "bid": [0.9, 1.0, 1.1, 1.2, 1.3, 1.4],
            "ask": [1.1, 1.2, 1.3, 1.4, 1.5, 1.6], "quality_status": "GOOD",
        }),
        LEGS[1]["symbol"]: pd.DataFrame({
            "time_key": times, "bid": [1.4, 1.4, 1.3, 1.2, 1.1, 1.0],
            "ask": [1.6, 1.6, 1.5, 1.4, 1.3, 1.2], "quality_status": "GOOD",
        }),
    }

    equity, trades, metrics = run_combo_backtest(histories, LEGS, fee_per_contract=0)

    assert len(equity) == 6
    assert len(trades) == 2
    assert metrics["开仓净价"] == pytest.approx(-0.3)
    assert metrics["平仓净价"] == pytest.approx(0.2)
    assert metrics["总收益率"] > 0


def test_structure_template_candidate_ranking_and_roll_symbol():
    rows = []
    for kind, marker in (("CALL", "C"), ("PUT", "P")):
        for strike in (200, 210, 220, 230):
            rows.append({
                "code": f"US.NVDA260724{marker}{strike * 1000}", "option_type": kind,
                "strike_price": strike, "bid_price": 1.0, "ask_price": 1.1,
                "option_open_interest": 1000,
            })
    chain = pd.DataFrame(rows)

    iron_condor = build_template(chain, "IRON_CONDOR", "US.NVDA", 215)
    ranked = rank_vertical_candidates(chain, 215)

    assert len(iron_condor) == 4
    assert len(ranked) == 6
    assert ranked["综合评分"].is_monotonic_decreasing
    assert roll_symbol("US.NVDA260724C220000", pd.Timestamp("2026-08-21").date()) == "US.NVDA260821C220000"


def test_combo_resilience_drill_passes():
    result = run_combo_resilience_drill()

    assert (result["结果"] == "通过").all()
