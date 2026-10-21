from __future__ import annotations

from types import SimpleNamespace
import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.tenant import TenantProfile


class ProbeStubBroker:
    """专用于下单撤单全链路探针测试的券商桩"""

    def __init__(self, env="SIMULATE", account_id="20170229"):
        self.trading_env = env
        self.account_id = account_id
        self.settings = SimpleNamespace(
            trading_env=env,
            real_trading_enabled=False,
            real_max_order_notional=1000.0,
            real_max_daily_notional=5000.0,
        )
        self.placed_orders: list[dict] = []
        self.cancelled_order_ids: list[str] = []
        self._positions = pd.DataFrame()
        self._orders = pd.DataFrame()

    def positions(self):
        return self._positions

    def orders(self):
        return self._orders

    def account_info(self):
        return pd.DataFrame([{"total_assets": 100000.0, "power": 50000.0, "unrealized_pl": 0.0}])

    def snapshots(self, symbols):
        # 现价 $150，确保 $0.01 挂单属于极低不可能成交价
        return pd.DataFrame([{"code": s, "bid_price": 150.0, "ask_price": 150.2, "last_price": 150.1} for s in symbols])

    def place_limit_order(self, symbol, side, quantity, price, **kwargs):
        order_id = f"probe-ord-{len(self.placed_orders) + 1}"
        record = {
            "order_id": order_id,
            "code": symbol,
            "trd_side": side,
            "qty": quantity,
            "price": price,
            "order_status": "SUBMITTED",
            "dealt_qty": 0.0,
            "dealt_avg_price": 0.0,
        }
        self.placed_orders.append(record)
        self._orders = pd.DataFrame(self.placed_orders)
        return pd.DataFrame([{"order_id": order_id}])

    def cancel_order(self, order_id, *, expected_env=None):
        self.cancelled_order_ids.append(order_id)
        for ord_dict in self.placed_orders:
            if ord_dict["order_id"] == order_id:
                ord_dict["order_status"] = "CANCELLED_ALL"
        self._orders = pd.DataFrame(self.placed_orders)
        return pd.DataFrame([{"order_id": order_id, "status": "CANCELLED_ALL"}])

    @staticmethod
    def is_cancellable_status(status):
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


@pytest.fixture(autouse=True)
def approval_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")


def test_submit_and_cancel_probe_001_price_flow(tmp_path):
    """
    全链路测试：
    1. 提交 $0.01 不可能成交的极低限价单；
    2. 校验 ExecutionManager 状态流转为 SUBMITTED，并记录券商订单号；
    3. 模拟撤单请求，状态流转为撤单完成；
    4. 对账三账核对，确认零成交、零持仓差异、无锁仓孤儿。
    """
    store = TradingStore(tmp_path / "probe.db")
    broker = ProbeStubBroker(env="SIMULATE", account_id="20170229")
    tenant = TenantProfile("futu_paper_sandbox", "模拟", "futu", "SIMULATE", "20170229")
    executor = ExecutionManager(broker, store, tenant=tenant)

    # 1. 提交 $0.01 不可能成交限价单 (smart_execution=False 保持精确出价)
    symbol = "US.AAPL"
    side = "BUY"
    quantity = 1
    impossible_price = 0.01

    res = executor.submit_limit(
        strategy_id="probe_test",
        intent_key="probe:order:001",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=impossible_price,
        metadata={"smart_execution": False, "probe_test": True},
        execution_mode_override="PAPER",
    )

    assert res.status == "SUBMITTED"
    assert res.broker_order_id == "probe-ord-1"
    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0]["price"] == 0.01

    # 2. 数据库意图记录校验
    intent = store.intent_by_broker_order("probe-ord-1")
    assert intent is not None
    assert intent["status"] == "SUBMITTED"
    assert intent["limit_price"] == 0.01
    assert intent["quantity"] == 1
    assert intent["filled_quantity"] == 0.0

    # 3. 执行撤单 (调用 cancel_stale_orders 机制)
    # 将创建时间设为 1000 秒前以触发超时撤单
    with store.connection() as conn:
        past_time = "2020-01-01T00:00:00+00:00"
        conn.execute("UPDATE order_intents SET created_at=? WHERE id=?", (past_time, intent["id"]))

    cancelled_count = executor.cancel_stale_orders()
    assert cancelled_count == 1
    assert "probe-ord-1" in broker.cancelled_order_ids

    # 4. 校验意图状态转为 CANCEL_REQUESTED
    updated_intent = store.intent(intent["id"])
    assert updated_intent["status"] == "CANCEL_REQUESTED"

    # 5. 模拟券商对账同步 CANCELLED_ALL
    executor.reconcile()
    final_intent = store.intent(intent["id"])
    assert final_intent["status"] == "CANCELLED"

    # 6. 三账核验：零成交、零持仓，账本完全 MATCHED，无锁仓
    report = reconcile_ledgers(store, broker, tenant_id=tenant.tenant_id)
    assert not report.empty
    mismatches = report[report["status"] == "MISMATCH"]
    assert mismatches.empty
    assert store.get_setting("recovery_blocked", "false") == "false"


