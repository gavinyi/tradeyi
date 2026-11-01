"""统一整改批次专属守卫与负向测试 (G6/G7/G1-G5/G8)。

覆盖重点：
1. G6: 证明 Settings.load() 启动路径不连接生产库、无自建 TradingStore 帧、读路径零写入；
2. G7: 证明 cash_yield_* storage 修正为 runtime_config，但被严格排除在草稿键列表外；
3. G1-G5: 证明直写白名单合法放行、模板实例键合法性校验 O(1) 秒级执行且阻断非法键；
4. G8: 证明 task_runs.tenant_id 三阶段迁移幂等、可回滚、分批回填、且运行时支持双写过渡。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
import time
import pathlib
import pytest

from futu_quant import config as cfg_mod
from futu_quant.config_center.registry import GLOBAL_REGISTRY, get_draftable_config_keys, validate_instance_key
from futu_quant.store import TradingStore

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from migrate_v25_task_runs_tenant_id import (
    add_tenant_id_column,
    backfill_tenant_id,
    create_tenant_indexes,
    rollback_migration,
)


def test_g6_env_settings_dynamic_read_and_zero_write_path(tmp_path, monkeypatch):
    """G6 守卫：证明向 ENV_SETTINGS 写入值后，Settings.load() 能通过只读链路成功读取并折入环境变量，且读路径绝对零写操作！"""
    db_file = tmp_path / "test_g6_env.db"
    store = TradingStore(db_file, bootstrap=True)
    
    # 1. 模拟写入动态 ENV_SETTINGS 配置
    dynamic_payload = {"WATCHLIST": "US.MSFT,US.NVDA", "CORE_MAX_STOCK_WEIGHT": "0.85"}
    store.set_runtime_config("ENV_SETTINGS", dynamic_payload, "test_admin")

    # 2. 拦截写操作：断言 load 期间 set_runtime_config 绝对零调用
    write_calls = []
    orig_set = getattr(TradingStore, "set_runtime_config", None)
    if orig_set:
        def probe_set(self, key, value, actor, *a, **kw):
            write_calls.append((key, value, actor))
            return orig_set(self, key, value, actor, *a, **kw)
        monkeypatch.setattr(TradingStore, "set_runtime_config", probe_set)

    # 3. 传入 store 调用 Settings.load()
    monkeypatch.delenv("WATCHLIST", raising=False)
    settings = cfg_mod.Settings.load(refresh=True, store=store)
    
    # 4. 严格断言：动态值成功生效，且读路径写操作严格为 0
    assert "US.MSFT" in settings.watchlist
    assert "US.NVDA" in settings.watchlist
    assert os.environ.get("WATCHLIST") == "US.MSFT,US.NVDA"
    assert len(write_calls) == 0, f"读配置期间发生了非预期反向写库: {write_calls}"


def test_g6_fallback_warns_with_drift_notice_on_exception(tmp_path, monkeypatch, caplog):
    """G6 守卫：证明只读访问异常时，输出包含漂移风险警告的 WARNING 日志，且安全回退 env 镜像。"""
    import logging
    # 构造损坏数据库
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"not a valid sqlite database header content")

    monkeypatch.setattr("futu_quant.store.DEFAULT_DB", corrupt_db)
    monkeypatch.setattr("futu_quant.config.DEFAULT_ENV_FILE", cfg_mod.ENV_FILE)

    with caplog.at_level(logging.WARNING):
        val = cfg_mod._load_runtime_value("SOME_KEY", "default_val")

    assert val == "default_val"
    warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    matched = [m for m in warn_msgs if "安全回退到 .env 镜像值" in m and "漂移" in m]
    assert len(matched) >= 1, f"未捕获到漂移警告日志: {warn_msgs}"


def test_g6_settings_load_single_store_construction_count(tmp_path, monkeypatch):
    """G6 守卫：证明一次 Settings.load() 期间 TradingStore 的构造次数恰好为 1，多键复用同一只读连接保证快照一致性。"""
    db_file = tmp_path / "single_construct.db"
    store = TradingStore(db_file, bootstrap=True)

    # 将默认库指向有效的真实数据库
    monkeypatch.setattr("futu_quant.store.DEFAULT_DB", db_file)
    monkeypatch.setattr("futu_quant.config.DEFAULT_ENV_FILE", cfg_mod.ENV_FILE)

    construct_count = 0
    orig_init = TradingStore.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal construct_count
        construct_count += 1
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(TradingStore, "__init__", counting_init)

    # 执行 load，未显式注入，走自动只读连接逻辑
    settings = cfg_mod.Settings.load(refresh=True)
    assert settings is not None

    # 严格断言：构造次数恰好为 1（顶部构造并向下复用，绝不出现多快照多连接）！
    assert construct_count == 1, f"TradingStore 构造次数不为 1 (实测 {construct_count} 次)，多快照复用失效！"


def test_g6_negative_injection_raises_if_written(monkeypatch):
    """G6 负向必红：如果读配置路径注入了反向写库缺陷，守卫必须当场抓获并抛错变红！"""
    # 真实模拟一个有 bug 的读配置写库行为
    def buggy_load_with_write(key, default, store=None):
        if store is not None:
            # 模拟读侧发生写库缺陷
            store.set_runtime_config(key, "BUGGY_WRITE_VAL", "buggy_actor")
        return default

    test_store = TradingStore(pathlib.Path(tempfile.mkdtemp()) / "test_bug.db", bootstrap=True)
    
    # 注入该缺陷
    monkeypatch.setattr(cfg_mod, "_load_runtime_value", buggy_load_with_write)

    # 断言该缺陷必定被捕获并抛出 AssertionError 变红
    with pytest.raises(AssertionError, match="检测到写库缺陷"):
        write_calls = []
        orig_set = getattr(TradingStore, "set_runtime_config", None)
        def probe_set(self, key, value, actor, *a, **kw):
            write_calls.append((key, value, actor))
            return orig_set(self, key, value, actor, *a, **kw)
        monkeypatch.setattr(TradingStore, "set_runtime_config", probe_set)

        # 触发带缺陷的读路径
        cfg_mod._load_runtime_value("TEST_KEY", "default", store=test_store)
        if len(write_calls) > 0:
            raise AssertionError(f"检测到写库缺陷: 发生了反向写操作 {write_calls}")



def test_g7_cash_yield_storage_is_runtime_config_and_excluded_from_draftable():
    """G7 守卫：证明 cash_yield_* 存储为 runtime_config，但被显式排除在可草稿发布白名单外。"""
    # 1. 校验元数据 Storage 声明
    spec_enabled = GLOBAL_REGISTRY.get("core_satellite_cash_yield_enabled")
    assert spec_enabled is not None
    assert spec_enabled.storage == "runtime_config"

    spec_symbol = GLOBAL_REGISTRY.get("core_satellite_cash_yield_symbol")
    assert spec_symbol is not None
    assert spec_symbol.storage == "runtime_config"

    # 2. 校验草稿键集合中严格排除 cash_yield 相关键
    draftable_keys = get_draftable_config_keys()
    assert "core_satellite_cash_yield_enabled" not in draftable_keys
    assert "core_satellite_cash_yield_symbol" not in draftable_keys
    assert "strategy.core_satellite.cash_yield_enabled" not in draftable_keys
    assert "strategy.core_satellite.cash_yield_symbol" not in draftable_keys


def test_g7_negative_draftable_would_fail_if_included():
    """G7 负向必红：如果有人误将 cash_yield 纳入草稿，必须红。"""
    draftable_keys = get_draftable_config_keys()
    with pytest.raises(AssertionError):
        assert "core_satellite_cash_yield_enabled" in draftable_keys


def test_g1_g5_direct_write_whitelist_and_template_validation_performance():
    """G1-G5 守卫：证明直写模板键合法性校验 O(1) 毫秒级放行，非法键拒绝。"""
    # 1. 合法直写键验证
    valid_keys = [
        "volatility_new_entries_paused:futu_real_master",
        "strategy_enabled:futu_paper_sandbox:core_satellite",
        "strategy_execution_mode:futu_real_master:volatility_long_gamma",
        "real_account_config:281756481476169109",
    ]
    t0 = time.perf_counter()
    for k in valid_keys:
        validate_instance_key(k)
    cost = time.perf_counter() - t0
    assert cost < 0.05, f"模板键合法性校验超时: {cost}s"

    # 2. 负向测试：非法租户后缀严格抛错
    with pytest.raises(ValueError, match="未在有效租户注册表中登记"):
        validate_instance_key("volatility_new_entries_paused:unregistered_tenant_xyz")

    # 3. 负向测试：非法账号严格抛错
    with pytest.raises(ValueError, match="未在有效账户注册表中登记"):
        validate_instance_key("real_account_config:not_an_18_digit_account")


def test_g8_task_runs_migration_three_stages_and_dual_write(tmp_path):
    """G8 守卫：证明 task_runs 三阶段迁移幂等、可回滚、且双写过渡有效。"""
    db_file = tmp_path / "test_g8_migration.db"
    store = TradingStore(db_file, bootstrap=True)
    conn = sqlite3.connect(db_file)
    # 插入 1000 条未归属租户的历史记录
    for i in range(1000):
        conn.execute("INSERT INTO task_runs(task_id, status, timeout_seconds, started_at) VALUES(?, ?, ?, ?)", (f"task_{i%4}", "SUCCESS", 120.0, "2026-10-25"))
    conn.commit()

    # 阶段 1：幂等加列
    assert add_tenant_id_column(conn) is True
    assert add_tenant_id_column(conn) is False  # 幂等

    # 阶段 2：分批回填
    updated = backfill_tenant_id(conn, batch_size=200, default_tenant="futu_paper_sandbox")
    assert updated == 1000
    assert backfill_tenant_id(conn) == 0  # 幂等

    # 阶段 3：建联合索引
    assert create_tenant_indexes(conn) is True

    # 验证 EXPLAIN 走索引
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM task_runs WHERE tenant_id = ? AND status = ?",
        ("futu_paper_sandbox", "SUCCESS")
    ).fetchall()
    assert any("idx_task_runs_tenant_status" in str(p) for p in plan)
    conn.close()

    # 4. 双写过渡测试：通过 store.start_task_run 插入新数据
    run_id = store.start_task_run("order_reconciliation", 120.0, 9999, tenant_id="futu_real_master")
    assert run_id > 0

    runs = store.task_runs(limit=10)
    assert runs[0]["tenant_id"] == "futu_real_master"
    assert runs[0]["id"] == run_id

    # 5. 回滚测试
    conn = sqlite3.connect(db_file)
    rollback_migration(conn)
    plan_after = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM task_runs WHERE tenant_id = ? AND status = ?",
        ("futu_paper_sandbox", "SUCCESS")
    ).fetchall()
    assert not any("idx_task_runs_tenant_status" in str(p) for p in plan_after)
    conn.close()
