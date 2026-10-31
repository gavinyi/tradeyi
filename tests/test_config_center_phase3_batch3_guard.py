"""配置中心 Phase 3 Batch 3 低风险配置受控编辑与审计守卫。

严格验证 Reviewer 验收门槛：
1. 未登录修改必红 (抛 PermissionError)；
2. 改 CRITICAL 锁定项必红 (抛 ValueError)；
3. 改 requires_confirmation 项必红 (抛 ValueError)；
4. 空 diff 提交必红 (抛 ValueError)；
5. 回滚到非法/不存在版本必红 (抛 ValueError)；
6. 写入侧硬门禁：不可静音事件 (如 REAL_TRADE_REJECTED、RECONCILIATION_ 前缀) 与通配符必红；
7. 正向全链路：Diff 展示、原因必填、audit_events 完整载荷留痕 (actor/reason/before/after/base_version) 及一键回滚。
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from futu_quant.config_center import edit_low_risk_config, rollback_low_risk_config
from futu_quant.store import TradingStore


def test_edit_low_risk_config_requires_admin_login(tmp_path):
    """断言 1：未登录或非法 actor 试图编辑，必须抛出 PermissionError。"""
    store = TradingStore(tmp_path / "test_auth.db")
    with pytest.raises(PermissionError, match="未授权或未登录管理员"):
        edit_low_risk_config(
            store, "alert_mute_list", ["ORDER_FAILED"], actor="anonymous", reason="try hack",
        )
    with pytest.raises(PermissionError, match="未授权或未登录管理员"):
        edit_low_risk_config(
            store, "alert_mute_list", ["ORDER_FAILED"], actor="", reason="try hack",
        )


def test_edit_low_risk_config_rejects_critical_specs(tmp_path):
    """断言 2：严禁通过低风险编辑通道修改 CRITICAL 极高风险项。"""
    store = TradingStore(tmp_path / "test_critical.db")
    actor = "streamlit_admin:shared_admin_credential"
    with pytest.raises(ValueError, match="CRITICAL 极高风险项"):
        edit_low_risk_config(
            store, "execution_mode", "REAL", actor=actor, reason="try edit critical",
        )


def test_edit_low_risk_config_rejects_requires_confirmation_specs(tmp_path):
    """断言 3：严禁通过低风险编辑通道修改标记为 requires_confirmation 的配置项。"""
    store = TradingStore(tmp_path / "test_confirm.db")
    actor = "streamlit_admin:shared_admin_credential"
    with pytest.raises(ValueError, match="requires_confirmation"):
        edit_low_risk_config(
            store, "VOLATILITY_CAMPAIGNS", [], actor=actor, reason="try edit confirm item",
        )


def test_edit_low_risk_config_rejects_empty_diff(tmp_path):
    """断言 4：前后无变化 (空 diff) 时禁止提交冗余变更。"""
    store = TradingStore(tmp_path / "test_empty_diff.db")
    actor = "streamlit_admin:shared_admin_credential"

    # 初始化配置为 ["ORDER_TIMEOUT"]
    store.create_config_release("alert_mute_list", ["ORDER_TIMEOUT"], actor, "init")
    rel_id = store.publish_config_release(1, actor)
    assert rel_id > 0

    # 再次提交相同值，必红
    with pytest.raises(ValueError, match="空 diff"):
        edit_low_risk_config(
            store, "alert_mute_list", ["ORDER_TIMEOUT"], actor=actor, reason="duplicate submit",
        )


def test_edit_low_risk_config_requires_non_empty_reason(tmp_path):
    """断言 4.2：变更原因必填，禁止无原因提交。"""
    store = TradingStore(tmp_path / "test_reason.db")
    actor = "streamlit_admin:shared_admin_credential"
    with pytest.raises(ValueError, match="变更原因 .* 必填"):
        edit_low_risk_config(
            store, "alert_mute_list", ["ORDER_FAILED"], actor=actor, reason="",
        )


def test_edit_low_risk_config_enforces_never_mutable_and_wildcard_rejection(tmp_path):
    """断言 6：写入侧直接调用 is_never_mutable 严格拦截不可静音项与通配符。"""
    store = TradingStore(tmp_path / "test_mute_rules.db")
    actor = "streamlit_admin:shared_admin_credential"

    # 通配符拦截
    with pytest.raises(ValueError, match="通配符"):
        edit_low_risk_config(
            store, "alert_mute_list", ["*"], actor=actor, reason="try wildcard",
        )
    with pytest.raises(ValueError, match="通配符"):
        edit_low_risk_config(
            store, "alert_mute_list", ["ORDER_*"], actor=actor, reason="try wildcard",
        )

    # 真实不可静音项拦截 (REAL_TRADE_REJECTED)
    with pytest.raises(ValueError, match="不可静音硬门禁"):
        edit_low_risk_config(
            store, "alert_mute_list", ["REAL_TRADE_REJECTED"], actor=actor, reason="try mute real trade reject",
        )

    # 前缀不可静音项拦截 (RECONCILIATION_*)
    with pytest.raises(ValueError, match="不可静音硬门禁"):
        edit_low_risk_config(
            store, "alert_mute_list", ["RECONCILIATION_MISMATCH"], actor=actor, reason="try mute reconciliation",
        )


def test_rollback_low_risk_config_fails_on_invalid_or_missing_release(tmp_path):
    """断言 5：回滚到不存在或非法版本时必抛错。"""
    store = TradingStore(tmp_path / "test_rollback_guard.db")
    actor = "streamlit_admin:shared_admin_credential"

    # 不存在的版本
    with pytest.raises(ValueError, match="不存在"):
        rollback_low_risk_config(store, 99999, actor=actor, reason="rollback missing")

    # 未发布的草稿版本不可回滚
    rel_id = store.create_config_release("alert_mute_list", ["ORDER_FAILED"], actor, "draft only")
    with pytest.raises(ValueError, match="只有 PUBLISHED 版本可执行回滚"):
        rollback_low_risk_config(store, rel_id, actor=actor, reason="rollback draft")


def test_low_risk_edit_and_rollback_positive_lifecycle(tmp_path):
    """断言 7：正向全链路测试：
    1. 管理员提交合法变更，成功发布；
    2. 检查 audit_events 包含完整的 actor/reason/before/after/base_version 载荷；
    3. 成功执行一键回滚，配置安全还原。
    """
    store = TradingStore(tmp_path / "test_lifecycle.db")
    actor = "streamlit_admin:shared_admin_credential"
    reason1 = "业务静音临时故障: ORDER_TIMEOUT"

    res = edit_low_risk_config(
        store, "alert_mute_list", ["ORDER_TIMEOUT"], actor=actor, reason=reason1,
    )
    assert res["status"] == "PUBLISHED"
    rel1 = res["release_id"]

    # 验证生效值
    cfg = store.get_runtime_config("alert_mute_list")
    assert cfg[0] == ["ORDER_TIMEOUT"]

    # 验证审计日志
    audits = [r for r in store.audit_events(limit=10) if r["event_type"] == "CONFIG_LOW_RISK_UPDATED"]
    assert len(audits) >= 1
    payload = json.loads(audits[0]["payload_json"])
    assert payload["actor"] == actor
    assert payload["reason"] == reason1
    assert payload["before"] is None
    assert payload["after"] == ["ORDER_TIMEOUT"]
    assert payload["base_version"] == 0

    # 执行一键回滚
    reason2 = "业务测试完毕，一键回滚至初始状态"
    rb_res = rollback_low_risk_config(store, rel1, actor=actor, reason=reason2)
    assert rb_res["status"] == "ROLLED_BACK"

    # 验证回滚后生效值
    cfg_after = store.get_runtime_config("alert_mute_list")
    assert cfg_after[0] is None
