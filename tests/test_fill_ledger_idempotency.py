"""A 修复守卫：一笔成交经多条写入路径上报，`fill_ledger` 必须只记一次。

背景（10-22 实盘事故的真实路径）：
  路径① deal push  → `record_fill(source='BROKER_DEAL', broker_deal_id=...)`
  路径② order push → `update_by_broker_order(...)` 的 delta 插入（source='BROKER_POLL_DIFF'）
  路径③ 轮询重放   → 同一路径再来一次

两处修复（Chief 落盘）：
  - `store.update_by_broker_order` 的投影插入：去掉 `source='BROKER_POLL_DIFF'` 限定，
    改为**不分来源查累计**，累计 ≥ 本笔累计成交量即跳过；
  - `store.record_fill` 的 deal 路径：若账本已有「同 intent + 同方向 + 同数量 + 同价格 +
    120 秒内 + 无 deal_id」的投影行，**认领**它（补 deal_id 与真实来源），不再插新行。

断言口径：无论经过几条路径、重放几次，`fill_ledger` 都是「补到券商累计成交」的投影。
"""
from __future__ import annotations

from futu_quant.store import TradingStore

SYMBOL = "US.AMZN261028P250000"


def _ledger(store: TradingStore, intent_id: int) -> list[dict]:
    with store.connection() as c:
        rows = c.execute(
            "SELECT source,quantity,price,broker_deal_id FROM fill_ledger "
            "WHERE intent_id=? ORDER BY id",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _make_intent(store: TradingStore, key: str) -> int:
    intent, _ = store.create_intent(
        "volatility_long_gamma", key, SYMBOL, "SELL", 1, 3.25,
        {"execution_mode": "REAL", "multiplier": 100},
    )
    intent_id = intent["id"]
    with store.connection() as c:
        c.execute(
            "UPDATE order_intents SET broker_order_id=? WHERE id=?", (f"BRK-{key}", intent_id)
        )
    return intent_id


def _total(rows: list[dict]) -> float:
    return round(sum(float(r["quantity"]) for r in rows), 6)


def test_deal_push_then_order_push_records_once(tmp_path):
    """场景 1：全额成交，deal push 先到、order push 后到（真金双计路径）。"""
    store = TradingStore(tmp_path / "s1.db")
    intent_id = _make_intent(store, "full")
    store.record_fill(
        intent_id, 1.0, 3.25, 2.51, source="BROKER_DEAL", broker_deal_id="DEAL-1", multiplier=100,
    )
    store.update_by_broker_order("BRK-full", "FILLED", 1.0, 3.25, 2.51)

    rows = _ledger(store, intent_id)
    assert len(rows) == 1, f"两条路径上报同一笔，账本应只有一行，实际 {len(rows)}"
    assert _total(rows) == 1.0


def test_order_push_then_deal_push_records_once(tmp_path):
    """场景 2：顺序反过来（order push 先到、deal push 后到）——修复前的漏点。

    认领的**可见效果**是把投影行 `source` 升级为 `BROKER_DEAL`，并按 Product 裁定
    **保留 `broker_deal_id = NULL`**（宁可归因未知，不可归因错误）。
    双计本身另由 `record_fill` 的过计护栏兜底（防御纵深），故本用例只认领效果。
    """
    store = TradingStore(tmp_path / "s2.db")
    intent_id = _make_intent(store, "rev")
    store.update_by_broker_order("BRK-rev", "FILLED", 1.0, 3.25, 2.51)
    store.record_fill(
        intent_id, 1.0, 3.25, 2.51, source="BROKER_DEAL", broker_deal_id="DEAL-9", multiplier=100,
    )

    rows = _ledger(store, intent_id)
    assert len(rows) == 1, f"反序上报应认领既有投影行，实际 {len(rows)} 行"
    assert _total(rows) == 1.0
    assert rows[0]["source"] == "BROKER_DEAL", (
        f"认领未升级 source（实际 {rows[0]['source']!r}）—— 投影行应被改标为券商成交"
    )
    assert rows[0]["broker_deal_id"] is None, (
        "认领不得写入 broker_deal_id —— 该行来自累计上报，硬标 deal_id 就是制造假归因"
    )


def test_partial_fills_with_alternating_paths(tmp_path):
    """场景 3：部分成交 0.5 → 0.5，两条路径交替上报（Chief 点名的定向场景）。"""
    store = TradingStore(tmp_path / "s3.db")
    intent_id = _make_intent(store, "part")
    store.record_fill(
        intent_id, 0.5, 3.20, 1.2, source="BROKER_DEAL", broker_deal_id="DEAL-A", multiplier=100,
    )
    # 同一笔的重复上报 → 跳过
    store.update_by_broker_order("BRK-part", "FILLED_PART", 0.5, 3.20, 1.2)
    assert len(_ledger(store, intent_id)) == 1, "半仓的重复上报应被跳过"

    store.record_fill(
        intent_id, 0.5, 3.30, 1.3, source="BROKER_DEAL", broker_deal_id="DEAL-B", multiplier=100,
    )
    # 累计 1.0 已满足 → order push 的累计上报应跳过
    store.update_by_broker_order("BRK-part", "FILLED", 1.0, 3.25, 2.5)

    rows = _ledger(store, intent_id)
    assert len(rows) == 2, f"两笔真实成交应各一行，实际 {len(rows)}"
    assert _total(rows) == 1.0


def test_poll_replay_of_same_cumulative_is_idempotent(tmp_path):
    """场景 4：同一上报连续重放 4 次，只记一行。"""
    store = TradingStore(tmp_path / "s4.db")
    intent_id = _make_intent(store, "replay")
    for _ in range(4):
        store.update_by_broker_order("BRK-replay", "FILLED", 1.0, 3.25, 2.51)

    rows = _ledger(store, intent_id)
    assert len(rows) == 1, f"重放应幂等，实际 {len(rows)} 行"
    assert _total(rows) == 1.0


def test_genuine_increments_are_not_suppressed(tmp_path):
    """场景 5：真正的新增成交必须照常落账（不得误杀）。"""
    store = TradingStore(tmp_path / "s5.db")
    intent_id = _make_intent(store, "grow")
    store.update_by_broker_order("BRK-grow", "FILLED_PART", 0.3, 3.10, 0.7)
    store.update_by_broker_order("BRK-grow", "FILLED_PART", 0.6, 3.15, 1.5)
    store.update_by_broker_order("BRK-grow", "FILLED", 1.0, 3.25, 2.5)

    rows = _ledger(store, intent_id)
    assert len(rows) == 3, f"三次真实增量应各落一行，实际 {len(rows)}"
    assert _total(rows) == 1.0


def test_filled_quantity_reset_then_re_reported_does_not_double_count(tmp_path):
    """场景 6：`filled_quantity` 被重置后券商再报同一笔 —— 今晚 13 条双计的实际成因。"""
    store = TradingStore(tmp_path / "s6.db")
    intent_id = _make_intent(store, "reset")
    store.update_by_broker_order("BRK-reset", "FILLED", 1.0, 3.25, 2.51)
    with store.connection() as c:
        c.execute("UPDATE order_intents SET filled_quantity=0 WHERE id=?", (intent_id,))
    store.update_by_broker_order("BRK-reset", "FILLED", 1.0, 3.25, 2.51)

    rows = _ledger(store, intent_id)
    assert len(rows) == 1, f"重置后重报不得再插，实际 {len(rows)} 行"
    assert _total(rows) == 1.0


def test_over_count_path_is_prevented(tmp_path):
    """守卫（Reviewer 推演的过计路径 · 预防侧）：order push 先报全量累计 + 后续 deal push 分笔到达。

    序列：两笔真成交各 0.5、同价、几乎同时；
      1. order push 先到并报累计 1.0 → 账本 0 < 1.0 → 插 proxy P1（qty=1.0，无 deal_id）；
      2. deal push（deal A，qty=0.5）→ 认领判据对 P1 的 1.0 不匹配 → 插 R1（0.5）→ 累计 1.5；
      3. deal push（deal B，qty=0.5）→ 同样不匹配 → 插 R2 → 累计 2.0 vs 券商 1.0 → **过计 1.0**。

    `update_by_broker_order` 的累计判定只能**阻止继续增长**，不会修复已发生的过计；
    两个插入点都必须加过计防护（`ledger_total + delta > broker_total + eps` → 跳过插入），
    否则过计会静默留在账本里 —— 今晚一切问题的母题。
    """
    store = TradingStore(tmp_path / "over_prev.db")
    intent_id = _make_intent(store, "overprev")

    store.update_by_broker_order("BRK-overprev", "FILLED", 1.0, 3.25, 2.51)   # 步骤 1
    store.record_fill(
        intent_id, 0.5, 3.25, 1.2, source="BROKER_DEAL", broker_deal_id="DEAL-A", multiplier=100,
    )                                                                        # 步骤 2
    store.record_fill(
        intent_id, 0.5, 3.25, 1.2, source="BROKER_DEAL", broker_deal_id="DEAL-B", multiplier=100,
    )                                                                        # 步骤 3

    rows = _ledger(store, intent_id)
    assert _total(rows) <= 1.0 + 1e-6, (
        f"过计未被预防：账本累计 {_total(rows)} > 券商累计 1.0"
        f"（行: {[(r['quantity'], r['broker_deal_id']) for r in rows]}）"
    )


def test_over_count_is_detected_when_it_occurs(tmp_path):
    """守卫（过计探测侧）：万一过计从别处漏出，下一轮上报必须检出并告警。

    与预防侧成对：预防负责"不发生"，探测负责"发生了必须可见"。
    """
    store = TradingStore(tmp_path / "over_detect.db")
    intent_id = _make_intent(store, "overdetect")
    # 模拟别处漏出的一行过计（直接写账本，绕过两个插入点）
    with store.connection() as c:
        c.execute(
            """INSERT INTO fill_ledger
            (intent_id,strategy_id,symbol,side,quantity,price,multiplier,fee_total,source,filled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (intent_id, "volatility_long_gamma", SYMBOL, "SELL", 2.0, 3.25, 100, 2.5,
             "LEAK", store.now()),
        )
    # 券商再报累计 1.0 → 账本 2.0 > 券商 1.0 → 必须告警
    store.update_by_broker_order("BRK-overdetect", "FILLED", 1.0, 3.25, 2.51)

    alerts = [a for a in store.alerts(limit=50) if a["event_type"] == "FILL_LEDGER_OVER_COUNT"]
    events = [
        e for e in store.operational_events(limit=200)
        if e["event_type"] == "FILL_LEDGER_OVER_COUNT"
    ]
    assert alerts or events, (
        "过计未被检出 —— 账本累计已超过券商累计，但没有任何 FILL_LEDGER_OVER_COUNT 告警/事件，"
        "过计会静默留在账本里"
    )
    # 明细（intent_id / 双方累计）落在 operational_events 的 payload_json；
    # alerts 表本身没有 payload 列，其 message 里带 intent_id。
    payload = ""
    for record in (events if events else alerts):
        payload += str(record.get("payload_json") or "")
        payload += str(record.get("message") or "")
    assert str(intent_id) in payload, f"过计记录未带 intent_id，无法定位：{payload[:160]}"


def test_deal_push_replayed_after_reset_stays_one_row(tmp_path):
    """场景 7：重置 + deal push 路径按同一 deal_id 重报，仍一行。"""
    store = TradingStore(tmp_path / "s7.db")
    intent_id = _make_intent(store, "reset2")
    store.record_fill(
        intent_id, 1.0, 3.25, 2.51, source="BROKER_DEAL", broker_deal_id="DEAL-R", multiplier=100,
    )
    with store.connection() as c:
        c.execute("UPDATE order_intents SET filled_quantity=0 WHERE id=?", (intent_id,))
    store.record_fill(
        intent_id, 1.0, 3.25, 2.51, source="BROKER_DEAL", broker_deal_id="DEAL-R", multiplier=100,
    )

    rows = _ledger(store, intent_id)
    assert len(rows) == 1, f"同 deal_id 重报应幂等，实际 {len(rows)} 行"
