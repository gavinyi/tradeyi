"""派生限额的变异守卫：六条变异必须**实跑变红**。

Reviewer 2026-11-04 阻断 3（第二版）：第一版 mut3-6 是自证式 —— 自己造
`SimpleNamespace(allowed=True)` 去断言，跟真守卫无关，把真守卫整段删掉照样绿。
本版改为：**monkeypatch 真实现（把守卫改成危险版本）→ 调用既有真用例 →
`pytest.raises(AssertionError)`**。判据是「删掉真守卫，用例必须红」。

六条变异对应七层派生里最容易被悄悄改回去的六处：
1. 未启用时页面拿派生预览值冒充生效值；
2. 变松基线取预览链，绕过二次确认；
3. 去掉 min(派生单笔, 容量线)；
4. 派生未解析时 BUY 仍放行（fail-open）；
5. 派生未解析时 SELL 也被拒（锁死退出通道）；
6. 容量线不过 clamp（可被无限放大）。
"""
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import futu_quant.limit_display as limit_display
import futu_quant.limit_profile as limit_profile
import futu_quant.limit_profile_store as profile_store
import futu_quant.real_trading as real_trading
from futu_quant.limit_profile import LimitProfile, resolve_limit_chain
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.store import TradingStore

TENANT = "futu_paper_sandbox"
EQUITY = 982_356.163
REAL_EQUITY = 6_622.6445


def _legacy():
    return SimpleNamespace(
        max_order_notional=2_500.0, max_daily_notional=5_000.0, max_daily_loss=100_000.0
    )


def _load_case_module(name: str):
    """加载既有真用例文件，直接调用其中用例（不复制断言）。"""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"mutcase_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 变异 1 / 2：页面同源与变松基线（第一版即为真变异，保留）
# ---------------------------------------------------------------------------


def _guard_legacy_shows_old_key(store):
    display = limit_display.resolve_display_limits(store, TENANT, EQUITY, _legacy())
    assert display.effective_per_order == 2_500.0


def test_mutation_1_preview_value_masquerading_as_effective_fails_guard(tmp_path, monkeypatch):
    """变异 1：未启用时返回派生预览值 → 既有守卫必须变红。"""
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


# ---------------------------------------------------------------------------
# 变异 3：去掉 min(派生单笔, 容量线)
# ---------------------------------------------------------------------------


def test_mutation_3_dropping_min_fails_real_capacity_case(tmp_path, monkeypatch):
    """变异 3：`effective_per_order` 不做 min → 跑既有同源用例必须红。"""
    cases = _load_case_module("test_limit_page_same_source")

    def mutated_effective_per_order(per_order, capacity_line):
        return per_order  # 变异：丢掉容量线约束

    monkeypatch.setattr(limit_display, "effective_per_order", mutated_effective_per_order)
    monkeypatch.setattr(cases, "effective_per_order", mutated_effective_per_order)
    with pytest.raises(AssertionError):
        cases.test_derived_state_with_capacity_ratio_is_clamped_by_capacity_line(tmp_path)


# ---------------------------------------------------------------------------
# 变异 4 / 5：实盘门禁方向分流
# ---------------------------------------------------------------------------


def _mutated_check_real_trade(monkeypatch, *, allow_buy: bool, allow_sell: bool):
    """把真门禁换成「守卫被删掉」的版本，其余调用原实现。"""
    original = real_trading.check_real_trade

    def mutated(store, settings, strategy_id, symbol, side, quantity, price, metadata=None):
        if str(side).upper() == "BUY" and allow_buy:
            return SimpleNamespace(allowed=True, reason="", details={})
        if str(side).upper() == "SELL" and not allow_sell:
            return SimpleNamespace(
                allowed=False, reason="LIMIT_PROFILE_UNRESOLVED",
                details={"reason_code": "LIMIT_PROFILE_UNRESOLVED"},
            )
        return original(store, settings, strategy_id, symbol, side, quantity, price, metadata or {})

    monkeypatch.setattr(real_trading, "check_real_trade", mutated)
    return mutated


def test_mutation_4_fail_open_buy_fails_real_gate_case(tmp_path, monkeypatch):
    """变异 4：派生未解析时 BUY 放行 → 跑既有 BUY 拒绝用例必须红。"""
    cases = _load_case_module("test_limit_profile_diagnostic")
    _mutated_check_real_trade(monkeypatch, allow_buy=True, allow_sell=True)
    with pytest.raises(AssertionError):
        cases.test_check_real_trade_rejects_unresolved_profile_with_reason_code(monkeypatch, tmp_path)


def test_mutation_5_blocking_sell_fails_real_gate_case(tmp_path, monkeypatch):
    """变异 5：派生未解析时 SELL 也拒 → 跑既有 SELL 放行用例必须红。"""
    cases = _load_case_module("test_limit_profile_diagnostic")
    _mutated_check_real_trade(monkeypatch, allow_buy=False, allow_sell=False)
    with pytest.raises(AssertionError):
        cases.test_check_real_trade_allows_sell_when_profile_unresolved(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# 变异 6：容量线不过 clamp
# ---------------------------------------------------------------------------


def test_mutation_6_unclamped_capacity_fails_real_clamp_case(monkeypatch):
    """变异 6：容量线不过 clamp → 跑既有 clamp 用例必须红。"""
    cases = _load_case_module("test_limit_derivation_wiring")
    original = limit_profile.resolve_limit_chain

    def mutated(store, tenant_id, equity, profile=None):
        chain = original(store, tenant_id, equity, profile)
        cfg = profile or LimitProfile()
        if cfg.capacity_ratio is None:
            return chain
        unclamped = float(equity) * float(cfg.capacity_ratio)  # 变异：不夹到 100%
        clamps = {k: v for k, v in (chain.clamped or {}).items() if k != "capacity_line"}
        return replace(chain, capacity_line=unclamped, clamped=clamps)

    monkeypatch.setattr(limit_profile, "resolve_limit_chain", mutated)
    monkeypatch.setattr(cases, "resolve_limit_chain", mutated)
    with pytest.raises(AssertionError):
        cases.test_capacity_ratio_is_clamped_and_classified()
