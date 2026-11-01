from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import pandas as pd
from streamlit.string_util import validate_material_icon

from .replay import run_combo_resilience_drill, run_resilience_drill


ROOT = Path(__file__).resolve().parents[2]


EVENT_NAMES = {
    "PAGE_COMPATIBILITY": "页面兼容性巡检",
    "PAPER_E2E": "模拟交易端到端验收",
    "IDEMPOTENCY_AUDIT": "订单幂等审计",
    "AUTO_RECOVERY": "自动故障恢复",
    "OPERATIONS_REPORT": "每日运营报告",
    "DYNAMIC_ALLOCATION": "动态资金调度",
}


def audit_streamlit_pages(root: Path | str = ROOT) -> dict:
    root = Path(root)
    files = [root / "app.py", *sorted((root / "pages").glob("*.py"))]
    issues: list[dict] = []
    checked = 0
    for path in files:
        if not path.exists():
            continue
        checked += 1
        source = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(root))
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            issues.append({"页面": relative, "行号": exc.lineno or 0, "级别": "错误", "问题": str(exc)})
            continue
        form_keys: dict[str, int] = {}
        state_assignments: dict[str, int] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.IfExp)
                and _contains_streamlit_render_call(node.value)
            ):
                issues.append({
                    "页面": relative,
                    "行号": node.lineno,
                    "级别": "错误",
                    "问题": "禁止用裸三元表达式调用 Streamlit 渲染函数；其返回值会被 magic 再次输出",
                })
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    key = _session_assignment_key(target)
                    if key:
                        state_assignments.setdefault(key, node.lineno)
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name.endswith(".form") or name == "form":
                key_node = node.args[0] if node.args else next(
                    (item.value for item in node.keywords if item.arg == "key"), None
                )
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    form_keys.setdefault(key_node.value, node.lineno)
            if name.endswith("NumberColumn") and len(node.args) > 1:
                issues.append({"页面": relative, "行号": node.lineno, "级别": "错误",
                               "问题": "NumberColumn 的范围参数必须使用关键字"})
            for keyword in node.keywords:
                if keyword.arg == "use_container_width":
                    issues.append({"页面": relative, "行号": node.lineno, "级别": "警告",
                                   "问题": "use_container_width 已弃用，请改用 width"})
                if keyword.arg == "icon" and isinstance(keyword.value, ast.Constant):
                    icon = keyword.value.value
                    if isinstance(icon, str) and icon.startswith(":material/"):
                        try:
                            validate_material_icon(icon)
                        except Exception as exc:
                            issues.append({"页面": relative, "行号": node.lineno, "级别": "错误",
                                           "问题": f"无效图标 {icon}：{exc}"})
        for key in sorted(form_keys.keys() & state_assignments.keys()):
            issues.append({"页面": relative, "行号": state_assignments[key], "级别": "错误",
                           "问题": f"Session State 键 {key!r} 与表单键冲突"})
        issues.extend(_audit_page_imports(root, tree, relative))
    errors = sum(item["级别"] == "错误" for item in issues)
    return {"passed": errors == 0, "pages_checked": checked, "errors": errors,
            "warnings": len(issues) - errors, "issues": issues,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def _resolve_futu_quant_module(root: Path, module_name: str) -> Path | None:
    """把 `futu_quant.a.b` 静态映射到源码文件（**不 import**，避免可选依赖误报）。"""
    parts = module_name.split(".")
    if not parts or parts[0] != "futu_quant":
        return None
    base = root.joinpath("src", *parts)
    as_file = base.with_suffix(".py")
    if as_file.is_file():
        return as_file
    as_package = base / "__init__.py"
    return as_package if as_package.is_file() else None


def _module_top_level_names(path: Path) -> tuple[set[str], bool]:
    """模块顶层被定义/导入的名字集合；第二项表示是否存在无法静态枚举的 `import *`。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set(), True  # 目标模块自身语法坏掉时不在本检查范围内
    names: set[str] = set()
    has_star_import = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple):
                    names.update(el.id for el in target.elts if isinstance(el, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star_import = True
                else:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names, has_star_import


def _audit_page_imports(root: Path, tree: ast.AST, relative: str) -> list[dict]:
    """页面的模块级 `futu_quant` 导入必须真实存在（2026-11-01 面板阻断事故）。

    事故根因：`pages/00_today_overview.py` 写了 `from futu_quant.ui_store import ui_store`，
    该模块从未存在；而本巡检当时只查语法、不解析导入，`ImportError` 一路漏到生产。
    同时页面把 `get_tenant_broker` 从 `futu_quant.broker` 导入（实际在 `ui.py`）。

    **纯静态**：不 import 任何 `futu_quant` 模块，因此不会因缺少可选运行依赖
    （`futu`、`exchange_calendars` 等）在本巡检里误报。
    """
    issues: list[dict] = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ImportFrom):
            continue
        module_name = node.module or ""
        if not module_name.startswith("futu_quant"):
            continue
        wanted = [alias.name for alias in node.names if alias.name != "*"]
        if not wanted:
            continue
        module_file = _resolve_futu_quant_module(root, module_name)
        if module_file is None:
            issues.append({"页面": relative, "行号": node.lineno, "级别": "错误",
                           "问题": f"导入的模块不存在：{module_name}（页面一打开即 ImportError）"})
            continue
        defined, has_star_import = _module_top_level_names(module_file)
        if has_star_import:
            continue
        for name in wanted:
            if name not in defined:
                issues.append({"页面": relative, "行号": node.lineno, "级别": "错误",
                               "问题": f"{module_name} 顶层没有符号 {name}（导入会失败）"})
    return issues


def _contains_streamlit_render_call(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and _call_name(child.func).startswith(("st.", "streamlit."))
        for child in ast.walk(node)
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _session_assignment_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr:
        if _call_name(node.value) in {"st.session_state", "streamlit.session_state"}:
            return node.attr
    if isinstance(node, ast.Subscript) and _call_name(node.value) in {
        "st.session_state", "streamlit.session_state",
    }:
        value = node.slice
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def paper_e2e_check(store=None) -> dict:
    single = run_resilience_drill()
    combo = run_combo_resilience_drill()
    rows = pd.concat([single, combo], ignore_index=True)
    passed = bool(not rows.empty and rows["结果"].eq("通过").all())
    result = {"passed": passed, "checks": len(rows),
              "failed": int((rows["结果"] != "通过").sum()),
              "details": rows.to_dict("records")}
    if store is not None:
        store.operational_event("readiness", "PAPER_E2E", "HEALTHY" if passed else "ERROR", result)
    return result


def idempotency_audit(store) -> dict:
    intents = store.intents(limit=50_000)
    seen: dict[tuple[str, str], list[dict]] = {}
    duplicate_broker_orders: dict[str, int] = {}
    for row in intents:
        seen.setdefault((str(row["strategy_id"]), str(row["intent_key"])), []).append(row)
        order_id = str(row.get("broker_order_id") or "")
        if order_id:
            duplicate_broker_orders[order_id] = duplicate_broker_orders.get(order_id, 0) + 1
    duplicate_keys = [key for key, rows in seen.items() if len(rows) > 1]
    duplicate_orders = [key for key, count in duplicate_broker_orders.items() if count > 1]
    passed = not duplicate_keys and not duplicate_orders
    result = {"passed": passed, "intents": len(intents),
              "duplicate_intent_keys": [list(key) for key in duplicate_keys],
              "duplicate_broker_order_ids": duplicate_orders}
    store.operational_event("execution", "IDEMPOTENCY_AUDIT", "HEALTHY" if passed else "ERROR", result)
    return result


def observability_snapshot(store, record: bool = True) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    runs = [row for row in store.task_runs(limit=10_000)
            if datetime.fromisoformat(row["started_at"]) >= since]
    intents = [row for row in store.intents(limit=50_000)
               if datetime.fromisoformat(row["created_at"]) >= since]
    latest: dict[str, dict] = {}
    for row in runs:
        latest.setdefault(row["task_id"], row)
    failed = sum(row["status"] in {"ERROR", "TIMEOUT"} for row in runs)
    rejected = sum(row["status"] == "REJECTED" for row in intents)
    unknown = sum(row["status"] == "UNKNOWN" for row in intents)
    snapshot = {
        "task_runs_24h": len(runs), "task_failures_24h": failed,
        "task_failure_rate": failed / len(runs) if runs else 0.0,
        "orders_24h": len(intents), "rejected_24h": rejected, "unknown_24h": unknown,
        "open_incidents": len(store.incidents("OPEN")),
        "latest_tasks": list(latest.values()),
    }
    status = "HEALTHY" if failed == 0 and unknown == 0 else "WARNING"
    if record:
        store.operational_event("observability", "METRICS_SNAPSHOT", status, snapshot)
    return snapshot


def automatic_recovery_cycle(store) -> dict:
    # 运行期周期自愈：先收敛死进程遗留的孤儿 RUNNING 记录，避免僵尸 RUNNING 成为最新记录而阻断 incident 恢复
    reconciled_orphans = store.reconcile_orphan_task_runs()

    latest: dict[str, dict] = {}
    for row in store.task_runs(limit=5000):
        latest.setdefault(row["task_id"], row)
    recovered: list[str] = []
    pending: list[str] = []
    now = datetime.now(timezone.utc)
    stale_hours = max(1, int(os.getenv("TRANSIENT_INCIDENT_AUTO_RECOVER_HOURS", "12")))
    for incident in store.incidents("OPEN"):
        key = str(incident["incident_key"])
        task = key.split(":", 1)[1] if key.startswith(("TASK_TIMEOUT:", "STRATEGY_ERROR:")) else None
        if task and latest.get(task, {}).get("status") == "SUCCESS":
            if store.recover_incident(key):
                recovered.append(key)
        elif _transient_incident_recovered(store, key, incident, now, stale_hours):
            if store.recover_incident(key):
                recovered.append(key)
        else:
            pending.append(key)
    result = {
        "recovered": recovered,
        "pending": pending,
        "safe_actions_only": True,
        "reconciled_orphans": reconciled_orphans,
    }
    store.operational_event("recovery", "AUTO_RECOVERY", "HEALTHY" if not pending else "WARNING", result)
    return result


def _transient_incident_recovered(
    store,
    key: str,
    incident: dict,
    now: datetime,
    stale_hours: int,
) -> bool:
    """Recover stale one-shot incidents without hiding active portfolio risk."""
    transient_keys = {"ORDER_FAILED", "VOLATILITY_SCANNER_ERROR"}
    if key == "DAILY_LOSS_KILL_SWITCH" and not store.kill_switch():
        transient_keys.add("DAILY_LOSS_KILL_SWITCH")
    if key not in transient_keys:
        return False
    try:
        last_seen = datetime.fromisoformat(str(incident.get("last_seen")))
    except (TypeError, ValueError):
        return False
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return now - last_seen >= timedelta(hours=stale_hours)


def strategy_paper_acceptance(store, strategy_id: str) -> dict:
    days_required = max(1, int(os.getenv("PAPER_ACCEPTANCE_MIN_DAYS", "5")))
    orders_required = max(1, int(os.getenv("PAPER_ACCEPTANCE_MIN_ORDERS", "20")))
    min_fill = float(os.getenv("PAPER_ACCEPTANCE_MIN_FILL_RATE", "0.70"))
    max_reject = float(os.getenv("PAPER_ACCEPTANCE_MAX_REJECT_RATE", "0.20"))
    max_slippage = float(os.getenv("PAPER_ACCEPTANCE_MAX_SLIPPAGE", "0.02"))
    rows = [row for row in store.intents(limit=50_000) if row["strategy_id"] == strategy_id
            and not str(row["status"]).startswith("SHADOW")]
    dates = {str(row["created_at"])[:10] for row in rows}
    filled = [row for row in rows if float(row.get("filled_quantity", 0) or 0) > 0]
    rejected = [row for row in rows if row["status"] == "REJECTED"]
    slips = []
    for row in filled:
        reference = float(row.get("limit_price", 0) or 0)
        actual = float(row.get("average_fill_price", 0) or 0)
        if reference and actual:
            direction = 1 if row["side"] == "BUY" else -1
            slips.append((actual / reference - 1) * direction)
    fill_rate = len(filled) / len(rows) if rows else 0.0
    reject_rate = len(rejected) / len(rows) if rows else 0.0
    avg_slippage = sum(slips) / len(slips) if slips else 0.0
    gates = {
        "连续观察天数": (len(dates), days_required, len(dates) >= days_required),
        "模拟订单样本": (len(rows), orders_required, len(rows) >= orders_required),
        "成交率": (fill_rate, min_fill, fill_rate >= min_fill),
        "拒单率": (reject_rate, max_reject, reject_rate <= max_reject),
        "平均不利滑点": (avg_slippage, max_slippage, avg_slippage <= max_slippage),
        "未知订单": (sum(row["status"] == "UNKNOWN" for row in rows), 0,
                     not any(row["status"] == "UNKNOWN" for row in rows)),
    }
    return {"strategy_id": strategy_id, "passed": all(value[2] for value in gates.values()),
            "gates": {key: {"value": value, "required": required, "passed": passed}
                      for key, (value, required, passed) in gates.items()}}


def dynamic_allocation_plan(store, record: bool = True) -> dict:
    budgets = store.strategy_risk_budgets()
    open_incidents = " ".join(item["incident_key"] for item in store.incidents("OPEN"))
    proposed = []
    for row in budgets:
        item = dict(row)
        acceptance = strategy_paper_acceptance(store, item["strategy_id"])
        factor, reason = 1.0, "运行稳定，维持当前配额"
        if item["strategy_id"] in open_incidents:
            factor, reason = 0.5, "存在未恢复故障，配额减半"
        elif not acceptance["passed"]:
            factor, reason = 0.75, "模拟验收样本或质量未达标，保守降配"
        item["recommended_capital_ratio"] = float(item["capital_ratio"]) * factor
        item["recommended_max_loss_ratio"] = float(item["max_loss_ratio"]) * factor
        item["recommended_option_premium_ratio"] = float(item["option_premium_ratio"]) * factor
        item["factor"] = factor
        item["reason"] = reason
        proposed.append(item)
    total = sum(item["recommended_capital_ratio"] for item in proposed if item["enabled"])
    if total > 0.9:
        scale = 0.9 / total
        for item in proposed:
            item["recommended_capital_ratio"] *= scale
    result = {"recommendations": proposed, "cash_reserve_ratio": max(
        0.0, 1 - sum(item["recommended_capital_ratio"] for item in proposed if item["enabled"])
    )}
    if record:
        store.operational_event("allocator", "DYNAMIC_ALLOCATION", "HEALTHY", result)
    return result


def apply_dynamic_allocation(store, plan: dict, actor: str) -> None:
    rows = [{"strategy_id": item["strategy_id"],
             "capital_ratio": item["recommended_capital_ratio"],
             "max_loss_ratio": item["recommended_max_loss_ratio"],
             "option_premium_ratio": item["recommended_option_premium_ratio"],
             "enabled": bool(item["enabled"])} for item in plan["recommendations"]]
    store.save_strategy_risk_budgets(rows)
    store.audit("DYNAMIC_ALLOCATION_APPLIED", {"actor": actor, "budgets": rows}, actor="service:allocation")


def operations_report(store) -> dict:
    metrics = observability_snapshot(store)
    idempotency = idempotency_audit(store)
    pages = audit_streamlit_pages()
    recoveries = automatic_recovery_cycle(store)
    status = (
        "HEALTHY" if pages["passed"] and idempotency["passed"]
        and metrics["unknown_24h"] == 0 and not recoveries["pending"] else "WARNING"
    )
    lines = ["# 模拟盘每日运营报告", "", f"生成时间：{datetime.now().astimezone():%F %T %Z}", "",
             f"- 总体状态：{'正常' if status == 'HEALTHY' else '需要关注'}",
             f"- 24 小时任务：{metrics['task_runs_24h']}，失败：{metrics['task_failures_24h']}",
             f"- 24 小时订单：{metrics['orders_24h']}，拒绝：{metrics['rejected_24h']}，未知：{metrics['unknown_24h']}",
             f"- 页面巡检：{pages['pages_checked']} 页，错误：{pages['errors']}，警告：{pages['warnings']}",
             f"- 幂等审计：{'通过' if idempotency['passed'] else '失败'}",
             f"- 自动恢复：{len(recoveries['recovered'])} 项，待处理：{len(recoveries['pending'])} 项"]
    report = {"status": status, "markdown": "\n".join(lines), "metrics": metrics,
              "pages": pages, "idempotency": idempotency, "recovery": recoveries}
    store.operational_event("operations", "OPERATIONS_REPORT", status, report)
    return report


def localized_incidents(store) -> list[dict]:
    severity = {"ERROR": "错误", "WARNING": "警告", "CRITICAL": "严重", "INFO": "信息"}
    status = {"OPEN": "待处理", "RECOVERED": "已恢复"}
    rows = []
    for item in store.incidents(limit=1000):
        rows.append({"事件键": item["incident_key"], "严重程度": severity.get(item["severity"], item["severity"]),
                     "状态": status.get(item["status"], item["status"]), "说明": item["message"],
                     "发生次数": item["count"], "首次发生": item["first_seen"],
                     "最近发生": item["last_seen"], "恢复时间": item["recovered_at"]})
    return rows
