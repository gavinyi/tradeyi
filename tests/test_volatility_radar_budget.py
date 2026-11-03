from __future__ import annotations

import futu_quant.config as config

TENANT = "futu_real_master"


def test_resolve_volatility_radar_budget_prefers_symbol_override(monkeypatch):
    # 契约在 P1 改了：下单口径的解析**必须带租户**（无租户 fail-closed 返回 0），
    # 因此这里显式传租户；语义仍是「标的单独预算优先于默认预算」，未放宽任何门槛。
    monkeypatch.setattr(
        config, "load_volatility_radar_budgets",
        lambda tenant_id=None, store=None: {"default": 1000.0, "symbols": {"US.GOOG": 2500.0}},
    )

    assert config.resolve_volatility_radar_budget("GOOG", TENANT) == 2500.0
    assert config.resolve_volatility_radar_budget("AMD", TENANT) == 1000.0
