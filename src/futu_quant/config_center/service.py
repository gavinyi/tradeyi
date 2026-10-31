"""统一配置中心 Phase 3 Batch 3 低风险配置受控编辑与审计服务。"""

from __future__ import annotations

import json
from typing import Any

from futu_quant.alerts import is_never_mutable, parse_mute_list
from futu_quant.config_center.registry import GLOBAL_REGISTRY, validate_instance_key
from futu_quant.store import TradingStore, validate_actor, AuditActorError

# Batch 3 严格准入白名单 (Reviewer 与 Chief 裁定锁定，仅允许 alert_mute_list，其余全部 C 档锁定)
BATCH_3_ALLOWED_EDIT_KEYS = {"alert_mute_list"}


def _normalize_diff_value(val: Any) -> Any:
    """归一化配置值用于空 diff 判定，避免元素顺序不同误判为实质变更。"""
    if isinstance(val, (list, tuple, set)):
        return sorted(val)
    return val


def edit_low_risk_config(
    store: TradingStore,
    config_key: str,
    new_value: Any,
    actor: str,
    reason: str,
) -> dict:
    """低风险配置受控编辑与草稿发布。

    五重硬门禁：
    1. 权限门禁：必须经过 validate_actor 校验且归属 streamlit_admin，未登录或非法格式直接拒绝；
    2. 准入白名单门禁：必须在 BATCH_3_ALLOWED_EDIT_KEYS 白名单内；严禁修改 CRITICAL、requires_confirmation 项；
    3. 原因必填门禁：reason 不能为空；
    4. 内容合法性门禁：若是 alert_mute_list，直接调用 is_never_mutable 与 parse_mute_list，含不可静音项或通配符直接报错；
    5. 空 diff 门禁：新旧值必须有实质差异 (归一化比对)，严禁空提交；
    6. 原子性保证：发布后落审计日志，审计失败立即自动回滚，确保绝不留未审计变更。
    """
    try:
        actor_class = validate_actor(actor)
    except AuditActorError as e:
        raise PermissionError(f"未授权或非法操作员格式: {e}") from e

    if actor_class != "streamlit_admin":
        raise PermissionError(f"非管理员身份，禁止执行配置变更 (actor: '{actor}')")

    if not reason or not reason.strip():
        raise ValueError("配置变更原因 (reason) 必填，禁止无原因提交变更！")

    spec = GLOBAL_REGISTRY.get(config_key)
    if not spec:
        raise ValueError(f"配置键 '{config_key}' 未在统一配置中心登记！")

    if spec.risk_level == "CRITICAL":
        raise ValueError(f"配置项 '{config_key}' 为 CRITICAL 极高风险项，处于硬锁定状态，禁止通过低风险编辑通道修改！")

    if spec.requires_confirmation:
        raise ValueError(f"配置项 '{config_key}' 标记为 requires_confirmation，禁止通过低风险编辑通道直接修改！")

    if config_key not in BATCH_3_ALLOWED_EDIT_KEYS:
        raise ValueError(f"配置项 '{config_key}' 不在 Batch 3 准入白名单中 (仅允许: {sorted(BATCH_3_ALLOWED_EDIT_KEYS)})！")

    # 业务内容强校验 (写入侧直接调用 is_never_mutable 严格门禁)
    if config_key == "alert_mute_list":
        items = new_value if isinstance(new_value, (list, tuple, set)) else [new_value]
        accepted, rejected = parse_mute_list(list(items))
        if rejected:
            raise ValueError(f"静音清单包含通配符或非法字符，拒绝写入: {rejected}")
        never_mute_hits = [it for it in accepted if is_never_mutable(it)]
        if never_mute_hits:
            raise ValueError(
                f"静音清单命中系统不可静音硬门禁 (包含 RECONCILIATION_/急停/风控等)，禁止写入: {never_mute_hits}"
            )
        new_value = sorted(accepted)

    # 读取旧值与版本
    current = store.get_runtime_config(config_key)
    before_value, base_version = current if current else (None, 0)

    # 空 diff 门禁 (归一化)
    if _normalize_diff_value(before_value) == _normalize_diff_value(new_value):
        raise ValueError("配置值前后无变化 (空 diff)，禁止提交冗余变更！")

    # 写入前二次验证实例键
    validate_instance_key(config_key)

    # 走标准的草稿发布通道
    release_id = store.create_config_release(config_key, new_value, actor, reason)
    store.publish_config_release(release_id, actor)

    # 发布与审计原子化保障：审计日志写入失败则强制自动回滚
    try:
        store.audit(
            "CONFIG_LOW_RISK_UPDATED",
            {
                "config_key": config_key,
                "before": before_value,
                "after": new_value,
                "base_version": base_version,
                "release_id": release_id,
                "actor": actor,
                "reason": reason,
            },
            strategy_id="config_center",
            actor=actor,
        )
    except Exception as exc:
        store.set_runtime_config(config_key, before_value, "automatic_audit_failure_rollback")
        raise RuntimeError(f"配置发布后写入审计事件失败，已强制自动回滚至前序版本！根因: {exc}") from exc

    return {
        "status": "PUBLISHED",
        "release_id": release_id,
        "config_key": config_key,
        "before": before_value,
        "after": new_value,
        "base_version": base_version,
    }


