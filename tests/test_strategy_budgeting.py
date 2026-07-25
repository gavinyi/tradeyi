from __future__ import annotations

import pytest

from futu_quant.strategy_budgeting import recommended_active_budget_rows


def test_recommended_budget_moves_earnings_capital_to_volatility():
    rows = recommended_active_budget_rows([
        {"strategy_id": "core_satellite", "capital_ratio": 0.65, "max_loss_ratio": 0.15,
         "option_premium_ratio": 0.0, "enabled": True},
        {"strategy_id": "earnings_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
         "option_premium_ratio": 0.10, "enabled": True},
        {"strategy_id": "earnings_gamma_manual", "capital_ratio": 0.05, "max_loss_ratio": 0.02,
         "option_premium_ratio": 0.05, "enabled": True},
        {"strategy_id": "volatility_long_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
         "option_premium_ratio": 0.10, "enabled": True},
    ])
    by_strategy = {row["strategy_id"]: row for row in rows}

    assert by_strategy["core_satellite"]["capital_ratio"] == pytest.approx(0.65)
    assert by_strategy["volatility_long_gamma"]["capital_ratio"] == pytest.approx(0.25)
    assert by_strategy["volatility_long_gamma"]["option_premium_ratio"] == pytest.approx(0.25)
    assert by_strategy["earnings_gamma"]["capital_ratio"] == 0
    assert by_strategy["earnings_gamma"]["enabled"] is False
    assert by_strategy["earnings_gamma_manual"]["capital_ratio"] == 0
    assert by_strategy["earnings_gamma_manual"]["enabled"] is False
    assert sum(row["capital_ratio"] for row in rows if row["enabled"]) == pytest.approx(0.90)
