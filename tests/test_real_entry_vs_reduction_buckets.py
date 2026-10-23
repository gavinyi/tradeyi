"""回归：实盘额度按方向分桶 —— BUY 计开仓额度，SELL 计减仓额度（P0-4 完整模型）。

背景（10-22 实盘事故，Product 额度模型）：
`check_real_trade()` 此前用**同一个桶**统计所有订单（含卖出）的当日名义金额，
且两处计算都套 `abs()`、都不看 `side` —— 于是「白天开仓用满额度 →
晚上盈利的平仓单被自己的买入额度挡住」，实测连续 9 笔卖出被拒。

修复（两条独立口径）：
- **开仓额度**：`submitted_real_entry_notional_since()` 只计 BUY；
- **减仓额度**：新增 `submitted_real_reduction_notional_since()` 只计 SELL，
  限额来自 `RISK_MAX_REDUCTION_ORDER_NOTIONAL` / `RISK_MAX_DAILY_REDUCTION_NOTIONAL`
  —— 与 `risk.py` 共用同一组环境变量，杜绝第三套实现。

本文件锁定「开满开仓额度后，卖出仍能通过」这条不变量。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from futu_quant.real_trading import (
    reduction_notional_limits,
    submitted_real_entry_notional_since,
    submitted_real_reduction_notional_since,
)
from futu_quant.store import TradingStore


def _insert_intent(store: TradingStore, *, side: str, price: float, qty: float = 1.0,
                   status: str = "FILLED", account_id: str = "281756481476169109",
                   symbol: str = "US.AMZN261028P250000") -> None:
    now = datetime.now(timezone.utc)
    metadata = '{"execution_mode":"REAL","account_id":"%s","multiplier":100.0}' % account_id
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


def test_buy_and_sell_are_counted_in_separate_buckets(tmp_path):
    """BUY 只进开仓桶、SELL 只进减仓桶，互不占用。"""
    store = TradingStore(tmp_path / "buckets.db")
    since = "2000-01-01T00:00:00+00:00"

    _insert_intent(store, side="BUY", price=2.94)      # 开仓 ≈ $294
    assert submitted_real_entry_notional_since(store, since) == pytest.approx(294.0)
    assert submitted_real_reduction_notional_since(store, since) == pytest.approx(0.0), (
        "BUY 被计入减仓桶"
    )

    _insert_intent(store, side="SELL", price=3.25)     # 减仓 ≈ $325
    assert submitted_real_reduction_notional_since(store, since) == pytest.approx(325.0)
    assert submitted_real_entry_notional_since(store, since) == pytest.approx(294.0), (
        "SELL 被计入开仓桶 —— 盈利的平仓单会被自己的买入额度挡住"
    )


def test_entry_budget_exhausted_does_not_block_sell(tmp_path):
    """核心不变量：开仓额度用尽后，卖出仍能通过（分桶互不干扰）。"""
    store = TradingStore(tmp_path / "exhausted.db")
    since = "2000-01-01T00:00:00+00:00"

    # 开仓桶塞满（模拟白天把开仓额度用掉）
    for price in (4.08, 2.94, 3.50):
        _insert_intent(store, side="BUY", price=price)
    entry_used = submitted_real_entry_notional_since(store, since)
    assert entry_used > 1000.0, f"测试前提：开仓额度应已被占用，实际 {entry_used}"

    # 减仓桶仍应是空的 —— 卖出不受开仓额度影响
    assert submitted_real_reduction_notional_since(store, since) == pytest.approx(0.0)


def test_reduction_limits_come_from_shared_env(tmp_path, monkeypatch):
    """减仓额度必须与 risk.py 共用同一组环境变量（不得另立默认值）。"""
    monkeypatch.setenv("RISK_MAX_REDUCTION_ORDER_NOTIONAL", "12345")
    monkeypatch.setenv("RISK_MAX_DAILY_REDUCTION_NOTIONAL", "67890")
    order_limit, daily_limit = reduction_notional_limits()
    assert (order_limit, daily_limit) == (12345.0, 67890.0), (
        "减仓额度未读取共享环境变量 —— 会与 risk.py 形成两套口径"
    )


def test_default_reduction_limits_match_risk_engine():
    """默认值必须与 risk.py 的默认值一致。"""
    order_limit, daily_limit = reduction_notional_limits()
    assert (order_limit, daily_limit) == (150_000.0, 500_000.0)
