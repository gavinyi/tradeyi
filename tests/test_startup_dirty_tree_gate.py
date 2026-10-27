"""流程门禁：生产工作区必须干净（改动先走 feat/fix 分支，不动生产）。

背景（2026-10-28 用户裁定）：当晚生产目录出现 9 个文件未提交就被 Worker 加载运行，
其中包含 `risk.py`（红区）与 `alerts.py`（告警口径）——代码没进分支、没审、没测
就上了生产。门禁把「生产工作区干净」变成启动硬约束。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts").resolve()))

import run_service  # noqa: E402


class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_clean_tree_returns_empty(monkeypatch):
    monkeypatch.setattr(
        run_service.subprocess, "run", lambda *a, **k: _Result("", 0)
    )
    assert run_service.modified_tracked_files() == []


def test_dirty_tree_returns_entries(monkeypatch):
    payload = " M src/futu_quant/risk.py\n M src/futu_quant/alerts.py\n"
    monkeypatch.setattr(
        run_service.subprocess, "run", lambda *a, **k: _Result(payload, 0)
    )
    dirty = run_service.modified_tracked_files()
    assert dirty == ["M src/futu_quant/risk.py", "M src/futu_quant/alerts.py"]


def test_git_failure_does_not_block_startup(monkeypatch):
    """git 不可用/报错时 fail-open：门禁本身不得成为新的启动单点故障。"""
    monkeypatch.setattr(
        run_service.subprocess, "run", lambda *a, **k: _Result("", 128)
    )
    assert run_service.modified_tracked_files() == []


def test_git_missing_does_not_block_startup(monkeypatch):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(run_service.subprocess, "run", _boom)
    assert run_service.modified_tracked_files() == []


def test_untracked_only_is_not_dirty(monkeypatch):
    """只查已跟踪文件：--untracked-files=no，data/、日志、测试副本不该拦住启动。"""
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result("", 0)

    monkeypatch.setattr(run_service.subprocess, "run", _run)
    run_service.modified_tracked_files()
    assert "--untracked-files=no" in captured["cmd"]
