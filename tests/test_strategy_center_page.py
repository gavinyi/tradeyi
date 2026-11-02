"""P0-UI-2 第三刀守卫：策略中心 = 策略启停 / 暂停新开仓**唯一可编辑入口**。

判据沿用 Reviewer 四条（2026-11-01）：

1. **写入层 = 读取层**：`set_strategy_enabled` / `set_strategy_entries_paused` 写
   `system_settings`（`set_setting`），读取方 `resolve_strategy_enabled` /
   `is_strategy_entries_paused` 读的也是 `get_setting` —— 同表同层；
2. **写后必须能被读到**：用真实解析函数断言，而不是只测「写入成功」；
3. **旧写入口本批删掉**：`pages/15_strategy_control_center.py` 里那套 data_editor 保存
   已改为只读 + 跳转，全站只允许一个写入点；
4. **红区不放宽**：执行模式（SHADOW/PAPER/REAL）直通实盘门禁，本页只读 + 指路，
   不做任何编辑；租户解析失败必须**拒写**，绝不回落旧全局键。
"""

from __future__ import annotations

import ast
from pathlib import Path

from futu_quant.strategy_pause import is_strategy_entries_paused, set_strategy_entries_paused
from futu_quant.strategy_toggle import resolve_strategy_enabled, set_strategy_enabled
from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
CENTER = ROOT / "pages" / "44_strategy_center.py"
LEGACY = ROOT / "pages" / "15_strategy_control_center.py"
TENANT = "futu_real_master"
STRATEGY = "core_satellite"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 写后必须能被真实读取链读到
# ---------------------------------------------------------------------------


def test_enable_write_is_visible_to_resolver(tmp_path):
    store = TradingStore(tmp_path / "enable.db")

    set_strategy_enabled(store, STRATEGY, False, tenant_id=TENANT)
    assert resolve_strategy_enabled(store, STRATEGY, TENANT) is False

    set_strategy_enabled(store, STRATEGY, True, tenant_id=TENANT)
    assert resolve_strategy_enabled(store, STRATEGY, TENANT) is True, (
        "写完调度器读不到 —— 写入层与读取层不一致"
    )


def test_pause_write_is_visible_to_resolver(tmp_path):
    store = TradingStore(tmp_path / "pause.db")

    set_strategy_entries_paused(store, STRATEGY, True, tenant_id=TENANT)
    assert is_strategy_entries_paused(store, STRATEGY, TENANT) is True

    set_strategy_entries_paused(store, STRATEGY, False, tenant_id=TENANT)
    assert is_strategy_entries_paused(store, STRATEGY, TENANT) is False


def test_tenant_isolation_is_preserved(tmp_path):
    """写到 A 租户不得在 B 租户留下键（跨租户连坐是最贵的事故形态）。"""
    store = TradingStore(tmp_path / "tenant.db")
    other = "futu_paper_sandbox"

    set_strategy_enabled(store, STRATEGY, True, tenant_id=TENANT)

    assert resolve_strategy_enabled(store, STRATEGY, TENANT) is True
    # B 租户不应被写：解析回落 default(False)，且其租户键必须为空
    assert resolve_strategy_enabled(store, STRATEGY, other) is False
    assert store.get_setting(f"strategy_enabled:{other}:{STRATEGY}", "") == ""
    assert store.get_setting(f"strategy_enabled:{TENANT}:{STRATEGY}", "") == "true"


# ---------------------------------------------------------------------------
# 2. 页面层：唯一写入点 + 旧入口只读 + 红区不放宽
# ---------------------------------------------------------------------------


def test_center_page_is_the_editor():
    source = _source(CENTER)

    assert "set_strategy_enabled(" in source
    assert "set_strategy_entries_paused(" in source
    assert "render_admin_login()" in source, "改策略启停需要管理员登录"
    assert "STRATEGY_CONTROLS_UPDATED" in source, "必须留审计"
    assert "tenant_id" in source


def test_center_page_refuses_write_without_tenant():
    source = _source(CENTER)

    assert "不写" in source or "拒绝" in source, "租户解析失败必须显式拒写"


def test_center_page_does_not_touch_execution_mode_red_zone():
    """执行模式属红区：本页不得写 `strategy_execution_mode` 或 `execution_mode` 键。"""
    source = _source(CENTER)

    assert "strategy_execution_mode" not in source, "执行模式写入口不得进本页（红区）"
    assert 'set_setting("execution_mode"' not in source


def test_legacy_page_no_longer_writes_strategy_controls():
    source = _source(LEGACY)

    assert "set_strategy_enabled(" not in source, "旧页面仍在写策略启停 —— 出现第二个写入口"
    assert "set_strategy_entries_paused(" not in source, "旧页面仍在写暂停新开仓"
    assert "44_strategy_center.py" in source, "旧页面必须给跳转"


def test_only_one_page_writes_strategy_controls():
    offenders = []
    for path in sorted((ROOT / "pages").glob("*.py")):
        text = _source(path)
        if "set_strategy_enabled(" in text or "set_strategy_entries_paused(" in text:
            offenders.append(path.name)

    assert offenders == ["44_strategy_center.py"], offenders


def test_center_page_imports_resolve():
    from futu_quant.operational_readiness import _audit_page_imports

    tree = ast.parse(_source(CENTER), filename=str(CENTER))
    assert _audit_page_imports(ROOT, tree, CENTER.name) == []
