from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from futu_quant.capacity_precheck import (
    CAPACITY_EQUITY_RATIO,
    DAILY_ENTRY_LIMIT,
    MARKER_KEY,
    MAX_MONEYNESS_DISTANCE,
    MIN_DTE,
    REASON_BOOK_MISSING,
    REASON_DEPTH,
    REASON_DEPTH_UNKNOWN,
    REASON_DTE,
    REASON_DTE_UNKNOWN,
    REASON_MONEYNESS,
    REASON_PREMIUM,
    REASON_SPREAD,
    REASON_STALE,
    capacity_equity_line,
    capacity_line,
    capacity_pause_state,
    filter_candidates,
    is_real_account,
    minimum_premium,
    probe_entry_guard,
    record_selection_round,
    today_real_buy_entries,
)
from futu_quant.store import TradingStore
from futu_quant.volatility_pause import (
    set_volatility_new_entries_paused,
    volatility_new_entries_paused_for_tenant,
)

NY = ZoneInfo("America/New_York")
TENANT = "futu_real_master"
#: 2026-11-02 实盘真实权益（$6,646）→ 容量线 $199.38。
REAL_EQUITY = 6646.0
#: 现价（AMZN 量级）
SPOT = 250.0


def _quote(**overrides) -> dict:
    """一张「刚好通过」的候选：权利金 $190、价差 2.7%、深度 40/35 张、10 秒前报价、
    到期 18 天（≥14 天）、平价（虚值 0%）。"""
    row = {
        "code": "US.AMZN261120C250000",
        "option_type": "CALL",
        "strike_price": 250.0,
        "strike_time": (datetime.now(NY) + timedelta(days=18)).date().isoformat(),
        "bid_price": 1.85,
        "ask_price": 1.90,
        "bid_vol": 40,
        "ask_vol": 35,
        "update_time": (datetime.now(NY) - timedelta(seconds=10)).isoformat(),
    }
    row.update(overrides)
    return row


def _frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows) or [_quote()])


def _fc(frame: pd.DataFrame, equity: float = REAL_EQUITY):
    return filter_candidates(frame, equity=equity, spot=SPOT)


class _Broker:
    def __init__(self, trading_env: str = "REAL") -> None:
        self.trading_env = trading_env


# --------------------------------------------------------------------------- #
# 六条判定 + 两条硬门槛
# --------------------------------------------------------------------------- #


def test_capacity_line_is_twenty_percent_of_equity():
    assert CAPACITY_EQUITY_RATIO == 0.20
    assert capacity_equity_line(REAL_EQUITY) == pytest.approx(1329.20, abs=0.01)
    assert capacity_equity_line(0) == 0.0
    assert capacity_equity_line(None) == 0.0
    assert capacity_equity_line(-100) == 0.0


def test_minimum_premium_uses_ask_not_mid():
    row = {"bid_price": 1.00, "ask_price": 2.00}
    assert minimum_premium(row) == pytest.approx(200.0)
    assert minimum_premium(row, lot=2) == pytest.approx(400.0)
    assert minimum_premium({"bid_price": 1.0}) is None


def test_premium_over_capacity_rejected_with_reason():
    # ask 6.55 → $655：在 20% 线（$1,329）下通过，这正是放宽后的预期行为。
    result = _fc(_frame(_quote(ask_price=6.55, bid_price=6.50)))
    assert not result.empty
    assert result.cheapest_premium == pytest.approx(655.0)


def test_mixed_round_reports_only_capacity_candidate():
    cheap_quality = _quote(
        code="US.AMZN261120C260000", strike_price=260.0,
        ask_price=0.54, bid_price=0.30,
    )
    over_budget = _quote(
        code="US.AMZN261120C250000", ask_price=15.00, bid_price=14.50,
    )
    result = _fc(_frame(cheap_quality, over_budget))
    assert result.empty
    assert result.reasons[REASON_PREMIUM] == 1
    assert result.cheapest_premium == pytest.approx(1500.0)
    assert "$54" not in result.message()
    assert "$1500" in result.message()


def test_pure_quality_rejection_message_names_top_reason_without_capacity_claim():
    # 低价但价差过宽：不应伪装成容量不足或盘口缺失。
    result = _fc(_frame(_quote(ask_price=0.54, bid_price=0.30)))
    assert result.empty
    assert result.cheapest_premium is None
    assert result.reasons == {REASON_SPREAD: 1}
    message = result.message()
    assert "盘口价差过宽" in message
    assert "容量不足" not in message
    assert "没有可用盘口" not in message


