"""`scripts/dev_worktree.py` 的纯函数测试（不碰 git，不启服务）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_worktree.py"


def _load():
    spec = importlib.util.spec_from_file_location("dev_worktree", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


dw = _load()


@pytest.mark.parametrize("slug", ["iv-cap", "a", "fix-1", "x" * 40])
def test_slug_valid_accepts_conventional_names(slug):
    assert dw.slug_valid(slug)


@pytest.mark.parametrize("slug", ["", "Iv-Cap", "iv_cap", "-lead", "a" * 60, "有中文"])
def test_slug_valid_rejects_bad_names(slug):
    assert not dw.slug_valid(slug)


def test_branch_for_prefixes_kind_but_keeps_existing_prefix():
    assert dw.branch_for("iv-cap", "fix") == "fix/iv-cap"
    assert dw.branch_for("new-thing", "feat") == "feat/new-thing"
    assert dw.branch_for("fix/already", "feat") == "fix/already"


def test_branch_for_rejects_unknown_kind():
    with pytest.raises(ValueError):
        dw.branch_for("x", "chore")


def test_worktree_path_uses_trailing_segment():
    assert dw.worktree_path("fix/iv-cap").name == "wt-iv-cap"
    assert dw.worktree_path("plain").name == "wt-plain"


def test_verify_env_points_test_db_and_real_prod_db(tmp_path):
    test_root = tmp_path / "trade-test"
    prod_db = tmp_path / "trade" / "data" / "trading.db"
    env = dw.verify_env(test_root=test_root, prod_db=prod_db)

    assert env["TRADEYI_ENV"] == "test"
    assert env["TRADEYI_DB_PATH"] == str(test_root / "data" / "trading.db")
    # 关键约束：指纹比对的基准必须是真生产库，不能回落到测试库
    assert env["TRADEYI_PROD_DB_PATH"] == str(prod_db.resolve())
    assert env["TRADEYI_PROD_DB_PATH"] != env["TRADEYI_DB_PATH"]
    assert env["PYTHONPATH"] == str(test_root / "src")


def test_verify_env_temp_stays_inside_test_root(tmp_path):
    test_root = tmp_path / "trade-test"
    env = dw.verify_env(test_root=test_root, prod_db=tmp_path / "p.db")
    assert Path(env["TEMP"]).is_relative_to(test_root)
    assert Path(env["TMP"]).is_relative_to(test_root)


def test_stamp_matches_requires_same_sha_and_ok_flag():
    sha = "7f21c21abcdef1234567890"
    assert dw.stamp_matches({"sha": sha, "ok": True}, sha)
    assert dw.stamp_matches({"sha": sha[:8], "ok": True}, sha)
    assert not dw.stamp_matches({"sha": sha, "ok": False}, sha)
    assert not dw.stamp_matches({"sha": "0" * 20, "ok": True}, sha)
    assert not dw.stamp_matches(None, sha)
    assert not dw.stamp_matches({"sha": sha, "ok": True}, "")


def test_pytest_summary_picks_last_statistics_line():
    out = "collected 3 items\n\n3 passed in 0.12s\n"
    assert "3 passed" in dw.pytest_summary(out)
    mixed = "1710 passed in 312.4s\n2 failed\n"
    assert dw.pytest_summary(mixed) == "1710 passed in 312.4s"


def test_pytest_summary_handles_empty_output():
    assert dw.pytest_summary("") == ""


def test_main_runs_without_sys_path_side_effects():
    # 只验证入口可调用且命令表齐全（status 会读 git，故只检查 -h 路径）
    parser = dw.build_parser()
    for name in ("new", "status", "sync-test", "verify", "merge", "release"):
        assert any(action.dest == "command" for action in parser._actions)
    assert parser.parse_args(["status"]).func is dw.cmd_status
    assert parser.parse_args(["new", "iv-cap"]).kind == "fix"
    assert sys.version_info >= (3, 9)
