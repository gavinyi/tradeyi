"""执行模式解析 —— **全仓唯一口径（P1-6）**。

背景：切换执行模式和「读当前模式」原本散在 5 处，各自的阶梯还不一致
（有的漏读租户键），于是「页面显示的模式」和「实际下单用的模式」可能不同 ——
在实盘上这就是「以为已降级、其实还在真金下单」。

阶梯固定为：

    strategy_execution_mode:{tenant}:{strategy}   ← 租户策略键（最高）
      → strategy_execution_mode:{strategy}        ← 旧无前缀策略键
      → execution_mode                            ← 全局模式
      → 环境变量 EXECUTION_MODE
      → 空串（调用方自己决定默认值）

**不存在 `execution_mode:{tenant}` 这一层**：生产侧没有任何读者，新增它只会变成
「写了没人读」的半状态字段（`registered_fill_*` 就是这么来的）。

这里只解析「显式配置」，不做券商环境自动提升 —— 「实盘账户 + 白名单策略自动
REAL」那段属于下单判定，仍在 `ExecutionManager.submit_limit()` 里。
"""

from __future__ import annotations

import os

from .tenant import active_tenants


def strategy_execution_mode_override(store, tenant_id: str | None, strategy_id: str) -> str:
    """只取**逐策略显式覆盖**（租户键优先，其次旧无前缀键）；没有则返回空串。"""
    tenant = str(tenant_id or "")
    if tenant:
        tenant_value = str(
            store.get_setting(f"strategy_execution_mode:{tenant}:{strategy_id}", "") or ""
        ).strip().upper()
        if tenant_value:
            return tenant_value
    return str(
        store.get_setting(f"strategy_execution_mode:{strategy_id}", "") or ""
    ).strip().upper()


def resolve_configured_execution_mode(store, tenant_id: str | None, strategy_id: str) -> str:
    """显式配置的生效模式（逐策略 → 全局模式 → env）；全无配置返回空串。

    页面展示、审计留痕、校验降级判定**一律调这里**，禁止各自拼键。
    """
    override = strategy_execution_mode_override(store, tenant_id, strategy_id)
    if override:
        return override
    return str(
        store.get_setting("execution_mode", os.getenv("EXECUTION_MODE", "")) or ""
    ).strip().upper()


def downgrade_strategy_execution_mode(store, strategy_id: str) -> list[str]:
    """把策略压到 SHADOW：写**全部活跃租户**的租户键 + 旧全局键兜底。

    为什么必须写租户键：`submit_limit()` 的阶梯里租户键优先级最高，只写旧全局键
    会被已有租户键盖过 —— 那就是「自动降级写进去了，实盘照旧 REAL 下单」。
    解析不到策略归属租户时，宁可按全部活跃租户降级（多降不漏降）。
    """
    keys: list[str] = []
    for profile in active_tenants():
        key = f"strategy_execution_mode:{profile.tenant_id}:{strategy_id}"
        store.set_setting(key, "SHADOW")
        keys.append(key)
    legacy_key = f"strategy_execution_mode:{strategy_id}"
    store.set_setting(legacy_key, "SHADOW")
    keys.append(legacy_key)
    return keys


def restore_strategy_execution_mode(store, strategy_id: str, tenant_id: str) -> list[str]:
    """解除某租户的降级：**删除**该租户键与旧全局键（不是写 PAPER）。

    为什么不是写 PAPER：实盘账户上 `PAPER` 会被 `submit_limit()` 的白名单自动
    提升成 `REAL`，写 PAPER 既没恢复「未降级」，又制造「以为在 PAPER、其实在
    REAL」的假象。删掉覆盖键才是真正的「回到原配置」。
    """
    removed: list[str] = []
    tenant_key = f"strategy_execution_mode:{tenant_id}:{strategy_id}"
    if str(store.get_setting(tenant_key, "") or "").strip():
        store.delete_setting(tenant_key)
        removed.append(tenant_key)
    legacy_key = f"strategy_execution_mode:{strategy_id}"
    if str(store.get_setting(legacy_key, "") or "").strip().upper() == "SHADOW":
        store.delete_setting(legacy_key)
        removed.append(legacy_key)
    return removed
