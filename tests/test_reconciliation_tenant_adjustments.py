from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore


REAL_ACC = "281756481476169109"
PAPER_ACC = "20170229"


class AccountBroker:
    """按账户隔离的券商桩：positions 只反映该账户的真实持仓。"""

    def __init__(self, env, account_id, positions: dict[str, float]):
        self.trading_env = env
        self.account_id = account_id
        self.settings = SimpleNamespace(trading_env=env)
        self._positions = pd.DataFrame(
            [{"code": code, "qty": qty} for code, qty in positions.items()]
        )
        self._orders = pd.DataFrame()

    def positions(self):
        return self._positions

    def orders(self):
        return self._orders

    def snapshots(self, symbols):
        return pd.DataFrame(
            [{"code": s, "bid_price": 100.0, "ask_price": 101.0, "last_price": 100.5} for s in symbols]
        )

    def account_info(self):
        return pd.DataFrame([{"total_assets": 56729.0, "power": 28364.0, "unrealized_pl": 0.0}])

    @staticmethod
    def is_cancellable_status(status):
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


def _fill_real_sell(store, symbol: str, qty: float, price: float, acc: str = REAL_ACC) -> None:
    intent, _ = store.create_intent(
        "core_satellite", f"core:{symbol}:sell:{acc}:REAL", symbol, "SELL", int(qty), price,
        {"account_id": acc, "reference_price": price},
        tenant_id="futu_real_master",
    )
    order_id = f"ord-{intent['id']}"
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id=order_id)
    store.update_by_broker_order(order_id, "FILLED", qty, price, 0.0)


def test_reconciliation_adjustments_have_tenant_and_account_dimension(tmp_path):
    store = TradingStore(tmp_path / "adj.db")
    with store.connection() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(reconciliation_adjustments)")}
    assert "tenant_id" in cols
    assert "account_id" in cols


def test_real_ledger_includes_own_account_native_base_holding_offset(tmp_path):
    """实盘账本必须计入本账户的原生底仓偏移，三账归零。"""
    store = TradingStore(tmp_path / "native.db")
    # 实盘 10-19 真实卖出 1 股 NVDA / 2 股 GLD（PAPER 侧测试买入不计入实盘）
    _fill_real_sell(store, "US.NVDA", 1, 190.0)
    _fill_real_sell(store, "US.GLD", 2, 250.0)

    # 以券商实际持仓为基准校准：NVDA 7 股、GLD 2.4344 股
    # 预期原生底仓偏移：NVDA = 7 - (-1) = 8，GLD = 2.4344 - (-2) = 4.4344
    nvda_sync = store.sync_virtual_allocation_to_target(
        "core_satellite", "US.NVDA", 7.0, 0.0,
        tenant_id="futu_real_master", account_id=REAL_ACC,
    )
    gld_sync = store.sync_virtual_allocation_to_target(
        "core_satellite", "US.GLD", 2.4344, 0.0,
        tenant_id="futu_real_master", account_id=REAL_ACC,
    )
    assert nvda_sync["residual"] == pytest.approx(8.0)
    assert gld_sync["residual"] == pytest.approx(4.4344)

    broker = AccountBroker("REAL", REAL_ACC, {"US.NVDA": 7.0, "US.GLD": 2.4344})
    report = reconcile_ledgers(store, broker, tenant_id="futu_real_master")

    assert not report.empty
    assert report[report["status"] == "MISMATCH"].empty
    assert store.get_setting(f"recovery_blocked:{REAL_ACC}") == "false"

    ledger = {r["symbol"]: float(r["quantity"]) for r in store.virtual_positions(env="REAL", account_id=REAL_ACC)}
    assert ledger["US.NVDA"] == pytest.approx(7.0)
    assert ledger["US.GLD"] == pytest.approx(2.4344)


def test_legacy_empty_account_adjustment_does_not_pollute_known_account(tmp_path):
    """legacy 空账户偏移行绝不污染已识别账户的账本。"""
    store = TradingStore(tmp_path / "legacy.db")
    # 历史遗留：空账户 NVDA 268 股
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO reconciliation_adjustments
            (tenant_id,account_id,strategy_id,symbol,quantity,reason,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            ("", "", "core_satellite", "US.NVDA", 268.0, "外部券商持仓调整", store.now()),
        )
    # 实盘账户仅有自己的偏移
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO reconciliation_adjustments
            (tenant_id,account_id,strategy_id,symbol,quantity,reason,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            ("futu_real_master", REAL_ACC, "core_satellite", "US.NVDA", 8.0, "外部券商持仓调整", store.now()),
        )

    real_ledger = store.virtual_positions(env="REAL", account_id=REAL_ACC)
    nvda = [r for r in real_ledger if r["symbol"] == "US.NVDA"]
    assert len(nvda) == 1
    assert float(nvda[0]["quantity"]) == pytest.approx(8.0)

    # 模拟账户同样不得吸收实盘偏移
    paper_ledger = store.virtual_positions(env="SIMULATE", account_id=PAPER_ACC)
    assert [r for r in paper_ledger if r["symbol"] == "US.NVDA"] == []


def test_paper_adjustment_does_not_leak_into_real_ledger(tmp_path):
    """模拟租户的偏移不得计入实盘账本（反之亦然）。"""
    store = TradingStore(tmp_path / "cross.db")
    store.sync_virtual_allocation_to_target(
        "core_satellite", "US.SPY", 100.0, 0.0,
        tenant_id="futu_paper_sandbox", account_id=PAPER_ACC,
    )
    real_ledger = store.virtual_positions(env="REAL", account_id=REAL_ACC)
    assert [r for r in real_ledger if r["symbol"] == "US.SPY"] == []

    paper_ledger = store.virtual_positions(env="SIMULATE", account_id=PAPER_ACC)
    spy = [r for r in paper_ledger if r["symbol"] == "US.SPY"]
    assert len(spy) == 1
    assert float(spy[0]["quantity"]) == pytest.approx(100.0)
