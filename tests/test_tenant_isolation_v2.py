from __future__ import annotations

from types import SimpleNamespace
import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.tenant import TenantProfile, resolve_tenant


class FakeBroker:
    def __init__(self, env="SIMULATE", account_id="20170229"):
        self.trading_env = env
        self.account_id = account_id
        self.settings = SimpleNamespace(trading_env=env, real_trading_enabled=False)
        self.calls = []
        self._positions = pd.DataFrame()
        self._orders = pd.DataFrame()

    def positions(self):
        return self._positions

    def orders(self):
        return self._orders

    def place_limit_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return pd.DataFrame([{"order_id": "test-order-1"}])

    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbols[0], "bid_price": 100.0, "ask_price": 101.0}])


def test_tenant_profile_resolution():
    synthetic_reg = {
        "tenant_alpha": TenantProfile(
            tenant_id="tenant_alpha",
            display_name="合成租户阿尔法",
            broker_type="futu",
            trading_env="SIMULATE",
            account_id="111111",
        ),
        "tenant_beta": TenantProfile(
            tenant_id="tenant_beta",
            display_name="合成租户贝塔",
            broker_type="futu",
            trading_env="REAL",
            account_id="222222",
        ),
    }
    # 1. 显式 ID 命中
    t1 = resolve_tenant("tenant_alpha", registry=synthetic_reg, strict=True)
    assert t1.tenant_id == "tenant_alpha"
    assert t1.account_id == "111111"

    # 2. 账号精确匹配
    t2 = resolve_tenant(account_id="222222", registry=synthetic_reg, strict=True)
    assert t2.tenant_id == "tenant_beta"

    # 3. 未知租户 strict=True 必须显式报错，杜绝无声回退
    with pytest.raises(ValueError, match="未注册的租户标识"):
        resolve_tenant("tenant_unknown", registry=synthetic_reg, strict=True)


def test_execution_manager_tenant_guard_conflict(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    broker = FakeBroker(env="SIMULATE", account_id="10001")
    tenant_real = TenantProfile(
        tenant_id="t_real",
        display_name="真金租户",
        broker_type="futu",
        trading_env="REAL",
        account_id="10001",
    )
    # 声明为 REAL 的租户挂载 SIMULATE 券商，必须坚决抛出异常
    with pytest.raises(ValueError, match="租户环境冲突"):
        ExecutionManager(broker, store, tenant=tenant_real)


def test_cross_tenant_intent_and_reconcile_isolation(tmp_path):
    t_sim = TenantProfile("t_sim", "模拟", "futu", "SIMULATE", "1001")
    t_real = TenantProfile("t_real", "实盘", "futu", "REAL", "2001")
    reg = {"t_sim": t_sim, "t_real": t_real}
    store = TradingStore(tmp_path / "orders.db", tenant_registry=reg)

    # 1. 通过 Store 写入分属不同租户的意图，intent_key 保持原有格式
    intent_sim, _ = store.create_intent(
        "volatility_long_gamma", "one:PAPER", "US.SPY", "BUY", 1, 100.0,
        tenant_id=t_sim.tenant_id,
    )
    intent_real, _ = store.create_intent(
        "volatility_long_gamma", "one:REAL", "US.QQQ", "BUY", 2, 200.0,
        tenant_id=t_real.tenant_id,
    )

    # 2. 租户查询隔离检索
    sim_intents = store.intents(tenant_id=t_sim.tenant_id)
    assert len(sim_intents) == 1
    assert sim_intents[0]["symbol"] == "US.SPY"

    real_intents = store.intents(tenant_id=t_real.tenant_id)
    assert len(real_intents) == 1
    assert real_intents[0]["symbol"] == "US.QQQ"

    # 3. 对账策略暂停标志按租户隔离
    b_sim = FakeBroker(env="SIMULATE", account_id="1001")
    # 本地策略账本记录有 1 股 SPY，但券商实际为 0 股 -> 触发 MISMATCH
    store.set_virtual_allocation("volatility_long_gamma", "US.SPY", 1.0, 100.0)
    b_sim._positions = pd.DataFrame()

    reconcile_ledgers(store, b_sim, tenant_id=t_sim.tenant_id)

    # 模拟租户的策略被标记暂停
    assert store.get_setting(f"strategy_reconciliation_disabled:{t_sim.tenant_id}:volatility_long_gamma") == "true"
    # 实盘租户对应的同名策略绝未被连坐暂停！
    assert store.get_setting(f"strategy_reconciliation_disabled:{t_real.tenant_id}:volatility_long_gamma") != "true"


def test_service_files_tenant_isolation():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path("scripts").resolve()))
    from run_service import service_files

    default_f = service_files()
    real_f = service_files("futu_real_master")
    paper_f = service_files("futu_paper_sandbox")

    # 1. 默认与实盘、模拟锁文件完全物理隔离
    assert default_f["lock"].name == "trading-service.lock"
    assert real_f["lock"].name == "trading-service-futu_real_master.lock"
    assert paper_f["lock"].name == "trading-service-futu_paper_sandbox.lock"

    # 2. PID 与 STOP 标志也严格独立
    assert real_f["pid"].name != paper_f["pid"].name
    assert real_f["stop"].name != paper_f["stop"].name


def test_kill_switch_dual_write_guarantees_account_read(tmp_path):
    store = TradingStore(tmp_path / "kill.db")
    tenant_id = "futu_real_master"
    account_id = "281756481476169109"

    # 1. 模拟 UI 路径（tenant+account 双参）写入急停
    store.set_kill_switch(True, account_id=account_id, tenant_id=tenant_id)

    # 2. 生产路径只传 account_id 读取，必须返回 True
    assert store.kill_switch(account_id=account_id) is True

    # 3. 租户路径只传 tenant_id 读取，也必须返回 True
    assert store.kill_switch(tenant_id=tenant_id) is True

    # 4. 未受波及的其他账户绝不误伤
    assert store.kill_switch(account_id="999999999") is False

    # 5. 解除急停双参写入
    store.set_kill_switch(False, account_id=account_id, tenant_id=tenant_id)
    assert store.kill_switch(account_id=account_id) is False
    assert store.kill_switch(tenant_id=tenant_id) is False
