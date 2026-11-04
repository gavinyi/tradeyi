"""限额配置中心发布通道；禁止页面或业务模块直写配置表。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .limit_profile import LimitChain, LimitProfile, classify_changes, resolve_limit_chain, validate_profile

PROFILE_KEY_PREFIX = "limit_profile:"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileResolution:
    tenant_id: str
    profile: LimitProfile
    source: str
    updated_at: str = ""
    actor: str = ""
    reason: str = ""


def profile_key(tenant_id: str) -> str:
    if not tenant_id or ":" in tenant_id:
        raise ValueError("非法租户标识")
    return f"{PROFILE_KEY_PREFIX}{tenant_id}"


def _note_unsupported_store(store, tenant_id: str) -> None:
    """store 不具备 runtime_config 读能力时的可查询留痕（绝不静默）。"""
    payload = {
        "tenant_id": tenant_id,
        "store": type(store).__name__,
        "reason": "store 未实现 get_runtime_config",
    }
    try:
        store.operational_event(
            "limit_profile", "LIMIT_PROFILE_STORE_UNSUPPORTED", "WARNING", payload
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断选标的
        pass
    logger.warning("LIMIT_PROFILE_STORE_UNSUPPORTED %s", payload)


def load_limit_profile(store, tenant_id: str) -> ProfileResolution:
    """从配置中心运行时权威表读取；绝不从旧 system_settings 生效回落。

    `store` 不支持 `runtime_config` 读（测试替身 / 受限实现）时按**未启用派生**
    处理并走旧键：生产 `TradingStore` 一定有该方法，所以线上行为不变，但选标的
    链路不会因为一个读不到配置的替身被打断（全量回归 2026-11-04 真回归）。
    """
    reader = getattr(store, "get_runtime_config", None)
    if not callable(reader):
        _note_unsupported_store(store, tenant_id)
        return ProfileResolution(tenant_id, LimitProfile(), "legacy:read_only")
    runtime = reader(profile_key(tenant_id))
    value = runtime[0] if runtime else None
    if value:
        if not isinstance(value, dict):
            raise ValueError("limit profile 必须是对象")
        allowed = {k: value[k] for k in LimitProfile.__dataclass_fields__ if k in value}
        return ProfileResolution(
            tenant_id, LimitProfile(**allowed), "derived:limit_profile",
            value.get("updated_at", ""), value.get("actor", ""), value.get("reason", ""),
        )
    return ProfileResolution(tenant_id, LimitProfile(), "legacy:read_only")


def _profile_fields(value: dict) -> dict:
    return {key: value.get(key) for key in LimitProfile.__dataclass_fields__}


def _legacy_chain_from_limits(
    tenant_id: str, equity: float, legacy_limits: Any
) -> LimitChain | None:
    """用**旧键实际生效金额**构造变松基线。

    页面传入的派生链是「新公式预览值」，不是当前实际生效值；拿它当基线会把
    `legacy → derived` 的首次启用（模拟盘 `$2,500 → $50,000`，20 倍变松）判成
    tighter/unchanged，从而绕过二次确认与审批指纹。
    """
    if legacy_limits is None:
        return None
    try:
        per_order = float(getattr(legacy_limits, "max_order_notional"))
        daily_entries = float(getattr(legacy_limits, "max_daily_notional"))
        daily_loss = float(getattr(legacy_limits, "max_daily_loss"))
    except (AttributeError, TypeError, ValueError):
        return None
    source = str(getattr(legacy_limits, "source", "") or "legacy:read_only")
    return LimitChain(
        tenant_id=tenant_id,
        equity=float(equity),
        per_order=per_order,
        batch=per_order,
        daily_entries=daily_entries,
        daily_loss=daily_loss,
        capacity_line=None,
        source=source,
    )


def resolve_baseline_chain(
    store, tenant_id: str, equity: float, legacy_limits: Any = None
) -> LimitChain:
    """变松判定基线：已启用取服务端旧 profile 派生值，首启用取旧键实际生效金额。"""
    existing = load_limit_profile(store, tenant_id)
    if existing.source == "derived:limit_profile":
        return resolve_limit_chain(store, tenant_id, equity, existing.profile)
    baseline = _legacy_chain_from_limits(tenant_id, equity, legacy_limits)
    if baseline is None:
        raise ValueError(
            "首启用派生层必须提供旧键实际生效金额作为基线，否则无法判定变松，拒绝发布"
        )
    return baseline


def publish_limit_profile(
    store,
    tenant_id: str,
    profile: LimitProfile,
    actor: str,
    reason: str,
    *,
    approval_fingerprint: str = "",
    legacy_limits: Any = None,
    equity: float | None = None,
    confirm: bool = False,
) -> dict:
    if not reason.strip():
        raise ValueError("变更理由必填")
    if equity is None:
        raise ValueError("权益缺失，拒绝保存")
    ok, error = validate_profile(profile, equity)
    if not ok:
        raise ValueError(error)
    # 基线一律由服务端解析，**不接受调用方传入的预览链**。
    old_chain = resolve_baseline_chain(store, tenant_id, equity, legacy_limits)
    new_chain = resolve_limit_chain(None, tenant_id, equity, profile)
    changes = classify_changes(old_chain, new_chain)
    loosened = any(v == "loosen" for v in changes.values())
    if loosened and (not approval_fingerprint or not confirm):
        raise PermissionError("变松必须二次确认并提供审批指纹")
    key = profile_key(tenant_id)
    now = datetime.now(timezone.utc).isoformat()
    value = {**profile.__dict__, "updated_at": now, "actor": actor, "reason": reason, "approval_fingerprint": approval_fingerprint}
    release_id = store.create_config_release(key, value, actor, reason)
    try:
        store.publish_config_release_with_audit(
            release_id, actor, "LIMIT_PROFILE_PUBLISHED",
            {"tenant_id": tenant_id,
             # `before`/`after` 会被发布通道覆盖成 runtime_config 原文（store.py:2624-2625），
             # 因此另存生效金额快照，保证事后可从审计复现「当时实际放行多少」。
             "effective_before": old_chain.as_dict(),
             "effective_after": new_chain.as_dict(),
             "changes": changes,
             "reason": reason,
             "approval_fingerprint": approval_fingerprint},
        )
    except Exception:
        # 发布事务失败：草稿必须就地作废，否则留下孤儿 DRAFT（Reviewer 2026-11-04 次要 1）。
        try:
            store.reject_config_release(release_id, actor, reason=f"发布失败自动作废：{reason}")
        except Exception:  # noqa: BLE001 - 作废失败不得掩盖原始发布异常
            pass
        raise

    # 发布后必须从 runtime_config 回读并逐字段比对；不一致立即走统一回滚通道。
    persisted = store.get_runtime_config(key)
    expected = _profile_fields(value)
    actual = persisted[0] if persisted else None
    if not isinstance(actual, dict) or _profile_fields(actual) != expected:
        try:
            store.rollback_config_release_with_audit(
                release_id, actor, reason="发布后回读不一致，自动回滚",
                audit_event_type="LIMIT_PROFILE_ROLLBACK",
                audit_payload={"tenant_id": tenant_id, "expected": expected, "actual": actual},
            )
        except Exception as rollback_error:
            raise RuntimeError(f"限额发布回读不一致，且自动回滚失败: {rollback_error}") from rollback_error
        raise RuntimeError("限额发布回读不一致，已自动回滚")
    return {
        "release_id": release_id,
        "key": key,
        "chain": new_chain.as_dict(),
        "before_chain": old_chain.as_dict(),
        "changes": changes,
        "updated_at": now,
    }
