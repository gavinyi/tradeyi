"""租户级波动触发覆盖。

REAL 账户只允许把阈值调严；SIMULATE 可放宽，但必须留下审计。
覆盖值是绝对阈值，基础值仍来自自适应 ``volatility_threshold_profile``。
"""

from __future__ import annotations

from .tenant import resolve_tenant, resolve_tenant_config_key

ZSCORE_KEY = "trigger_zscore_override"
MOVE_KEY = "trigger_move_override"


def _number(store, key: str, tenant_id: str) -> float | None:
    raw = str(store.get_setting(resolve_tenant_config_key(tenant_id, key), "") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = -1.0
    valid = (0.1 <= value <= 10.0) if key == ZSCORE_KEY else (0.001 <= value <= 0.20)
    if not valid:
        store.operational_event(
            "volatility_config", "VOLATILITY_TRIGGER_OVERRIDE_INVALID", "ERROR",
            {"tenant_id": tenant_id, "setting": key, "value": raw},
        )
        return None
    return value


def resolve_volatility_trigger_profile(store, tenant_id: str, base: dict[str, float]) -> dict[str, float]:
    """返回租户最终阈值；未知租户 fail-closed，REAL 放宽请求被拒绝。"""
    profile = resolve_tenant(tenant_id, strict=True)
    result = dict(base)
    requested = {
        "entry_zscore": _number(store, ZSCORE_KEY, tenant_id),
        "minimum_underlying_move": _number(store, MOVE_KEY, tenant_id),
    }
    for field, value in requested.items():
        if value is None:
            continue
        original = float(result[field])
        if profile.trading_env == "REAL" and value < original:
            store.operational_event(
                "volatility_config", "REAL_TRIGGER_OVERRIDE_LOOSEN_REJECTED", "ERROR",
                {
                    "tenant_id": tenant_id, "field": field, "requested": value,
                    "base": original, "effective": original,
                },
            )
            continue
        result[field] = value
        if profile.trading_env == "SIMULATE" and value < original:
            store.audit(
                "PAPER_TRIGGER_OVERRIDE_RELAXED",
                {"tenant_id": tenant_id, "field": field, "base": original, "effective": value},
                actor="service:volatility_scanner",
            )
    return result
