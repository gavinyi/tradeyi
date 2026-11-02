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


def test_capacity_line_is_three_percent_of_equity():
    assert CAPACITY_EQUITY_RATIO == 0.03
    assert capacity_equity_line(REAL_EQUITY) == pytest.approx(199.38, abs=0.01)
    assert capacity_equity_line(0) == 0.0
    assert capacity_equity_line(None) == 0.0
    assert capacity_equity_line(-100) == 0.0


def test_minimum_premium_uses_ask_not_mid():
    row = {"bid_price": 1.00, "ask_price": 2.00}
    assert minimum_premium(row) == pytest.approx(200.0)
    assert minimum_premium(row, lot=2) == pytest.approx(400.0)
    assert minimum_premium({"bid_price": 1.0}) is None


def test_premium_over_capacity_rejected_with_reason():
    # ask 6.55 → $655，超过 $199.38 容量线（正是 2026-11-02 被拒的实盘那张）。
    result = _fc(_frame(_quote(ask_price=6.55, bid_price=6.50)))
    assert result.empty
    assert result.reasons == {REASON_PREMIUM: 1}
    assert result.cheapest_premium == pytest.approx(655.0)
    assert result.needed_equity == pytest.approx(21833.33, abs=1.0)
    assert "需要权益约 $21,833" in result.message()


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
        _quote(code="US.AMZN261120C260000", strike_price=255.0, ask_price=3.0),
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
    return filter_candidates(pd.DataFrame(), equity=REAL_EQUITY, spot=SPOT)


def test_three_empty_rounds_pause_new_entries_and_alert(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    for expected_streak in (1, 2, 3):
        state = record_selection_round(store, TENANT, _empty_round())
        assert state["streak"] == expected_streak
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
    assert store.get_setting(MARKER_KEY.format(tenant_id=TENANT)) == "true"
    assert any("VOLATILITY_CAPACITY_PAUSED" in str(row) for row in store.alerts(limit=50))
    assert volatility_new_entries_paused_for_tenant(store, "futu_paper_sandbox") is False


def test_two_empty_rounds_do_not_pause(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    record_selection_round(store, TENANT, _empty_round())
    record_selection_round(store, TENANT, _empty_round())
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is False


def test_manual_pause_is_never_auto_released(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    set_volatility_new_entries_paused(store, TENANT, True)
    for _ in range(3):
        record_selection_round(store, TENANT, _empty_round())
    passing = _fc(_frame())
    state = record_selection_round(store, TENANT, passing)
    assert state["action"] == "none"
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True


def test_capacity_pause_auto_released_when_candidate_fits(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    for _ in range(3):
        record_selection_round(store, TENANT, _empty_round())
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
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
    candidates = _frame(_quote(ask_price=6.55, bid_price=6.50))

    kept, result = _apply_real_capacity_precheck(
        store, _AccountBroker(), campaign, candidates, "US.AMZN", SPOT,
    )
    assert kept.empty
    assert result.equity == pytest.approx(REAL_EQUITY)
    assert result.line == pytest.approx(199.38, abs=0.01)
    assert result.reasons == {REASON_PREMIUM: 1}


def test_paper_tenant_never_gets_capacity_pause(tmp_path):
    store = TradingStore(tmp_path / "capacity.db")
    assert is_real_account(_AccountBroker("SIMULATE")) is False
    for _ in range(3):
        record_selection_round(store, TENANT, _empty_round())
    assert volatility_new_entries_paused_for_tenant(store, TENANT) is True
    paper = capacity_pause_state(store, "futu_paper_sandbox")
    assert paper["paused"] is False and paper["owned"] is False
