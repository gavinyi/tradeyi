from __future__ import annotations

import numpy as np
import pandas as pd

from futu_quant.portfolio_risk import (
    portfolio_exposure, projected_portfolio_allowed, underlying_of,
)
from futu_quant.risk_calibration import generate_recommendations
from futu_quant.store import TradingStore
from futu_quant.walk_forward import walk_forward_validate


def test_portfolio_exposure_combines_stock_and_option_family():
    positions = pd.DataFrame([
        {"code": "US.NVDA", "qty": 10},
        {"code": "US.NVDA260717C150000", "qty": 1},
    ])
    quotes = pd.DataFrame([
        {"code": "US.NVDA", "last_price": 100},
        {"code": "US.NVDA260717C150000", "last_price": 5,
         "option_delta": 0.5, "option_gamma": 0.02,
         "option_vega": 0.1, "option_theta": -0.05,
         "option_contract_multiplier": 100},
    ])

    exposure = portfolio_exposure(positions, quotes)

    assert underlying_of("US.NVDA260717C150000") == "US.NVDA"
    assert exposure["families"]["US.NVDA"] == 1250
    assert exposure["gamma"] == 2


def test_projected_greeks_include_candidate_order(monkeypatch):
    monkeypatch.setenv("RISK_MAX_ABS_GAMMA", "1")

    class Broker:
        def positions(self):
            return pd.DataFrame()

        def account_info(self):
            return pd.DataFrame([{"total_assets": 100_000}])

        def snapshots(self, symbols):
            return pd.DataFrame([{
                "code": symbols[0], "last_price": 2, "option_delta": .5,
                "option_gamma": .02, "option_vega": .1, "option_theta": -.05,
                "option_contract_multiplier": 100,
            }])

    allowed, reason, exposure = projected_portfolio_allowed(
        Broker(), "US.NVDA260717C200000", "BUY", 1, 2, 100, "US.NVDA",
    )

    assert not allowed
    assert "Gamma" in reason
    assert exposure["projected_gamma"] == 2


def test_walk_forward_returns_stability_and_bootstrap():
    count = 360
    prices = 100 + np.sin(np.arange(count) / 8) * 8 + np.arange(count) * 0.05
    bars = pd.DataFrame({
        "time_key": pd.date_range("2024-01-01", periods=count, freq="B").astype(str),
        "open": prices, "close": prices + np.sin(np.arange(count) / 3),
    })

    result = walk_forward_validate(bars, "SMA Cross", 100_000, 10, 1, 2)

    assert len(result["windows"]) == 4
    assert len(result["sensitivity"]) == 3
    assert 0 <= result["summary"]["bootstrap_positive_probability"] <= 1


def test_risk_calibration_creates_pending_recommendation(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    intent, _ = store.create_intent("sma", "x", "US.NVDA", "BUY", 10, 100)
    store.update_shadow_intent(intent["id"], "SHADOW_FILLED", 10, 100.2, 1)

    generated = generate_recommendations(store)

    assert generated
    assert {row["status"] for row in store.risk_recommendations()} == {"PENDING"}
