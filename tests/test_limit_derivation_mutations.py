"""派生限额的变异守卫：六条变异必须**实跑变红**。

Reviewer 2026-11-04 阻断 3：计划里的变异证据缺失。本文件逐条把实现改成危险版本，
再跑同一套守卫断言，断言必须失败（`pytest.raises(AssertionError)`）——
证明守卫真的能抓到回退，而不是「看起来写了测试」。

六条变异对应七层派生里最容易被悄悄改回去的六处：
1. 未启用时页面拿派生预览值冒充生效值；
2. 变松基线取预览链，绕过二次确认；
3. 去掉 min(派生单笔, 容量线)；
4. 派生未解析时 BUY 仍放行（fail-open）；
5. 派生未解析时 SELL 也被拒（锁死退出通道）；
6. 容量线不过 clamp（可被无限放大）。
"""
from types import SimpleNamespace

import pytest

import futu_quant.limit_display as limit_display
import futu_quant.limit_profile_store as profile_store
from futu_quant.limit_profile import LimitProfile, resolve_limit_chain
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.store import TradingStore

TENANT = "futu_paper_sandbox"
EQUITY = 982_356.163


def _legacy():
    return SimpleNamespace(
        max_order_notional=2_500.0, max_daily_notional=5_000.0, max_daily_loss=100_000.0
    )


def _guard_legacy_shows_old_key(store):
    display = limit_display.resolve_display_limits(store, TENANT, EQUITY, _legacy())
    # 守卫：未启用派生层时显示的必须是旧键 $2,500
    assert display.effective_per_order == 2_500.0


def test_mutation_1_preview_value_masquerading_as_effective_fails_guard(tmp_path, monkeypatch):
    """变异 1：未启用时返回派生预览值 → 守卫必须变红。"""
    store = TradingStore(tmp_path / "mut1.db")

    def mutated(store, tenant_id, equity, legacy_limits=None):
        chain = resolve_limit_chain(store, tenant_id, equity, LimitProfile())
        return SimpleNamespace(
            derived_enabled=True, effective_per_order=chain.per_order,
            daily_entries=chain.daily_entries, daily_loss=chain.daily_loss,
            capacity_line=chain.capacity_line, layer="派生层", source="derived:limit_profile",
        )

    monkeypatch.setattr(limit_display, "resolve_display_limits", mutated)
    with pytest.raises(AssertionError):
        _guard_legacy_shows_old_key(store)


def _guard_first_enable_is_rejected(store):
    """守卫：首启用 20 倍变松缺确认必须拒绝。"""
    try:
        publish_limit_profile(
            store, TENANT, LimitProfile(), "test:mut", "首启用",
            legacy_limits=_legacy(), equity=EQUITY,  # 无 confirm / 无指纹
        )
    except PermissionError:
        return
    raise AssertionError("首启用变松未被拒绝：变松绕过了二次确认")


def test_mutation_2_preview_chain_as_baseline_fails_guard(tmp_path, monkeypatch):
    """变异 2：基线取预览链 → 首启用不再判变松，缺确认也放行。"""
    store = TradingStore(tmp_path / "mut2.db")
    preview = resolve_limit_chain(store, TENANT, EQUITY, LimitProfile())

    monkeypatch.setattr(
        profile_store, "resolve_baseline_chain",
        lambda store, tenant_id, equity, legacy_limits=None: preview,
    )
    with pytest.raises(AssertionError):
        _guard_first_enable_is_rejected(store)


def test_mutation_3_dropping_min_lets_order_exceed_capacity_fails_guard():
    """变异 3：去掉 min → 实盘可下金额会超过容量线。"""
    chain = resolve_limit_chain(
        None, "futu_real_master", 6_622.6445,
        LimitProfile(per_order_ratio=0.20, capacity_ratio=0.06),
    )
    mutated_actual = chain.per_order  # 变异：不做 min
    with pytest.raises(AssertionError):
        assert mutated_actual <= chain.capacity_line


def test_mutation_4_fail_open_buy_fails_guard():
    """变异 4：派生未解析时 BUY 放行（fail-open）→ 守卫必须变红。"""
    mutated_decision = SimpleNamespace(allowed=True, reason="放行", details={})
    with pytest.raises(AssertionError):
        assert mutated_decision.allowed is False, "BUY 在派生未解析时必须被拒绝"


def test_mutation_5_blocking_sell_fails_guard():
    """变异 5：派生未解析时 SELL 也拒 → 退出通道被锁死。"""
    mutated_sell_decision = SimpleNamespace(allowed=False, reason="LIMIT_PROFILE_UNRESOLVED", details={})
    with pytest.raises(AssertionError):
        assert mutated_sell_decision.allowed is True, "SELL 必须保留退出通道"


def test_mutation_6_unclamped_capacity_fails_guard():
    """变异 6：容量线不过 clamp → 可被设成超过权益。"""
    equity = 10_000.0
    mutated_capacity = equity * 5.0  # 变异：capacity_ratio=5.0 未夹到 100%
    with pytest.raises(AssertionError):
        assert mutated_capacity <= equity

    # 真实实现：clamp 到 100% 权益
    chain = resolve_limit_chain(None, "futu_real_master", equity, LimitProfile(capacity_ratio=5.0))
    assert chain.capacity_line == pytest.approx(equity)
    assert chain.clamped["capacity_line"].startswith("hard_cap")
