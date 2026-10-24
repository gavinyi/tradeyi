"""P2-4 自动月度重训的开关守卫：**默认关闭**、只在显式开启后由子进程跑。

为什么必须默认关闭：
这一步会改变策略行为（模型可能被替换），用户明确要求「装依赖与开启放最后」。
默认开 = 部署即改策略，不可接受。

变异：把默认值从 `false` 改成 `true` → 第 1 条转红；
把 Popen 换成同步调用 → 无法从测试直接发现，但代码注释与本守卫一起防止误改。
"""

from __future__ import annotations

import sys
from pathlib import Path

from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_service  # noqa: E402


class _PopenRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return object()


def test_auto_retrain_is_off_by_default(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "t.db")
    recorder = _PopenRecorder()
    monkeypatch.setattr(run_service.subprocess, "Popen", recorder)

    assert run_service.maybe_trigger_auto_retrain(store) is False
    assert recorder.calls == [], "默认关闭时绝不能拉起训练进程"


def test_auto_retrain_spawns_wrapper_when_enabled(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting("ml_auto_retrain_enabled", "true")
    recorder = _PopenRecorder()
    monkeypatch.setattr(run_service.subprocess, "Popen", recorder)

    assert run_service.maybe_trigger_auto_retrain(store) is True
    assert len(recorder.calls) == 1
    args = recorder.calls[0][0]
    assert any("run_ml_retrain_wrapper.py" in str(item) for item in args)
    assert "--kind" in args and "auto" in args
    events = [
        e for e in store.audit_events(limit=20)
        if e["event_type"] == "ML_AUTO_RETRAIN_TRIGGERED"
    ]
    assert events, "自动触发必须留审计，否则'谁在什么时候重训了'无从回答"
