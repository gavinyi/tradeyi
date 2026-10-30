"""策略启停开关的**唯一读写口径**（tenant 前缀 + 显示名/策略 ID 两种拼法）。

背景（2026-10-30 用户反馈「实盘开关好乱」）：
- 调度器 ``scripts/run_service.py::resolve_configured_strategies`` 的解析顺序是
  「本租户显示名键 → 本租户策略 ID 键 → 全局显示名键 → 全局策略 ID 键 → 环境默认」。
- 而「策略与资金控制」页面（pages/15）过去**只读写全局 ``strategy_enabled:{显示名}``**，
  租户键存在时这个全局键根本不参与调度 —— 界面显示「已启用」，调度器却按租户键
  跑另一套。同一类病在 P1-6（execution_mode）与 P1-9（暂停开仓）都踩过。
- 对账路径（reconciliation.py）为了避免同类漂移，写侧是**覆盖所有拼法变体**的。

本模块把这三处的口径收敛成两个函数：
- ``resolve_strategy_enabled`` 读：与调度器逐级一致，不引入第二种解析顺序。
- ``set_strategy_enabled`` 写：覆盖该作用域下所有会被读到的变体键。

**不要在页面里再手拼 strategy_enabled 键**，包括显示名/策略 ID 的选择。
"""
from __future__ import annotations

from typing import Any

from .strategy_plugins import BY_ID

BASE_KEY = "strategy_enabled"


def _names(strategy_id: str) -> tuple[str, ...]:
    """一个策略在配置键里可能出现的所有名字写法（显示名优先，与调度器一致）。"""
    plugin = BY_ID.get(strategy_id)
    names: list[str] = []
    if plugin and plugin.service_name:
        names.append(plugin.service_name)
    names.append(strategy_id)
    return tuple(dict.fromkeys(names))


def strategy_enable_keys(strategy_id: str, tenant_id: str | None = None) -> tuple[str, ...]:
    """该策略在某作用域下**所有**会被读取的键（写侧必须全覆盖）。

    顺序与读侧一致：显示名在前、策略 ID 在后；无租户时返回全局键。
    """
    prefix = f"{tenant_id}:" if tenant_id else ""
    return tuple(f"{BASE_KEY}:{prefix}{name}" for name in _names(strategy_id))


def resolve_strategy_enabled(
    store: Any,
    strategy_id: str,
    tenant_id: str | None = None,
    default: bool = False,
) -> bool:
    """解析策略是否启用：租户键优先，逐级回落全局键，最后用默认值。

    读序**必须**与 ``scripts/run_service.py::resolve_configured_strategies`` 相同：
    租户显示名 → 租户策略 ID → 全局显示名 → 全局策略 ID。
    先命中的非空值即为准（空字符串视为未配置）。
    """
    if tenant_id:
        for key in strategy_enable_keys(strategy_id, tenant_id):
            raw = str(store.get_setting(key, "") or "").strip().lower()
            if raw:
                return raw == "true"
    for key in strategy_enable_keys(strategy_id, None):
        raw = str(store.get_setting(key, "") or "").strip().lower()
        if raw:
            return raw == "true"
    return bool(default)


def set_strategy_enabled(
    store: Any,
    strategy_id: str,
    enabled: bool,
    tenant_id: str | None = None,
) -> tuple[str, ...]:
    """写入策略启停，覆盖本作用域下所有会被读到的变体键。

    为什么写全部变体而不只写一个：读侧「先命中的非空值即为准」，只要
    ``{tenant}:Core Satellite`` 还是 "true"，单独写 ``{tenant}:core_satellite``
    的 "false" 就永远读不到 —— 界面点关闭、调度器照旧跑（对账路径已踩过此坑）。

    返回实际写入的键，便于审计与测试断言。
    """
    value = "true" if enabled else "false"
    keys = strategy_enable_keys(strategy_id, tenant_id)
    for key in keys:
        store.set_setting(key, value)
    return keys
