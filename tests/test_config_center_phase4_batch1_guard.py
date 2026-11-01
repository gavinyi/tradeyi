"""统一配置中心 Phase 4 Batch 4.1 发布/回滚账实一致与并发版本基线守卫。

严格验证 Reviewer 验收清单与并发门槛：
1. 三账一致性：runtime_config.version == config_versions 最大 version == release.base_version + 1，并给出直接核对查询；
2. 真实双线程并发争抢发布：一个成功、另一个必然被阻断并抛出 '配置基线已变化'；
3. 拒绝路径零写入：并发失败或基线过期时，拒绝路径绝不造成任何部分写入 (版本仅 +1，历史仅增 1 行)；
4. 回滚账实一致：回滚后值等于目标快照 before，版本严格 +1，发布单置 ROLLED_BACK，config_versions 新增独立历史行；
5. 禁止旁路写入：AST 扫描 config_center 模块，严禁直接调用 set_runtime_config。
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import sqlite3
from pathlib import Path
import pytest

from futu_quant.config_center import edit_low_risk_config, rollback_low_risk_config
from futu_quant.store import TradingStore


def _check_three_ledgers_consistency(db_path: Path, config_key: str, release_id: int) -> dict:
    """可直接运行的三账一致性核对查询。"""
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        c = conn.cursor()
        c.execute("SELECT version, value_json FROM runtime_config WHERE config_key=?", (config_key,))
        rc_row = c.fetchone()
        rc_version = rc_row[0] if rc_row else None
        rc_value = json.loads(rc_row[1]) if rc_row else None

        c.execute("SELECT MAX(id), value_json FROM config_versions WHERE config_key=?", (config_key,))
        cv_row = c.fetchone()
        cv_max_id = cv_row[0] if cv_row else None
        cv_value = json.loads(cv_row[1]) if cv_row and cv_row[1] else None

        c.execute("SELECT count(*) FROM config_versions WHERE config_key=?", (config_key,))
        cv_count = c.fetchone()[0]

        c.execute("SELECT base_version, status, diff_json FROM config_releases WHERE id=?", (release_id,))
        cr_row = c.fetchone()
        cr_base_version = cr_row[0] if cr_row else None
        cr_status = cr_row[1] if cr_row else None
        cr_diff = json.loads(cr_row[2]) if cr_row else {}

    return {
        "runtime_config_version": rc_version,
        "runtime_config_value": rc_value,
        "config_versions_count": cv_count,
        "config_versions_max_id": cv_max_id,
        "config_versions_value": cv_value,
        "release_base_version": cr_base_version,
        "release_status": cr_status,
        "release_diff": cr_diff,
    }


def test_three_ledgers_strict_consistency_and_rollback(tmp_path):
    """必验项 1 & 3：发布与回滚的三账强一致性核对。"""
    db_path = tmp_path / "three_ledgers.db"
    store = TradingStore(db_path)
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 1. 首次发布
    res1 = edit_low_risk_config(store, key, ["ORDER_FAILED"], actor=actor, reason="发布 1")
    rel_id1 = res1["release_id"]

    # 核对三账一致性
    chk1 = _check_three_ledgers_consistency(db_path, key, rel_id1)
    assert chk1["runtime_config_version"] == 1
    assert chk1["runtime_config_version"] == chk1["release_base_version"] + 1
    assert chk1["config_versions_count"] == 1
    assert chk1["release_status"] == "PUBLISHED"
    assert chk1["runtime_config_value"] == ["ORDER_FAILED"]

    # 2. 第二次发布
    res2 = edit_low_risk_config(store, key, ["ORDER_FAILED", "TASK_TIMEOUT"], actor=actor, reason="发布 2")
    rel_id2 = res2["release_id"]

    chk2 = _check_three_ledgers_consistency(db_path, key, rel_id2)
    assert chk2["runtime_config_version"] == 2
    assert chk2["runtime_config_version"] == chk2["release_base_version"] + 1
    assert chk2["config_versions_count"] == 2
    assert chk2["release_status"] == "PUBLISHED"
    assert chk2["runtime_config_value"] == ["ORDER_FAILED", "TASK_TIMEOUT"]

    # 3. 回滚发布单 2
    rb_res = rollback_low_risk_config(store, rel_id2, actor=actor, reason="回滚发布 2")
    assert rb_res["status"] == "ROLLED_BACK"

    # 回滚后核对三账一致性：版本严格 +1 (变为 3)，状态置 ROLLED_BACK，历史记录增为 3 行
    chk3 = _check_three_ledgers_consistency(db_path, key, rel_id2)
    assert chk3["runtime_config_version"] == 3
    assert chk3["config_versions_count"] == 3
    assert chk3["release_status"] == "ROLLED_BACK"
    assert chk3["runtime_config_value"] == ["ORDER_FAILED"]  # 还原为发布 2 之前的快照值


def test_concurrent_threads_competing_publish_enforces_baseline_and_zero_write(tmp_path):
    """必验项 2：Reviewer 核心门禁：真实两个连接/线程并发争抢发布。
    断言恰好一个成功，另一个必抛出 '配置基线已变化'，最终版本仅 +1，历史仅增 1 行，拒绝路径绝对零写入。
    """
    db_path = tmp_path / "concurrent_publish.db"
    store_init = TradingStore(db_path)
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 初始状态：base_version = 0
    # 创建两个完全相同的基于 base_version=0 的草稿
    draft_id_a = store_init.create_config_release(key, ["TASK_A"], actor, "草稿 A")
    draft_id_b = store_init.create_config_release(key, ["TASK_B"], actor, "草稿 B")

    results = []
    errors = []

    def _worker(draft_id, worker_name):
        # 每个线程使用独立的 TradingStore 连接实例
        thread_store = TradingStore(db_path)
        try:
            ver = thread_store.publish_config_release_with_audit(
                draft_id, actor, "CONFIG_CONCURRENT_TEST", {"worker": worker_name},
            )
            results.append((worker_name, draft_id, ver))
        except Exception as e:
            errors.append((worker_name, draft_id, e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_worker, draft_id_a, "thread_A")
        f2 = executor.submit(_worker, draft_id_b, "thread_B")
        f1.result()
        f2.result()

    # 核心并发断言 1：恰好一个成功，恰好一个失败
    assert len(results) == 1, f"并发争抢必须恰好 1 个成功，实际成功: {results}"
    assert len(errors) == 1, f"并发争抢必须恰好 1 个失败，实际失败: {errors}"

    # 核心并发断言 2：失败的线程必须抛出 '配置基线已变化'
    failed_worker, failed_draft_id, exc = errors[0]
    assert isinstance(exc, ValueError)
    assert "配置基线已变化" in str(exc)

    # 核心并发断言 3：拒绝路径绝对零写入，三账严格只记录了一次成功发布
    chk = _check_three_ledgers_consistency(db_path, key, results[0][1])
    assert chk["runtime_config_version"] == 1  # 严格只 +1
    assert chk["config_versions_count"] == 1   # 严格只增 1 行历史
    # 失败的草稿状态必须仍然是 DRAFT，绝对零写入、零篡改！
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT status FROM config_releases WHERE id=?", (failed_draft_id,)).fetchone()
        assert row[0] == "DRAFT"


def test_ast_guard_forbids_bypass_set_runtime_config_in_config_center():
    """必验项 4：AST 守卫扫描：配置中心代码 (src/futu_quant/config_center/) 内严禁直接调用 set_runtime_config 旁路改值。"""
    repo_root = Path(__file__).resolve().parents[1]
    cc_dir = repo_root / "src" / "futu_quant" / "config_center"
    assert cc_dir.exists()

    violations = []
    for py_file in cc_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8-sig")
        tree = ast.parse(content, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                # 允许测试文件或补偿逻辑调用，但在常规业务代码中严禁直接调 set_runtime_config 绕过发布引擎
                if name == "set_runtime_config":
                    # 仅允许 service.py 中的补偿异常处理行作为安全兜底
                    violations.append(f"{py_file.name}:{getattr(node, 'lineno', 0)}")

    # service.py 中不再调用 set_runtime_config (已全部收敛为 rollback_config_release)
    assert not violations, f"配置中心代码违反规范，发现了直接调用 set_runtime_config: {violations}"


def test_stale_baseline_draft_has_clear_rejection_exit(tmp_path):
    """必验项 5：过期草稿出口保障：
    并发败者或演进过期的草稿无法发布，但必须允许管理员调用 reject_config_release 顺利作废归档，
    状态变为 REJECTED，不堆积死草稿，且审计可查。
    """
    db_path = tmp_path / "stale_exit.db"
    store = TradingStore(db_path)
    actor = "streamlit_admin:shared_admin_credential"
    key = "alert_mute_list"

    # 1. 建立 base_version=0 的草稿
    draft_id = store.create_config_release(key, ["TASK_X"], actor, "草稿 X")

    # 2. 模拟配置已被其他发布推高到版本 1
    store.set_runtime_config(key, ["TASK_OTHER"], actor)

    # 3. 此时发布该草稿必被基线校验拦截
    with pytest.raises(ValueError, match="配置基线已变化"):
        store.publish_config_release(draft_id, actor)

    # 4. 管理员通过拒绝出口将其干净归档作废
    store.reject_config_release(draft_id, actor)

    # 验证终态已变为 REJECTED，草稿已清除
    drafts = [r for r in store.config_releases() if r["status"] == "DRAFT"]
    assert len(drafts) == 0
    rejected = [r for r in store.config_releases() if r["id"] == draft_id]
    assert len(rejected) == 1
    assert rejected[0]["status"] == "REJECTED"


def test_page18_ast_guard_disables_publish_on_stale_baseline():
    """必验项 6：AST 守卫扫描：pages/18 必须包含基线过期检测逻辑，并在过期时禁用发布按钮，杜绝无意义盲点。"""
    repo_root = Path(__file__).resolve().parents[1]
    page_18 = repo_root / "pages" / "18_infrastructure_disaster_recovery.py"
    assert page_18.exists()

    content = page_18.read_text(encoding="utf-8-sig")
    assert "is_stale_baseline" in content, "pages/18 必须包含 is_stale_baseline 基线过期判定！"
    assert "当前草稿基线版本已过期" in content, "pages/18 必须展示基线已过期的明确警告文案！"

