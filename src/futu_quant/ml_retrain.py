"""ML 重训控制（P2 区块②）：手动触发 + 并发锁 + 超时 + 历史 + 可见的 SKIP。

为什么需要「SKIP 也算一次记录」：
- 缺 `sklearn` 时**不是错误**（依赖按用户决定放在最后装），但必须**看得见**
  （`SKIP:sklearn_missing`），否则"点了重训、没反应"又会变成一次静默失效；
- 并发触发时第二次必须被锁挡住并留痕（`SKIP:already_running`），而不是排队跑两次
  把生产模型来回替换。

状态语义（写进 `ml_retrain_runs.status`）：
- `RUNNING` / `SUCCESS` / `FAILED` / `TIMEOUT` / `SKIP`
- SKIP 的 `reason` 取值：`sklearn_missing` / `already_running`
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .filelock import FileLockError, exclusive_lock, unlock_exclusive
from .store import TradingStore

ROOT = Path(__file__).resolve().parents[2]
RETRAIN_SCRIPT = ROOT / "scripts" / "ml_retrain_monthly.py"
LOCK_PATH = ROOT / "data" / "ml_retrain.lock"
REPORT_DIR = ROOT / "data" / "ml_training" / "reports"
DEFAULT_TIMEOUT_SECONDS = 1800


def sklearn_available() -> bool:
    try:
        return importlib.util.find_spec("sklearn") is not None
    except (ImportError, ValueError):
        return False


def _newest_report() -> tuple[str, dict | None]:
    """取最新一份重训报告（脚本自己写 json）。读不到就返回空。"""
    try:
        reports = sorted(REPORT_DIR.glob("retrain_*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return "", None
    if not reports:
        return "", None
    newest = reports[-1]
    try:
        return str(newest), json.loads(newest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 报告损坏不能把重训结论变成崩溃
        return str(newest), None


def _aucs_from_report(report: dict | None) -> tuple[float | None, float | None]:
    if not isinstance(report, dict):
        return None, None
    new_auc = old_auc = None
    for step in (report.get("steps") or {}).values():
        if not isinstance(step, dict):
            continue
        new_auc = step.get("new_auc", new_auc)
        old_auc = step.get("old_auc", old_auc)
    return (
        float(new_auc) if isinstance(new_auc, (int, float)) else None,
        float(old_auc) if isinstance(old_auc, (int, float)) else None,
    )


def run_retrain(
    store: TradingStore,
    *,
    triggered_by: str,
    trigger_kind: str = "manual",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    script: Path | None = None,
    lock_path: Path | None = None,
    runner: Any = None,
) -> dict:
    """跑一次重训，返回 {status, reason, run_id, ...}。**永不抛出**。"""
    run_script = Path(script or RETRAIN_SCRIPT)
    lock_file = Path(lock_path or LOCK_PATH)
    run_subprocess = runner or subprocess.run

    def _skip(reason: str) -> dict:
        run_id = store.record_ml_retrain_run(
            triggered_by=triggered_by, trigger_kind=trigger_kind,
            status="SKIP", reason=reason,
        )
        store.audit(
            "ML_RETRAIN_SKIPPED",
            {"reason": reason, "triggered_by": triggered_by, "kind": trigger_kind},
            actor=f"streamlit_admin:{triggered_by}" if trigger_kind == "manual" else "scheduler:main",
        )
        return {"status": "SKIP", "reason": reason, "run_id": run_id}

    # ① 依赖缺失是可预期的状态，不是错误：留痕 + SKIP，不告警
    if not sklearn_available():
        return _skip("sklearn_missing")

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "ab") as handle:
        try:
            exclusive_lock(handle, blocking=False)
        except (FileLockError, OSError):
            return _skip("already_running")
        try:
            run_id = store.record_ml_retrain_run(
                triggered_by=triggered_by, trigger_kind=trigger_kind, status="RUNNING",
            )
            try:
                env = dict(os.environ)
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                completed = run_subprocess(
                    [sys.executable, str(run_script)],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=timeout_seconds, env=env,
                )
            except subprocess.TimeoutExpired as exc:
                store.finish_ml_retrain_run(
                    run_id, status="TIMEOUT", reason=f"超过 {timeout_seconds}s 未完成：{exc}",
                )
                store.audit(
                    "ML_RETRAIN_TIMEOUT",
                    {"timeout_seconds": timeout_seconds, "triggered_by": triggered_by},
                    actor="service:ml_retrain",
                )
                return {"status": "TIMEOUT", "run_id": run_id}
            except Exception as exc:  # noqa: BLE001
                store.finish_ml_retrain_run(run_id, status="FAILED", reason=f"启动失败：{exc}")
                return {"status": "FAILED", "reason": str(exc), "run_id": run_id}

            report_path, report = _newest_report()
            new_auc, old_auc = _aucs_from_report(report)
            if completed.returncode == 0:
                status, reason = "SUCCESS", ""
            else:
                tail = (completed.stderr or completed.stdout or "").strip()[-500:]
                status, reason = "FAILED", f"返回码 {completed.returncode}：{tail}"
            store.finish_ml_retrain_run(
                run_id, status=status, reason=reason, new_auc=new_auc, old_auc=old_auc,
                report_path=report_path,
            )
            store.audit(
                "ML_RETRAIN_FINISHED",
                {
                    "status": status, "reason": reason, "triggered_by": triggered_by,
                    "new_auc": new_auc, "old_auc": old_auc, "report_path": report_path,
                },
                actor="service:ml_retrain",
            )
            if status == "FAILED":
                store.operational_event(
                    "ml_retrain", "ML_RETRAIN_FAILED", "ERROR",
                    {"reason": reason, "report_path": report_path},
                )
            return {
                "status": status, "reason": reason, "run_id": run_id,
                "new_auc": new_auc, "old_auc": old_auc, "report_path": report_path,
            }
        finally:
            handle.seek(0)
            try:
                unlock_exclusive(handle)
            except Exception:  # noqa: BLE001 - 解锁失败不该让调用方崩
                pass