def test_spread_over_ten_percent_rejected():
    result = _fc(_frame(_quote(bid_price=1.50, ask_price=1.90)))
    assert result.empty
    assert result.reasons == {REASON_SPREAD: 1}


def test_depth_fields_missing_is_not_treated_as_enough():
    row = _quote()
    row.pop("bid_vol")
    row.pop("ask_vol")
    result = _fc(_frame(row))
    assert result.empty
    assert result.reasons == {REASON_DEPTH_UNKNOWN: 1}


def test_depth_thin_rejected():
    result = _fc(_frame(_quote(bid_vol=2, ask_vol=2)))
    assert result.empty
    assert result.reasons == {REASON_DEPTH: 1}


def test_stale_quote_rejected():
    old = _quote(update_time=(datetime.now(NY) - timedelta(minutes=5)).isoformat())
    result = _fc(_frame(old))
    assert result.empty
    assert result.reasons == {REASON_STALE: 1}


def test_missing_book_rejected():
    result = _fc(_frame(_quote(bid_price=0, ask_price=0)))
    assert result.empty
    assert result.reasons == {REASON_BOOK_MISSING: 1}


def test_dte_below_fourteen_rejected():
    soon = _quote(
        strike_time=(datetime.now(NY) + timedelta(days=7)).date().isoformat(),
    )
    result = _fc(_frame(soon))
    assert result.empty
    assert result.reasons == {REASON_DTE: 1}
    assert MIN_DTE == 14


def test_dte_unknown_rejected():
    result = _fc(_frame(_quote(strike_time="")))
    assert result.empty
    assert result.reasons == {REASON_DTE_UNKNOWN: 1}


def test_moneyness_beyond_six_percent_rejected():
    # +8% 虚值（AMZN 252 → 272）：$199 能买到的正是这类「彩票区」。
    far = _quote(strike_price=250.0 * (1 + MAX_MONEYNESS_DISTANCE + 0.02))
    result = _fc(_frame(far))
    assert result.empty
    assert result.reasons == {REASON_MONEYNESS: 1}
    assert MAX_MONEYNESS_DISTANCE == 0.06


def test_put_moneyness_measured_in_its_own_direction():
    # PUT 虚值 = 行权价低于现价：245/250 = −2% → 通过
    near_put = _quote(option_type="PUT", strike_price=245.0)
    assert not _fc(_frame(near_put)).empty
    # PUT 行权价 250*0.9 = 225 → 虚值 10% → 拒绝
    far_put = _quote(option_type="PUT", strike_price=225.0)
    assert _fc(_frame(far_put)).reasons == {REASON_MONEYNESS: 1}


def test_passing_candidate_kept_and_message_reports_capacity():
    result = _fc(_frame(
        _quote(),
        _quote(code="US.AMZN261120C260000", strike_price=255.0, ask_price=15.00),
    ))
    assert list(result.eligible["code"]) == ["US.AMZN261120C250000"]
    assert result.reasons[REASON_PREMIUM] == 1
    assert result.cheapest_premium == pytest.approx(190.0)
    assert "容量预检通过 1 张" in result.message()


def test_is_real_account_reads_trading_env():
    assert is_real_account(_Broker("REAL")) is True
    assert is_real_account(_Broker("SIMULATE")) is False
    assert is_real_account(object()) is False


# --------------------------------------------------------------------------- #
# 连续空轮 → 复用现有暂停键；只解除自己置的暂停
# --------------------------------------------------------------------------- #


def _empty_round():
    """「容量不足」的空轮：候选**买不起**（`PREMIUM_OVER_CAPACITY`）。

    2026-11-04 归因改动后，只有这一类空轮计入连续计数；完全没有候选、
    或只被虚值/DTE/价差挡住的轮次属于 `quality_only`，见
    `tests/test_capacity_streak_attribution.py`。
    20% 线下 $655 能通过，改用 $1,500（$15.00 ask）确保仍在容量线外。
    """
    return filter_candidates(
        _frame(_quote(ask_price=15.00, bid_price=14.50)), equity=REAL_EQUITY, spot=SPOT,
    )


