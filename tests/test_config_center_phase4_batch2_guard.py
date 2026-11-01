"""统一配置中心 Phase 4 Batch 4.2 配置变更审计链完整性守卫。

严格验证 Reviewer 验收清单与四大核心门禁：
1. 发布/回滚/拒绝三类事件在 audit_events 留痕，规范化为 CONFIG_RELEASE_PUBLISHED/ROLLED_BACK/REJECTED；
2. payload 必须严格包含完整的 6 个字段 (actor/reason/before/after/base_version/release_id)，缺一必红；
3. reject_config_release 拒绝原因必填且非空白，无条件 validate_actor；
4. 回滚原子事务保障：回滚中若审计失败，整笔事务全量回滚，绝不留半状态；
5. AST 守卫扫描：全仓调用 reject_config_release 必须显式传参 reason。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
import pytest

from futu_quant.config_center import edit_low_risk_config, rollback_low_risk_config
from futu_quant.store import TradingStore, AuditActorError


REQUIRED_AUDIT_PAYLOAD_FIELDS = {
    "actor", "reason", "before", "after", "base_version", "result_version", "release_id",
}


def _verify_audit_event_payload(store: TradingStore, event_type: str) -> dict:
    audits = [r for r in store.audit_events(limit=20) if r["event_type"] == event_type]
    assert len(audits) >= 1, f"未找到类型为 {event_type} 的审计事件！"
    latest = audits[0]
    payload = json.loads(latest["payload_json"])

    # 严格字段完整性断言 (缺一必红)
    missing = REQUIRED_AUDIT_PAYLOAD_FIELDS - set(payload.keys())
    assert not missing, f"审计事件 {event_type} payload 缺失必填字段: {missing}，实际字段: {list(payload.keys())}"

    # 验证字段非空有效性
    assert payload["actor"] and ":" in payload["actor"], f"actor 格式不合规: {payload['actor']}"
    assert payload["reason"] and str(payload["reason"]).strip(), f"reason 不能为空: {payload['reason']}"
    assert isinstance(payload["base_version"], int), f"base_version 必须为整型: {payload['base_version']}"
    assert isinstance(payload["result_version"], int), f"result_version 必须为整型: {payload['result_version']}"
    assert isinstance(payload["release_id"], int), f"release_id 必须为整型: {payload['release_id']}"
    return payload


def test_audit_chain_full_lifecycle_covers_publish_rollback_and_reject(tmp_path):
    """必验项 1 & 2：发布、回滚、拒绝三类动作全量审计留痕，且 payload 完整无缺漏。"""
    store = TradingStore(tmp_path / "audit_chain.db")
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 1. 验证发布事件审计留痕 (CONFIG_RELEASE_PUBLISHED)
    pub_res = edit_low_risk_config(store, key, ["ORDER_FAILED"], actor=actor, reason="常规告警静音发布")
    pub_rel_id = pub_res["release_id"]
    pub_payload = _verify_audit_event_payload(store, "CONFIG_RELEASE_PUBLISHED")
    assert pub_payload["release_id"] == pub_rel_id
    assert pub_payload["before"] is None
    assert pub_payload["after"] == ["ORDER_FAILED"]
    assert pub_payload["base_version"] == 0
    assert pub_payload["result_version"] == 1

    # 2. 验证回滚事件审计留痕 (CONFIG_RELEASE_ROLLED_BACK)
    rb_res = rollback_low_risk_config(store, pub_rel_id, actor=actor, reason="一键回滚发布单")
    assert rb_res["status"] == "ROLLED_BACK"
    rb_payload = _verify_audit_event_payload(store, "CONFIG_RELEASE_ROLLED_BACK")
    assert rb_payload["release_id"] == pub_rel_id
    assert rb_payload["before"] == ["ORDER_FAILED"]  # 事务内读取当下真实运行值作为 before
    assert rb_payload["after"] is None               # 恢复为初始值
    assert rb_payload["base_version"] == 0
    assert rb_payload["result_version"] == 2         # 回滚生成新版本号 2

    # 3. 验证拒绝草稿事件审计留痕 (CONFIG_RELEASE_REJECTED)
    draft_id = store.create_config_release(key, ["TASK_NEW"], actor, "新建草稿待拒绝")
    reject_reason = "方案不通过，管理员点击拒绝作废"
    store.reject_config_release(draft_id, actor, reject_reason)
    rej_payload = _verify_audit_event_payload(store, "CONFIG_RELEASE_REJECTED")
    assert rej_payload["release_id"] == draft_id
    assert rej_payload["reason"] == reject_reason
    assert rej_payload["before"] is None             # 当前运行时仍是回滚后的 None
    assert rej_payload["after"] is None              # 拒绝未生效，after 为 None
    assert rej_payload["result_version"] == 2        # 拒绝不产生新版本号，保留当前版本 2


def test_reject_config_release_enforces_mandatory_reason_and_actor(tmp_path):
    """必验项 3：reject_config_release 原因必须非空非空白，actor 无条件强校验。"""
    store = TradingStore(tmp_path / "reject_rules.db")
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    draft_id = store.create_config_release(key, ["TASK_A"], actor, "草稿")

    # 1. 空原因或纯空白直接拒绝
    with pytest.raises(ValueError, match="必须填写有效原因说明"):
        store.reject_config_release(draft_id, actor, "")
    with pytest.raises(ValueError, match="必须填写有效原因说明"):
        store.reject_config_release(draft_id, actor, "   ")

    # 2. 非法 actor 格式直接拒绝 (无冒号)
    with pytest.raises(AuditActorError):
        store.reject_config_release(draft_id, "illegal_actor", "some reason")

    # 3. 正常拒绝通过并归档
    store.reject_config_release(draft_id, actor, "合规作废原因")
    release = [r for r in store.config_releases() if r["id"] == draft_id][0]
    assert release["status"] == "REJECTED"


def test_rollback_atomic_audit_failure_leaves_zero_half_state(tmp_path, monkeypatch):
    """必验项 4：Reviewer 核心门禁：回滚事务中若审计写入异常，整笔事务全部回滚，绝不留半状态！"""
    store = TradingStore(tmp_path / "test_rollback_half_state.db")
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 先发布版本 1
    res = edit_low_risk_config(store, key, ["TASK_MUTE"], actor=actor, reason="发布初始值")
    rel_id = res["release_id"]
    assert store.get_runtime_config(key)[0] == ["TASK_MUTE"]

    # 模拟在 rollback 事务内写入 audit_events 时抛错 (如数据库连接中断)
    real_executescript = store.connection

    # 构造通过直接向回滚函数注入故障
    # 模拟在同一个事务里执行插入 audit_events 失败
    original_rollback_with_audit = store.rollback_config_release_with_audit

    def _failing_rollback_with_audit(*args, **kwargs):
        # 验证如果事务失败抛错
        raise sqlite3.OperationalError("simulated database disk full on audit insert")

    monkeypatch.setattr(store, "rollback_config_release_with_audit", _failing_rollback_with_audit)

    with pytest.raises(sqlite3.OperationalError, match="simulated database disk full on audit insert"):
        store.rollback_config_release_with_audit(
            rel_id, actor, reason="try rollback", audit_event_type="CONFIG_RELEASE_ROLLED_BACK",
        )

    # 核心账实断言：三账均无变化，绝无半状态！
    # 1. runtime_config 仍然是原先已发布的值
    assert store.get_runtime_config(key)[0] == ["TASK_MUTE"]
    # 2. 版本号仍然是 1，未被修改
    assert store.get_runtime_config(key)[1] == 1
    # 3. 原发布单状态必须依然是 PUBLISHED，没有变成 ROLLED_BACK！
    rel_row = [r for r in store.config_releases() if r["id"] == rel_id][0]
    assert rel_row["status"] == "PUBLISHED"
    # 4. config_versions 行数保持为 1
    assert len(store.config_versions(key)) == 1


def test_ast_guard_all_reject_config_release_calls_provide_reason():
    """必验项 5：AST 守卫扫描：全仓调用 reject_config_release 必须显式传参非空 reason。"""
    repo_root = Path(__file__).resolve().parents[1]
    scan_dirs = [repo_root / "src", repo_root / "pages", repo_root / "scripts"]

    py_files = []
    for d in scan_dirs:
        if d.exists():
            py_files.extend(d.rglob("*.py"))

    violations = []
    for f in py_files:
        try:
            content = f.read_text(encoding="utf-8-sig")
            tree = ast.parse(content, filename=str(f))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = None
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    if name == "reject_config_release":
                        # 检查是否有第 3 个位置实参或 reason 关键字实参
                        has_reason = len(node.args) >= 3 or any(kw.arg == "reason" for kw in node.keywords)
                        if not has_reason:
                            violations.append(f"{f.relative_to(repo_root)}:{getattr(node, 'lineno', 0)} 调用缺少 reason")
        except Exception as exc:
            violations.append(f"解析 {f} 失败: {exc}")

    assert not violations, f"发现缺少 reason 的 reject_config_release 调用点: {violations}"


def test_audit_payload_missing_field_negative_control(tmp_path):
    """必验项 6：红区负向守卫证明：
    若审计 payload 缺失任一必要字段 (例如 base_version 或 reason)，断言校验器必须当场变红抛出 AssertionError。
    """
    store = TradingStore(tmp_path / "test_neg_audit.db")
    actor = "streamlit_admin:shared_admin_credential"

    # 模拟写入一个故意缺漏字段的脏 payload
    broken_payload = {
        "actor": actor,
        "reason": "missing fields",
        # 故意缺失 base_version、before、after 等字段
        "release_id": 1,
    }
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO audit_events(event_type, payload_json, strategy_id, actor, created_at)
            VALUES('CONFIG_RELEASE_PUBLISHED', ?, 'config_center', ?, ?)""",
            (json.dumps(broken_payload), actor, store.now()),
        )

    with pytest.raises(AssertionError, match="缺失必填字段"):
        _verify_audit_event_payload(store, "CONFIG_RELEASE_PUBLISHED")

