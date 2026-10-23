"""回归：fill_ledger 幂等 —— 同一笔成交被重复上报时不得双计。

背景（P0-5，10-22 实测）：
`update_by_broker_order()` 用 `INSERT OR IGNORE` 写 fill_ledger，但该表**没有唯一约束**，
`OR IGNORE` 实际上不产生任何去重效果。同一笔成交被多个轮次重复上报时（轮询对账、
重启补偿、对账周期重放），就会插入多行 —— 实测 **13 个 intent 双计**，
其中 intent 818 的两行相隔 **4 天**（2026-10-15 与 2026-10-19），正是跨轮次重放所致。

后果：账本持仓被双计（本地账本多于券商实物），直接污染三账对账。
本文件锁定「同 intent + 同方向 + 同量 + 同价 的 BROKER_POLL_DIFF 行只允许存在一条」。
"""
from __future__ import annotations

from datetime import datetime, timezone

from futu_quant.store import TradingStore


def _seed_intent(store: TradingStore, *, symbol: str = "US.AMZN261120P205000",
                 side: str = "BUY", qty: float = 1.0, price: float = 10.0,
                 status: str = "SUBMITTED", broker_order_id: str = "brk-1") -> int:
    now = datetime.now(timezone.utc)
    metadata = '{"execution_mode":"PAPER","multiplier":100.0,"reference_price":9.8}'
    with store.connection() as conn:
        cur = conn.execute(
            """INSERT INTO order_intents
               (strategy_id,intent_key,symbol,side,quantity,limit_price,status,
                filled_quantity,average_fill_price,created_at,updated_at,metadata_json,tenant_id,
                broker_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "volatility_long_gamma",
                f"test:{symbol}:{side}:{now.timestamp()}",
                symbol, side, qty, price, status, 0.0, 0.0,
                now.isoformat(), now.isoformat(), metadata, "futu_paper_sandbox",
                broker_order_id,
            ),
        )
        return int(cur.lastrowid)


def _fill_count(store: TradingStore, intent_id: int) -> int:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT count(*) n FROM fill_ledger WHERE intent_id=?", (intent_id,)
        ).fetchone()
    return int(row["n"])


def test_repeated_poll_fill_does_not_double_count(tmp_path):
    """同一笔成交被重复上报两次 → fill_ledger 只能有一行。"""
    store = TradingStore(tmp_path / "dedup.db")
    intent_id = _seed_intent(store)

    store.update_by_broker_order("brk-1", "FILLED", 1.0, 10.0, 2.5)
    assert _fill_count(store, intent_id) == 1, "首次上报应写入一行"

    # 模拟跨轮次重放：同样的成交再报一次（甚至 4 天后）
    store.update_by_broker_order("brk-1", "FILLED", 1.0, 10.0, 2.5)
    assert _fill_count(store, intent_id) == 1, (
        "重复上报导致 fill_ledger 双计 —— 账本持仓会多于券商实物，污染三账对账"
    )


def test_partial_fill_progression_still_records(tmp_path):
    """回归：真实的部分成交推进（0 → 0.5 → 1.0）必须逐笔记录，不得被去重误杀。"""
    store = TradingStore(tmp_path / "partial.db")
    intent_id = _seed_intent(store, qty=2.0, broker_order_id="brk-2")

    store.update_by_broker_order("brk-2", "FILLED_PART", 1.0, 10.0, 1.25)
    assert _fill_count(store, intent_id) == 1

    store.update_by_broker_order("brk-2", "FILLED", 2.0, 10.0, 2.5)
    assert _fill_count(store, intent_id) == 2, "部分成交推进应各记一行"


def test_different_price_fills_are_not_collapsed(tmp_path):
    """不同成交价的两次成交必须分别记账（去重不得按 intent 粗放合并）。"""
    store = TradingStore(tmp_path / "twoprice.db")
    intent_id = _seed_intent(store, qty=2.0, broker_order_id="brk-3")

    store.update_by_broker_order("brk-3", "FILLED_PART", 1.0, 10.0, 1.25)
    store.update_by_broker_order("brk-3", "FILLED", 2.0, 11.0, 2.5)
    assert _fill_count(store, intent_id) == 2, "不同价成交被错误合并"
