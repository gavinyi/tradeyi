"""P0-UI-2 第一刀守卫：风险与限额页的「唯一可编辑入口」与 5 项限额。

用户诉求原话：「我怎么没有一个可以改的地方呢」。P0-UI-1 只做了只读骨架，
点进去什么都改不了 —— 本文件锁住「真的能改、改对地方、改错会被拦」：

1. **元数据锁 5 项**（PRD §12 B 段），不得私自扩项；
2. **生效值唯一来源** = `RiskLimits`，页面不读 env 拼第二套；
3. **校验**：区间/非数字/NaN/Inf 必须被拒，不允许写进去；
4. **只写变化项**：值没变就不写库，避免无意义审计与脏写；
5. **单写入口**：页面只调用 `save_runtime_settings`（唯一写路径）+ 审计；
6. **②实盘门禁只读**：页面不得出现 `REAL_MAX_*` 的写入；
7. **薄视图**：业务逻辑在库里，页面不得自己实现校验/取值。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from futu_quant.risk_limit_editing import (
    EDITABLE_ITEMS,
    EDITABLE_KEYS,
    build_changes,
    effective_values,
    validate_value,
)
from futu_quant.ui import open_trading_store  # noqa: F401 - 仅为确认页面依赖的入口存在

PAGE = Path(__file__).resolve().parents[1] / "pages" / "45_risk_and_limits.py"


class _FakeLimits:
    """模拟 `RiskLimits` 上被页面引用的 5 个属性。"""

    max_order_notional = 20000.0
    max_daily_notional = 100000.0
    max_reduction_order_notional = 150000.0
    max_daily_reduction_notional = 500000.0
    max_daily_loss = 5000.0


def test_editable_items_are_exactly_five_per_prd():
    """PRD §12 锁定 5 项；扩项必须重走 PRD，不允许页面悄悄加第 6 个。"""
    assert len(EDITABLE_ITEMS) == 5
    assert EDITABLE_KEYS == (
        "RISK_MAX_ORDER_NOTIONAL",
        "RISK_MAX_DAILY_NOTIONAL",
        "RISK_MAX_REDUCTION_ORDER_NOTIONAL",
        "RISK_MAX_DAILY_REDUCTION_NOTIONAL",
        "RISK_MAX_DAILY_LOSS",
    )


def test_editable_items_are_all_positive_bounded_and_described():
    """每项必须有区间、有业务说明（PRD AC4：影响说清楚）。"""
    for item in EDITABLE_ITEMS:
        assert item.minimum > 0
        assert item.maximum > item.minimum
        assert item.step > 0
        assert item.impact.strip(), f"{item.key} 缺少「它控制什么」说明"


def test_effective_values_reads_from_limits_only():
    values = effective_values(_FakeLimits())

    assert values["RISK_MAX_ORDER_NOTIONAL"] == 20000.0
    assert values["RISK_MAX_DAILY_LOSS"] == 5000.0
    assert len(values) == 5


def test_effective_values_skips_missing_attribute_instead_of_zero():
    """取不到就**不出现**，绝不填 0 冒充「额度为 0」。"""
    class _Partial:
        max_order_notional = 1234.0

    values = effective_values(_Partial())

    assert values == {"RISK_MAX_ORDER_NOTIONAL": 1234.0}


def test_validate_rejects_out_of_range_and_non_numeric():
    item = EDITABLE_ITEMS[0]

    with pytest.raises(ValueError):
        validate_value(item, item.minimum - 1)
    with pytest.raises(ValueError):
        validate_value(item, item.maximum + 1)
    with pytest.raises(ValueError):
        validate_value(item, "abc")
    with pytest.raises(ValueError):
        validate_value(item, float("nan"))
    with pytest.raises(ValueError):
        validate_value(item, float("inf"))


def test_validate_accepts_boundaries():
    item = EDITABLE_ITEMS[0]

    assert validate_value(item, item.minimum) == item.minimum
    assert validate_value(item, item.maximum) == item.maximum


def test_build_changes_only_returns_modified_keys():
    current = effective_values(_FakeLimits())
    wanted = dict(current)
    wanted["RISK_MAX_ORDER_NOTIONAL"] = 30000.0

    changes = build_changes(wanted, current)

    assert changes == {"RISK_MAX_ORDER_NOTIONAL": "30000.0"}


def test_build_changes_returns_empty_when_nothing_changed():
    current = effective_values(_FakeLimits())

    assert build_changes(dict(current), current) == {}


def test_build_changes_writes_all_when_no_current():
    wanted = {item.key: item.minimum for item in EDITABLE_ITEMS}

    changes = build_changes(wanted, None)

    assert set(changes) == set(EDITABLE_KEYS)


# ---------------------------------------------------------------------------
# 页面层守卫
# ---------------------------------------------------------------------------


def _page_tree() -> ast.Module:
    return ast.parse(PAGE.read_text(encoding="utf-8"), filename=str(PAGE))


def test_page_writes_through_single_save_path_and_audits():
    source = PAGE.read_text(encoding="utf-8")

    assert "save_runtime_settings(" in source, "必须走唯一写路径 save_runtime_settings"
    assert "RISK_LIMITS_UPDATED" in source, "改钱相关参数必须留审计"
    assert "store.set_setting(" not in source, "不得绕过唯一写路径直接写键"
    assert "mutate_runtime_config(" not in source, "不得在页面层自行拼写配置版本"


def test_page_has_second_confirmation_and_admin_gate():
    source = PAGE.read_text(encoding="utf-8")

    assert "render_admin_login()" in source, "改钱相关参数必须要求管理员登录"
    assert "st.checkbox(" in source, "必须有表单内二次确认"


def test_page_does_not_write_real_gate_limits():
    """②类实盘门禁项只读：页面不得出现 REAL_MAX_* 的写入。"""
    source = PAGE.read_text(encoding="utf-8")

    for banned in ("REAL_MAX_ORDER_NOTIONAL", "REAL_MAX_DAILY_NOTIONAL"):
        assert banned not in source, f"页面不得编辑/写入 {banned}（②类只读 + 指路）"


def test_page_does_not_reimplement_validation():
    """薄视图：页面不得自带区间校验（否则会和库里的规则分叉）。"""
    source = PAGE.read_text(encoding="utf-8")

    assert "from futu_quant.risk_limit_editing import" in source
    assert "def validate_value(" not in source
    assert "def build_changes(" not in source


def test_page_free_of_fake_defaults():
    """未解析成功时不得显示 0 冒充正常（用户最反感的假状态）。"""
    source = PAGE.read_text(encoding="utf-8")

    assert "未统计" in source
    assert 'f"{item.label} $0"' not in source


def test_page_imports_resolve():
    """页面顶层 futu_quant 导入必须可解析（防 2026-11-01 那类凭空依赖）。"""
    from futu_quant.operational_readiness import _audit_page_imports

    root = Path(__file__).resolve().parents[1]
    issues = _audit_page_imports(root, _page_tree(), PAGE.name)

    assert issues == [], issues
