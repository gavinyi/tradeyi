"""页面限额显示必须与运行时实际生效值同源（两态真值断言）。

Reviewer 2026-11-04 阻断 1：页面在派生未启用时显示派生预览值（模拟盘 `$50,000`），
而运行时实际生效仍是旧键 `$2,500` —— 页面一个值、实际另一个值。
本文件锁两件事：
1. `resolve_display_limits` 在 `legacy` / `derived` 两态都返回**真正生效**的金额；
2. 两个页面都只通过该函数取数，页面内不得再算一遍（不得出现 `equity *`）。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from futu_quant.limit_display import effective_per_order, resolve_display_limits
from futu_quant.limit_profile import LimitProfile
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.store import TradingStore

ROOT = Path(__file__).parents[1]
RISK_PAGE = ROOT / "pages" / "45_risk_and_limits.py"
HOME_PAGE = ROOT / "pages" / "00_today_overview.py"

TENANT = "futu_paper_sandbox"
EQUITY = 982_356.163  # 模拟盘生产实测权益
LEGACY_PER_ORDER = 2_500.0
LEGACY_DAILY = 5_000.0
LEGACY_LOSS = 100_000.0


def _legacy_limits():
    return SimpleNamespace(
        max_order_notional=LEGACY_PER_ORDER,
        max_daily_notional=LEGACY_DAILY,
        max_daily_loss=LEGACY_LOSS,
    )


def test_legacy_state_shows_old_key_amount_not_derived_preview(tmp_path):
    """未发布派生层：页面必须显示旧键 $2,500，绝不能显示派生预览 $50,000。"""
    store = TradingStore(tmp_path / "legacy.db")
    display = resolve_display_limits(store, TENANT, EQUITY, _legacy_limits())

    assert display.derived_enabled is False
    assert display.effective_per_order == LEGACY_PER_ORDER
    assert display.daily_entries == LEGACY_DAILY
    assert display.daily_loss == LEGACY_LOSS
    assert display.layer == "旧键"
    # 派生预览值（6% 被硬顶成 50,000）不得出现在未启用态的显示值里
    assert display.effective_per_order != pytest.approx(50_000.0)
    assert display.effective_per_order != pytest.approx(EQUITY * 0.06)


def test_derived_state_shows_hard_capped_value_equal_to_runtime_min(tmp_path):
    """已启用：显示值 = 硬顶截断后的 $50,000，且等于运行时 min(派生单笔, 容量线)。"""
    store = TradingStore(tmp_path / "derived.db")
    published = publish_limit_profile(
        store, TENANT, LimitProfile(), "streamlit_admin:test", "初始化派生限额",
        legacy_limits=_legacy_limits(), equity=EQUITY,
        approval_fingerprint="fp-init", confirm=True,
    )
    display = resolve_display_limits(store, TENANT, EQUITY, _legacy_limits())
    runtime = published["chain"]

    assert display.derived_enabled is True
    # 公式值 58,941.37 必须被单笔硬顶截断为 50,000
    assert EQUITY * 0.06 == pytest.approx(58_941.36978)
    assert display.per_order == 50_000.0
    assert display.per_order != pytest.approx(EQUITY * 0.06)
    # 页面单笔 == 运行时生效链的单笔（同源）
    assert display.per_order == runtime["per_order"]
    assert display.daily_entries == runtime["daily_entries"]
    assert display.daily_loss == runtime["daily_loss"]
    assert display.effective_per_order == effective_per_order(
        runtime["per_order"], runtime.get("capacity_line")
    )


def test_derived_state_with_capacity_ratio_is_clamped_by_capacity_line(tmp_path):
    """实盘：派生单笔调大后，实际可下仍不得超过独立的容量线。"""
    store = TradingStore(tmp_path / "capacity.db")
    tenant = "futu_real_master"
    equity = 6_622.6445
    publish_limit_profile(
        store, tenant,
        LimitProfile(per_order_ratio=0.20, capacity_ratio=0.06),
        "streamlit_admin:test", "调大单笔比例验证容量线独立",
        legacy_limits=SimpleNamespace(
            max_order_notional=1_000.0, max_daily_notional=2_500.0, max_daily_loss=400.0
        ),
        equity=equity, approval_fingerprint="fp-cap", confirm=True,
    )
    display = resolve_display_limits(store, tenant, equity)

    assert display.per_order > display.capacity_line
    assert display.effective_per_order == pytest.approx(display.capacity_line)
    assert display.effective_per_order <= display.capacity_line
    assert display.layer == "容量线"


def test_pages_read_effective_amount_from_single_module():
    """页面必须是薄视图：只调 resolve_display_limits，不得自己再算一遍金额。"""
    for page in (RISK_PAGE, HOME_PAGE):
        source = page.read_text(encoding="utf-8")
        assert "resolve_display_limits" in source, f"{page.name} 未走统一展示出口"
        assert "equity * 0.06" not in source
        assert "equity * cfg" not in source
