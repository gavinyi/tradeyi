"""策略启停开关读写口径测试（strategy_toggle）。

回归背景（2026-10-30）：「策略与资金控制」页面只读写全局
``strategy_enabled:{显示名}``，而调度器先查**本租户**显示名键、再查租户策略 ID 键，
租户键存在时全局键根本不参与 → 界面显示与真实调度长期不一致。
"""
from __future__ import annotations

from pathlib import Path

from futu_quant.store import TradingStore
from futu_quant.strategy_toggle import (
    resolve_strategy_enabled,
    set_strategy_enabled,
    strategy_enable_keys,
)


def _store(tmp_path: Path) -> TradingStore:
    return TradingStore(str(tmp_path / "toggle.db"))


def test_keys_cover_display_name_and_strategy_id():
    """一个策略至少要覆盖显示名与策略 ID 两种拼法（调度器两种都认）。"""
    keys = strategy_enable_keys("core_satellite", "futu_real_master")
    assert keys == (
        "strategy_enabled:futu_real_master:Core Satellite",
        "strategy_enabled:futu_real_master:core_satellite",
    )
    assert strategy_enable_keys("core_satellite") == (
        "strategy_enabled:Core Satellite",
        "strategy_enabled:core_satellite",
    )


def test_tenant_key_wins_over_global(tmp_path):
    """租户键存在时以租户为准，全局键不得反过来覆盖。"""
    store = _store(tmp_path)
    store.set_setting("strategy_enabled:core_satellite", "false")
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "true")
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master") is True
    # 没有租户键的租户回落到全局
    assert resolve_strategy_enabled(store, "core_satellite", "futu_paper_sandbox") is False


def test_display_name_key_wins_over_strategy_id_key(tmp_path):
    """读序必须与调度器一致：显示名键先命中即为准。"""
    store = _store(tmp_path)
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "true")
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "false")
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master") is True


def test_write_covers_every_variant_so_ui_toggle_actually_applies(tmp_path):
    """写侧必须覆盖所有变体：否则显示名键残留 true 会让「关闭」永远读不到。"""
    store = _store(tmp_path)
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "true")
    written = set_strategy_enabled(store, "core_satellite", False, tenant_id="futu_real_master")
    assert "strategy_enabled:futu_real_master:Core Satellite" in written
    assert "strategy_enabled:futu_real_master:core_satellite" in written
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master") is False

    set_strategy_enabled(store, "core_satellite", True, tenant_id="futu_real_master")
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master") is True


def test_unconfigured_falls_back_to_default(tmp_path):
    store = _store(tmp_path)
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master", default=False) is False
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master", default=True) is True


def test_empty_value_is_treated_as_unset(tmp_path):
    """空字符串视为未配置，继续向下回落（与调度器「先取到非空即止」一致）。"""
    store = _store(tmp_path)
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "")
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "true")
    assert resolve_strategy_enabled(store, "core_satellite", "futu_real_master") is True
