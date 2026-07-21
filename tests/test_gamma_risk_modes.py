from __future__ import annotations

from futu_quant.gamma_risk import gamma_add_allowed, iv_entry_allowed, normalize_iv


def test_volatility_mode_uses_separate_higher_iv_limits(monkeypatch):
    monkeypatch.setenv("GAMMA_MAX_IV_RV", "2.5")
    monkeypatch.setenv("GAMMA_MAX_IV_PERCENTILE", "0.95")
    monkeypatch.setenv("GAMMA_VOLATILITY_MAX_IV_RV", "5.0")
    monkeypatch.setenv("GAMMA_VOLATILITY_MAX_IV_PERCENTILE", "1.0")
    analysis = {"IV/RV": 3.0, "IV历史百分位": 1.0}

    earnings_allowed, _ = iv_entry_allowed(analysis, "EARNINGS")
    volatility_allowed, _ = iv_entry_allowed(analysis, "VOLATILITY")

    assert not earnings_allowed
    assert volatility_allowed


def test_futu_na_numeric_fields_do_not_crash_gamma_risk():
    assert normalize_iv("N/A") == 0.0
    allowed, _ = gamma_add_allowed(
        {"Delta": 0, "Gamma": 0, "Vega": 0},
        {
            "option_contract_multiplier": "N/A", "option_delta": "N/A",
            "option_gamma": "N/A", "option_vega": "N/A",
        },
        1,
    )
    assert allowed