def rollback_low_risk_config(
    store: TradingStore,
    release_id: int,
    actor: str,
    reason: str,
) -> dict:
    """低风险配置一键回滚。

    安全门禁：
    1. 权限门禁：必须经过 validate_actor 校验且归属 streamlit_admin；
    2. 目标存在性门禁：release_id 必须存在且状态为 PUBLISHED；
    3. 白名单与风险门禁：回滚必须复用和编辑完全相同的准入白名单，禁止回滚 CRITICAL 或 requires_confirmation 项；
    4. 内容再校验门禁：回滚恢复的值必须重新走 parse_mute_list / is_never_mutable 内容校验，杜绝还原历史脏数据；
    5. 回滚前二次调用 validate_instance_key 校验；
    6. 强制记录 CONFIG_LOW_RISK_ROLLED_BACK 审计事件。
    """
    try:
        actor_class = validate_actor(actor)
    except AuditActorError as e:
        raise PermissionError(f"未授权或非法操作员格式: {e}") from e

    if actor_class != "streamlit_admin":
        raise PermissionError(f"非管理员身份，禁止执行配置回滚 (actor: '{actor}')")

    if not reason or not reason.strip():
        raise ValueError("配置回滚原因必填！")

    with store.connection() as conn:
        row = conn.execute("SELECT * FROM config_releases WHERE id=?", (release_id,)).fetchone()
        if not row:
            raise ValueError(f"目标发布版本 (release_id={release_id}) 不存在，无法回滚！")
        release = dict(row)

    if release["status"] != "PUBLISHED":
        raise ValueError(f"目标发布版本当前状态为 '{release['status']}'，只有 PUBLISHED 版本可执行回滚！")

    config_key = release["config_key"]
    if config_key not in BATCH_3_ALLOWED_EDIT_KEYS:
        raise ValueError(f"配置项 '{config_key}' 不在 Batch 3 准入白名单中，禁止通过本通道回滚！")

    spec = GLOBAL_REGISTRY.get(config_key)
    if not spec:
        raise ValueError(f"配置键 '{config_key}' 未在统一配置中心登记！")

    if spec.risk_level == "CRITICAL":
        raise ValueError(f"禁止回滚到 CRITICAL 极高风险项 ({config_key})！")

    if spec.requires_confirmation:
        raise ValueError(f"配置项 '{config_key}' 标记为 requires_confirmation，禁止通过低风险通道回滚！")

    # 回滚前二次校验
    validate_instance_key(config_key)

    # 提取 diff 中的原始值
    diff = json.loads(release["diff_json"]) if isinstance(release["diff_json"], str) else (release["diff_json"] or {})
    target_value = diff.get("before")

    # 回滚目标值内容强校验 (防历史脏数据恢复)
    if config_key == "alert_mute_list" and target_value is not None:
        items = target_value if isinstance(target_value, (list, tuple, set)) else [target_value]
        accepted, rejected = parse_mute_list(list(items))
        if rejected:
            raise ValueError(f"回滚目标历史值包含通配符或非法字符，拒绝回滚: {rejected}")
        never_mute_hits = [it for it in accepted if is_never_mutable(it)]
        if never_mute_hits:
            raise ValueError(
                f"回滚目标历史值命中系统不可静音硬门禁 (包含 RECONCILIATION_/急停/风控等)，禁止回滚装入静默缺口: {never_mute_hits}"
            )
        target_value = sorted(accepted)

    rollback_release_id = store.rollback_config_release(release_id, actor)

    # 强制记录审计日志
    try:
        store.audit(
            "CONFIG_LOW_RISK_ROLLED_BACK",
            {
                "original_release_id": release_id,
                "rollback_release_id": rollback_release_id,
                "config_key": config_key,
                "restored_value": target_value,
                "actor": actor,
                "reason": reason,
            },
            strategy_id="config_center",
            actor=actor,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("配置回滚后写入审计日志失败: %s", exc)

    return {
        "status": "ROLLED_BACK",
        "original_release_id": release_id,
        "rollback_release_id": rollback_release_id,
        "config_key": config_key,
        "restored_value": target_value,
    }
