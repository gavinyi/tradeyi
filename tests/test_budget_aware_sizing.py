"""按预算反推每批张数（2026-11-03 Product §16 / Reviewer 四条红线）。

背景：旧逻辑是「先按固定张数生成整批，再拿批总价去比预算」——
`2 × $11.26 × 100 = $2,252 > $1,000` 直接整批被拒，**一张都不出**，
而其中 1 张完全买得起（$1,126 里真正买不起的是第二张）。

新逻辑：`n = floor(预算 / (ask × 100))`，向下取整；`n = 0` 显式拒绝且带数字。

四条红线（Reviewer）：
1. 分母取 `min(campaign 批预算, loss_limit)`，不能只按 $1,000；
2. 单张成本用 **ask**，不用 mid；
3. `n = 0` 必须显式拒绝并给数字，不静默、不回落"买 1 张试试"；
4. 反推只决定**数量**，不豁免 IV / DTE / 虚值 / 盘口等质量过滤。
"""

from __future__ import annotations

from futu_quant.volatility_optimizer import affordable_stage_contracts


def test_two_contracts_do_not_fit_but_one_does():
    # $11.26/张 → 单张 $1,126 装不下；但预算 $2,000 时 1 张可以
    assert affordable_stage_contracts(11.26, 100, 2, 2000.0) == 1
    # 预算 $1,000 连 1 张都装不下（$1,126 > $1,000）→ 0 张，交给调用方显式拒绝
    assert affordable_stage_contracts(11.26, 100, 2, 1000.0) == 0


def test_cheap_contract_fills_configured_quantity():
    # $3.00/张 → 单张 $300，预算 $1,000 可装 3 张，配置要 2 张 → 取 2
    assert affordable_stage_contracts(3.0, 100, 2, 1000.0) == 2
    # 配置要 5 张时按预算只能给 3 张
    assert affordable_stage_contracts(3.0, 100, 5, 1000.0) == 3


def test_floor_never_rounds_up():
    # $6.55/张 → 单张 $655；预算 $1,000 → floor(1.52) = 1（ceil 会变 2，直接超预算）
    assert affordable_stage_contracts(6.55, 100, 2, 1000.0) == 1
    # 预算恰好等于单张成本 → 1 张（边界）
    assert affordable_stage_contracts(10.0, 100, 2, 1000.0) == 1
    # 差 1 美分 → 0 张（边界）
    assert affordable_stage_contracts(10.0, 100, 2, 999.99) == 0


def test_zero_or_missing_inputs_fail_closed():
    assert affordable_stage_contracts(0.0, 100, 2, 1000.0) == 0
    assert affordable_stage_contracts(-1.0, 100, 2, 1000.0) == 0
    assert affordable_stage_contracts(3.0, 100, 2, 0.0) == 0
    assert affordable_stage_contracts(3.0, 100, 2, None) == 2  # 无预算限制 → 按配置


def test_multiplier_other_than_100_is_honoured():
    # 非标准乘数仍按 ask × multiplier 计算，不写死 100
    assert affordable_stage_contracts(3.0, 10, 2, 1000.0) == 2
    assert affordable_stage_contracts(3.0, 1000, 2, 1000.0) == 0


def test_sizing_does_not_relax_quality_filters():
    """反推只决定数量：同一候选在预算放大后，张数可以变，但质量判定不变。"""
    from futu_quant.volatility_optimizer import rank_volatility_contracts

    import pandas as pd

    row = {
        "code": "US.QQQ261120C740000", "option_type": "CALL",
        "strike_time": "2026-11-20", "strike_price": 740.0,
        "bid_price": 1.60, "ask_price": 1.65, "mid_price": 1.625,
        "spread_pct": 3.0, "volume": 500, "option_open_interest": 3000,
        "option_delta": 0.45, "option_gamma": 0.02, "option_theta": -0.5,
        "option_vega": 0.1, "option_rho": 0.01, "option_contract_multiplier": 100,
        "option_implied_volatility": 0.5, "model_expected_return": 0.5,
        "iv_history_percentile": 0.5, "iv_percentile_status": "HISTORICAL",
        "iv_rv": 1.0, "iv_rv_limit": 1.5, "expected_exit_bid": 2.0,
        "moneyness_pct": 1.0,
    }
    frame = pd.DataFrame([row])
    tight = rank_volatility_contracts(
        frame, 700.0, 0.4, None,
        signal_active=True, strong_signal=False,
        max_premium_per_stage=1000.0, contracts_per_stage=2,
    )
    loose = rank_volatility_contracts(
        frame, 700.0, 0.4, None,
        signal_active=True, strong_signal=False,
        max_premium_per_stage=5000.0, contracts_per_stage=2,
    )
    # 单张 $165：预算 $1,000 → 2 张（$330 装得下）；$5,000 → 仍最多 2 张（配置上限）
    assert int(tight.iloc[0]["stage_contracts"]) == 2
    assert int(loose.iloc[0]["stage_contracts"]) == 2
    # 质量判定与预算无关：两边的可交易结论必须一致
    assert bool(tight.iloc[0]["volatility_eligible"]) == bool(loose.iloc[0]["volatility_eligible"])


def test_zero_contracts_reported_with_numbers():
    import pandas as pd

    from futu_quant.volatility_optimizer import rank_volatility_contracts

    row = {
        "code": "US.AMD261120C200000", "option_type": "CALL",
        "strike_time": "2026-11-20", "strike_price": 200.0,
        "bid_price": 16.40, "ask_price": 16.50, "mid_price": 16.45,
        "spread_pct": 2.0, "volume": 500, "option_open_interest": 3000,
        "option_delta": 0.45, "option_gamma": 0.02, "option_theta": -0.5,
        "option_vega": 0.1, "option_rho": 0.01, "option_contract_multiplier": 100,
        "option_implied_volatility": 0.5, "model_expected_return": 0.5,
        "iv_history_percentile": 0.5, "iv_percentile_status": "HISTORICAL",
        "iv_rv": 1.0, "iv_rv_limit": 1.5, "expected_exit_bid": 20.0,
        "moneyness_pct": 1.0,
    }
    result = rank_volatility_contracts(
        pd.DataFrame([row]), 198.0, 0.4, None,
        signal_active=True, strong_signal=False,
        max_premium_per_stage=1000.0, contracts_per_stage=2,
    )
    assert int(result.iloc[0]["stage_contracts"]) == 0
    assert bool(result.iloc[0]["premium_budget_ok"]) is False
    reason = str(result.iloc[0]["volatility_filter_reason"])
    # 必须带数字：预算 $1,000 < 单张成本 $1,650
    assert "$1,000" in reason and "$1,650" in reason
    assert "可装 0 张" in reason
