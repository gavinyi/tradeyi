"""统一配置中心 Phase 4 Batch 4.3 运行态校验与告警恢复联动守卫。

严格验证 Reviewer 验收清单与四大核心门禁：
1. 发布侧审计对称自填：即便调用方传入空 payload，事务内自填完整的 8 项真实审计字段；
2. 运行态非法值 fail-closed 与告警可观测性：非法静音项 (如通配符) 触发 WARNING 日志、计数递增、且绝对不生效；删掉 WARN 日志断言必红；
3. 草稿绝对隔离：未发布的 DRAFT 变更对运行态读取完全隔离，运行时始终读取已发布值；
4. 告警恢复联动端到端闭环：异常告警 -> 修复恢复 -> incident 状态置 RECOVERED 且审计留痕可查。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import pytest

from futu_quant.alerts import AlertManager, parse_mute_list
from futu_quant.store import TradingStore


def test_publish_config_release_with_audit_symmetric_self_filling(tmp_path):
    """必验项 1：发布侧审计对称自填：即便传入空 payload，事务内也能自填完整的核心审计字段。"""
    store = TradingStore(tmp_path / "test_publish_audit.db")
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 1. 建立草稿 (base_version = 0)
    draft_id = store.create_config_release(key, ["TASK_A"], actor, "自填测试草稿")

    # 2. 调用发布接口，故意传入空 audit_payload (模拟调用方少传或漏传)
    version = store.publish_config_release_with_audit(
        draft_id, actor, audit_event_type="CONFIG_RELEASE_PUBLISHED", audit_payload={},
    )
    assert version == 1

    # 3. 验证审计表中落库的 payload 已经被事务内完整自填
    audits = [r for r in store.audit_events() if r["event_type"] == "CONFIG_RELEASE_PUBLISHED"]
    assert len(audits) == 1
    payload = json.loads(audits[0]["payload_json"])

    # 校验自填字段完整性
    assert payload["actor"] == actor
    assert payload["reason"] == "自填测试草稿"
    assert payload["before"] is None
    assert payload["after"] == ["TASK_A"]
    assert payload["base_version"] == 0
    assert payload["result_version"] == 1
    assert payload["release_id"] == draft_id
    assert payload["config_key"] == key


def test_runtime_illegal_mute_entry_fails_closed_and_logs_warning(caplog):
    """必验项 2：运行态非法值 fail-closed：含通配符的静音项必须触发 WARNING 日志，且绝对不生效。
    红区守卫：删掉 WARN 日志断言必红。
    """
    with caplog.at_level(logging.WARNING, logger="futu_quant.alerts"):
        raw_input = ["*", "ORDER_TIMEOUT", "?", "  ", "VALID_TASK"]
        accepted, rejected = parse_mute_list(raw_input)

    # 1. 断言非法项被严格剔除，合法项正常保留
    assert accepted == {"ORDER_TIMEOUT", "VALID_TASK"}
    assert set(rejected) == {"*", "?", "  "}

    # 2. 断言必须打印了显式可审计的 WARNING 日志 (防静默丢弃)
    assert "解析静音清单发现非法项" in caplog.text
    assert "*" in caplog.text


def test_alert_manager_runtime_fails_closed_on_illegal_mute_config(tmp_path):
    """必验项 2.2：AlertManager 在运行态读到被篡改的非法通配符配置时，绝不静音任何东西 (fail-closed)。"""
    store = TradingStore(tmp_path / "test_alert_manager_fail_closed.db")
    # 模拟有人绕过门禁在底层写入了含通配符的非法配置
    store.set_setting("alert_mute_list", '["*"]')

    alerts = AlertManager(store)
    mute_set = alerts._mute_list()

    # 断言非法通配符未被采纳，实际生效静音集为空
    assert mute_set == set()
    # 断言非法计数递增
    assert alerts.rejected_mute_count >= 1

    # 断言安全类事件依然正常发送，绝不被通配符阻断
    assert "ORDER_FAILED" not in mute_set
    assert "DAILY_LOSS_KILL_SWITCH" not in mute_set


def test_draft_isolation_does_not_affect_runtime_reading(tmp_path):
    """必验项 3：草稿隔离性：存在未发布的 DRAFT 时，运行态读取必须仍是已发布值，绝不发生提前泄露。"""
    store = TradingStore(tmp_path / "test_draft_iso.db")
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 1. 初始已发布状态
    d1 = store.create_config_release(key, ["PUBLISHED_VAL"], actor, "正式版本 1")
    store.publish_config_release(d1, actor)

    # 2. 新建草稿 DRAFT (修改为全新值)
    d2 = store.create_config_release(key, ["DRAFT_VAL_UNPUBLISHED"], actor, "草稿版本 2 未发布")
    assert d2 > d1

    # 3. 运行态读取：必须依然是已发布的初始值，DRAFT 绝不泄露给运行时！
    cur_cfg = store.get_runtime_config(key)
    assert cur_cfg[0] == ["PUBLISHED_VAL"]
    assert cur_cfg[1] == 1

    # AlertManager 运行态读取也必须是已发布值
    alerts = AlertManager(store)
    assert alerts._mute_list() == {"PUBLISHED_VAL"}


def test_alert_incident_recovery_closed_loop(tmp_path):
    """必验项 4：告警恢复联动闭环：异常告警 -> 状态 OPEN -> 执行恢复 -> 状态 RECOVERED 且审计留痕。"""
    store = TradingStore(tmp_path / "test_incident_loop.db")
    alerts = AlertManager(store)
    incident_key = "TASK_TIMEOUT:order_reconciliation"

    # 1. 触发 ERROR 告警产生 OPEN incident
    alerts.send("ERROR", "TASK_TIMEOUT", "任务耗时过长", incident_key=incident_key)
    open_incidents = store.incidents("OPEN")
    assert any(i["incident_key"] == incident_key for i in open_incidents)

    # 2. 执行恢复动作
    alerts.recover("TASK_TIMEOUT", "耗时已恢复正常", incident_key=incident_key)

    # 3. 验证 incident 状态已闭环置为 RECOVERED 且审计留痕可查
    recovered_incidents = [i for i in store.incidents() if i["incident_key"] == incident_key]
    assert len(recovered_incidents) >= 1
    assert recovered_incidents[0]["status"] == "RECOVERED"
    assert recovered_incidents[0]["recovered_at"] is not None
    # 验证审计表留痕
    audits = [a for a in store.audit_events(limit=10) if incident_key in a["payload_json"]]
    assert len(audits) >= 1