def test_probe_immediate_broker_cancel_flow(tmp_path):
    """
    实机探针场景测试：
    下发 $0.01 挂单后，取得 broker_order_id 立即发出 cancel_order 撤单请求，
    验证对账同步后意图状态变为 CANCELLED，且无残留挂单。
    """
    store = TradingStore(tmp_path / "probe_immediate.db")
    broker = ProbeStubBroker(env="SIMULATE", account_id="20170229")
    tenant = TenantProfile("futu_paper_sandbox", "模拟", "futu", "SIMULATE", "20170229")
    executor = ExecutionManager(broker, store, tenant=tenant)

    # 1. 提交 $0.01 极低价 BUY 单
    res = executor.submit_limit(
        strategy_id="probe_test",
        intent_key="probe:order:immediate",
        symbol="US.AAPL",
        side="BUY",
        quantity=1,
        price=0.01,
        metadata={"smart_execution": False, "probe_test": True},
        execution_mode_override="PAPER",
    )
    assert res.status == "SUBMITTED"
    broker_oid = res.broker_order_id
    assert broker_oid == "probe-ord-1"

    # 2. 探针毫秒级发起撤单
    cancel_res = broker.cancel_order(broker_oid)
    assert not cancel_res.empty
    assert cancel_res.iloc[0]["status"] == "CANCELLED_ALL"

    # 3. 对账触发状态收敛
    executor.reconcile()
    intent = store.intent_by_broker_order(broker_oid)
    assert intent["status"] == "CANCELLED"
    assert intent["filled_quantity"] == 0.0

    # 4. 再次对账验证零挂单零差异
    report = reconcile_ledgers(store, broker, tenant_id=tenant.tenant_id)
    assert report[report["status"] == "MISMATCH"].empty


def test_probe_order_cross_environment_cancel_blocked(tmp_path):
    store = TradingStore(tmp_path / "cross.db")
    broker_sim = ProbeStubBroker(env="SIMULATE", account_id="20170229")
    t_sim = TenantProfile("futu_paper_sandbox", "模拟", "futu", "SIMULATE", "20170229")
    executor_sim = ExecutionManager(broker_sim, store, tenant=t_sim)

    # 伪造一条实盘订单意图
    intent_real, _ = store.create_intent(
        "probe_test", "probe:order:real:001:REAL", "US.AAPL", "BUY", 1, 0.01,
        metadata={"smart_execution": False},
        tenant_id="futu_real_master",
    )
    store.update_intent(intent_real["id"], "SUBMITTED", broker_order_id="real-ord-999")
    with store.connection() as conn:
        conn.execute("UPDATE order_intents SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (intent_real["id"],))

    # 模拟执行器尝试超时撤单，必须被 _order_environment_ok 坚决拦截，禁止撤销实盘挂单！
    cancelled = executor_sim.cancel_stale_orders()
    assert cancelled == 0
    assert "real-ord-999" not in broker_sim.cancelled_order_ids


def test_probe_order_cancel_idempotency(tmp_path):
    store = TradingStore(tmp_path / "idemp.db")
    broker = ProbeStubBroker(env="SIMULATE", account_id="20170229")
    executor = ExecutionManager(broker, store)

    intent, _ = store.create_intent(
        "probe_test", "probe:order:idemp:PAPER", "US.AAPL", "BUY", 1, 0.01,
        metadata={"smart_execution": False},
    )
    store.update_intent(intent["id"], "CANCELLED", broker_order_id="ord-already-cancelled")
    with store.connection() as conn:
        conn.execute("UPDATE order_intents SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (intent["id"],))

    # 已是 CANCELLED 的订单再次超时扫描，直接跳过，零重复撤单发包
    cancelled = executor.cancel_stale_orders()
    assert cancelled == 0
    assert len(broker.cancelled_order_ids) == 0

