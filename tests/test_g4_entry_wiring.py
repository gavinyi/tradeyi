"""G4 接线守卫：`risk.py` 熔断评估 + `execution.py` BUY 聚合层。

对应验收：V16（既有硬停逐值不变）、V18-a（聚合层不取数）、V23（blocked_by 可区分）、
SIMULATE 显式拒绝（不拦、只写 audit）、任何等级不影响 SELL。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.risk_breaker import (
    ACCOUNT_HALT,
    DRAWDOWN_ABSOLUTE,
    DRAWDOWN_STOP,
    INTRADAY_DRAWDOWN,
)
from futu_quant.store import TradingStore

ACCOUNT = "281756481476169109"
ACCOUNT_ENV = f"REAL_{ACCOUNT}"
TENANT = "futu_real_master"


class _Broker:
    """最小券商桩：可配权益/账户；REAL 但无 tenant_id（走 account 后缀）。"""

    def __init__(self, assets: float, account_id: str = ACCOUNT, env: str = "REAL"):
        self.account_id = account_id
        self.trading_env = env
        self._assets = float(assets)
        self._positions = pd.DataFrame()
        self._orders = pd.DataFrame()
        self.settings = SimpleNamespace(trading_env=env, real_trading_enabled=False)

    def positions(self):
        return self._positions

    def orders(self):
        return self._orders

    def account_info(self):
        return pd.DataFrame([{
            "total_assets": self._assets, "power": 1_000_000.0, "unrealized_pl": 0.0,
        }])

    def is_cancellable_status(self, status) -> bool:
        return False


def _engine(store: TradingStore, monkeypatch=None, **limit_overrides) -> RiskEngine:
    limits = dict(max_order_notional=10_000, max_daily_notional=50_000)
    limits.update(limit_overrides)
    engine = RiskEngine(store, RiskLimits(**limits))
    if monkeypatch is not None:
        monkeypatch.setattr(
            engine, "_settings_provider",
            lambda: SimpleNamespace(real_account_id=int(ACCOUNT)),
        )
    return engine


def _set_thresholds(store: TradingStore, mode: str) -> None:
    suffix = f":{TENANT}"  # 生产解析出的租户键后缀（tenant_of_broker）
    store.set_setting(f"risk_breaker_mode{suffix}", mode)
    store.set_setting(f"risk_drawdown_warn_pct{suffix}", "0.08")
    store.set_setting(f"risk_drawdown_stop_pct{suffix}", "0.10")
    store.set_setting(f"risk_drawdown_resume_pct{suffix}", "0.06")
    store.set_setting(f"risk_drawdown_rearm_gap_pp{suffix}", "0.02")


def _seed_peak(store: TradingStore, peak: float, current: float) -> None:
    store.record_equity(peak, peak * 0.5, peak * 0.5, env=ACCOUNT_ENV)
    store.record_equity(current, current * 0.5, current * 0.5, env=ACCOUNT_ENV)


def _breaker_events(store: TradingStore, status: str | None = None) -> list[dict]:
    rows = [
        row for row in store.operational_events(limit=100)
        if row.get("component") == "risk_breaker"
    ]
    if status is None:
        return rows
    return [row for row in rows if row.get("status") == status]


# ------------------------------------------------------------------ V16 行为冻结
def test_breaker_off_keeps_existing_behavior_bit_for_bit(tmp_path):
    """V16：熔断默认 off，既有放行/拦截结果与 reason 逐值不变，且不写 state/audit。"""
    store = TradingStore(tmp_path / "freeze.db")
    broker = _Broker(1_000_000.0)
    risk = _engine(store)

    allowed = risk.check(broker, "US.QQQ", "BUY", 1, 100)
    assert allowed.allowed is True
    assert allowed.reason == "风控通过"
    assert allowed.notional == pytest.approx(100.0)
    assert allowed.blocked_by == ()
    assert allowed.breaker_reasons == ()
    assert allowed.breaker_unsupported == ""

    store.set_kill_switch(True, account_id=ACCOUNT, tenant_id=TENANT)
    blocked = risk.check(broker, "US.QQQ", "BUY", 1, 100)
    assert blocked.allowed is False
    assert blocked.blocked_by == (ACCOUNT_HALT,)
    # V16 逐值冻结：reason/notional 也必须与改造前完全一致（只加了关键字标注）
    assert blocked.reason == f"账户 {ACCOUNT} 急停已开启，禁止新增仓位"
    assert blocked.notional == pytest.approx(100.0)

    assert store.load_risk_state(TENANT) is None, "mode=off 不得写 risk_state"
    assert _breaker_events(store) == [], "mode=off 不得写熔断 audit"


def test_v16_intraday_hard_stop_still_fires_with_original_code(tmp_path):
    """V16 第二段：既有日内回撤硬停仍按原条件、原原因码触发（判定与副作用未动）。"""
    store = TradingStore(tmp_path / "freeze2.db")
    store.set_kill_switch(False, account_id=ACCOUNT, tenant_id=TENANT)
    store.record_equity(1_000.0, 500.0, 500.0, env=ACCOUNT_ENV)
    broker = _Broker(900.0)  # 今日 1000 → 900 = 10% ≥ max_drawdown_pct(5%)
    risk = _engine(store)
    decision = risk.check(broker, "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    assert decision.allowed is False
    assert decision.blocked_by == (INTRADAY_DRAWDOWN,)
    assert decision.reason == f"账户 {ACCOUNT} 日内回撤达到 10.00%，已自动急停"
    assert decision.notional == pytest.approx(100.0)
    assert decision.breaker_reasons == (), "熔断未启用时不得附加熔断原因"


def test_link_kill_switch_audit_carries_structured_blocked_by(tmp_path):
    """链路级（非函数级）：`submit_limit` 被拒时 audit 必须带结构化 `blocked_by`。

    V9 范围说明：既有 4 闸门仍是 RiskEngine 内**单原因早退**（本批不改判定），
    因此这里断言的是「当次拦截闸门的精确码」进入 audit；多原因并行不在本批范围。
    """
    import json

    from futu_quant.execution import ExecutionManager

    class _ExecBroker(_Broker):
        def __init__(self):
            super().__init__(1_000_000.0, account_id="20170229", env="SIMULATE")
            self.placed = []

        def place_limit_order(self, symbol, side, quantity, price, **kwargs):
            self.placed.append((symbol, side, quantity, price))
            return pd.DataFrame([{"order_id": "ord-1"}])

    store = TradingStore(tmp_path / "link.db")
    store.set_setting("execution_mode", "PAPER")
    store.set_kill_switch(True, account_id="20170229")
    broker = _ExecBroker()
    executor = ExecutionManager(broker, store=store)
    result = executor.submit_limit("core_satellite", "g4-link-1", "US.QQQ", "BUY", 1, 100.0)
    assert result.status == "REJECTED"
    assert broker.placed == []
    rejected = [
        entry for entry in store.audit_events()
        if entry["event_type"] == "RISK_REJECTED"
    ]
    assert len(rejected) == 1, "V9：拒绝只能有一条 RISK_REJECTED（不能分成两个事件）"
    payload = json.loads(rejected[0]["payload_json"])
    assert payload["blocked_by"] == ["account_halt"]
    assert payload["block_details"][0]["blocked_by"] == "account_halt"
    # 单出口：被既有闸门拦下时不得再写熔断事件（否则又是两条路径）
    assert not [e for e in store.audit_events() if e["event_type"] == "ENTRY_GATE_SHADOW"]


def test_link_simulate_breaker_records_shadow_audit_without_blocking(tmp_path):
    """链路级：SIMULATE + enforce 时 BUY 照常放行，只写 ENTRY_GATE_SHADOW（含未启用原因）。"""
    import json

    from futu_quant.execution import ExecutionManager

    class _ExecBroker(_Broker):
        def __init__(self):
            super().__init__(1_000_000.0, account_id="20170229", env="SIMULATE")
            self.placed = []

        def place_limit_order(self, symbol, side, quantity, price, **kwargs):
            self.placed.append((symbol, side, quantity, price))
            return pd.DataFrame([{"order_id": "ord-1"}])

    store = TradingStore(tmp_path / "link-sim.db")
    store.set_setting("execution_mode", "PAPER")
    store.set_setting("risk_breaker_mode:futu_paper_sandbox", "enforce")
    for key, value in (("risk_drawdown_warn_pct", "0.08"), ("risk_drawdown_stop_pct", "0.10"),
                       ("risk_drawdown_resume_pct", "0.06"), ("risk_drawdown_rearm_gap_pp", "0.02")):
        store.set_setting(f"{key}:futu_paper_sandbox", value)
    broker = _ExecBroker()
    executor = ExecutionManager(broker, store=store)
    result = executor.submit_limit("core_satellite", "g4-link-sim", "US.QQQ", "BUY", 1, 100.0)
    assert result.status != "REJECTED", "SIMULATE 不得因熔断停单"
    assert len(broker.placed) == 1
    shadow = [e for e in store.audit_events() if e["event_type"] == "ENTRY_GATE_SHADOW"]
    assert len(shadow) == 1
    payload = json.loads(shadow[0]["payload_json"])
    assert payload["breaker_unsupported_env"] == "SIMULATE"
    assert payload["breaker_code"] == "breaker_unsupported_env"
    assert not [e for e in store.audit_events() if e["event_type"] == "RISK_REJECTED"]


def test_breaker_block_codes_are_exact_prd_literals(tmp_path, monkeypatch):
    """Reviewer/PRD 要求：运行时 `BlockReason.code` 必须是 V23 的精确字面量。"""
    assert DRAWDOWN_ABSOLUTE == "dd_vs_peak_absolute"
    assert DRAWDOWN_STOP == "dd_since_reset_stop"
    from futu_quant import risk_breaker as rb

    assert rb.DRAWDOWN_WARN == "dd_since_reset_warn"

    store = TradingStore(tmp_path / "codes.db")
    _seed_peak(store, peak=1_000.0, current=890.0)  # dd_vs_peak=11% → 绝对底线
    _set_thresholds(store, "shadow")
    risk = _engine(store, monkeypatch, max_drawdown_pct=0.99)
    decision = risk.check(_Broker(890.0), "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    assert decision.breaker_would_block is True
    assert decision.breaker_reasons[0].code == "dd_vs_peak_absolute"
    # audit 里也必须出现精确码（S2 shadow 观测按它分档统计）
    events = _breaker_events(store, "SHADOW")
    assert events and "dd_vs_peak_absolute" in str(events[0])


# ------------------------------------------------------------------ SIMULATE 显式拒绝
def test_simulate_enforce_never_blocks_but_records_unsupported(tmp_path):
    store = TradingStore(tmp_path / "sim.db")
    broker = _Broker(1_000_000.0)
    _set_thresholds(store, "enforce")
    risk = _engine(store)
    decision = risk.check(broker, "US.QQQ", "BUY", 1, 100, execution_mode="PAPER")
    assert decision.allowed is True, "SIMULATE 不得因熔断停单"
    assert decision.breaker_unsupported == "SIMULATE"
    assert decision.breaker_reasons == ()
    assert store.load_risk_state(TENANT) is None
    events = _breaker_events(store, "NOT_ENABLED")
    assert events and "SIMULATE" in str(events[0])


# ------------------------------------------------------------------ REAL shadow / enforce
def test_real_shadow_records_would_block_without_blocking(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "shadow.db")
    _seed_peak(store, peak=1_000.0, current=890.0)  # dd_vs_peak = 11%
    _set_thresholds(store, "shadow")
    broker = _Broker(890.0)
    # 既有日内回撤硬停会先触发（今天从 1000 跌到 900 = 10%），这里把日内阈值抬高，
    # 只为隔离验证「跨日熔断」自身的 shadow/enforce 行为（既有硬停另有 V16 冻结用例）。
    risk = _engine(store, monkeypatch, max_drawdown_pct=0.99)

    decision = risk.check(broker, "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    assert decision.allowed is True, "shadow 只记录、不拦"
    assert decision.breaker_would_block is True
    assert decision.breaker_reasons, "shadow 必须带结构化原因供 audit"
    assert decision.breaker_reasons[0].code in {DRAWDOWN_STOP, DRAWDOWN_ABSOLUTE}
    state = store.load_risk_state(TENANT)
    assert state is not None and state["breaker_level"] == 2
    assert _breaker_events(store, "SHADOW"), "shadow 必须写 would_block audit"


def test_real_enforce_blocks_buy_with_specific_block_code(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "enforce.db")
    _seed_peak(store, peak=1_000.0, current=890.0)
    _set_thresholds(store, "enforce")
    broker = _Broker(890.0)
    risk = _engine(store, monkeypatch, max_drawdown_pct=0.99)

    decision = risk.check(broker, "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    assert decision.allowed is False
    assert decision.blocked_by, "enforce 必须带 blocked_by 具体闸门"
    assert decision.blocked_by[0] in {DRAWDOWN_STOP, DRAWDOWN_ABSOLUTE}
    assert "熔断" in decision.reason


def test_enforce_never_blocks_sell(tmp_path, monkeypatch):
    """红线：任何等级都不限制退出（META 教训）。"""
    store = TradingStore(tmp_path / "sell.db")
    _seed_peak(store, peak=1_000.0, current=890.0)
    _set_thresholds(store, "enforce")
    broker = _Broker(890.0)
    broker._positions = pd.DataFrame([{
        "code": "US.QQQ", "qty": 10.0, "can_sell_qty": 10.0, "market_val": 9_000.0,
    }])
    risk = _engine(store, monkeypatch, max_drawdown_pct=0.99)
    decision = risk.check(broker, "US.QQQ", "SELL", 1, 100, strategy_id="core_satellite",
                          execution_mode="REAL")
    assert decision.allowed is True, "熔断不得拦 SELL"


def test_missing_thresholds_fail_closed_in_enforce(tmp_path, monkeypatch):
    """D4.2：阈值类缺键 → fail-closed 拒绝开仓（不回落默认值）。"""
    store = TradingStore(tmp_path / "cfg.db")
    store.set_setting(f"risk_breaker_mode:{TENANT}", "enforce")
    broker = _Broker(1_000_000.0)
    risk = _engine(store, monkeypatch)
    decision = risk.check(broker, "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    assert decision.allowed is False
    assert decision.blocked_by == ("risk_breaker_config",)


def test_breaker_state_key_uses_tenant_and_updates_single_row(tmp_path, monkeypatch):
    """状态按租户单行 upsert：重复评估不新增行，峰值/等级持续更新。"""
    store = TradingStore(tmp_path / "iso.db")
    _set_thresholds(store, "shadow")
    _seed_peak(store, peak=1_000.0, current=890.0)
    risk = _engine(store, monkeypatch, max_drawdown_pct=0.99)
    risk.check(_Broker(890.0), "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    first = store.load_risk_state(TENANT)
    assert first is not None and first["tenant_id"] == TENANT
    risk.check(_Broker(890.0), "US.QQQ", "BUY", 1, 100, execution_mode="REAL")
    second = store.load_risk_state(TENANT)
    assert second is not None
    with store.connection() as connection:
        rows = connection.execute("SELECT COUNT(*) FROM risk_state").fetchone()[0]
    assert rows == 1, "risk_state 必须按 (tenant_id, scope) 单行 upsert"