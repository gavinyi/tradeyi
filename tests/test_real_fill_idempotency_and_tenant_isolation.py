"""回归：A 真金 fill_ledger 幂等 / B 跨租户隔离 / C 静默 catch 补告警 / E 文案 / F 推送路径写显式止损限额。

背景（10-23 Reviewer 复审 + Product 定案）：
- **A（硬阻塞）**：`DealPushHandler` 对 REAL 调 `record_fill`，而 `OrderPushHandler`
  对 REAL 调 `update_by_broker_order`（delta 插入、无 deal_id、无唯一约束）
  → 一笔真金成交产生**两条** `fill_ledger` 行。`fill_ledger` 驱动 P&L、
  `position_batch`、`allocate_exit` 平仓成本 → **真金盈亏与平仓成本翻倍**。
  修法：`update_by_broker_order` 的 delta 插入改成**幂等投影**（账本 = 补到券商累计成交），
  且不依赖 `deal_id` 一定拿得到。
- **B（产品红线）**：`register_fill_to_campaign` 的 state key 无租户维度、
  `owned` 判定扫全局 campaign、推送线程写全局配置 → 跨租户连坐。
- **C**：两处静默 catch（读配置失败 / 标记写失败）必须补告警。
- **E**：heartbeat 文案仍写「模拟盘」，而 REAL 也走这条路。
- **F（关键）**：推送路径新建的 campaign 必须带显式止损限额，
  否则 P0-3 的 fail-closed 被自己的写入路径绕过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.order_push import register_fill_to_campaign  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
SYMBOL = "US.AMZN261028P250000"
# #1 键级分区后 ADOPT 写的是**租户键**，断言要读那个键（全局键里本来就该是空的）
tenant_key = f"VOLATILITY_CAMPAIGNS:{REAL_TENANT}"


def _intent(broker_order_id: str | None = "FH-1") -> dict:
    return {"id": 7, "symbol": SYMBOL, "broker_order_id": broker_order_id,
            "metadata_json": '{"multiplier":100,"execution_mode":"REAL"}'}


# ------------------------------------------------------------------ A 幂等投影

def _make_intent(store: TradingStore, key: str) -> int:
    intent, _ = store.create_intent(
        "volatility_long_gamma", key, SYMBOL, "BUY", 1, 3.25,
        {"execution_mode": "REAL", "multiplier": 100},
    )
    intent_id = intent["id"]
    with store.connection() as c:
        c.execute(
            "UPDATE order_intents SET broker_order_id=? WHERE id=?",
            (f"BRK-{key}", intent_id),
        )
    return intent_id


def _ledger_qty(store: TradingStore, intent_id: int) -> float:
    with store.connection() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(quantity),0) t FROM fill_ledger WHERE intent_id=?", (intent_id,),
        ).fetchone()
    return float(row["t"])


def test_real_fill_is_idempotent_across_push_paths(tmp_path):
    """A 核心：一笔真金成交经两条推送路径，fill_ledger 必须恰好覆盖券商累计（一行/一个数量）。"""
    store = TradingStore(tmp_path / "idempotent.db")
    intent_id = _make_intent(store, "realpaths")

    # 路径①：deal push —— 按单笔成交写
    store.record_fill(intent_id, 1.0, 3.25, 2.5, source="BROKER_DEAL",
                      broker_deal_id="DEAL-1", multiplier=100)
    # 路径②：order push —— 按券商累计成交写
    store.update_by_broker_order("BRK-realpaths", "FILLED", 1.0, 3.25, 2.5,
                                 source="BROKER_PUSH")

    assert _ledger_qty(store, intent_id) == pytest.approx(1.0), (
        "真金成交经两条路径后账本翻倍 —— fill_ledger 驱动 P&L 与平仓成本，会直接算错"
    )


def test_filled_quantity_reset_then_re_reported_does_not_double_count(tmp_path):
    """A 第 4 条（Reviewer 点名）：`filled_quantity` 被重置后，券商再报同一笔不得再插。

    这是 10-22 那 13 条双计的**实际成因**（intent 818 两行相隔 4 天）——
    intent 的 `filled_quantity` 被重置为 0 后，若幂等判定只看 intent 自身的量，
    就会认为「还没记过」而再插一行。判定必须落在**账本累计**上。
    """
    store = TradingStore(tmp_path / "reset.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "r:REAL", SYMBOL, "BUY", 1, 3.25,
        {"execution_mode": "REAL", "multiplier": 100},
    )
    intent_id = intent["id"]
    with store.connection() as c:
        c.execute(
            "UPDATE order_intents SET broker_order_id=? WHERE id=?", ("BRK-reset", intent_id),
        )

    store.update_by_broker_order("BRK-reset", "FILLED", 1.0, 3.25, 2.5, source="BROKER_PUSH")
    assert _ledger_qty(store, intent_id) == pytest.approx(1.0)

    # 模拟 intent 的 filled_quantity 被重置（外部写入，绕过 update_by_broker_order 的
    # terminal/earlier 早退分支 —— 那正是今晚双计漏掉的路径）
    with store.connection() as c:
        c.execute(
            "UPDATE order_intents SET filled_quantity=0, average_fill_price=0 WHERE id=?",
            (intent_id,),
        )

    # 券商再报同一笔
    store.update_by_broker_order("BRK-reset", "FILLED", 1.0, 3.25, 2.5, source="BROKER_PUSH")

    assert _ledger_qty(store, intent_id) == pytest.approx(1.0), (
        "filled_quantity 重置后重报导致双计 —— 正是 10-22 那 13 条脏行的成因"
    )


def test_deal_push_replay_after_order_push_is_one_row(tmp_path):
    """A 第 2 条反向（Chief 探针抓出的第二处）：order push 先到、deal push 后到。

    deal push 只查 `broker_deal_id`（查不到）→ 必须**认领**已有的投影行
    （同 intent + 同方向 + 同数量 + 同价格 + 无 deal_id），而不是再插一行。
    """
    store = TradingStore(tmp_path / "order_first.db")
    intent_id = _make_intent(store, "orderfirst")

    # order push 先写投影行
    store.update_by_broker_order("BRK-orderfirst", "FILLED", 1.0, 3.25, 2.5,
                                 source="BROKER_PUSH")
    # deal push 后到
    store.record_fill(intent_id, 1.0, 3.25, 2.5, source="BROKER_DEAL",
                      broker_deal_id="DEAL-9", multiplier=100)

    rows = _ledger_rows(store, intent_id)
    assert len(rows) == 1, f"order push 先到、deal push 后到仍产生 {len(rows)} 行"
    assert _ledger_qty(store, intent_id) == pytest.approx(1.0)


def _ledger_rows(store: TradingStore, intent_id: int) -> list[dict]:
    with store.connection() as c:
        rows = c.execute(
            "SELECT source,quantity,price FROM fill_ledger WHERE intent_id=? ORDER BY id",
            (intent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_partial_fills_alternating_paths_converge_to_broker_total(tmp_path):
    """A 定向用例（Chief 点名）：部分成交 2 笔 × 0.5，**两条路径交替上报**。

    账本必须是「补到券商累计成交」的投影 —— 判定看**累计**，不看行数。
    双计的定义是「同一笔被记两次」（累计会超过券商累计），不是「行数 > 1」。
    """
    store = TradingStore(tmp_path / "partial.db")
    intent, _ = store.create_intent(
        "volatility_long_gamma", "p:REAL", SYMBOL, "BUY", 2, 3.25,
        {"execution_mode": "REAL", "multiplier": 100},
    )
    intent_id = intent["id"]
    with store.connection() as c:
        c.execute(
            "UPDATE order_intents SET broker_order_id=? WHERE id=?", ("BRK-partial", intent_id),
        )

    # ① deal push 部分成交 0.5
    store.record_fill(intent_id, 0.5, 3.25, 1.25, source="BROKER_DEAL",
                      broker_deal_id="D1", multiplier=100)
    # ② order push 累计 0.5
    store.update_by_broker_order("BRK-partial", "FILLED_PART", 0.5, 3.25, 1.25,
                                 source="BROKER_PUSH")
    # ③ deal push 第二笔 0.5
    store.record_fill(intent_id, 0.5, 3.30, 1.25, source="BROKER_DEAL",
                      broker_deal_id="D2", multiplier=100)
    # ④ order push 累计 1.0
    store.update_by_broker_order("BRK-partial", "FILLED", 1.0, 3.27, 2.5,
                                 source="BROKER_PUSH")

    assert _ledger_qty(store, intent_id) == pytest.approx(1.0), (
        "部分成交 + 交替上报后累计不等于券商累计 —— 账本失真，P&L 与平仓成本会算错"
    )


def test_repeated_order_push_does_not_double_count(tmp_path):
    """同一笔被 order push 重复上报 N 次，账本仍只覆盖券商累计。"""
    store = TradingStore(tmp_path / "repeat.db")
    intent_id = _make_intent(store, "repeat")
    for _ in range(5):
        store.update_by_broker_order("BRK-repeat", "FILLED", 1.0, 3.25, 2.5,
                                     source="BROKER_PUSH")
    assert _ledger_qty(store, intent_id) == pytest.approx(1.0)


def test_idempotent_hit_does_not_emit_false_overcount(tmp_path):
    """幂等命中**不得**误报过计 —— 此前「已覆盖」被当成「过计」告警，刷屏且掩盖真过计。"""
    store = TradingStore(tmp_path / "nospam.db")
    intent_id = _make_intent(store, "nospam")
    store.record_fill(intent_id, 1.0, 3.25, 2.5, source="BROKER_DEAL",
                      broker_deal_id="DEAL-X", multiplier=100)
    store.update_by_broker_order("BRK-nospam", "FILLED", 1.0, 3.25, 2.5,
                                 source="BROKER_PUSH")

    events = [
        e for e in store.operational_events(limit=50)
        if e["event_type"] == "FILL_LEDGER_OVER_COUNT"
    ]
    assert not events, f"幂等命中被误报成过计：{events}"


def test_real_overcount_still_alarms(tmp_path):
    """回归：真的过计仍必须告警（不得因改判定而失去过计防护）。"""
    store = TradingStore(tmp_path / "realover.db")
    intent_id = _make_intent(store, "realover")
    # 直接写一行超额账本，模拟别处漏写
    with store.connection() as c:
        c.execute(
            """INSERT INTO fill_ledger
            (intent_id,strategy_id,symbol,side,quantity,price,multiplier,fee_total,source,filled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (intent_id, "volatility_long_gamma", SYMBOL, "BUY", 2.0, 3.25, 100, 2.5,
             "LEAK", store.now()),
        )
    store.update_by_broker_order("BRK-realover", "FILLED", 1.0, 3.25, 2.5)

    events = [
        e for e in store.operational_events(limit=50)
        if e["event_type"] == "FILL_LEDGER_OVER_COUNT"
    ]
    assert events, "真过计未被告警 —— 过计防护失效"


