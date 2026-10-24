"""测试环境运行器守卫（用户要求：测试区要能跑全流程，且绝不碰生产）。

守三件事：
1. **只能在 `E:\\Thisyi\\trade-test` 跑**：从生产目录或开发 worktree 调用一律拒绝；
2. **DB 必须在测试工作区内**（且不在生产目录下）；
3. **端口与生产隔离**（Streamlit 8601 vs 生产 8501）、只拉模拟租户。

变异：把 `assert_not_production` 改成空函数 → 前两条转红；
把端口改回 8501 → 端口隔离用例转红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import test_env  # noqa: E402


def test_refuses_production_root(tmp_path):
    with pytest.raises(SystemExit):
        test_env.assert_not_production(Path(r"E:\Thisyi\trade"))


def test_refuses_non_test_workspace(tmp_path):
    with pytest.raises(SystemExit):
        test_env.assert_not_production(tmp_path)


def test_db_path_stays_inside_test_root():
    path = test_env.db_path(Path(r"E:\Thisyi\trade-test"))
    assert path.name == "trading.db"
    assert "trade-test" in str(path)
    assert "E:\\Thisyi\\trade\\" not in str(path).replace("trade-test", "")


def test_port_and_tenant_isolated_from_production():
    assert test_env.TEST_STREAMLIT_PORT != 8501, "测试区不得与生产共用 Streamlit 端口"
    assert test_env.TEST_TENANT == "futu_paper_sandbox", "测试区只用模拟租户（SIMULATE）"
    assert test_env.PRODUCTION_ROOT != test_env.TEST_ROOT


def test_child_env_marks_test_and_points_db():
    env = test_env.child_env(Path(r"E:\Thisyi\trade-test"))
    assert env["TRADEYI_ENV"] == "test"
    assert env["TRADEYI_DB_PATH"].endswith("trading.db")
    assert "trade-test" in env["TRADEYI_DB_PATH"]
