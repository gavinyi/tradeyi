"""页面限额显示必须与派生链实际生效值同源。"""
from pathlib import Path

import pytest

from futu_quant.limit_profile import LimitProfile, resolve_limit_chain


ROOT = Path(__file__).parents[1]
RISK_PAGE = ROOT / "pages" / "45_risk_and_limits.py"
HOME_PAGE = ROOT / "pages" / "00_today_overview.py"


def test_risk_page_displays_chain_outputs_not_formula_values():
    source = RISK_PAGE.read_text(encoding="utf-8")
    # 页面金额必须取 chain 的实际输出；禁止直接用 equity * ratio 展示。
    assert "amount = getattr(chain, output)" in source
    assert "f\"${amount:,.2f}\"" in source
    assert "equity *" not in source


def test_home_status_bar_uses_same_effective_amount_and_layer():
    source = HOME_PAGE.read_text(encoding="utf-8")
    assert "actual = min(chain.per_order, chain.capacity_line)" in source
    assert "resolve_limit_chain(store, tenant_id, equity" in source
    assert "实际生效层" in source


def test_page_contract_covers_hard_cap_and_capacity_binding():
    # 模拟盘公式值 6% = 58,941.37，但实际值必须被单笔硬顶截断为 50,000。
    paper = resolve_limit_chain(
        None,
        "futu_paper_sandbox",
        982356.163,
        LimitProfile(per_order_ratio=0.06, hard_cap_per_order=50_000.0),
    )
    assert 982356.163 * 0.06 == pytest.approx(58941.36978)
    assert paper.per_order == 50_000.0
    assert paper.per_order != pytest.approx(982356.163 * 0.06)

    # 实盘单笔与容量线均来自同一派生链，最终实际值不得超过容量线。
    real = resolve_limit_chain(
        None,
        "futu_real_master",
        6622.6445,
        LimitProfile(per_order_ratio=0.06, capacity_ratio=0.06),
    )
    actual = min(real.per_order, real.capacity_line)
    assert actual == pytest.approx(real.capacity_line)
    assert actual <= real.capacity_line