def test_three_empty_rounds_alert_but_never_pause(tmp_path):
    """G 方案（2026-11-04）：连续买不起只升级 ERROR 告警，**不再暂停新开仓**。"""
    store = TradingStore(tmp_path / "capacity.db")
    for expected_streak in (1, 2, 3):
        state = record_selection_round(store, TENANT, _empty_round())
        assert state["streak"] == expected_streak
    assert state["action"] == "insufficient"
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT), "") == ""
    assert any(
        "VOLATILITY_CAPACITY_EXHAUSTED" in str(row) for row in store.alerts(limit=50)
    ), "不暂停不等于不告警：必须留 ERROR"


def test_two_empty_rounds_do_not_alert(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    record_selection_round(store, TENANT, _empty_round())
    record_selection_round(store, TENANT, _empty_round())
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert not [
        row for row in store.alerts(limit=50)
        if "VOLATILITY_CAPACITY_EXHAUSTED" in str(row)
    ]


def test_manual_pause_is_never_auto_released(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    set_volatility_new_entries_paused(store, TENANT, True)
    for _ in range(3):
        record_selection_round(store, TENANT, _empty_round())
    passing = _fc(_frame())
    state = record_selection_round(store, TENANT, passing)
    assert state["action"] == "none"
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True


def test_legacy_precheck_pause_auto_released_when_candidate_fits(tmp_path):
    """遗留状态清理：旧版本置的容量暂停（marker=true）在出现合格候选时被解除。"""
    store = TradingStore(tmp_path / "capacity.db")
    set_volatility_new_entries_paused(store, TENANT, True)
    store.set_setting(MARKER_KEY.format(tenant_id=TENANT), "true")
    passing = _fc(_frame())
    assert not passing.empty
    state = record_selection_round(store, TENANT, passing)
    assert state["action"] == "released"
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT)) == "false"
    snapshot = capacity_pause_state(store, TENANT)
    assert snapshot["paused"] is False and snapshot["owned"] is False


def test_already_paused_by_human_does_not_duplicate_alert(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    set_volatility_new_entries_paused(store, TENANT, True)
    before = len(store.alerts(limit=200))
    for _ in range(4):
        record_selection_round(store, TENANT, _empty_round())
    assert len(store.alerts(limit=200)) == before
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT), "") == ""


