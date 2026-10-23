"""持仓采用时效与止损口径 —— P0 验收守卫（Architect 五条）。

背景（Architect 实测 + Product 规格）：
- `order_push.py` 的 `DealPushHandler` 只处理 SIMULATE 环境成交，REAL 成交被过滤
  → `record_fill` 从不执行 → 真金开仓存在**无保护窗口**，只能等轮询兜底；
- 雷达滚动路径（`volatility_scanner`）没有「保留在险合约」护栏，会把已有券商持仓的
  campaign 顶掉 → 仓位瞬间无归属 → 止损评估看不见它。

本文件逐条对应 Architect 的验收项，补丁落地前应当为红。
"""
from __future__ import annotations

import ast
from types import SimpleNamespace
from pathlib import Path

import pandas as pd


REAL_TENANT = "futu_real_master"
# #1 键级分区后 ADOPT 写的是**租户键**，断言要读那个键（全局键里本来就该是空的）
tenant_key = f"VOLATILITY_CAMPAIGNS:{REAL_TENANT}"


def _broker_with_tenant():
    """生产里 handler 一定有 broker（open_order_push(broker, store)）。"""
    return SimpleNamespace(tenant_id=REAL_TENANT, account_id="281756481476169109")


def _fake_deal_row(trd_env: str, order_id: str = "brk-1", code: str = "US.AMZN261120P205000"):
    return {
        "order_id": order_id,
        "deal_id": "deal-1",
        "code": code,
        "trd_side": "BUY",
        "trd_env": trd_env,
        "order_status": "ORDERSTATUS_FILLED",
        "dealt_qty": 1.0,
        "dealt_avg_price": 10.0,
        "qty": 1.0,
        "price": 10.0,
        "commission": 0.0,
    }


def test_real_env_deal_push_records_fill(monkeypatch, tmp_path):
    """验收 1：REAL 环境成交必须进入 record_fill（不再被 SIMULATE 过滤）。"""
    from futu import RET_OK

    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "push_real.db")
    recorded: list[tuple] = []
    monkeypatch.setattr(store, "record_fill", lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(
        store, "intent_by_broker_order", lambda _oid: {"id": 7, "metadata_json": "{}"}
    )

    monkeypatch.setattr(
        op.TradeDealHandlerBase, "on_recv_rsp",
        lambda self, _rsp: (RET_OK, pd.DataFrame([_fake_deal_row("REAL")])),
    )

    op.DealPushHandler(store, _broker_with_tenant()).on_recv_rsp(object())

    assert recorded, "REAL 环境成交未进入 record_fill —— 真金侧仍存在无保护窗口"


def test_simulate_env_deal_push_still_records_fill(monkeypatch, tmp_path):
    """回归：SIMULATE 成交的既有行为不得被破坏。"""
    from futu import RET_OK

    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "push_sim.db")
    recorded: list[tuple] = []
    monkeypatch.setattr(store, "record_fill", lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(
        store, "intent_by_broker_order", lambda _oid: {"id": 7, "metadata_json": "{}"}
    )
    monkeypatch.setattr(
        op.TradeDealHandlerBase, "on_recv_rsp",
        lambda self, _rsp: (RET_OK, pd.DataFrame([_fake_deal_row("SIMULATE")])),
    )

    op.DealPushHandler(store, _broker_with_tenant()).on_recv_rsp(object())

    assert recorded, "SIMULATE 成交的 record_fill 行为被破坏（回归）"


def test_fill_with_active_campaign_registers_immediately(monkeypatch, tmp_path):
    """验收 2：成交时该 code 有活跃 campaign → 同一次推送内标记登记，不等周期。"""
    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    assert hasattr(op, "register_fill_to_campaign"), (
        "缺少 register_fill_to_campaign —— 成交即登记未实现"
    )
    store = TradingStore(tmp_path / "register.db")
    op.register_fill_to_campaign(store, None, {"id": 7, "symbol": "US.AMZN261120P205000"}, 1, 10.0)


def test_fill_without_owner_adopts_immediately(monkeypatch, tmp_path):
    """验收 3：真实成交（intent 带券商订单号）无归属 → 当场建 exit-only campaign。

    ⚠️ 必须隔离：`upsert_campaign` 若不接 store 会自建 `TradingStore()` 直连生产库。
    本用例此前因此把测试合约写进了**生产 campaign 配置**（10-23 实测到一条幽灵）。
    """
    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    assert hasattr(op, "register_fill_to_campaign"), (
        "缺少 register_fill_to_campaign —— 无归属当场 ADOPT 未实现"
    )
    store = TradingStore(tmp_path / "adopt.db")
    op.register_fill_to_campaign(
        store, None,
        {"id": 7, "symbol": "US.AMZN261120P205000", "broker_order_id": "FH-TEST-1"}, 1, 10.0,
        REAL_TENANT,
    )
    campaigns = store.campaigns(tenant_id=REAL_TENANT)
    adopted = [c for c in campaigns if c.get("code") == "US.AMZN261120P205000"]
    assert adopted, "无归属的成交未当场建 exit-only campaign"
    assert adopted[0].get("exit_only") is True, "采用产生的 campaign 必须是 exit_only"
    assert adopted[0].get("orphan_adoption_verified") is False, "未经券商确认必须显式标注"


def test_adoption_does_not_write_production_config(tmp_path):
    """隔离守卫：采用流程必须写进调用方传入的 store，而不是自建的那个生产库。

    10-23 事故：`upsert_campaign` 自建 `TradingStore()` 直连生产库，
    导致守卫测试在生产配置里造出一条幽灵 campaign（无 intent/fill/券商持仓）。

    断言直接读**生产库**（`runtime_config` 表），不走 `load_volatility_campaigns()`
    —— 后者在 `ENV_FILE` 被别的用例改写时会读临时 `.env`，不能作为生产真值。
    """
    import json
    import sqlite3

    from futu_quant import order_push as op
    from futu_quant.store import DEFAULT_DB, TradingStore

    store = TradingStore(tmp_path / "isolated.db")
    op.register_fill_to_campaign(
        store, None,
        {"id": 99, "symbol": "US.AMZN261120P205000", "broker_order_id": "FH-ISO-1"}, 1, 10.0,
        REAL_TENANT,
    )
    # 必须落在传入的库上
    adopted = [c for c in store.campaigns(tenant_id=REAL_TENANT)
               if c.get("code") == "US.AMZN261120P205000"]
    assert adopted, "采用结果未写进调用方传入的 store"
    # 生产库不得出现该测试合约
    with sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True) as con:
        row = con.execute(
            "SELECT value_json FROM runtime_config WHERE config_key='VOLATILITY_CAMPAIGNS'"
        ).fetchone()
    prod = json.loads(row[0]) if row else []
    assert not [c for c in prod if c.get("code") == "US.AMZN261120P205000"], (
        "测试合约被写进了生产 campaign 配置 —— upsert_campaign 仍自建生产 store"
    )


