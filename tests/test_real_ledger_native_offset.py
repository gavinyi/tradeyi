"""实盘账本必须计入「本账户」原生底仓偏移（reconciliation_adjustments）。

背景：用户手中有未被任何策略认领的长线底仓（如实盘账户 8 股 NVDA、4.4344 股 GLD）。
`sync_virtual_allocation_to_target()` 会把它写成 reconciliation_adjustments 残差。
若实盘分支不读这张表，账本只剩策略成交流水（NVDA -1 / GLD -2），
对账每轮都会把用户自有底仓误判成策略丢单，熔断 core_satellite 并锁死卖盘。

本文件锁定三条契约：
1. 实盘账本必须计入本账户的 reconciliation_adjustments 偏移；
2. 偏移必须按账户/租户物理隔离，严禁跨租户串账（模拟租户不得被实盘偏移污染）；
3. 计入偏移后三账必须归零（MATCHED），不得误报差异、不得熔断策略。
"""
from __future__ import annotations

import json

import pandas as pd

from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore

REAL_ACCOUNT = "281756481476169109"
REAL_TENANT = "futu_real_master"
PAPER_ACCOUNT = "20170229"
PAPER_TENANT = "futu_paper_sandbox"


def _require_tenant_columns(store: TradingStore) -> None:
    """reconciliation_adjustments 必须带租户 + 账户维度，否则无法隔离偏移。"""
    with store.connection() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(reconciliation_adjustments)")}
    missing = {"tenant_id", "account_id"} - cols
    assert not missing, (
        f"reconciliation_adjustments 缺少租户/账户维度列 {sorted(missing)}："
        "无法按账户隔离原生底仓偏移，跨租户串账风险未解除"
    )


def _insert_intent(
    store: TradingStore, *, strategy_id: str, intent_key: str, symbol: str, side: str,
    filled_quantity: float, tenant_id: str, account_id: str, status: str = "FILLED",
) -> None:
    now = store.now()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO order_intents
               (strategy_id,intent_key,symbol,side,quantity,limit_price,status,
                broker_order_id,metadata_json,tenant_id,filled_quantity,average_fill_price,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (strategy_id, intent_key, symbol, side, int(filled_quantity), 100.0, status,
             f"brk-{intent_key}", json.dumps({"account_id": account_id}), tenant_id,
             filled_quantity, 100.0, now, now),
        )


def _insert_adjustment(
    store: TradingStore, *, strategy_id: str, symbol: str, quantity: float,
    tenant_id: str = "", account_id: str = "",
) -> None:
    _require_tenant_columns(store)
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO reconciliation_adjustments
               (strategy_id,symbol,quantity,reason,tenant_id,account_id,updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (strategy_id, symbol, quantity, "外部券商持仓调整", tenant_id, account_id, store.now()),
        )


def _qty(rows, symbol: str) -> float:
    for row in rows:
        if row["symbol"] == symbol:
            return float(row["quantity"])
    return 0.0


class _StubBroker:
    def __init__(self, positions: dict[str, float], account_id: str, env: str):
        self._positions = positions
        self.account_id = account_id
        self.trading_env = env

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"code": code, "qty": qty} for code, qty in self._positions.items()]
        )


def test_real_ledger_counts_own_account_native_offset(tmp_path):
    """实盘账本必须计入本账户 reconciliation_adjustments 偏移。"""
    store = TradingStore(tmp_path / "real_offset.db")
    # 策略成交流水：core_satellite 卖出 1 股 NVDA -> 账本 -1
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # 用户自有底仓偏移：+8（券商实际 7 = 账本 -1 + 8）
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=8.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )

    # 修复前：实盘分支不读 adjustments -> -1（错误）；修复后必须为 7
    assert _qty(store.virtual_positions(env="REAL", account_id=REAL_ACCOUNT), "US.NVDA") == 7.0
    # 按租户解析同样生效
    assert _qty(store.virtual_positions(tenant_id=REAL_TENANT), "US.NVDA") == 7.0


def test_real_ledger_offset_isolated_by_account(tmp_path):
    """偏移必须按账户隔离：实盘偏移不得污染模拟租户账本。"""
    store = TradingStore(tmp_path / "real_iso.db")
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # 实盘账户偏移 +8
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=8.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # 模拟账户偏移 +3（刻意取不同值，便于识别串账）
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=3.0,
        tenant_id=PAPER_TENANT, account_id=PAPER_ACCOUNT,
    )

    # 实盘视角：只能看到自己的 +8（成交流水 -1 只属于实盘）
    assert _qty(store.virtual_positions(env="REAL", account_id=REAL_ACCOUNT), "US.NVDA") == 7.0
    # 模拟视角：只能看到自己的 +3，绝不能被实盘 +8 污染（否则为 10 或 11）
    assert _qty(store.virtual_positions(env="SIMULATE", account_id=PAPER_ACCOUNT), "US.NVDA") == 3.0


def test_reconcile_real_native_holdings_zeroes_three_ledgers(tmp_path):
    """端到端：计入偏移后三账归零，不得误报差异、不得熔断策略。"""
    store = TradingStore(tmp_path / "real_reconcile.db")
    # NVDA：策略卖出 1 股（账本 -1）+ 用户自有底仓 +8 -> 账本 7，券商 7
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=8.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # GLD：策略卖出 2 股（账本 -2）+ 用户自有底仓 +4.4344 -> 账本 2.4344，券商 2.4344
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:gld:REAL", symbol="US.GLD",
        side="SELL", filled_quantity=2, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.GLD", quantity=4.4344,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )

    broker = _StubBroker({"US.NVDA": 7.0, "US.GLD": 2.4344}, REAL_ACCOUNT, "REAL")
    report = reconcile_ledgers(store, broker, tenant_id=REAL_TENANT)

    assert report[report["status"] == "MISMATCH"].empty, report.to_dict("records")
    # 策略不得被对账熔断
    assert store.get_setting("strategy_reconciliation_disabled:futu_real_master:core_satellite", "false") != "true"
    assert store.get_setting(f"recovery_blocked:{REAL_ACCOUNT}", "false") == "false"


