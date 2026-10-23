"""租户启用键的两种历史写法都必须被调度器识别。

背景（生产实测）：对账的 disable/recover 路径写的是**小写策略 ID** 形式
（``strategy_enabled:futu_real_master:core_satellite``），而调度器按
``service_name`` 查（``strategy_enabled:futu_real_master:Core Satellite``）
→ 查空 → 静默回落到全局 ``strategy_enabled:Core Satellite=false``
→ **租户的启用声明被完全忽略**，实盘 core_satellite 永远不会被调度。
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_service import resolve_configured_strategies  # noqa: E402

from futu_quant.store import TradingStore  # noqa: E402


def _store(tmp_path: Path, settings: dict[str, str]) -> TradingStore:
    store = TradingStore(tmp_path / "enable.db")
    for key, value in settings.items():
        store.set_setting(key, value)
    return store


def test_tenant_key_with_strategy_id_form_is_honoured(tmp_path):
    """契约 1：小写策略 ID 形式的租户键必须生效（对账路径写的就是这种）。"""
    store = _store(tmp_path, {
        "strategy_enabled:futu_real_master:core_satellite": "true",
        "strategy_enabled:Core Satellite": "false",      # 全局为 false，不得反压租户
    })
    configured = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" in configured, (
        "租户键（小写策略 ID 形式）被忽略，回落到了全局 false → 实盘 core_satellite 不会被调度"
    )


def test_tenant_key_with_display_name_form_is_honoured(tmp_path):
    """契约 2：显示名形式的租户键同样必须生效（历史写法）。"""
    store = _store(tmp_path, {
        "strategy_enabled:futu_real_master:Core Satellite": "true",
        "strategy_enabled:Core Satellite": "false",
    })
    configured = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" in configured


def test_tenant_key_overrides_global(tmp_path):
    """契约 3：租户键存在时必须优先于全局键（租户隔离的核心语义）。"""
    store = _store(tmp_path, {
        "strategy_enabled:futu_real_master:core_satellite": "false",
        "strategy_enabled:Core Satellite": "true",       # 全局为 true
    })
    configured = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" not in configured, "租户键显式 false 被全局 true 覆盖，租户隔离失效"


def test_missing_tenant_key_falls_back_to_global(tmp_path):
    """契约 4：两种租户写法都缺失时才回落全局键。"""
    store = _store(tmp_path, {
        "strategy_enabled:Core Satellite": "true",
    })
    configured = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" in configured


def test_tenants_do_not_leak_into_each_other(tmp_path):
    """契约 5：实盘启用不得让模拟也启用，反之亦然。"""
    store = _store(tmp_path, {
        "strategy_enabled:futu_real_master:core_satellite": "true",
        "strategy_enabled:futu_paper_sandbox:core_satellite": "false",
        "strategy_enabled:Core Satellite": "false",
    })
    real = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    paper = resolve_configured_strategies(store, "futu_paper_sandbox", {"Core Satellite"})
    assert "Core Satellite" in real
    assert "Core Satellite" not in paper, "模拟租户被实盘的启用声明连坐"
