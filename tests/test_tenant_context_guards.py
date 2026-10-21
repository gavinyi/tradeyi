"""租户上下文守卫用例（阶段一放行前置四件套）。

覆盖：
1. tenant 上下文缺失/未注册 → 显式报错，绝不静默折默认租户（Reviewer 放行前置）；
2. 跨租户读写隔离与账号冲突拦截；
3. 对账锁仓按租户切片（REAL 方向镜像）；
4. 存量意图迁移回填按 intent_key 尾段派生；
5. 执行模式覆盖键优先级：租户键 > 全局策略键 > 全局 EXECUTION_MODE（旧键回退兼容）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.store import TradingStore
from futu_quant.tenant import TenantProfile, resolve_tenant


@pytest.fixture(autouse=True)
def approval_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")


class StubBroker:
    """最小券商桩：可配 env/account，统计下单调用。"""

    def __init__(self, env="SIMULATE", account_id="20170229"):
        self.trading_env = env
        self.account_id = account_id
        self.settings = SimpleNamespace(trading_env=env, real_trading_enabled=False)
        self.calls = 0
        self._positions = pd.DataFrame()
        self._orders = pd.DataFrame()

    def positions(self):
        return self._positions

    def orders(self):
        return self._orders

    def account_info(self):
        return pd.DataFrame([{"total_assets": 1_000_000, "power": 500_000, "unrealized_pl": 0}])

    def place_limit_order(self, symbol, side, quantity, price, *args, **kwargs):
        self.calls += 1
        return pd.DataFrame([{"order_id": f"stub-{self.calls}"}])

    def snapshots(self, symbols):
        return pd.DataFrame([{"code": symbols[0], "bid_price": 100.0, "ask_price": 101.0}])


class NoAccountBroker(StubBroker):
    """无 account_id 属性的券商桩（暴露 strict 解析缺口的最小构造）。"""

    def __init__(self):
        super().__init__()
        del self.account_id


TWO_SIM_REGISTRY = {
    "tenant_alpha": TenantProfile("tenant_alpha", "阿尔法", "futu", "SIMULATE", "111111"),
    "tenant_beta": TenantProfile("tenant_beta", "贝塔", "futu", "SIMULATE", "222222"),
}


# ---------- 1. 上下文缺失 / 未注册租户：显式报错（放行前置） ----------


def test_missing_context_strict_raises():
    with pytest.raises(ValueError):
        resolve_tenant(None, registry=TWO_SIM_REGISTRY, strict=True)


def test_unknown_tenant_id_strict_raises():
    with pytest.raises(ValueError, match="未注册的租户标识"):
        resolve_tenant("tenant_gamma", registry=TWO_SIM_REGISTRY, strict=True)


def test_execution_manager_rejects_unknown_tenant(tmp_path):
    """显式传入未注册租户必须报错，绝不静默折到第一个同环境租户（幂等防线前提）。"""
    store = TradingStore(tmp_path / "orders.db")
    with pytest.raises(ValueError):
        ExecutionManager(
            NoAccountBroker(), store, tenant="tenant_gamma", tenant_registry=TWO_SIM_REGISTRY
        )


def test_virtual_positions_refuses_unknown_tenant(tmp_path):
    """读路径传入未注册租户同样不应静默折到模拟租户切片。"""
    store = TradingStore(tmp_path / "orders.db")
    with pytest.raises(ValueError):
        store.virtual_positions(tenant_id="tenant_gamma")


# ---------- 2. 账号冲突拦截与跨租户隔离 ----------


def test_execution_manager_rejects_account_conflict(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    broker = StubBroker(env="REAL", account_id="999999")
    profile = TenantProfile("t_real", "实盘", "futu", "REAL", "10001")
    with pytest.raises(ValueError, match="账号冲突"):
        ExecutionManager(broker, store, tenant=profile)


def test_cross_tenant_intent_rows_isolated(tmp_path):
    store = TradingStore(tmp_path / "orders.db")
    store.create_intent("strat", "a:PAPER", "US.SPY", "SELL", 1, 1.0, tenant_id="t_sim")
    store.create_intent("strat", "b:REAL", "US.QQQ", "SELL", 1, 2.0, tenant_id="t_real")

    sim_rows = store.intents(tenant_id="t_sim")
    real_rows = store.intents(tenant_id="t_real")
    assert [r["intent_key"] for r in sim_rows] == ["a:PAPER"]
    assert [r["intent_key"] for r in real_rows] == ["b:REAL"]


# ---------- 3. 对账锁仓按租户切片（REAL 方向镜像） ----------


def test_reconcile_real_mismatch_does_not_pause_paper_strategy(tmp_path):
    store = TradingStore(tmp_path / "reconcile.db")
    t_real = TenantProfile("futu_real_master", "实盘", "futu", "REAL", "281756481476169109")
    t_sim = TenantProfile("futu_paper_sandbox", "模拟", "futu", "SIMULATE", "20170229")

    # 实盘租户：本地有虚拟分配、券商无持仓 → 触发 MISMATCH
    store.set_virtual_allocation("volatility_long_gamma", "US.QQQ", 1.0, 100.0)
    store.create_intent(
        "volatility_long_gamma", "one:REAL", "US.QQQ", "BUY", 1, 100.0, tenant_id=t_real.tenant_id
    )
    b_real = StubBroker(env="REAL", account_id="281756481476169109")
    reconcile_ledgers(store, b_real, tenant_id=t_real.tenant_id)

    assert store.get_setting(
        f"strategy_reconciliation_disabled:{t_real.tenant_id}:volatility_long_gamma"
    ) == "true"
    assert store.get_setting(
        f"strategy_reconciliation_disabled:{t_sim.tenant_id}:volatility_long_gamma"
    ) != "true"


# ---------- 4. 存量迁移回填按 intent_key 尾段派生 ----------


def test_backfill_derives_tenant_from_intent_key_tail(tmp_path):
    db = tmp_path / "legacy.db"
    store = TradingStore(db)
    # 模拟旧库行：tenant_id 留空
    store.create_intent("strat", "k1:REAL", "US.SPY", "SELL", 1, 1.0)
    store.create_intent("strat", "k2:PAPER", "US.SPY", "SELL", 1, 1.0)
    store.create_intent("strat", "k3:SHADOW", "US.SPY", "SELL", 1, 1.0)

    reopened = TradingStore(db)  # 重开触发 initialize 内的回填 UPDATE
    rows = {r["intent_key"]: r["tenant_id"] for r in reopened.intents()}
    assert rows["k1:REAL"] == "futu_real_master"
    assert rows["k2:PAPER"] == "futu_paper_sandbox"
    assert rows["k3:SHADOW"] == "futu_paper_sandbox"


# ---------- 5. 执行模式覆盖键优先级与旧键回退兼容 ----------


def test_mode_override_priority_tenant_over_global(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")
    store = TradingStore(tmp_path / "mode.db")
    store.set_setting("execution_mode", "SHADOW")
    profile = TenantProfile("t_sim", "模拟", "futu", "SIMULATE", "20170229")
    broker = StubBroker(env="SIMULATE", account_id="20170229")
    executor = ExecutionManager(broker, store, tenant=profile)

    # 无任何 override：全局 EXECUTION_MODE 兜底 → SHADOW，不碰券商
    r1 = executor.submit_limit("sma", "k1", "US.NVDA", "BUY", 1, 100)
    assert r1.status == "SHADOW"
    assert broker.calls == 0

    # 全局旧键 override（无租户前缀）→ PAPER：向下兼容仍生效，真实提交券商
    store.set_setting("strategy_execution_mode:sma", "PAPER")
    r2 = executor.submit_limit("sma", "k2", "US.NVDA", "BUY", 1, 100)
    assert r2.status == "SUBMITTED"
    assert broker.calls == 1

    # 租户键 override → SHADOW：优先级最高，压过全局旧键
    store.set_setting("strategy_execution_mode:t_sim:sma", "SHADOW")
    r3 = executor.submit_limit("sma", "k3", "US.NVDA", "BUY", 1, 100)
    assert r3.status == "SHADOW"
    assert broker.calls == 1


# ---------- 6. P0 防回归：租户急停双写必须被生产消费端看见 ----------


def test_tenant_kill_switch_blocks_risk_engine_buy(tmp_path):
    """Reviewer P0 端到端防回归：按 UI 路径（tenant+account 双参）写入急停后，
    RiskEngine 视角（risk.py:100 只读 account 键）必须立即拦下 BUY。"""
    from futu_quant.risk import RiskEngine, RiskLimits

    store = TradingStore(tmp_path / "kill.db")
    broker = StubBroker(env="REAL", account_id="281756481476169109")
    risk = RiskEngine(store, RiskLimits(max_order_notional=10_000, max_daily_notional=50_000))

    # 写入前：BUY 正常通过急停检查
    ok = risk.check(broker, "US.QQQ", "BUY", 1, 100)
    assert ok.allowed

    # 按 UI 路径写入（pages/7_risk_center 的调用形态：tenant_id + account_id 双参）
    store.set_kill_switch(True, account_id="281756481476169109", tenant_id="futu_real_master")

    # RiskEngine 生产读路径（只传 account_id）必须看见急停
    blocked = risk.check(broker, "US.QQQ", "BUY", 1, 100)
    assert not blocked.allowed
    assert "急停" in blocked.reason

    # SELL 平仓通道保持畅通（急停只禁新增风险；预置持仓过数量校验）
    broker._positions = pd.DataFrame([{
        "code": "US.QQQ", "qty": 10.0, "can_sell_qty": 10.0, "market_val": 1000.0,
    }])
    sell = risk.check(broker, "US.QQQ", "SELL", 1, 100, strategy_id="core_satellite")
    assert sell.allowed

    # 其他账户/租户不受连坐
    other = StubBroker(env="REAL", account_id="999999")
    untouched = risk.check(other, "US.QQQ", "BUY", 1, 100)
    assert untouched.allowed

    # 解除后恢复
    store.set_kill_switch(False, account_id="281756481476169109", tenant_id="futu_real_master")
    resumed = risk.check(broker, "US.QQQ", "BUY", 1, 100)
    assert resumed.allowed