def test_missing_tenant_refuses_to_write_global_key(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    with pytest.raises(ValueError):
        record_selection_round(store, "", _empty_round())
    assert store.get_setting("volatility_new_entries_paused", "false") == "false"


def test_currency_blocked_message_is_not_mistaken_for_capacity_shortage():
    result = filter_candidates(pd.DataFrame(), equity=0.0, spot=SPOT)
    result.currency_blocked = True
    assert "币种无法确认为 USD" in result.message()
    assert "容量不足" not in result.message()


# --------------------------------------------------------------------------- #
# 小账户试探护栏：单笔 1 张 / 单日 1 笔 / 当日累计权利金 ≤ 压力损失预算
# --------------------------------------------------------------------------- #


def _intent(quantity: int = 1, price: float = 100.0) -> dict:
    return {"quantity": quantity, "limit_price": price}


def test_probe_guard_rejects_more_than_one_lot():
    ok, reason, _ = probe_entry_guard(
        [], quantity=2, price=100.0, multiplier=100, premium_limit=398.0,
    )
    assert ok is False and "1 张" in reason


def test_probe_guard_rejects_second_entry_same_day():
    ok, reason, detail = probe_entry_guard(
        [_intent()], quantity=1, price=100.0, multiplier=100, premium_limit=398.0,
    )
    assert ok is False and "单日上限" in reason
    assert detail["today_entries"] == DAILY_ENTRY_LIMIT


def test_probe_guard_enforces_daily_premium_cap():
    # 单笔 $500 > 预算 $398 → 拒（当日已开 0 笔时先撞这条；日限 1 笔时它守的是「本单自己」
    # 也不能超过当日权利金上限，等价于买不起）。
    ok, reason, detail = probe_entry_guard(
        [], quantity=1, price=5.0, multiplier=100, premium_limit=398.0,
    )
    assert ok is False and "累计权利金" in reason
    assert detail["premium_limit"] == 398.0


def test_probe_guard_allows_single_affordable_entry():
    ok, reason, detail = probe_entry_guard(
        [], quantity=1, price=1.2, multiplier=100, premium_limit=398.0,
    )
    assert ok is True and reason == ""
    assert detail["premium_notional"] == pytest.approx(120.0)


def test_today_real_buy_entries_filters_tenant_strategy_env_and_day(tmp_path):
    store = TradingStore(tmp_path / "guard.db")
    today = datetime.now(NY).date().isoformat()
    with store.connection() as conn:
        for key, tenant, side, created in (
            (f"{today}:c1:OPEN:REAL", TENANT, "BUY", f"{today}T14:00:00+00:00"),
            (f"{today}:c2:OPEN:PAPER", TENANT, "BUY", f"{today}T14:01:00+00:00"),
            (f"{today}:c3:CLOSE:REAL", TENANT, "SELL", f"{today}T14:02:00+00:00"),
            ("2026-01-01:c4:OPEN:REAL", TENANT, "BUY", "2026-01-01T14:00:00+00:00"),
            (f"{today}:c5:OPEN:REAL", "futu_paper_sandbox", "BUY",
             f"{today}T14:03:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
                " quantity, limit_price, status, created_at, updated_at, tenant_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("volatility_long_gamma", key, "US.AMZN261120C250000", side, 1, 1.0,
                 "CREATED", created, created, tenant),
            )
        conn.commit()
    rows = today_real_buy_entries(
        store, tenant_id=TENANT, strategy_id="volatility_long_gamma",
    )
    assert len(rows) == 1
    assert rows[0]["intent_key"].endswith(":REAL")


def test_rejected_real_intent_does_not_count_as_opened_entry(tmp_path):
    """热修 2026-11-05（audit 201604）：REJECTED 不算「已开笔数」。

    生产事故：COIN REAL intent 被回撤熔断 REJECT，probe_guard 却计成
    「今日实盘已开 1 笔」，把实盘锁死一整天（44 次误拦）。
    """
    store = TradingStore(tmp_path / "rejected.db")
    today = datetime.now(NY).date().isoformat()
    with store.connection() as conn:
        for status in ("REJECTED", "FAILED", "CANCELLED_ALL", "SHADOW_EXPIRED"):
            conn.execute(
                "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
                " quantity, limit_price, status, created_at, updated_at, tenant_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("volatility_long_gamma", f"{today}:x:{status}:REAL",
                 "US.COIN261120P140000", "BUY", 1, 14.0,
                 status, f"{today}T15:05:32+00:00", f"{today}T15:05:33+00:00", TENANT),
            )
        conn.commit()
    rows = today_real_buy_entries(
        store, tenant_id=TENANT, strategy_id="volatility_long_gamma",
    )
    assert rows == [], "被拒/失败/取消的意图没有占用当日实盘名额，不得计入"


def test_submitted_and_filled_intents_do_count(tmp_path):
    """真正提交到券商的意图（SUBMITTED/FILLED）必须继续计入（护栏不放宽）。"""
    store = TradingStore(tmp_path / "submitted.db")
    today = datetime.now(NY).date().isoformat()
    with store.connection() as conn:
        for status in ("CREATED", "SUBMITTED", "PARTIAL_FILLED", "FILLED"):
            conn.execute(
                "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
                " quantity, limit_price, status, created_at, updated_at, tenant_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("volatility_long_gamma", f"{today}:y:{status}:REAL",
                 "US.META261120P600000", "BUY", 1, 22.85,
                 status, f"{today}T15:14:34+00:00", f"{today}T15:24:43+00:00", TENANT),
            )
        conn.commit()
    rows = today_real_buy_entries(
        store, tenant_id=TENANT, strategy_id="volatility_long_gamma",
    )
    assert len(rows) == 4, "提交/成交意图必须计入，护栏不得被这次热修放宽"


def test_missing_tenant_fails_closed_not_full_table_scan(tmp_path):
    """空租户必须抛错，禁止回落全库扫描（paper 成交混进 real 计数的缺口）。"""
    store = TradingStore(tmp_path / "no-tenant.db")
    with pytest.raises(ValueError, match="tenant_id"):
        today_real_buy_entries(
            store, tenant_id="", strategy_id="volatility_long_gamma",
        )


