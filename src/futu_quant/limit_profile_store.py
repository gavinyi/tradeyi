"""限额配置中心发布通道；禁止页面或业务模块直写配置表。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .limit_profile import LimitChain, LimitProfile, classify_changes, resolve_limit_chain, validate_profile

PROFILE_KEY_PREFIX = "limit_profile:"


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


def load_limit_profile(store, tenant_id: str) -> ProfileResolution:
    """从配置中心运行时权威表读取；绝不从旧 system_settings 生效回落。"""
    runtime = store.get_runtime_config(profile_key(tenant_id))
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


def publish_limit_profile(store, tenant_id: str, profile: LimitProfile, actor: str, reason: str, *, approval_fingerprint: str = "", old_chain: LimitChain | None = None, equity: float | None = None, confirm: bool = False) -> dict:
    if not reason.strip():
        raise ValueError("变更理由必填")
    if equity is None:
        raise ValueError("权益缺失，拒绝保存")
    ok, error = validate_profile(profile, equity)
    if not ok:
        raise ValueError(error)
    new_chain = resolve_limit_chain(None, tenant_id, equity, profile)
    if old_chain is not None:
        changes = classify_changes(old_chain, new_chain)
        loosened = any(v == "loosen" for v in changes.values())
        if loosened and (not approval_fingerprint or not confirm):
            raise PermissionError("变松必须二次确认并提供审批指纹")
    key = profile_key(tenant_id)
    now = datetime.now(timezone.utc).isoformat()
    value = {**profile.__dict__, "updated_at": now, "actor": actor, "reason": reason, "approval_fingerprint": approval_fingerprint}
    release_id = store.create_config_release(key, value, actor, reason)
    store.publish_config_release_with_audit(
        release_id, actor, "LIMIT_PROFILE_PUBLISHED",
        {"tenant_id": tenant_id, "before": old_chain.as_dict() if old_chain else None,
         "after": new_chain.as_dict(), "reason": reason,
         "approval_fingerprint": approval_fingerprint},
    )
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
    return {"release_id": release_id, "key": key, "chain": new_chain.as_dict(), "updated_at": now}
