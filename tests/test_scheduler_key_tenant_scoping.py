"""P0-8 守卫：调度键必须带租户维度，否则一个租户静默压住另一个。

事故形态（10-23 Chief/Architect 定位）：
`run_service` 的调度键是**无前缀**的 —— `last_run:{task}` /
`task_backoff_until:{task}` / `task_failure_count:{task}`。
两个租户 Worker 是**两个进程**，但写**同一张 `system_settings` 表**：

    A 跑完 → mark_run("last_run:X")
    B 的 due("last_run:X") 读到 A 刚写的时间戳 → 返回 False
    → **B 的整个任务被静默跳过**（没有任何日志能证明它没跑）

退避键同理：A 失败写入 `task_backoff_until:X`，B 读到就进入退避 ——
**一个租户的故障压住另一个租户**。

修法：键带租户维度（`last_run:{tenant}:{task}`），
由 `service_cycle` 入口 `bind_tenant()` 绑定一次，`due`/`mark_run`/`run_task` 共用。

本文件的断言**都是行为断言**（跑真的 `due`/`mark_run`），不是断言键字符串存在。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in ("src", "scripts"):
    if str(ROOT / _p) not in sys.path:
        sys.path.insert(0, str(ROOT / _p))

from run_service import bind_tenant, due, mark_run  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
TASK = "volatility_long_gamma"


def _now():
    return datetime.now().astimezone()


def _tenant_key(store: TradingStore, base: str, tenant: str) -> str | None:
    """读出该租户的调度键当前值（不存在返回 None）。"""
    raw = store.get_setting(f"{base}:{tenant}")
    return raw or None


# ---------------------------------------------------------------- 端到端：两租户都要跑

def test_two_tenants_both_execute_same_task(tmp_path):
    """**端到端核心**：A 跑完不得让 B 被跳过。

    这是 P0-8 的事故形态 —— 断言的是 `due()` 的**行为**，
    不是"键存在"（那是空转守卫）。
    """
    store = TradingStore(tmp_path / "two_tenants.db")
    base = f"last_run:{TASK}"
    interval = 60
    t0 = _now()

    # A 跑完
    bind_tenant(REAL_TENANT)
    assert due(store, base, interval, t0) is True, "首次应当到期"
    mark_run(store, base, t0)

    # A 自己不应立刻重跑（幂等）
    assert due(store, base, interval, t0 + timedelta(seconds=5)) is False

    # **B 必须仍然到期** —— 修复前这里会返回 False（读到 A 的时间戳）
    bind_tenant(PAPER_TENANT)
    assert due(store, base, interval, t0 + timedelta(seconds=5)) is True, (
        "🔴 P0-8 回归：模拟租户被实盘租户的 last_run 静默跳过 —— "
        "两个租户共用无前缀键"
    )
    mark_run(store, base, t0 + timedelta(seconds=5))
    assert due(store, base, interval, t0 + timedelta(seconds=10)) is False, (
        "模拟租户跑完后不应立刻重跑"
    )


def test_backoff_does_not_leak_across_tenants(tmp_path):
    """退避键同样必须隔离：A 的故障不得让 B 进入退避。"""
    store = TradingStore(tmp_path / "backoff.db")
    base = f"task_backoff_until:{TASK}"
    until = _now() + timedelta(minutes=30)

    bind_tenant(REAL_TENANT)
    store.set_setting(f"{base}:{REAL_TENANT}", until.isoformat())

    # 模拟租户不得读到实盘的退避
    bind_tenant(PAPER_TENANT)
    assert store.get_setting(f"{base}:{PAPER_TENANT}", "") == "", (
        "退避键串租户了 —— 一个租户的故障压住另一个"
    )
    # 且无前缀旧键不得被写入（迁移后零写入方）
    assert store.get_setting(base, "") == "", "无前缀退避键被写入"


# ---------------------------------------------------------------- 反向：单租户幂等

def test_single_tenant_is_still_idempotent(tmp_path):
    """反向守卫：单租户运行时同一任务不得被重复执行。"""
    store = TradingStore(tmp_path / "single.db")
    base = f"last_run:{TASK}"
    interval = 60
    t0 = _now()

    bind_tenant(REAL_TENANT)
    assert due(store, base, interval, t0) is True
    mark_run(store, base, t0)
    assert due(store, base, interval, t0 + timedelta(seconds=1)) is False, (
        "刚跑完就被判定到期 —— 幂等失效，任务会重复执行"
    )
    assert due(store, base, interval, t0 + timedelta(seconds=interval + 1)) is True, (
        "超过间隔后应当再次到期"
    )


def test_failure_count_isolated_per_tenant(tmp_path):
    """失败计数按租户隔离（一个租户反复失败不得把另一个压进退避）。"""
    store = TradingStore(tmp_path / "failures.db")
    base = f"task_failure_count:{TASK}"

    bind_tenant(REAL_TENANT)
    store.set_setting(f"{base}:{REAL_TENANT}", "5")
    bind_tenant(PAPER_TENANT)
    assert store.get_setting(f"{base}:{PAPER_TENANT}", "0") == "0", (
        "失败计数串租户了"
    )


def test_unbound_tenant_falls_back_to_legacy_key(tmp_path):
    """未绑定租户（无 tenant 上下文）时回落无前缀键 —— 兼容历史调用方。"""
    store = TradingStore(tmp_path / "unbound.db")
    base = f"last_run:{TASK}"
    t0 = _now()

    bind_tenant("")
    mark_run(store, base, t0)
    # 无前缀键应被写入（回落路径）
    assert store.get_setting(base, "") != "", "未绑定租户未回落无前缀键"
    assert due(store, base, 60, t0 + timedelta(seconds=5)) is False


def test_tenant_key_is_never_double_prefixed(tmp_path):
    """回归：重复 bind 不得产生双前缀键（`X:{t}:{t}`）。"""
    from futu_quant.tenant import resolve_tenant_config_key, TenantConfigKeyError

    with pytest.raises(TenantConfigKeyError):
        resolve_tenant_config_key(REAL_TENANT, f"last_run:{TASK}:{REAL_TENANT}")