def test_fill_without_broker_order_does_not_adopt(tmp_path):
    """没有券商订单号的 intent 不可能是真实成交 → 不得建 campaign（防幽灵）。"""
    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "no_broker_order.db")
    result = op.register_fill_to_campaign(
        store, None, {"id": 7, "symbol": "US.AMZN261120P205000"}, 1, 10.0, REAL_TENANT,
    )
    assert result.get("action") == "SKIPPED_NO_BROKER_ORDER", f"竟采用了无券商订单号的 intent：{result}"
    assert not [
        c for c in store.campaigns(tenant_id=REAL_TENANT)
        if c.get("code") == "US.AMZN261120P205000"
    ], "出现了幽灵 campaign"


def test_fill_not_in_broker_positions_does_not_create_phantom_campaign(tmp_path):
    """幽灵 campaign 守卫：券商持仓快照里没有该合约时，**不得**建 campaign。

    10-22 生产实测：`US.AMZN261120P205000` 被推送路径采用，但券商从未持有它
    （0 条 intent、0 条 fill、0 条 decision）。它既无真实持仓、也无策略记录，
    只能每轮被静默跳过 —— 一个纯幽灵。
    """
    import pandas as pd

    from futu_quant import order_push as op
    from futu_quant.store import TradingStore

    class _EmptyBroker:
        account_id = "281756481476169109"

        def positions(self):
            return pd.DataFrame(columns=["code", "qty", "can_sell_qty"])

    store = TradingStore(tmp_path / "no_phantom.db")
    result = op.register_fill_to_campaign(
        store, _EmptyBroker(), {"id": 7, "symbol": "US.AMZN261120P205000"}, 1, 10.0, REAL_TENANT,
    )
    assert result is None or result.get("action") == "SKIPPED_UNVERIFIED", (
        f"券商未确认的成交竟建立了 campaign：{result}"
    )
    campaigns = store.campaigns(tenant_id=REAL_TENANT)
    assert not [c for c in campaigns if c.get("code") == "US.AMZN261120P205000"], (
        "出现了幽灵 campaign —— 券商未持有该合约"
    )
    events = [
        e for e in store.operational_events(limit=50)
        if e["event_type"] == "FILL_ADOPTION_SKIPPED_UNVERIFIED"
    ]
    assert events, "跳过未确认采用时未留 WARN 事件"


def _count_calls(path: Path, func_name: str) -> int:
    """统计源码里对某个函数的**实际调用点**数量（不是名字出现次数）。

    只查名字出现会空转：import 了但没调用照样"通过"。必须查 Call 节点。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == func_name:
            count += 1
        elif isinstance(func, ast.Attribute) and func.attr == func_name:
            count += 1
    return count


def test_radar_roll_path_uses_shared_retain_invariant():
    """验收 4：雷达滚动路径必须**调用**共享不变量（持有券商持仓或挂单的 campaign 永不被删）。

    检查的是**调用点**而非名字出现——只 import 不调用等于护栏不存在。
    """
    scanner = Path("src/futu_quant/volatility_scanner.py")
    worker = Path("scripts/run_volatility_strategy.py")

    assert _count_calls(worker, "retain_campaign_if_at_risk") >= 1, (
        "worker 滚动路径未调用共享不变量 retain_campaign_if_at_risk"
    )
    assert _count_calls(scanner, "retain_campaign_if_at_risk") >= 1, (
        "雷达滚动路径未调用共享不变量 —— 在险 campaign 会被顶掉，仓位瞬间无归属"
    )


def test_protection_coverage_metric_exists():
    """验收 5：保护覆盖率必须成为常驻指标（任一时刻可解析的期权券商持仓都有归属）。"""
    from futu_quant import volatility_orphans as vo

    assert hasattr(vo, "protection_coverage"), (
        "缺少保护覆盖率指标 —— 覆盖率低于 1.0 必须能当场告警，而不是靠人工发现"
    )