# ------------------------------------------------------------------ B 跨租户

def test_state_key_is_tenant_scoped(tmp_path):
    """B：state key 必须带租户维度，否则实盘与模拟同 code 共用 state。

    走「已有归属」分支 —— 先由本租户建 campaign，再登记成交，才会写 state。
    """
    store = TradingStore(tmp_path / "tenant_state.db")
    # 先由**实盘租户**建归属
    assert register_fill_to_campaign(
        store, None, _intent("FH-A"), 1, 10.0, REAL_TENANT,
    )["action"] == "ADOPTED"
    # 再来一笔同租户成交 → 走 MARKED 分支，写带租户维度的 state key
    result = register_fill_to_campaign(store, None, _intent("FH-B"), 1, 10.0, REAL_TENANT)
    assert result["action"] == "MARKED", f"同租户同 code 未被认成已有归属：{result}"
    keys = store.campaign_states()
    assert f"volatility_long_gamma:{REAL_TENANT}:{SYMBOL}" in keys, (
        f"state key 未带租户维度 —— 跨租户共用。实际 keys: {list(keys)}"
    )


def test_state_key_not_polluted_across_tenants(tmp_path):
    """B：两个租户各自登记，state key 互不覆盖。"""
    store = TradingStore(tmp_path / "two_tenants.db")
    register_fill_to_campaign(store, None, _intent("FH-R"), 1, 10.0, REAL_TENANT)
    register_fill_to_campaign(store, None, _intent("FH-R2"), 1, 11.0, REAL_TENANT)
    register_fill_to_campaign(store, None, _intent("FH-P"), 1, 12.0, PAPER_TENANT)
    register_fill_to_campaign(store, None, _intent("FH-P2"), 1, 13.0, PAPER_TENANT)
    keys = store.campaign_states()
    assert f"volatility_long_gamma:{REAL_TENANT}:{SYMBOL}" in keys
    assert f"volatility_long_gamma:{PAPER_TENANT}:{SYMBOL}" in keys
    # 两边各自记的是自己的成交价，未被对方覆盖
    assert keys[f"volatility_long_gamma:{REAL_TENANT}:{SYMBOL}"]["registered_fill_price"] == 11.0
    assert keys[f"volatility_long_gamma:{PAPER_TENANT}:{SYMBOL}"]["registered_fill_price"] == 13.0


