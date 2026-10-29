"""P2 重训控制守卫：SKIP 可见、并发锁、超时、历史留痕。

要守的退化形态（每一种都会让"重训"变成静默失效）：
- 缺 `sklearn` 时抛异常 → 用户点了重训只看到报错，不知道"依赖还没装"；
- 并发触发两次 → 两次训练同时替换生产模型；
- 训练超时 → 进程挂死、状态永远 RUNNING；
- SKIP/失败只在日志里 → 页面看不到，等于没发生。

变异：`run_retrain` 里缺依赖时改成 `raise` → SKIP 用例红；
去掉锁 → 并发用例红；超时改成 `pass` → 超时用例红。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from futu_quant import ml_retrain
from futu_quant.filelock import FileLockError
from futu_quant.store import TradingStore


def _store(tmp_path):
    return TradingStore(tmp_path / "t.db")


def test_skip_when_sklearn_missing_is_recorded_not_raised(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: False)

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock",
        script=tmp_path / "x.py",
    )

    assert result["status"] == "SKIP" and result["reason"] == "sklearn_missing"
    rows = store.ml_retrain_runs(limit=5)
    assert rows and rows[0]["status"] == "SKIP"
    assert rows[0]["reason"] == "sklearn_missing", "SKIP 也必须留痕，否则'点了没反应'无从排查"


def test_concurrent_trigger_is_blocked_by_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(
        ml_retrain, "exclusive_lock",
        lambda handle, blocking=True: (_ for _ in ()).throw(FileLockError("busy")),
    )

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock",
        script=tmp_path / "x.py",
    )

    assert result["status"] == "SKIP" and result["reason"] == "already_running"
    rows = store.ml_retrain_runs(limit=5)
    assert rows[0]["reason"] == "already_running"


def test_timeout_is_recorded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="retrain", timeout=1)

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock",
        script=tmp_path / "x.py", runner=_timeout, timeout_seconds=1,
    )

    assert result["status"] == "TIMEOUT"
    rows = store.ml_retrain_runs(limit=5)
    assert rows[0]["status"] == "TIMEOUT" and rows[0]["finished_at"]


def test_success_records_aucs_from_report(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    reports = iter([
        ("", None),
        ("reports/retrain_x.json", {"steps": {"s3": {"new_auc": 0.71, "old_auc": 0.63}}}),
    ])
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: next(reports))

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock",
        script=tmp_path / "x.py",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert result["status"] == "SUCCESS"
    assert result["new_auc"] == pytest.approx(0.71) and result["old_auc"] == pytest.approx(0.63)
    rows = store.ml_retrain_runs(limit=5)
    assert rows[0]["status"] == "SUCCESS" and rows[0]["report_path"].endswith(".json")


def test_auto_success_requires_candidate_artifact(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    missing = tmp_path / "candidate.pkl"
    reports = iter([
        ("", None),
        ("reports/retrain_x.json", {
            "status": "CANDIDATE_ONLY", "candidate_model": str(missing),
        }),
    ])
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: next(reports))

    result = ml_retrain.run_retrain(
        store, triggered_by="scheduler", trigger_kind="auto", no_promote=True,
        lock_path=tmp_path / "lock", script=Path("x.py"),
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert result["status"] == "FAILED"
    assert "候选模型" in result["reason"]
    assert any(e["event_type"] == "ML_RETRAIN_FAILED" for e in store.operational_events(limit=20))


def test_auto_success_records_verified_candidate_artifact(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    candidate = tmp_path / "candidate.pkl"
    candidate.write_bytes(b"model")
    reports = iter([
        ("", None),
        ("reports/retrain_x.json", {
            "status": "CANDIDATE_ONLY", "candidate_model": str(candidate),
        }),
    ])
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: next(reports))

    def runner(*args, **kwargs):
        candidate.write_bytes(b"new-model")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = ml_retrain.run_retrain(
        store, triggered_by="scheduler", trigger_kind="auto", no_promote=True,
        lock_path=tmp_path / "lock", script=Path("x.py"), runner=runner,
    )

    assert result["status"] == "SUCCESS"
    assert result["report_path"].endswith(".json")


def test_old_report_cannot_make_empty_run_success(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    old = tmp_path / "retrain_old.json"
    old.write_text('{"status":"SUCCESS"}', encoding="utf-8")
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: (str(old), {"status": "SUCCESS"}))

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock", script=tmp_path / "x.py",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert result["status"] == "FAILED"
    assert "本次有效报告" in result["reason"]


def test_new_report_referencing_preexisting_candidate_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    candidate_dir = tmp_path / "models"
    candidate_dir.mkdir()
    candidate = candidate_dir / "unreferenced_old_candidate.pkl"
    candidate.write_bytes(b"old-model")
    monkeypatch.setattr(ml_retrain, "MODEL_DIR", candidate_dir)
    reports = iter([
        ("", None),
        (str(tmp_path / "new.json"), {
            "status": "CANDIDATE_ONLY", "candidate_model": str(candidate),
        }),
    ])
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: next(reports))

    result = ml_retrain.run_retrain(
        store, triggered_by="scheduler", trigger_kind="auto",
        lock_path=tmp_path / "lock", script=tmp_path / "x.py",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert result["status"] == "FAILED"
    assert "候选模型" in result["reason"]


def test_auto_trigger_forces_no_promote_inside_controller(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    candidate = tmp_path / "candidate-new.pkl"
    candidate.write_bytes(b"model")
    reports = iter([
        ("", None),
        (str(tmp_path / "new.json"), {
            "status": "CANDIDATE_ONLY", "candidate_model": str(candidate),
        }),
    ])
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: next(reports))
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        candidate.write_bytes(b"new-model")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = ml_retrain.run_retrain(
        store, triggered_by="scheduler", trigger_kind="auto", no_promote=False,
        lock_path=tmp_path / "lock", script=tmp_path / "x.py", runner=runner,
    )

    assert result["status"] == "SUCCESS"
    assert "--no-promote" in seen["command"]


def test_failed_run_is_visible(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    monkeypatch.setattr(ml_retrain, "exclusive_lock", lambda handle, blocking=True: None)
    monkeypatch.setattr(ml_retrain, "unlock_exclusive", lambda handle: None)
    monkeypatch.setattr(ml_retrain, "_newest_report", lambda: ("", None))

    result = ml_retrain.run_retrain(
        store, triggered_by="panel", lock_path=tmp_path / "lock",
        script=tmp_path / "x.py",
        runner=lambda *a, **k: SimpleNamespace(returncode=3, stdout="", stderr="boom"),
    )

    assert result["status"] == "FAILED" and "3" in result["reason"]
    events = [
        e for e in store.operational_events(limit=20)
        if e["event_type"] == "ML_RETRAIN_FAILED"
    ]
    assert events, "失败必须留一条可见事件（页面/告警才能看到）"
