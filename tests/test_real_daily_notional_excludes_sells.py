"""回归：实盘当日额度只计开仓，卖出（减仓）不得占用。

背景（10-22 实盘事故，Backlog 第 38 项）：
`real_trading.check_real_trade()` 在风控引擎**之前**执行，而它调用的
`submitted_real_entry_notional_since()` 把 **BUY 与 SELL 混算**进同一道
`REAL_MAX_DAILY_NOTIONAL`。后果是「该平仓时平不掉」：

- 实盘 AMZN 看跌期权盈利后，策略连续生成 9 笔锁盈/盈利保护卖出，
  **全部被同一句「当日累计名义金额将超过 $5,000」拒绝**；
- 仓位因此无法退出，一路持有到到期，承担被行权做成裸空的风险。

卖出是**降低**敞口，有独立且更宽的额度（`risk.py` 的
`RISK_MAX_REDUCTION_ORDER_NOTIONAL` / `RISK_MAX_DAILY_REDUCTION_NOTIONAL`）在风控引擎处生效，
不该在实盘闸门这里重复限制。

本文件锁定「SELL 不计入当日开仓额度」这条不变量。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from futu_quant.store import TradingStore
from futu_quant.real_trading import submitted_real_entry_notional_since


def _insert_intent(store: TradingStore, *, side: str, price: float, qty: float = 1.0,
                   status: str = "FILLED", account_id: str = "281756481476169109") -> None:
    """插入一条 REAL 模式的 intent（最小字段，够额度计算用）。"""
    now = datetime.now(timezone.utc)
    metadata = (
        '{"execution_mode":"REAL","account_id":"%s","multiplier":100.0}' % account_id
    )
    # intent_key 有唯一约束，用计数 + 纳秒时间戳保证不撞键
    _insert_intent.seq += 1
    key = f"test:US.AMZN261028P250000:{side}:{_insert_intent.seq}:{now.timestamp()}:REAL"
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO order_intents
               (strategy_id,intent_key,symbol,side,quantity,limit_price,status,
                filled_quantity,average_fill_price,created_at,updated_at,metadata_json,tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "volatility_long_gamma",
                key,
                "US.AMZN261028P250000",
                side,
                qty,
                price,
                status,
                qty if status.startswith("FILLED") else 0.0,
                price if status.startswith("FILLED") else 0.0,
                now.isoformat(),
                now.isoformat(),
                metadata,
                "futu_real_master",
            ),
        )


_insert_intent.seq = 0


def test_sell_does_not_consume_daily_entry_budget(tmp_path):
    """核心不变量：SELL 不得计入当日开仓额度。"""
    store = TradingStore(tmp_path / "sell_budget.db")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    _insert_intent(store, side="BUY", price=2.94)      # 开仓 1 张 ≈ $294
    buy_only = submitted_real_entry_notional_since(store, since)
    assert buy_only == pytest.approx(294.0), f"BUY 应计入开仓额度，实际 {buy_only}"

    _insert_intent(store, side="SELL", price=3.25)     # 减仓 1 张 ≈ $325
    after_sell = submitted_real_entry_notional_since(store, since)
    assert after_sell == pytest.approx(294.0), (
        f"SELL 计入开仓额度会导致「该平仓时平不掉」，实际 {after_sell}"
    )


def test_multiple_sells_still_do_not_block_entry_budget(tmp_path):
    """多笔卖出后，开仓额度占用应保持不变（复现 10-22 的连续拒绝场景）。"""
    store = TradingStore(tmp_path / "many_sells.db")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    _insert_intent(store, side="BUY", price=4.08)
    baseline = submitted_real_entry_notional_since(store, since)

    for price in (3.25, 3.30, 3.20, 3.35, 3.10, 3.15, 3.40, 3.05, 3.45):
        _insert_intent(store, side="SELL", price=price)

    assert submitted_real_entry_notional_since(store, since) == pytest.approx(baseline), (
        "多笔 SELL 后开仓额度占用发生变化 —— 减仓卖出被误算进开仓额度"
    )


def test_buy_still_counts_toward_entry_budget(tmp_path):
    """回归：BUY 仍必须计入开仓额度，不得因修复而失去限额保护。"""
    store = TradingStore(tmp_path / "buy_still_counts.db")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    _insert_intent(store, side="BUY", price=4.08)
    _insert_intent(store, side="BUY", price=2.94)
    total = submitted_real_entry_notional_since(store, since)
    assert total == pytest.approx(408.0 + 294.0), f"两笔 BUY 应合计计入，实际 {total}"


def test_excluded_intent_side_is_respected(tmp_path):
    """exclude_intent_id 仍应生效（既有行为不得被破坏）。"""
    store = TradingStore(tmp_path / "exclude.db")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    _insert_intent(store, side="BUY", price=5.0)
    total = submitted_real_entry_notional_since(store, since)
    assert total > 0

    # 排除一条 BUY 后，占用应下降
    with store.connection() as conn:
        row = conn.execute("SELECT id FROM order_intents LIMIT 1").fetchone()
    excluded = submitted_real_entry_notional_since(store, since, exclude_intent_id=row["id"])
    assert excluded == pytest.approx(0.0), "exclude_intent_id 未生效"