def test_adoption_is_tenant_tagged(tmp_path):
    """B：推送线程新建的 campaign 必须带 tenant_id。"""
    store = TradingStore(tmp_path / "tenant_tag.db")
    register_fill_to_campaign(store, None, _intent(), 1, 10.0, REAL_TENANT)
    adopted = [c for c in store.campaigns(tenant_id=REAL_TENANT) if c.get("code") == SYMBOL]
    assert adopted and adopted[0].get("tenant_id") == REAL_TENANT, (
        "campaign 未带 tenant_id —— 跨租户可见，无法隔离"
    )


def test_same_code_different_tenant_not_treated_as_owned(tmp_path):
    """B 核心：模拟租户的同 code campaign 不得让实盘成交被误判「已有归属」。"""
    store = TradingStore(tmp_path / "cross.db")
    # 模拟租户先建了同 code 的 campaign
    register_fill_to_campaign(store, None, _intent(), 1, 10.0, PAPER_TENANT)
    # 实盘成交再来 —— 不得因为「同 code 已有 campaign」就不登记
    result = register_fill_to_campaign(store, None, _intent("FH-2"), 1, 10.0, REAL_TENANT)
    assert result["action"] == "ADOPTED", (
        f"实盘成交被跨租户误判为已有归属：{result}"
    )


