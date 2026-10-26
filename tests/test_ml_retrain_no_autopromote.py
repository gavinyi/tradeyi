"""A-4a（2026-10-27）：自动重训**绝不自动 promote**。

背景（用户拍板边界「promote 仍人工」）：`ml_retrain_monthly.promote_if_better` 会在
新模型 AUC 更好时直接覆盖生产 `volatility_scorer_v3.pkl/.json`。而自动重训是每日无人
值守触发 —— 等于每天自动换生产模型、没有人工复核。A-4a 收口：

1. 自动触发链路（`run_service.maybe_trigger_auto_retrain`）必须带 `--no-promote`；
2. `run_retrain(no_promote=True)` 必须把 `--no-promote` 传给训练脚本，并在
   `ml_retrain_runs.reason` 留「candidate_only（未替换生产模型）」痕迹；
3. 训练脚本 `--no-promote` 只落候选文件，**生产模型字节不变**，报告 `status=CANDIDATE_ONLY`。

另锁住口径：docstring 里宣称的 `ml_auto_retrain_interval_days` 从未实现，实际是日频（24h），
守卫断言源码不再出现该键的「默认 30」说法，防止再次误导。
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for entry in (str(ROOT / "src"), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import ml_retrain_monthly  # noqa: E402
import run_service  # noqa: E402
from futu_quant import ml_retrain  # noqa: E402

AUTO_CMD_MARKER = "--no-promote"


class _FakeStore:
    """只实现自动重训触发/重训链路用到的 store 方法。"""

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = dict(settings or {})
        self.events: list[tuple] = []
        self.audits: list[tuple] = []
        self.runs: list[dict] = []
        self.finished: list[dict] = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def operational_event(self, component, event_type, status, payload=None):
        self.events.append((component, event_type, status, payload))

    def audit(self, event_type, payload=None, actor=None):
        self.audits.append((event_type, payload, actor))

    def record_ml_retrain_run(self, **kwargs):
        self.runs.append(dict(kwargs))
        return len(self.runs)

    def finish_ml_retrain_run(self, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})


# ---------------------------------------------------------------------------
# 1. 自动触发链路必须带 --no-promote
# ---------------------------------------------------------------------------


def test_auto_trigger_spawns_wrapper_with_no_promote(monkeypatch):
    commands: list[list[str]] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            commands.append(list(cmd))

    monkeypatch.setattr(run_service.subprocess, "Popen", _FakePopen)
    store = _FakeStore({"ml_auto_retrain_enabled": "true"})

    assert run_service.maybe_trigger_auto_retrain(store) is True
    assert len(commands) == 1, "自动重训没有真正拉起子进程"
    command = commands[0]
    assert AUTO_CMD_MARKER in command, "自动重训未带 --no-promote → 会每日自动覆盖生产模型"
    assert "--kind" in command and "auto" in command
    assert store.audits and store.audits[-1][1].get("no_promote") is True


def test_disabled_switch_still_logs_skip_and_spawns_nothing(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(run_service.subprocess, "Popen", lambda cmd, **kw: commands.append(list(cmd)))
    store = _FakeStore({})

    assert run_service.maybe_trigger_auto_retrain(store) is False
    assert commands == []
    assert any(event[1] == "ML_AUTO_RETRAIN_SKIPPED" for event in store.events)


# ---------------------------------------------------------------------------
# 2. run_retrain(no_promote=True) 把开关透传给训练脚本并留痕
# ---------------------------------------------------------------------------


def test_run_retrain_passes_no_promote_flag_to_script(tmp_path, monkeypatch):
    captured: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _runner(cmd, **kwargs):
        captured.append(list(cmd))
        return _Completed()

    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)
    store = _FakeStore()

    result = ml_retrain.run_retrain(
        store, triggered_by="scheduler", trigger_kind="auto",
        script=tmp_path / "fake_retrain.py", lock_path=tmp_path / "retrain.lock",
        runner=_runner, no_promote=True,
    )

    assert captured, "训练子进程没有被调用"
    assert AUTO_CMD_MARKER in captured[0]
    assert result["status"] == "SUCCESS"
    assert any("candidate_only" in str(item.get("reason") or "") for item in store.finished)


def test_run_retrain_manual_keeps_old_semantics(tmp_path, monkeypatch):
    """人工手动重训不带 --no-promote（promote 行为留给人工判断）。"""
    captured: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ml_retrain, "sklearn_available", lambda: True)

    ml_retrain.run_retrain(
        _FakeStore(), triggered_by="panel", trigger_kind="manual",
        script=tmp_path / "fake_retrain.py", lock_path=tmp_path / "retrain.lock",
        runner=lambda cmd, **kwargs: (captured.append(list(cmd)), _Completed())[1],
    )
    assert AUTO_CMD_MARKER not in captured[0]


# ---------------------------------------------------------------------------
# 3. 训练脚本 --no-promote：只落候选，生产模型一个字节不动
# ---------------------------------------------------------------------------


def _write_fake_model(path: Path, auc: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump({"model": "fake", "feature_cols": ["x"], "auc": auc}, handle)
    path.with_suffix(".json").write_text(json.dumps({"auc": auc}), encoding="utf-8")


def test_monthly_script_no_promote_never_touches_production(tmp_path, monkeypatch):
    production = tmp_path / "data/ml_models/volatility_scorer_v3.pkl"
    _write_fake_model(production, 0.60)
    production_bytes = production.read_bytes()
    production_json = production.with_suffix(".json").read_bytes()
    data_csv = tmp_path / "train.csv"
    data_csv.write_text("a,b\n1,2\n", encoding="utf-8")

    reports: list[dict] = []
    monkeypatch.setattr(ml_retrain_monthly, "ROOT", tmp_path)
    monkeypatch.setattr(ml_retrain_monthly, "MIN_SAMPLES", 1)
    monkeypatch.setattr(ml_retrain_monthly, "export_data", lambda start=None: data_csv)
    monkeypatch.setattr(
        ml_retrain_monthly, "train_model",
        lambda data_path, out: _write_fake_model(Path(out), 0.99),
    )
    monkeypatch.setattr(ml_retrain_monthly, "write_report", lambda report: reports.append(dict(report)))
    monkeypatch.setattr(
        ml_retrain_monthly, "promote_if_better",
        lambda *args, **kwargs: pytest.fail("--no-promote 下不得调用 promote_if_better"),
    )
    monkeypatch.setattr(sys, "argv", [
        "ml_retrain_monthly.py", "--model-output", str(production), "--no-promote",
    ])

    assert ml_retrain_monthly.main() == 0

    assert production.read_bytes() == production_bytes, "生产 pkl 被 --no-promote 路径改动了"
    assert production.with_suffix(".json").read_bytes() == production_json, "生产 json 被改动"
    report = reports[-1]
    assert report["status"] == "CANDIDATE_ONLY"
    assert report["promoted"] is False
    assert report["candidate_model"].endswith("volatility_scorer_candidate_auto.pkl")
    assert Path(report["candidate_model"]).exists(), "候选模型没有落盘（人工 promote 无从下手）"


def test_monthly_script_keeps_promote_path_and_interval_docstring():
    """默认（未带 --no-promote）仍走人工 promote 语义；间隔口径与实现对齐。"""
    source = (SCRIPTS / "ml_retrain_monthly.py").read_text(encoding="utf-8")
    assert "--no-promote" in source
    assert "promote_if_better" in source, "人工路径被删掉了，手动重训语义变了"

    service_source = (SCRIPTS / "run_service.py").read_text(encoding="utf-8")
    assert "，默认 30）" not in service_source, (
        "docstring 又写回了旧的「interval_days 默认 30」说法 —— 实际默认 1 天"
    )
    assert "ml_auto_retrain_interval_days" in service_source, "间隔键没有真正接线"
    assert "auto_retrain_interval_seconds(store)" in service_source, (
        "service_cycle 的 due gate 没有用参数化间隔"
    )


# ---------------------------------------------------------------------------
# 4. A-4a 间隔参数化：ml_auto_retrain_interval_days 真实生效，默认 24h 不变
# ---------------------------------------------------------------------------


def test_interval_defaults_to_one_day_without_key():
    store = _FakeStore({})
    assert run_service.auto_retrain_interval_seconds(store) == 24 * 60 * 60
    assert store.events == [], "没有配置键时不该报错"


def test_interval_reads_configured_days():
    store = _FakeStore({run_service.AUTO_RETRAIN_INTERVAL_KEY: "7"})
    assert run_service.auto_retrain_interval_seconds(store) == 7 * 24 * 60 * 60


@pytest.mark.parametrize("bad_value", ["abc", "0", "-3", "400", "1.5e99"])
def test_interval_invalid_falls_back_to_default_and_logs_error(bad_value):
    store = _FakeStore({run_service.AUTO_RETRAIN_INTERVAL_KEY: bad_value})
    assert run_service.auto_retrain_interval_seconds(store) == 24 * 60 * 60
    events = [event for event in store.events if event[1] == "ML_AUTO_RETRAIN_INTERVAL_INVALID"]
    assert events, f"非法间隔 {bad_value!r} 未留 ERROR 事件（静默改节奏）"
    assert events[0][2] == "ERROR"
