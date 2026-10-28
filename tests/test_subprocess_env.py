"""`subprocess_env()` 助手：子进程必须能 import 本仓包。

2026-10-28 事故：子进程测试在 test 区假红，真因是子进程环境 `PYTHONPATH` 缺 `src`
且该 venv 缺 editable `.pth` → `ModuleNotFoundError: futu_quant`。补 `.pth` 是掩盖，
换 venv/换机器会复发；因此凡是起本仓 python 子进程，环境一律走这个助手。
"""
from __future__ import annotations

import os
from pathlib import Path

from futu_quant.subprocess_env import SRC_DIR, subprocess_env


def test_injects_repo_src_into_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = subprocess_env()
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert SRC_DIR in parts, "子进程 PYTHONPATH 没有本仓 src —— 换 venv 就会 ModuleNotFoundError"
    assert Path(SRC_DIR).name == "src"


def test_preserves_existing_pythonpath_and_prepends(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([r"C:\other"]))
    env = subprocess_env()
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == SRC_DIR, "本仓 src 必须前置，否则可能被别的 futu_quant 抢走"
    assert r"C:\other" in parts


def test_does_not_duplicate_src(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SRC_DIR, r"C:\other"]))
    parts = subprocess_env()["PYTHONPATH"].split(os.pathsep)
    assert parts.count(SRC_DIR) == 1


def test_sets_utf8_encoding_defaults(monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    env = subprocess_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_extra_overrides_and_does_not_mutate_os_environ(monkeypatch):
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    env = subprocess_env(extra={"PYTHONUNBUFFERED": "1"})
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "PYTHONUNBUFFERED" not in os.environ, "助手不得污染父进程环境"


def test_ensure_src_false_leaves_pythonpath_alone(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\only")
    env = subprocess_env(ensure_src=False)
    assert env["PYTHONPATH"] == r"C:\only"


def test_base_is_respected(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\should_not_appear")
    env = subprocess_env(base={})
    assert "should_not_appear" not in env.get("PYTHONPATH", "")
    assert SRC_DIR in env["PYTHONPATH"]
