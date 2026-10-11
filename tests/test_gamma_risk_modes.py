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


def test_iv_entry_blocks_extreme_absolute_iv():
    # 隐含波动率超过默认 85% 绝对上限（例如 TSLA 极端高波），应被阻断
    analysis_extreme = {"隐含波动率": 0.95, "IV/RV": 1.8, "IV历史百分位": 0.6}
    allowed, reason = iv_entry_allowed(analysis_extreme, "VOLATILITY")
    assert not allowed
    assert "超过绝对上限" in reason

    # 正常隐含波动率（如 45%），且 IV/RV 和百分位正常，应放行
    analysis_normal = {"隐含波动率": 0.45, "IV/RV": 1.2, "IV历史百分位": 0.5}
    allowed, reason = iv_entry_allowed(analysis_normal, "VOLATILITY")
    assert allowed
    assert "IV 风控通过" in reason


def test_realized_volatility_computes_with_short_samples():
    import pandas as pd
    from futu_quant.gamma_risk import realized_volatility

    df = pd.DataFrame({"close": [100.0, 102.0, 101.0, 103.0, 102.5, 104.0]})
    rv = realized_volatility(df)
    assert rv > 0.0


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
