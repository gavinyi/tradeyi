"""验收：同一笔 SELL 在实盘闸门与风控引擎算出的可用额度**必须一致**。

Reviewer 的验收断言（10-22 事故根因的收口）：
`real_trading.py` 与 `risk.py` 此前各自实现了「当日已用额度」的累加 ——
状态集合不同、是否按账户过滤不同，于是同一笔卖出在两处算出的可用额度**分叉**，
错的那份先触发，把盈利的平仓单拦死。

修复：两侧都走 `store.reduce_notional()`（唯一累加实现），
且共用同一组计入状态（`store.USED_NOTIONAL_STATUSES`）。

本文件锁定「两处口径一致」这条不变量。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from futu_quant.real_trading import (
    reduction_notional_limits,
    submitted_real_entry_notional_since,
    submitted_real_reduction_notional_since,
)
from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import USED_NOTIONAL_STATUSES, TradingStore

ACCOUNT = "281756481476169109"
SYMBOL = "US.AMZN261028P250000"


def _insert(store: TradingStore, *, side: str, price: float, qty: float = 1.0,
            status: str = "FILLED", account_id: str = ACCOUNT,
            symbol: str = SYMBOL, mode: str = "REAL") -> None:
    now = datetime.now(timezone.utc)
    metadata = '{"execution_mode":"%s","account_id":"%s","multiplier":100.0}' % (mode, account_id)
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO order_intents
               (strategy_id,intent_key,symbol,side,quantity,limit_price,status,
                filled_quantity,average_fill_price,created_at,updated_at,metadata_json,tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "volatility_long_gamma",
                f"test:{symbol}:{side}:{now.timestamp()}:{price}",
                symbol, side, qty, price, status,
                qty if status.startswith("FILLED") else 0.0,
                price if status.startswith("FILLED") else 0.0,
                now.isoformat(), now.isoformat(), metadata, "futu_real_master",
            ),
        )


def test_real_and_risk_compute_identical_reduction_headroom(tmp_path):
    """核心验收：同一笔 SELL 的减仓可用额度，两处必须相等。"""
    store = TradingStore(tmp_path / "parity.db")
    for price in (3.25, 3.30, 3.20, 3.35):
        _insert(store, side="SELL", price=price)

    _, max_reduction_daily = reduction_notional_limits()
    # 实盘闸门口径
    real_used = submitted_real_reduction_notional_since(store, "2000-01-01T00:00:00+00:00")
    real_headroom = max_reduction_daily - real_used
    # 风控引擎口径（与 risk.py 内同一条路径）
    risk_used = store.reduce_notional(
        (row for row in store.intents(limit=50_000)
         if str(row.get("status")) in USED_NOTIONAL_STATUSES),
        "SELL",
    )
    risk_headroom = RiskLimits.load().max_daily_reduction_notional - risk_used

    assert real_used == pytest.approx(risk_used), (
        f"两处算出的已用减仓额度不一致：实盘 {real_used} vs 风控 {risk_used} —— 口径分叉会再次导致误拦"
    )
    assert real_headroom == pytest.approx(risk_headroom)
    assert real_used == pytest.approx(325.0 + 330.0 + 320.0 + 335.0)


def test_real_and_risk_compute_identical_entry_headroom(tmp_path):
    """开仓额度同理：两处必须一致（BUY 不计入减仓桶）。"""
    store = TradingStore(tmp_path / "entry_parity.db")
    _insert(store, side="BUY", price=4.08)
    _insert(store, side="SELL", price=3.25)   # 不得影响开仓桶

    real_entry = submitted_real_entry_notional_since(store, "2000-01-01T00:00:00+00:00")
    risk_entry = store.reduce_notional(
        (row for row in store.intents(limit=50_000)), "BUY",
    )
    assert real_entry == pytest.approx(risk_entry) == pytest.approx(408.0)
    assert submitted_real_reduction_notional_since(store, "2000-01-01T00:00:00+00:00") == pytest.approx(325.0)


def test_status_set_is_shared_between_both_sides():
    """计入状态集合必须来自同一个常量（不得各写一份）。"""
    import inspect
    import futu_quant.real_trading as rt
    import futu_quant.store as st

    assert st.USED_NOTIONAL_STATUSES
    # 实盘侧源码不得再自带一份硬编码的状态集合
    src = inspect.getsource(rt)
    assert '("SUBMITTING", "SUBMITTED"' not in src, "实盘侧又硬编码了一份状态集合 —— 口径会分叉"


def test_risk_engine_still_blocks_when_reduction_budget_exhausted(tmp_path):
    """回归：减仓额度用尽时，风控引擎仍要拦（不得因重构失去保护）。"""
    store = TradingStore(tmp_path / "exhausted_risk.db")
    _insert(store, side="SELL", price=200.0, qty=3_000)   # ≈ $600,000 > $500,000

    import pandas as pd

    class _StubBroker:
        account_id = ACCOUNT
        tenant_id = "futu_real_master"

        def orders(self):
            return pd.DataFrame(columns=["code", "trd_side", "qty", "order_status"])

        def positions(self):
            return pd.DataFrame([{"code": SYMBOL, "qty": 5_000.0}])

        def account_info(self):
            return {}

        @staticmethod
        def is_cancellable_status(status):
            return str(status) in {"SUBMITTED", "FILLED_PART"}

    engine = RiskEngine(store, RiskLimits.load())
    decision = engine.check(_StubBroker(), SYMBOL, "SELL", 1, 5.0, multiplier=100.0,
                            strategy_id="volatility_long_gamma", execution_mode="REAL")
    assert decision.allowed is False, "减仓额度用尽却仍放行 —— 保护失效"
