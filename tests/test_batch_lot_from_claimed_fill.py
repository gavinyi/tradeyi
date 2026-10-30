"""批次链回归：认领「轮询投影行」的成交也必须登记 position_batches / batch_lots。

背景（2026-10-31 排查）：实盘最常见的成交顺序是
    轮询先写一行 BROKER_POLL_DIFF 投影行 → 成交推送带 deal_id 到达并「认领」它。
`record_fill` 的认领分支此前直接 `return`（只升级 source），完全跳过批次登记，
于是全库 `position_batches` 恒为 0 行：分档台账、按批次复盘、R 倍数全部失效。

本用例锁死三件事：
1. 认领路径必须补建批次与批次明细；
2. 重复投递同一 deal_id 不得把同一笔成交记成两条明细（仓位不能翻倍）；
3. 先推送后轮询（无投影行）的老路径行为不变。
"""
from __future__ import annotations

from futu_quant.store import TradingStore

BATCH_ID = "volatility_long_gamma:US.COIN261120P140000:cycle0:stage1"
SYMBOL = "US.COIN261120P140000"


def _intent(store: TradingStore) -> dict:
    intent, _ = store.create_intent(
        "volatility_long_gamma", "k1", SYMBOL, "BUY", 1, 9.15,
        {
            "batch_id": BATCH_ID,
            "underlying": "US.COIN",
            "structure": "SINGLE",
            "initial_risk": 73.2,
        },
    )
    return intent


def _lots(store: TradingStore) -> int:
    with store.connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM batch_lots").fetchone()[0])


def _insert_poll_projection(store: TradingStore, intent: dict) -> int:
    """复刻轮询路径的直插（`store.py` 的 BROKER_POLL_DIFF 分支）：不经 record_fill。"""
    now = store.now()
    with store.connection() as connection:
        cursor = connection.execute(
            """INSERT INTO fill_ledger
            (intent_id,strategy_id,symbol,side,quantity,price,multiplier,commission,
             regulatory_fee,fee_total,source,quote_json,filled_at,
             fee_is_estimate,fee_unavailable_reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (intent["id"], intent["strategy_id"], intent["symbol"], intent["side"],
             1, 9.15, 100.0, 0.65, 0.0, 0.65, "BROKER_POLL_DIFF", "{}", now, 1, ""),
        )
        return int(cursor.lastrowid)


def test_claimed_poll_projection_still_creates_batch(tmp_path):
    store = TradingStore(tmp_path / "claim.db")
    intent = _intent(store)

    # 1) 轮询先直插一行投影（拿不到 deal_id），此时没有任何批次
    projection = _insert_poll_projection(store, intent)
    assert store.position_batches(strategy_id="volatility_long_gamma") == []

    # 2) 推送带 deal_id 到达：认领同一行，并且**必须**建批次
    claimed = store.record_fill(
        intent["id"], 1, 9.15, 0.65, source="BROKER_DEAL", broker_deal_id="DEAL-1",
    )
    assert claimed == projection, "认领应返回既有的账本行，而不是新插一行"
    batches = store.position_batches(strategy_id="volatility_long_gamma")
    assert len(batches) == 1
    assert batches[0]["batch_id"] == BATCH_ID
    assert float(batches[0]["entry_qty"]) == 1.0
    assert float(batches[0]["remaining_qty"]) == 1.0
    assert _lots(store) == 1

    # 3) 同一 deal_id 重复投递：账本与批次都不得翻倍
    again = store.record_fill(
        intent["id"], 1, 9.15, 0.65, source="BROKER_DEAL", broker_deal_id="DEAL-1",
    )
    assert again == claimed
    assert _lots(store) == 1
    assert float(store.position_batches(strategy_id="volatility_long_gamma")[0]["entry_qty"]) == 1.0


def test_push_first_path_unchanged(tmp_path):
    """没有投影行可认领时（推送先到），照旧插行并建批次。"""
    store = TradingStore(tmp_path / "push.db")
    intent = _intent(store)

    fill_id = store.record_fill(
        intent["id"], 1, 9.15, 0.65, source="BROKER_DEAL", broker_deal_id="DEAL-9",
    )
    assert fill_id is not None
    batches = store.position_batches(strategy_id="volatility_long_gamma")
    assert len(batches) == 1 and float(batches[0]["entry_qty"]) == 1.0
    assert _lots(store) == 1