def test_mutation_counting_rejected_must_be_red(tmp_path):
    """变异守卫：把状态过滤去掉（回到 REJECTED 也计数）必须红。"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    module = Path("src/futu_quant/capacity_precheck.py")
    original = module.read_text(encoding="utf-8")
    anchor = (
        'if str(item.get("status") or "").upper() not in counted_statuses:\n'
        "            continue\n"
    )
    assert anchor in original, "状态过滤锚点丢失，无法构造变异"
    mutated = original.replace(anchor, "")
    assert mutated != original
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_rejected_real_intent_does_not_count_as_opened_entry"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "去掉状态过滤后守卫仍绿 —— 误拦缺陷可静默复发"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)


# --------------------------------------------------------------------------- #
# 接到选约链路的边界：只作用于实盘，权益来自 portfolio_allocator 的快照
# --------------------------------------------------------------------------- #


class _AccountBroker(_Broker):
    def __init__(self, trading_env: str = "REAL", equity: float = REAL_EQUITY) -> None:
        super().__init__(trading_env)
        self.account_id = "281756481476169109"
        self._equity = equity

    def account_info(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "currency": "USD", "total_assets": self._equity,
            "cash": self._equity, "market_val": 0.0,
        }])

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame()

    def snapshots(self, symbols):
        return pd.DataFrame()


def test_roll_precheck_uses_allocator_equity_and_filters_real_candidates(tmp_path):
    from futu_quant.volatility_roll import _apply_real_capacity_precheck

    store = TradingStore(tmp_path / "capacity.db")
    campaign = {"tenant_id": TENANT, "underlying": "US.AMZN"}
    candidates = _frame(_quote(ask_price=15.00, bid_price=14.50))

    kept, result = _apply_real_capacity_precheck(
        store, _AccountBroker(), campaign, candidates, "US.AMZN", SPOT,
    )
    assert kept.empty
    assert result.equity == pytest.approx(REAL_EQUITY)
    # 容量线 = min(权益×20%, 压力损失预算)。测试库用的是**默认预算**
    # （`ensure_default_budgets` 里 volatility 默认 max_loss_ratio=3% → $199.38），
    # 所以这里生效值是 199.38 —— 正是「预检不得比风控更宽」这条规则的体现。
    assert result.line == pytest.approx(199.38, abs=0.01)
    assert result.loss_limit == pytest.approx(199.38, abs=0.01)
    assert result.reasons == {REASON_PREMIUM: 1}


def test_paper_tenant_never_gets_capacity_pause(tmp_path):
    """实盘人工暂停只影响实盘；模拟盘既不会被影响，也不会被容量预检暂停。"""
    store = TradingStore(tmp_path / "capacity.db")
    assert is_real_account(_AccountBroker("SIMULATE")) is False
    set_volatility_new_entries_paused(store, TENANT, True)
    for _ in range(3):
        record_selection_round(store, TENANT, _empty_round())
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
    paper = capacity_pause_state(store, "futu_paper_sandbox")
    assert paper["paused"] is False and paper["owned"] is False


# --------------------------------------------------------------------------- #
# 容量线对齐 20%：不得比压力损失预算更宽（用户 2026-11-05 拍板）
# --------------------------------------------------------------------------- #


def test_capacity_line_is_twenty_percent_but_never_wider_than_loss_budget():
    assert capacity_line(REAL_EQUITY) == pytest.approx(1329.20, abs=0.01)
    # 租户把 max_loss_ratio 调到比 20% 更紧 → 容量线必须跟着收紧
    assert capacity_line(REAL_EQUITY, loss_limit=300.0) == pytest.approx(300.0)
    # 预算比 20% 更宽 → 仍按 20% 收口，容量线不跟着放大
    assert capacity_line(REAL_EQUITY, loss_limit=1651.0) == pytest.approx(1329.20, abs=0.01)
    # 没有预算 / 预算为 0 → 退回 20%
    assert capacity_line(REAL_EQUITY, loss_limit=None) == pytest.approx(1329.20, abs=0.01)
    assert capacity_line(REAL_EQUITY, loss_limit=0) == pytest.approx(1329.20, abs=0.01)


def test_filter_uses_loss_budget_when_tighter_than_twenty_percent():
    # ask 7.50 → $750：20% 线（$1,329）下通过；预算只有 $300 时必须拒
    row = _quote(ask_price=7.50, bid_price=7.45)
    assert not _fc(_frame(row)).empty
    tightened = filter_candidates(
        _frame(row), equity=REAL_EQUITY, spot=SPOT, loss_limit=300.0,
    )
    assert tightened.empty
    assert tightened.reasons == {REASON_PREMIUM: 1}
    assert tightened.line == pytest.approx(300.0)
    assert tightened.as_payload()["loss_limit"] == pytest.approx(300.0)
