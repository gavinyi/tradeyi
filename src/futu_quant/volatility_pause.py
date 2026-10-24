"""波动率策略「暂停新开仓」的**唯一生效口径**（P1-9）。

为什么单独成模块：这个判定原本写在 `scripts/run_gamma_strategy.py` 里，
页面想展示「当前到底暂停没有」就只能自己拼键 —— 而页面拼键一旦和生效口径
不一致，就会出现「页面显示放行、实际已暂停」或反过来。生效值必须只有一个出口。

口径：
- **租户级优先**：`volatility_new_entries_paused:{tenant_id}`；
- **全局键兜底**：`volatility_new_entries_paused`（兼容历史设置，改键名不会让
  已经停掉的实盘悄悄恢复）；
- **写侧只写租户键**，且拿不到租户时**拒绝写**：写全局会把其他租户一起停掉，
  那正是 10-23 修掉的那个漏洞。
"""

from __future__ import annotations

from .store import TradingStore
from .tenant import tenant_id_of_broker

LEGACY_KEY = "volatility_new_entries_paused"
TENANT_KEY_TEMPLATE = "volatility_new_entries_paused:{tenant_id}"


def volatility_new_entries_paused(store: TradingStore, broker=None) -> bool:
    """波动率期权是否暂停**新开仓**（退出管理不受影响）。"""
    tenant_id = ""
    if broker is not None:
        # P0-9：走共享入口 tenant_of_broker（显式 → 账户反解），不再裸 getattr。
        tenant_id = tenant_id_of_broker(broker) or ""
    return volatility_new_entries_paused_for_tenant(store, tenant_id)


def volatility_new_entries_paused_for_tenant(
    store: TradingStore, tenant_id: str | None,
) -> bool:
    """按显式租户判定（页面/监控用；调用方自己拿租户，别再从 broker 反解）。"""
    tenant = str(tenant_id or "").strip()
    if tenant:
        value = store.get_setting(TENANT_KEY_TEMPLATE.format(tenant_id=tenant), "")
        if value.strip():
            return value.strip().lower() == "true"
    return store.get_setting(LEGACY_KEY, "false").strip().lower() == "true"


def set_volatility_new_entries_paused(
    store: TradingStore, tenant_id: str | None, paused: bool,
) -> str:
    """写「暂停新开仓」：只写租户键，返回写入的键。

    拿不到租户时**抛错而不是回落全局**：回落全局会把所有租户一起停掉，
    而调用方（页面）此时其实无法证明用户想停的是哪一个租户。
    """
    tenant = str(tenant_id or "").strip()
    if not tenant:
        raise ValueError(
            "无法解析当前租户，拒绝写入暂停键 —— 写全局会把其他租户一起停掉"
        )
    key = TENANT_KEY_TEMPLATE.format(tenant_id=tenant)
    store.set_setting(key, "true" if paused else "false")
    return key
