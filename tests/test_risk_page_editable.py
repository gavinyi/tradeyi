"""P0-UI-2 第一刀守卫：风险与限额页的「唯一可编辑入口」与 5 项限额。

用户诉求原话：「我怎么没有一个可以改的地方呢」。P0-UI-1 只做了只读骨架，
点进去什么都改不了 —— 本文件锁住「真的能改、改对地方、改完真生效、改错会被拦」。

其中**最重要的一条**是 Reviewer 2026-11-01 抓出的写入层问题：
页面原先写 `ENV_SETTINGS`（最低优先级的 env 兜底），而生产库租户层
`risk_limits:{tenant}` 存在，租户层永远压过 env —— 会出现「保存成功但风控没变」。
本文件用**真实解析链**验证：写完后 `resolve_risk_limits` 必须真的读到新值。

1. 元数据锁 5 项（PRD §12 B 段），不得私自扩项；
2. 生效值唯一来源 = `RiskLimits`，页面不读 env 拼第二套；
3. 校验：区间/非数字/NaN/Inf 必须被拒；
4. 只写变化项；
5. **写租户层、且写完解析链必须生效**；账户级接管时禁写；
6. **写的是完整 10 字段**（部分配置会被 fail-closed 拒绝，这是真事故）；
7. ②实盘门禁只读；页面薄视图。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from futu_quant.risk_limit_editing import (
    EDITABLE_ITEMS,
    EDITABLE_KEYS,
    build_changes,
    build_tenant_risk_payload,
    effective_values,
    save_tenant_risk_limits,
    source_is_writable_here,
    validate_value,
)
from futu_quant.risk_limits import (
    ACCOUNT_RISK_FIELDS,
    resolve_risk_limits,
    risk_limits_tenant_key,
)
from futu_quant.store import TradingStore

PAGE = Path(__file__).resolve().parents[1] / "pages" / "45_risk_and_limits.py"

TENANT = "futu_paper_sandbox"
ACCOUNT = "20170229"


def _complete_risk_section(**overrides) -> dict:
    """一份**完整**的 10 字段 risk 段（部分配置会被 fail-closed 拒绝）。"""
    base = {
        "max_entry_notional": 20000.0,
        "max_entry_daily_notional": 100000.0,
        "max_reduction_order_notional": 150000.0,
        "max_daily_reduction_notional": 500000.0,
        "max_pending_orders": 20,
        "max_symbol_weight": 0.25,
        "max_gross_exposure": 0.85,
        "max_option_premium": 30000.0,
        "max_daily_loss": 5000.0,
        "max_drawdown_pct": 0.05,
    }
    base.update(overrides)
    return base


def _scope(account_id: str = ACCOUNT, tenant_id: str = TENANT) -> SimpleNamespace:
    return SimpleNamespace(account_id=account_id, tenant_id=tenant_id)


def _seed_tenant_limits(store: TradingStore, **overrides) -> None:
    import json

    store.set_setting(
        risk_limits_tenant_key(TENANT),
        json.dumps(_complete_risk_section(**overrides)),
    )


# ---------------------------------------------------------------------------
# 1. 元数据与取值
# ---------------------------------------------------------------------------


def test_editable_items_are_exactly_five_per_prd():
    assert len(EDITABLE_ITEMS) == 5
    assert EDITABLE_KEYS == (
        "max_entry_notional",
        "max_entry_daily_notional",
        "max_reduction_order_notional",
        "max_daily_reduction_notional",
        "max_daily_loss",
    )


def test_editable_keys_are_real_risk_section_fields():
    """键名必须是 risk 段真字段名，否则写下去等于写了个风控不认识的键。"""
    known = {name for name, _ in ACCOUNT_RISK_FIELDS}
    for key in EDITABLE_KEYS:
        assert key in known, f"{key} 不是 risk 段字段，写入会被忽略/告警"


def test_editable_items_are_bounded_and_described():
    for item in EDITABLE_ITEMS:
        assert item.minimum > 0
        assert item.maximum > item.minimum
        assert item.step > 0
        assert item.impact.strip()


def test_effective_values_reads_from_limits_only():
    limits = SimpleNamespace(
        max_order_notional=20000.0, max_daily_notional=100000.0,
        max_reduction_order_notional=150000.0, max_daily_reduction_notional=500000.0,
        max_daily_loss=5000.0,
    )
    values = effective_values(limits)

    assert values["max_entry_notional"] == 20000.0
    assert values["max_daily_loss"] == 5000.0
    assert len(values) == 5


def test_effective_values_skips_missing_attribute_instead_of_zero():
    values = effective_values(SimpleNamespace(max_order_notional=1234.0))

    assert values == {"max_entry_notional": 1234.0}


# ---------------------------------------------------------------------------
# 2. 校验
# ---------------------------------------------------------------------------


def test_validate_rejects_out_of_range_and_non_finite():
    item = EDITABLE_ITEMS[0]
    for bad in (item.minimum - 1, item.maximum + 1, "abc", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            validate_value(item, bad)


def test_validate_accepts_boundaries():
    item = EDITABLE_ITEMS[0]
    assert validate_value(item, item.minimum) == item.minimum
    assert validate_value(item, item.maximum) == item.maximum


def test_build_changes_only_returns_modified_keys():
    current = {item.key: item.minimum for item in EDITABLE_ITEMS}
    wanted = dict(current)
    wanted["max_entry_notional"] = 30000.0

    assert build_changes(wanted, current) == {"max_entry_notional": "30000.0"}


def test_build_changes_returns_empty_when_unchanged():
    current = {item.key: item.minimum for item in EDITABLE_ITEMS}
    assert build_changes(dict(current), current) == {}


# ---------------------------------------------------------------------------
# 3. 写入层：写租户层，且写完解析链必须真的变（Reviewer 的核心关切）
# ---------------------------------------------------------------------------


def test_save_writes_tenant_layer_and_takes_effect(tmp_path):
    """写完 `resolve_risk_limits` 必须读到新值 —— 这条能抓「保存成功但没生效」。"""
    store = TradingStore(tmp_path / "eff.db")
    _seed_tenant_limits(store)
    scope = _scope()

    before = resolve_risk_limits(store, scope)
    assert before.max_order_notional == 20000.0

    payload = build_tenant_risk_payload(before, {"max_entry_notional": "30000.0"})
    written_key = save_tenant_risk_limits(store, TENANT, payload)

    assert written_key == risk_limits_tenant_key(TENANT)
    after = resolve_risk_limits(store, scope)
    assert after.max_order_notional == 30000.0, "写完风控读不到新值 —— 就是本次要修的坑"
    # 未改动字段必须保持原值（只改这 5 项，其余不动）
    assert after.max_daily_loss == before.max_daily_loss


def test_payload_is_complete_so_fail_closed_is_not_triggered(tmp_path):
    """只写 5 项会让 risk 段变「部分配置」→ 风控 fail-closed 拒绝一切开仓。"""
    store = TradingStore(tmp_path / "complete.db")
    _seed_tenant_limits(store)
    limits = resolve_risk_limits(store, _scope())

    payload = build_tenant_risk_payload(limits, {"max_daily_loss": "6000.0"})

    assert set(payload) == {name for name, _ in ACCOUNT_RISK_FIELDS}
    assert payload["max_daily_loss"] == 6000.0
    # 写下去后解析链必须仍然可用（不完整的段会抛 RiskLimitsConfigError）
    save_tenant_risk_limits(store, TENANT, payload)
    assert resolve_risk_limits(store, _scope()).max_daily_loss == 6000.0


def test_account_level_source_is_not_writable_here():
    """账户级 risk 段在租户层之上：本页改了也不生效，必须禁写并指路。"""
    assert source_is_writable_here(f"real_account_config:{ACCOUNT}", TENANT) is False
    assert source_is_writable_here(risk_limits_tenant_key(TENANT), TENANT) is True
    assert source_is_writable_here("env", TENANT) is True
    assert source_is_writable_here("env", "") is False


def test_save_refuses_without_tenant_id(tmp_path):
    store = TradingStore(tmp_path / "no_tenant.db")
    with pytest.raises(ValueError):
        save_tenant_risk_limits(store, "", {"max_entry_notional": 1.0})


def test_save_writes_the_same_table_the_resolver_reads(tmp_path):
    """必须写进 `get_setting` 读的那张表。

    2026-11-01 实测坑：`mutate_runtime_config` 写的是 `runtime_config` 表，而风控解析走
    `get_setting` → `system_settings` 表。用前者写租户级风控，行确实进库了，
    `resolve_risk_limits` 却完全看不到 —— 又一次「写进去了但没生效」。
    """
    import json

    store = TradingStore(tmp_path / "own_table.db")
    _seed_tenant_limits(store)
    limits = resolve_risk_limits(store, _scope())
    key = risk_limits_tenant_key(TENANT)

    save_tenant_risk_limits(
        store, TENANT, build_tenant_risk_payload(limits, {"max_daily_loss": "6000.0"}),
    )

    raw = store.get_setting(key, "")
    assert raw, "写完 get_setting 读不到 —— 风控也读不到"
    assert json.loads(raw)["max_daily_loss"] == 6000.0
    assert resolve_risk_limits(store, _scope()).max_daily_loss == 6000.0


# ---------------------------------------------------------------------------
# 4. 页面层守卫
# ---------------------------------------------------------------------------


def _page_source() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_page_only_writes_derived_profile_not_legacy_dollar_keys():
    """金额只读后：页面唯一写路径是派生层发布，绝不回写旧金额键或 env 兜底层。"""
    source = _page_source()

    assert "publish_limit_profile(" in source, "派生层发布必须走配置中心通道"
    assert "save_tenant_risk_limits(" not in source, (
        "金额已改为只读；再写旧 risk_limits 金额键就会出现第二套真值"
    )
    assert "save_runtime_settings(" not in source, (
        "又写回 ENV_SETTINGS（env 兜底层）—— 租户层存在时保存等于没改"
    )
    assert 'f"limit_profile:' not in source, "配置键禁止在页面手拼，必须走 profile_key"


def test_page_shows_legacy_effective_values_when_derived_disabled():
    """派生未启用时页面必须显示旧键真实生效值，不能拿预览值冒充实际值。"""
    source = _page_source()

    assert "resolve_display_limits(" in source
    assert "派生层未启用" in source
    assert "当前生效（旧键）" in source


def test_page_requires_admin_and_second_confirmation():
    source = _page_source()

    assert "render_admin_login()" in source
    assert "st.checkbox(" in source, "改限额必须二次确认"
    assert "变更理由" in source, "变更理由必填"
    assert "审批指纹" in source, "变松必须留审批指纹"


def test_audit_records_effective_before_and_after_chains():
    """审计必须能回答「当时实际放行多少」：由发布通道落 effective_before/after 快照。

    页面不再自己拼 payload（那是第二套审计口径）；这里锁页面确实只透传审批指纹与理由，
    真正的 before/after 链快照由 `limit_profile_store.publish_limit_profile` 保证。
    """
    source = _page_source()

    assert "approval_fingerprint=approval" in source
    assert "legacy_limits=legacy" in source, (
        "首启用基线必须是旧键实际生效值，传预览链会绕过二次确认"
    )
    assert '"written_payload": dict(payload)' not in source, "页面不得自建审计 payload"


def test_page_shows_capacity_line_and_effective_per_order():
    """45 页必须与首页同口径显示容量线与 min 后的实际单笔。"""
    source = _page_source()

    assert "容量线" in source
    assert "min(派生单笔, 容量线)" in source
    assert "st.metric(" in source, "实际生效金额必须以指标卡展示"


def test_page_does_not_write_real_gate_limits():
    source = _page_source()
    for banned in ("REAL_MAX_ORDER_NOTIONAL", "REAL_MAX_DAILY_NOTIONAL"):
        assert banned not in source


def test_page_does_not_reimplement_validation_or_key_mapping():
    source = _page_source()

    assert "from futu_quant.risk_limit_editing import" in source
    assert "def validate_value(" not in source
    assert "def build_changes(" not in source
    assert "mutate_runtime_config(" not in source, "页面不得自己拼写库"


def test_page_free_of_fake_defaults():
    source = _page_source()
    assert "未统计" in source


def test_page_imports_resolve():
    from futu_quant.operational_readiness import _audit_page_imports

    root = Path(__file__).resolve().parents[1]
    tree = ast.parse(_page_source(), filename=str(PAGE))
    assert _audit_page_imports(root, tree, PAGE.name) == []
