"""单策略粒度「暂停新开仓 (Reduce-Only)」单源解析与配置模块。

根据架构设计方案 `docs/per_strategy_pause_entries_design.md`：
1. 拦截范围**严谨锁定在 `side == BUY`**（开仓/增仓）；
2. **`side == SELL`（止盈、硬止损、临期处置、保护性平仓）绝对豁免、无条件放行**；
3. 多租户命名空间严格隔离，并提供向下兼容的历史键回退链。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "strategy_entries_paused"
VOLATILITY_LEGACY_PREFIX = "volatility_new_entries_paused"


def strategy_entries_paused_key(strategy_id: str, tenant_id: str = "") -> str:
    """返回标准租户策略暂停新开仓配置键。"""
    s_id = strategy_id.strip()
    t_id = tenant_id.strip() if tenant_id else ""
    if t_id:
        return f"{PREFIX}:{t_id}:{s_id}"
    return f"{PREFIX}:{s_id}"


def is_strategy_entries_paused(store: Any, strategy_id: str, tenant_id: str = "") -> bool:
    """解析指定租户下的指定策略是否开启「暂停新开仓 (Reduce-Only)」。

    优先级与兼容回退链：
    1. 租户标准键: strategy_entries_paused:{tenant_id}:{strategy_id}
    2. 波动率策略历史租户键兼容: volatility_new_entries_paused:{tenant_id} (仅限 volatility_long_gamma)
    3. 全局策略键兼容: strategy_entries_paused:{strategy_id}
    4. 波动率策略历史全局键兼容: volatility_new_entries_paused (仅限 volatility_long_gamma)
    5. 默认值: False (放行)
    """
    if store is None:
        return False

    s_id = str(strategy_id or "").strip()
    t_id = str(tenant_id or "").strip()
    is_volatility = s_id.lower() in ("volatility_long_gamma", "volatility long gamma")

    candidates: list[str] = []
    if t_id:
        candidates.append(f"{PREFIX}:{t_id}:{s_id}")
        if is_volatility:
            candidates.append(f"{VOLATILITY_LEGACY_PREFIX}:{t_id}")

    candidates.append(f"{PREFIX}:{s_id}")
    if is_volatility:
        candidates.append(VOLATILITY_LEGACY_PREFIX)

    for key in candidates:
        try:
            val = store.get_setting(key, "")
            if val is not None and str(val).strip():
                return str(val).strip().lower() == "true"
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取策略暂停开仓键 %s 失败: %s", key, exc)
            continue

    return False


def set_strategy_entries_paused(
    store: Any,
    strategy_id: str,
    paused: bool,
    tenant_id: str = "",
    actor: str = "streamlit_admin:panel",
) -> None:
    """设置指定租户下的指定策略「暂停新开仓」状态并记录审计。"""
    s_id = str(strategy_id or "").strip()
    t_id = str(tenant_id or "").strip()
    key = strategy_entries_paused_key(s_id, t_id)
    val_str = "true" if paused else "false"

    store.set_setting(key, val_str)
    try:
        store.audit(
            "STRATEGY_ENTRIES_PAUSED_CHANGED",
            {
                "strategy_id": s_id,
                "tenant_id": t_id,
                "entries_paused": paused,
                "setting_key": key,
            },
            strategy_id=s_id,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录策略暂停开仓审计失败: %s", exc)