def test_legacy_empty_tenant_adjustments_do_not_leak_into_real_ledger(tmp_path):
    """Reviewer 陷阱守卫：legacy 空租户/空账户偏移行（7 月旧值）绝不得进入实盘账户账本。

    生产里 reconciliation_adjustments 躺着 NVDA +211 / SPY +425 / QQQ +277 的 7 月旧值；
    若实盘分支无条件吸收，账本会瞬间多出 3 个新 MISMATCH（210 / 425 / 277），
    把熔断标的从 2 个扩到 5 个。
    """
    store = TradingStore(tmp_path / "legacy_guard.db")
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=8.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # legacy 空租户旧值 +211（迁移后落入 ''/'' 分区）
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=211.0,
    )

    # 实盘账本必须仍为 7.0；若被 +211 污染会变成 218.0
    assert _qty(store.virtual_positions(env="REAL", account_id=REAL_ACCOUNT), "US.NVDA") == 7.0


def test_empty_tenant_virtual_allocation_does_not_leak_into_real_ledger(tmp_path):
    """Reviewer 隔离守卫：空租户 virtual_allocations 行不得跨租户串入实盘账本。"""
    store = TradingStore(tmp_path / "valloc_guard.db")
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.NVDA", quantity=8.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # 空租户历史行（模拟侧遗留）
    store.set_virtual_allocation("core_satellite", "US.NVDA", 50.0, 100.0, tenant_id="")

    assert _qty(store.virtual_positions(env="REAL", account_id=REAL_ACCOUNT), "US.NVDA") == 7.0


def test_legacy_orphan_adjustments_never_enter_any_ledger_view(tmp_path):
    """Chief 点名的隐患：迁到 ''/'' legacy 分区的旧行不得被任何账本视图重新算进去。

    实测：`virtual_positions()` 无参调用时 adj_param 退化为 ''，
    `WHERE account_id = ''` 会重新把 legacy 行 SUM 进账本（SPY 425 复现）。
    带账户的调用已安全；无参调用方（risk.py:167 / option_lifecycle / connectivity_protection
    / pages/8_runtime_log）仍会读到孤儿行。修法二选一：迁移时作废或改挂到具体租户，
    或让无参视图同样排除空租户孤儿行。
    """
    store = TradingStore(tmp_path / "orphan_guard.db")
    _insert_adjustment(
        store, strategy_id="core_satellite", symbol="US.SPY", quantity=425.0,
    )

    rows = store.virtual_positions()
    spy = [r for r in rows if r["symbol"] == "US.SPY"]
    assert spy == [], f"legacy 孤儿偏移行泄漏进无参账本视图：{spy}"


def test_manual_reconcile_sync_writes_account_scoped_offset(tmp_path):
    """一键平账（UI 路径）写入的偏移必须带账户维度，且只影响该账户。"""
    store = TradingStore(tmp_path / "manual_sync.db")
    _insert_intent(
        store, strategy_id="core_satellite", intent_key="core:nvda:REAL", symbol="US.NVDA",
        side="SELL", filled_quantity=1, tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )

    result = store.sync_virtual_allocation_to_target(
        "core_satellite", "US.NVDA", 7.0, 100.0,
        tenant_id=REAL_TENANT, account_id=REAL_ACCOUNT,
    )
    # 券商 7 - 策略成交净量(-1) = 原生底仓偏移 8
    assert result["residual"] == 8.0

    with store.connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT tenant_id, account_id, quantity FROM reconciliation_adjustments "
            "WHERE strategy_id=? AND symbol=?", ("core_satellite", "US.NVDA"),
        ).fetchall()]
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == REAL_TENANT, "一键平账偏移未带租户维度，会落 legacy 分区"
    assert rows[0]["account_id"] == REAL_ACCOUNT, "一键平账偏移未带账户维度，会落 legacy 分区"

    # 只影响该账户：实盘账本与券商归零差异
    assert _qty(store.virtual_positions(env="REAL", account_id=REAL_ACCOUNT), "US.NVDA") == 7.0
    # 模拟账户不受实盘平账影响
    assert _qty(store.virtual_positions(env="SIMULATE", account_id=PAPER_ACCOUNT), "US.NVDA") == 0.0


def test_launch_approval_page_passes_tenant_and_account_to_sync():
    """静态守卫：UI 一键平账必须把租户/账户传给 sync_virtual_allocation_to_target。

    漏传会把偏移写进 ('','') legacy 分区——对真实账户账本毫无作用，
    又会落回无账户视图，把刚清掉的 211/425/277 重新请回来。
    """
    import ast
    from pathlib import Path

    source = Path("pages/9_launch_approval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sync_virtual_allocation_to_target"
    )
    keywords = {kw.arg for kw in call.keywords}
    assert "tenant_id" in keywords, "一键平账未传 tenant_id，偏移会落 legacy 分区"
    assert "account_id" in keywords, "一键平账未传 account_id，偏移会落 legacy 分区"


def test_adjustment_table_requires_tenant_and_account_columns(tmp_path):
    """契约守卫：reconciliation_adjustments 必须具备租户/账户维度。"""
    store = TradingStore(tmp_path / "schema_guard.db")
    _require_tenant_columns(store)  # 迁移落地前必须在此明确失败
