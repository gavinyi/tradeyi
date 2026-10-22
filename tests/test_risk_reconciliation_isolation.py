from __future__ import annotations

from types import SimpleNamespace

from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import TradingStore


class MockBroker:
    def __init__(self, account_id="281756481476169109"):
        self.account_id = account_id
        self.trading_env = "REAL"
        self.settings = SimpleNamespace(
            trading_env="REAL",
            real_max_order_notional=10000.0,
            real_max_daily_notional=50000.0,
        )

    def positions(self):
        import pandas as pd
        return pd.DataFrame([{"code": "US.NVDA", "qty": 7.0}])

    def orders(self):
        import pandas as pd
        return pd.DataFrame()

    def account_info(self):
        import pandas as pd
        return pd.DataFrame([{"total_assets": 100000.0, "power": 50000.0, "unrealized_pl": 0.0}])

    def snapshots(self, symbols):
        import pandas as pd
        return pd.DataFrame([{"code": s, "bid_price": 100.0, "ask_price": 101.0, "last_price": 100.5} for s in symbols])


def test_fine_grained_reconciliation_risk_isolation(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    broker = MockBroker()
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    # 1. 模拟股票策略发生对账差异：全局 recovery_blocked=true，且 core_satellite 被暂停
    store.set_setting("recovery_blocked", "true")
    store.set_setting("recovery_blocked:281756481476169109", "true")
    store.set_setting("strategy_reconciliation_disabled:core_satellite", "true")
    store.set_setting("symbol_blocked:281756481476169109:US.SPY", "true")

    # 2. 发生对账差异的策略 core_satellite 尝试买入 -> 必须精准阻断
    dec_strat_blocked = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_strat_blocked.allowed is False
    assert "存在持仓差异已被对账暂停" in dec_strat_blocked.reason

    # 3. 发生对账差异的标的 US.SPY 尝试买入 -> 必须精准阻断
    dec_sym_blocked = risk.check(
        broker, "US.SPY", "BUY", 1, 400.0,
        strategy_id="other_strat", execution_mode="REAL",
    )
    assert dec_sym_blocked.allowed is False
    assert "存在持仓对账差异" in dec_sym_blocked.reason

    # 4. 重点：独立健康的期权策略 volatility_long_gamma 买入 AMD 期权 -> 绝不连坐误伤，必须正常放行！
    dec_option_pass = risk.check(
        broker, "US.AMD261030C560000", "BUY", 1, 15.0,
        multiplier=100.0, strategy_id="volatility_long_gamma", execution_mode="REAL",
    )
    assert dec_option_pass.allowed is True

    # 5. 平仓通道绝不阻断：即使处于对账差异策略中的持仓，SELL 平仓严禁被拦截！
    dec_sell_pass = risk.check(
        broker, "US.NVDA", "SELL", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_sell_pass.allowed is True

    # 6. 对账自愈闭环：差异修复后，对账标志清零，买入开仓立即自动恢复
    store.set_setting("strategy_reconciliation_disabled:core_satellite", "false")
    store.set_setting("symbol_blocked:281756481476169109:US.SPY", "false")
    dec_recovered = risk.check(
        broker, "US.SPY", "BUY", 1, 400.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_recovered.allowed is True


def test_reconciliation_isolation_fallback_without_strategy_id(tmp_path):
    """验证未指定具体策略 ID 时的全局阻断兜底行为与 SHADOW 豁免"""
    store = TradingStore(tmp_path / "risk_fallback.db")
    broker = MockBroker()
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    store.set_setting("recovery_blocked", "true")

    # 未指定 strategy_id，触发全局兜底阻断
    dec_fallback = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id=None, execution_mode="REAL",
    )
    assert dec_fallback.allowed is False
    assert "启动恢复发现三账差异" in dec_fallback.reason

    # SHADOW 模式即便无 strategy_id 也不受阻断
    dec_shadow = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id=None, execution_mode="SHADOW",
    )
    assert dec_shadow.allowed is True


def test_reconciliation_isolation_tenant_id_prefix(tmp_path):
    """验证按租户前缀 strategy_reconciliation_disabled:{tenant_id}:{strategy_id} 的精准阻断与隔离"""
    store = TradingStore(tmp_path / "risk_tenant.db")
    broker = MockBroker()
    broker.tenant_id = "futu_real_master"
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    # 按租户前缀停用实盘的 core_satellite
    store.set_setting("strategy_reconciliation_disabled:futu_real_master:core_satellite", "true")

    # 属于该租户的 core_satellite 尝试开仓 -> 精准阻断
    dec_blocked = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_blocked.allowed is False
    assert "存在持仓差异已被对账暂停" in dec_blocked.reason

    # 另一个租户（如模拟租户）的同名策略 -> 不受阻断
    broker_paper = MockBroker(account_id="20170229")
    broker_paper.tenant_id = "futu_paper_sandbox"
    dec_paper_pass = risk.check(
        broker_paper, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="PAPER",
    )
    assert dec_paper_pass.allowed is True


def test_alert_manager_whitelisted_ledger_mismatch_not_swallowed_when_incident_already_open(tmp_path):
    """
    防静默熔断用例：
    验证即便事故早已处于 OPEN 状态，白名单致命事件（如 LEDGER_MISMATCH）调用 alerts.send 时
    绝不会在第一毫秒被 return 拦截吞噬，必须允许向下派发告警与卡片。
    """
    from futu_quant.alerts import AlertManager
    store = TradingStore(tmp_path / "alerts_anti_silence.db")
    alerts = AlertManager(store)

    # 1. 模拟对账底层抢先写入了 OPEN 状态的 incident
    store.record_incident("LEDGER_MISMATCH", "ERROR", "底层对账差异预先记录")

    # 2. 模拟外部调度器再次调用 alerts.send，验证不会被提前 return 拦截，必须生成 alert 记录
    alerts.send("ERROR", "LEDGER_MISMATCH", "发现持仓差异，策略已暂停")

    # 3. 校验 alert 已成功落盘
    recent = store.alerts(limit=5)
    assert len(recent) > 0
    assert any(a["event_type"] == "LEDGER_MISMATCH" for a in recent)