# ------------------------------------------------------------------ C 静默 catch

def test_config_read_failure_alarms(tmp_path, monkeypatch):
    """C：读配置失败必须留 ERROR，不得无声 return。"""
    import futu_quant.config as cfg

    store = TradingStore(tmp_path / "cfgfail.db")
    monkeypatch.setattr(cfg, "load_volatility_campaigns", _boom)

    result = register_fill_to_campaign(store, None, _intent(), 1, 10.0, REAL_TENANT)
    assert result is None
    events = [
        e for e in store.operational_events(limit=20)
        if e["event_type"] == "FILL_REGISTRATION_CONFIG_UNREADABLE"
    ]
    assert events, "读配置失败未留告警 —— 静默的失败"


def _boom(*a, **k):
    raise RuntimeError("config unreadable")


def test_no_silent_bare_pass_in_registration():
    """C：源码层面不得再有裸 `except: pass` / 无声 `return None`。"""
    src = (ROOT / "src" / "futu_quant" / "order_push.py").read_text(encoding="utf-8")
    body = src.split("def register_fill_to_campaign")[1].split("\ndef open_order_push")[0]
    assert "except Exception:\n            pass" not in body, "仍存在裸 except: pass"
    assert "except Exception:\n            return None" not in body, "仍存在无声 return None"


# ------------------------------------------------------------------ E 文案

def test_heartbeat_text_mentions_tenant_not_simulate():
    """E：heartbeat 文案不得再写死「模拟盘」—— REAL 也走这条路。"""
    src = (ROOT / "src" / "futu_quant" / "order_push.py").read_text(encoding="utf-8")
    assert "模拟盘订单推送已接收" not in src, "heartbeat 文案仍写死模拟盘"
    assert "租户" in src


# ------------------------------------------------------------------ F 显式止损限额

def test_pushed_campaign_carries_explicit_stop_limits(tmp_path):
    """F：推送路径新建的 campaign 必须带显式止损限额（否则绕过 P0-3 的 fail-closed）。"""
    store = TradingStore(tmp_path / "limits.db")
    register_fill_to_campaign(store, None, _intent(), 1, 10.0, REAL_TENANT)
    adopted = [c for c in store.campaigns(tenant_id=REAL_TENANT) if c.get("code") == SYMBOL]
    assert adopted, "未建立 campaign"
    assert adopted[0].get("max_position_loss_usd") == 300.0
    assert adopted[0].get("max_position_loss_ratio") == 0.25


def test_adopt_path_carries_explicit_stop_limits(tmp_path):
    """F：券商确认的接管路径（adopt_volatility_orphan_position）同样必须带显式限额。"""
    from futu_quant.volatility_orphans import adopt_volatility_orphan_position, VolatilityOrphanPosition

    store = TradingStore(tmp_path / "adopt_limits.db")
    item = VolatilityOrphanPosition(
        code=SYMBOL, underlying="US.AMZN", quantity=1, can_sell_quantity=1,
        cost_price=3.25, nominal_price=3.5, market_value=350.0, pnl_value=-52.0,
        pnl_ratio=-12.94, strategy_net_quantity=1.0,
        pending_order_count=0, status="SELLABLE_ORPHAN", reason="test",
    )
    adopted = adopt_volatility_orphan_position(store, item, [], REAL_TENANT)
    assert adopted.get("max_position_loss_usd") == 300.0
    assert adopted.get("max_position_loss_ratio") == 0.25
    assert adopted.get("tenant_id") == REAL_TENANT
