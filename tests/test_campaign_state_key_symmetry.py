"""P0-9 守卫：campaign state 的**写读必须对称** —— 写进去的键不得被清理逻辑删掉。

背景（10-23 Architect 定位，每一轮都在发生）：
`order_push` 写的是**带租户**的键（跨租户隔离所需）：
    volatility_long_gamma:{tenant_id}:{code}
而清理侧只认**不带租户**的形态：
    active_keys = { f"volatility_long_gamma:{code}" ... }
    prune_campaign_states("volatility_long_gamma:", active_keys)
    → DELETE WHERE state_key LIKE 'volatility_long_gamma:%' AND state_key NOT IN active_keys

**后果**：P0-1 的「成交即登记」（`registered_fill_price` 等）写进去，
**同一个周期就被当 stale 删掉** —— P0-1 的全部意义被这一处抵消。

修法：抽共享助手 `volatility_state_keys()`，一次性产出带租户与不带租户两种形态；
写侧、清理侧、保护侧都从它取（各自拼字符串就是下一次不对称的起点）。

本文件按 Tester 要求做**端到端**断言：不是断言某个变量，而是
「`order_push` 写进去的状态键，经过一个完整周期后仍然存在」。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.order_push import register_fill_to_campaign  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.volatility_orphans import (  # noqa: E402
    active_volatility_state_keys,
    protected_volatility_state_keys,
    volatility_state_keys,
)

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
CODE = "US.AMZN261028P250000"
UNDERLYING = "US.AMZN"


def _broker(tenant_id: str, account_id: str = "281756481476169109"):
    return SimpleNamespace(tenant_id=tenant_id, account_id=account_id)


def test_state_key_helper_emits_both_forms():
    """共享助手必须同时产出带租户与不带租户两种形态。"""
    keys = volatility_state_keys(CODE, REAL_TENANT, UNDERLYING)
    assert f"volatility_long_gamma:{CODE}" in keys, "缺少历史形态"
    assert f"volatility_long_gamma:{REAL_TENANT}:{CODE}" in keys, "缺少带租户形态"
    assert f"volatility_long_gamma:signal:{UNDERLYING}" in keys
    assert f"volatility_long_gamma:{REAL_TENANT}:signal:{UNDERLYING}" in keys


def test_no_tenant_still_emits_legacy_form():
    """无租户上下文时只产出历史形态（向后兼容）。"""
    keys = volatility_state_keys(CODE, "", UNDERLYING)
    assert f"volatility_long_gamma:{CODE}" in keys
    assert not any(REAL_TENANT in k for k in keys)


def _write_tenant_scoped_state(store, symbol: str, tenant: str, order_id: str) -> str:
    """让 `order_push` 真的写出带租户的 state 键。

    注意：带租户的键写在**已有归属**（MARKED）分支 —— 首次调用是 ADOPT
    （只建 campaign、不写 state）。所以要两次调用：第一次建归属，第二次写 state。
    """
    register_fill_to_campaign(
        store, None, {"id": 1, "symbol": symbol, "broker_order_id": f"FH-{order_id}-A"},
        1, 10.0, tenant,
    )
    result = register_fill_to_campaign(
        store, None, {"id": 2, "symbol": symbol, "broker_order_id": f"FH-{order_id}-B"},
        1, 10.0, tenant,
    )
    assert result["action"] == "MARKED", f"未走到 MARKED 分支：{result}"
    key = f"volatility_long_gamma:{tenant}:{symbol}"
    assert key in store.campaign_states(), "MARKED 分支未写出带租户的 state 键"
    return key


def test_written_key_survives_a_full_cycle(tmp_path):
    """**端到端核心**：order_push 写进去的键，经过完整周期后必须仍然存在。

    复刻 run_once 的清理序列：写键 → 按 campaign 构建 active_keys → prune。
    """
    store = TradingStore(tmp_path / "p09.db")
    written = _write_tenant_scoped_state(store, CODE, REAL_TENANT, "P09")

    # 复刻 run_once 的 active_keys 构建 —— **调生产用的同一个共享函数**，
    # 不再自己拼（自己拼就是空转守卫：生产改错，守卫照样绿）。
    campaigns = [{"code": CODE, "underlying": UNDERLYING}]
    tenant_id = REAL_TENANT
    active_keys = active_volatility_state_keys(
        campaigns, tenant_id,
        protected_volatility_state_keys(
            store, None, None, _broker(tenant_id), tenant_id=tenant_id,
        ),
    )
    assert written in active_keys, "带租户的键未被 active_keys 认得（P0-9 未修）"

    # 执行清理
    store.prune_campaign_states("volatility_long_gamma:", active_keys)

    assert written in store.campaign_states(), (
        "🔴 P0-9 回归：刚写进去的状态键被 prune_campaign_states 删掉了 —— "
        "P0-1 的「成交即登记」被完全抵消"
    )


def test_production_cleanup_calls_shared_helper_with_tenant():
    """**调用点断言**：生产的清理逻辑必须把 tenant_id 传进去。

    为什么需要这条：只测共享函数是不够的 —— 生产可以把 tenant_id 传成 ""
    而函数本身仍然正确（10-23 变异测试实测：那样改，只测函数的守卫照样绿）。
    所以必须钉住**调用点传的是租户变量**，不只是"函数存在"。
    """
    import ast

    src = (ROOT / "scripts" / "run_volatility_strategy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    run_once = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_once"
    )
    seg = ast.dump(run_once)
    assert "active_volatility_state_keys" in seg, "生产清理未调用共享函数"
    assert "_tenant_id" in seg, "生产清理未取租户上下文"
    # 调用必须把 _tenant_id 作为第二个参数传入（不得是空串或缺失）
    call = next(
        n for n in ast.walk(run_once)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "active_volatility_state_keys"
    )
    assert len(call.args) >= 2, "调用缺少 tenant_id 参数"
    assert isinstance(call.args[1], ast.Name) and call.args[1].id == "_tenant_id", (
        f"第二个参数必须是 _tenant_id，实际是 {ast.dump(call.args[1])[:80]} —— "
        "传空串会让带租户的键被当成 stale 删掉（P0-9 原缺陷）"
    )


def test_protected_keys_cover_tenant_scoped_form(tmp_path):
    """P0-9 第二处漏：保护键必须覆盖带租户形态（否则「有持仓就不许剪」失效）。"""
    import pandas as pd

    store = TradingStore(tmp_path / "protect.db")
    positions = pd.DataFrame([{"code": CODE, "qty": 1.0}])
    protected = protected_volatility_state_keys(
        store, positions, None, _broker(REAL_TENANT), tenant_id=REAL_TENANT,
    )
    assert f"volatility_long_gamma:{REAL_TENANT}:{CODE}" in protected, (
        "带租户形态未受保护 —— 有券商持仓时它会被剪掉"
    )
    assert f"volatility_long_gamma:{CODE}" in protected


def test_other_tenants_keys_are_still_prunable(tmp_path):
    """回归：别的租户的键仍应可被清理（不得因修复而永不清理）。"""
    store = TradingStore(tmp_path / "other.db")
    other = _write_tenant_scoped_state(store, CODE, PAPER_TENANT, "P09B")
    real_key = _write_tenant_scoped_state(store, CODE, REAL_TENANT, "P09C")

    # 实盘租户的 active_keys 不应包含模拟租户的键 → 应被清理
    campaigns = [{"code": CODE, "underlying": UNDERLYING}]
    active_keys = set()
    for item in campaigns:
        active_keys |= volatility_state_keys(item["code"], REAL_TENANT, UNDERLYING)
    store.prune_campaign_states("volatility_long_gamma:", active_keys)
    assert other not in store.campaign_states(), "别的租户的键未被清理"
    assert real_key in store.campaign_states(), "本租户的键被误删"
