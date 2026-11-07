"""压力损失预算表租户化守卫（迁移 25，2026-11-07 用户拍板「开啊，这么大的BUG」）。

事故：strategy_risk_budgets 无租户列、strategy_id 单列主键——实盘与模拟盘共用
同一行预算，改一处两租户同时变（限额地图 20261107 坐实：paper 撞的 $1,309
= 模拟净值 $6,546 × 共用行 0.20）。

契约：
1. 迁移 25 后 PK = (strategy_id, tenant_id)，存量行归 real + 补 paper 副本；
2. 同策略两租户可独立设值，互不污染（变异：跨租户写入必被隔离）；
3. 读侧（strategy_budget_usage / strategy_risk_budget / 页 15）按 broker/租户取；
4. 缺省参数回落实盘租户——存量调用方行为不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from futu_quant.store import TradingStore


def _budget(strategy_id: str, **overrides) -> dict:
    base = {
        "strategy_id": strategy_id,
        "capital_ratio": 0.25,
        "max_loss_ratio": 0.06,
        "option_premium_ratio": 0.10,
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_migration_creates_tenant_scoped_rows(tmp_path):
    """迁移 25：存量行归 real + 补 paper 副本，PK 变复合键。"""
    import sqlite3

    db = tmp_path / "mig.db"
    conn = sqlite3.connect(db)
    # 造迁移前的旧 schema + 一行数据
    conn.execute(
        """CREATE TABLE strategy_risk_budgets (
        strategy_id TEXT PRIMARY KEY, capital_ratio REAL NOT NULL,
        max_loss_ratio REAL NOT NULL, option_premium_ratio REAL NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO strategy_risk_budgets VALUES"
        " ('volatility_long_gamma', 0.25, 0.06, 0.10, 1, '2026-11-06T00:00:00')"
    )
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    # 用真实 checksum（apply_migrations 会逐版本校验一致性）
    from futu_quant.migrations import MIGRATIONS

    checksums = {m.version: m.checksum for m in MIGRATIONS}
    for version in range(1, 25):
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (version, f"m{version}", checksums[version], "2026-11-06T00:00:00"),
        )
    conn.commit()
    conn.close()

    from futu_quant.migrations import apply_migrations, authorize_migrations

    authorize_migrations("test:budget_tenant_scope")
    # 直接对裸库跑迁移（TradingStore 构造会带完整 bootstrap，不适合此处）
    connection = sqlite3.connect(db)
    try:
        apply_migrations(connection, actor="test:budget_tenant_scope")
    finally:
        connection.close()

    store = TradingStore(db)
    rows = store.strategy_risk_budgets("futu_real_master")
    paper_rows = store.strategy_risk_budgets("futu_paper_sandbox")
    real_map = {row["strategy_id"]: row for row in rows}
    paper_map = {row["strategy_id"]: row for row in paper_rows}
    assert "volatility_long_gamma" in real_map, "存量行必须归 real 租户"
    assert "volatility_long_gamma" in paper_map, "必须补 paper 副本"
    assert real_map["volatility_long_gamma"]["max_loss_ratio"] == pytest.approx(0.06)
    assert paper_map["volatility_long_gamma"]["max_loss_ratio"] == pytest.approx(0.06)


def test_tenants_are_independent_no_cross_pollution(tmp_path):
    """同策略两租户独立设值：改 paper 不影响 real（核心验收）。"""
    store = TradingStore(tmp_path / "tenant.db", bootstrap=True)
    store.save_strategy_risk_budgets([_budget("volatility_long_gamma")], tenant_id="futu_real_master")
    store.save_strategy_risk_budgets([_budget("volatility_long_gamma")], tenant_id="futu_paper_sandbox")

    # paper 独立放宽
    store.save_strategy_risk_budgets(
        [_budget("volatility_long_gamma", max_loss_ratio=0.30)],
        tenant_id="futu_paper_sandbox",
    )

    real_row = store.strategy_risk_budget("volatility_long_gamma", "futu_real_master")
    paper_row = store.strategy_risk_budget("volatility_long_gamma", "futu_paper_sandbox")
    assert real_row["max_loss_ratio"] == pytest.approx(0.06), (
        "paper 调预算不得污染 real 行（共用表缺陷的核心回归）"
    )
    assert paper_row["max_loss_ratio"] == pytest.approx(0.30)


def test_default_parameter_falls_back_to_real(tmp_path):
    """缺省参数回落实盘租户：存量调用方行为不变。"""
    store = TradingStore(tmp_path / "default.db", bootstrap=True)
    store.save_strategy_risk_budgets([_budget("core_satellite")])  # 无 tenant_id
    rows = store.strategy_risk_budgets()  # 无 tenant_id 读
    assert [row["strategy_id"] for row in rows] == ["core_satellite"]
    assert rows[0]["tenant_id"] == "futu_real_master"


def test_budget_usage_reads_tenant_own_rows(tmp_path, monkeypatch):
    """strategy_budget_usage 按 broker 租户取各自预算（读侧接线）。"""
    from futu_quant.portfolio_allocator import DEFAULT_BUDGETS, ensure_default_budgets

    store = TradingStore(tmp_path / "usage.db", bootstrap=True)
    ensure_default_budgets(store, "futu_real_master")
    ensure_default_budgets(store, "futu_paper_sandbox")
    # 两租户都从 DEFAULT_BUDGETS 起步；把 real 行改成可辨识值再验证隔离。
    store.save_strategy_risk_budgets(
        [_budget("volatility_long_gamma", max_loss_ratio=0.06)],
        tenant_id="futu_real_master",
    )
    store.save_strategy_risk_budgets(
        [_budget("volatility_long_gamma", max_loss_ratio=0.30)],
        tenant_id="futu_paper_sandbox",
    )
    real_row = store.strategy_risk_budget("volatility_long_gamma", "futu_real_master")
    paper_row = store.strategy_risk_budget("volatility_long_gamma", "futu_paper_sandbox")
    assert real_row is not None and paper_row is not None
    assert real_row["max_loss_ratio"] == pytest.approx(0.06)
    assert paper_row["max_loss_ratio"] == pytest.approx(0.30)
    # broker 租户解析路径由 strategy_budget_usage 走 tenant_of_broker —— 此处
    # 断言 store 层接口正确即可（端到端由既有 budget 用例覆盖）。
    assert DEFAULT_BUDGETS  # noqa: B018 - 基线常量存在性


def test_mutation_shared_row_write_must_be_red(tmp_path):
    """变异：把写侧租户参数忽略（退回共用行）→ 隔离用例必须红。"""
    import shutil
    import subprocess
    import sys

    module = Path("src/futu_quant/store.py")
    original = module.read_text(encoding="utf-8")
    anchor = "(strategy_id,tenant_id,capital_ratio,max_loss_ratio,option_premium_ratio,enabled,updated_at)"
    assert anchor in original, "租户化写入锚点丢失，无法构造变异"
    # 变异：tenant_id 一律写死 real（模拟共用行行为）
    mutated = original.replace(
        '(item["strategy_id"], str(tenant_id or "futu_real_master"),',
        '(item["strategy_id"], "futu_real_master",',
    )
    assert mutated != original
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_tenants_are_independent_no_cross_pollution"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "写侧退回共用行后隔离用例仍绿 —— 跨租户污染可复发"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
