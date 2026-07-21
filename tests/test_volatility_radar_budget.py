from __future__ import annotations

import futu_quant.config as config


def test_resolve_volatility_radar_budget_prefers_symbol_override(monkeypatch):
    monkeypatch.setattr(
        config, "load_volatility_radar_budgets",
        lambda: {"default": 1000.0, "symbols": {"US.GOOG": 2500.0}},
    )

    assert config.resolve_volatility_radar_budget("GOOG") == 2500.0
    assert config.resolve_volatility_radar_budget("AMD") == 1000.0
