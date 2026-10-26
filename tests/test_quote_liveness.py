"""批 B：期权行情「盘口活性」判据的守卫（2026-10-26）。

根因：Futu 期权快照 `update_time` 是**成交驱动**的，深度 ITM 十几分钟不成交就冻结，
但 bid/ask 实时在动 —— 旧判据把这类合约全判「行情过期」，当日 91 条拦截里 82 条如此。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from futu_quant.data_quality import option_quote_activity, option_quote_freshness
from futu_quant.option_history import collect_option_snapshots
from futu_quant.quote_liveness import BookActivityTracker, book_signature, option_book_moved
from futu_quant.store import TradingStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

NY = ZoneInfo("America/New_York")


def _row(bid: float, ask: float, last: float = 0.0) -> dict:
    return {"bid_price": bid, "ask_price": ask, "last_price": last}


def _quote_at(seconds_ago: int) -> str:
    stamp = datetime.now(NY) - timedelta(seconds=seconds_ago)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 1. 盘口活性工具
# ---------------------------------------------------------------------------


def test_book_signature_accepts_both_column_names():
    assert book_signature({"bid_price": 1.5, "ask_price": 1.7, "last_price": 1.6}) == (1.5, 1.7, 1.6)
    assert book_signature({"bid": 2, "ask": 3}) == (2.0, 3.0, 0.0)
    assert book_signature(None) == (0.0, 0.0, 0.0)


def test_option_book_moved_only_counts_real_changes():
    assert option_book_moved(_row(1.0, 1.2), _row(1.0, 1.2)) is False
    assert option_book_moved(_row(1.0, 1.2), _row(1.1, 1.2)) is True
    assert option_book_moved(_row(1.0, 1.2), _row(1.0, 1.3)) is True
    # 没有盘口不能当成「在动」
    assert option_book_moved(_row(1.0, 1.2), _row(0.0, 0.0)) is False
    assert option_book_moved(None, _row(1.0, 1.2)) is False


def test_book_activity_tracker_reports_age_since_last_change():
    tracker = BookActivityTracker()
    t0 = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
    assert tracker.observe("US.X", _row(1.0, 1.2), t0) is None  # 首次 = 未知
    assert tracker.observe("US.X", _row(1.0, 1.2), t0 + timedelta(seconds=40)) == 40.0
    assert tracker.observe("US.X", _row(1.1, 1.2), t0 + timedelta(seconds=45)) == 0.0
    assert tracker.observe("US.X", _row(1.1, 1.2), t0 + timedelta(seconds=70)) == 25.0
    # 没有盘口不更新状态
    assert tracker.observe("US.X", _row(0.0, 0.0), t0 + timedelta(seconds=90)) is None


# ---------------------------------------------------------------------------
# 2. 新判据：成交停滞但盘口活跃 → 放行
# ---------------------------------------------------------------------------


def test_quote_activity_keeps_normal_fresh_path():
    row = {"bid_price": 1.0, "ask_price": 1.2, "update_time": _quote_at(2)}
    fresh, reason, age, degraded = option_quote_activity(row, book_moved=False)
    assert fresh and not degraded and age < 60
    assert "行情新鲜" in reason


def test_quote_activity_passes_stale_trade_when_book_moves():
    """复现 AVGO：成交冻结 11.5 分钟，但相邻快照 bid 在动 → 放行。"""
    row = {"bid_price": 9.80, "ask_price": 9.95, "update_time": _quote_at(690)}
    fresh, reason, age, degraded = option_quote_activity(row, book_moved=True)
    assert fresh is True, "盘口在动仍被判过期（本次修复的核心用例）"
    assert degraded is False
    assert age >= 600 and "盘口活跃" in reason


def test_quote_activity_uses_book_age_for_shadow_matching():
    row = {"bid_price": 1.0, "ask_price": 1.2, "update_time": _quote_at(2000)}
    fresh, reason, _, _ = option_quote_activity(
        row, book_active_seconds=120.0, maximum_seconds=900,
    )
    assert fresh is True
    assert "盘口" in reason and "120" in reason


def test_quote_activity_600s_fallback_only_passes_usable_book():
    row = {"bid_price": 1.0, "ask_price": 1.2, "update_time": _quote_at(500)}
    fresh, _, _, degraded = option_quote_activity(row, book_moved=False)
    assert fresh is True and degraded is True, "500s 应走 600s 兜底降级"

    stale = {"bid_price": 9.80, "ask_price": 9.95, "update_time": _quote_at(7300)}
    fresh, reason, age, degraded = option_quote_activity(stale, book_moved=False)
    assert fresh is False and degraded is False
    assert age > 600 and "盘口变化年龄 未知" in reason


def test_quote_activity_blocked_when_previous_judgement_still_blocks_wide_book():
    stale_wide = {"bid_price": 1.0, "ask_price": 2.0, "update_time": _quote_at(5000)}
    fresh, _, _, degraded = option_quote_activity(stale_wide, book_moved=False)
    assert fresh is False and degraded is False


# ---------------------------------------------------------------------------
# 3. 入场取价：两次快照 + 每次重取时钟
# ---------------------------------------------------------------------------


def _patch_gamma_module(monkeypatch):
    import run_gamma_strategy as gamma

    monkeypatch.setattr(gamma.clock, "sleep", lambda *_a, **_k: None)
    return gamma


class _SequenceBroker:
    """每次 snapshots() 返回下一组盘口（模拟盘口在动）。"""

    def __init__(self, option_quotes, underlying_quotes=None):
        self._option_quotes = list(option_quotes)
        self._underlying_quotes = list(underlying_quotes or [330.0] * len(option_quotes))

    def snapshots(self, symbols):
        option = self._option_quotes.pop(0) if len(self._option_quotes) > 1 else self._option_quotes[0]
        underlying = (
            self._underlying_quotes.pop(0)
            if len(self._underlying_quotes) > 1 else self._underlying_quotes[0]
        )
        return pd.DataFrame([
            {"code": "US.AMD261030P520000", "bid_price": option[0], "ask_price": option[1],
             "update_time": option[2]},
            {"code": "US.AMD", "last_price": underlying,
             "update_time": _quote_at(1)},
        ])


def test_refreshed_trade_quotes_uses_book_movement(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    stale_trade_time = _quote_at(700)  # 成交冻结，但盘口在动
    broker = _SequenceBroker([
        (9.75, 9.95, stale_trade_time),
        (9.80, 9.95, stale_trade_time),
    ])
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=2, wait_seconds=0, maximum_stale_seconds=600,
    )
    assert check["option_fresh"] is True
    assert check["book_moved"] is True
    assert check["attempts"][-1]["bid_price"] == 9.80
    assert check["attempts"][-1]["previous_bid_price"] == 9.75


def test_refreshed_trade_quotes_rejects_stale_and_frozen_book(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    frozen = _quote_at(700)
    broker = _SequenceBroker([(9.75, 9.95, frozen)] * 3)
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=2, wait_seconds=0, maximum_stale_seconds=600,
    )
    assert check["option_fresh"] is False, "既无成交、盘口也没动，应保持拒绝"
    assert all(attempt["book_moved"] is False for attempt in check["attempts"])


def test_refreshed_trade_quotes_accepts_cross_round_book_activity(monkeypatch):
    """成交冻结 700s，但采集侧记录「盘口 120s 内有变化」→ 按盘口基准放行（第 3 档）。"""
    gamma = _patch_gamma_module(monkeypatch)
    frozen = _quote_at(700)
    broker = _SequenceBroker([(9.75, 9.95, frozen)] * 3)
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=2, wait_seconds=0, maximum_stale_seconds=600,
        book_active_seconds=120.0,
    )
    assert check["option_fresh"] is True, check["option_reason"]
    assert "盘口" in check["option_reason"]
    # 年龄按更小的盘口年龄报，而不是 700s 的成交年龄
    assert check["option_age_seconds"] <= 180


def test_refreshed_trade_quotes_blocks_static_book_with_frozen_trade(monkeypatch):
    """跨轮次盘口也静止（5000s 未变）+ 成交冻结 700s → 仍 fail-closed 拦截。"""
    gamma = _patch_gamma_module(monkeypatch)
    frozen = _quote_at(700)
    broker = _SequenceBroker([(9.75, 9.95, frozen)] * 3)
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=2, wait_seconds=0, maximum_stale_seconds=600,
        book_active_seconds=5000.0,
    )
    assert check["option_fresh"] is False, check["option_reason"]
    assert all(attempt["book_moved"] is False for attempt in check["attempts"])


def test_refreshed_trade_quotes_reads_clock_per_attempt(monkeypatch):
    """重试必须每次重取时钟（旧实现三次共用一个 now → 三次年龄完全相同）。

    不靠 wall-clock 分辨率：注入递进时钟，每次 now() 比上次晚 2s。
    """
    gamma = _patch_gamma_module(monkeypatch)
    frozen = _quote_at(700)
    retries = 2
    base = datetime.now(NY)
    ticks = iter([base + timedelta(seconds=2 * index) for index in range(retries + 1)])

    class _AdvancingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = next(ticks)
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(gamma, "datetime", _AdvancingClock)
    broker = _SequenceBroker([(9.75, 9.95, frozen)] * 3)
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=retries, wait_seconds=0, maximum_stale_seconds=600,
    )
    ages = [attempt["option_age_seconds"] for attempt in check["attempts"]]
    assert len(ages) == retries + 1
    assert len(set(ages)) == len(ages), f"三次 attempt 年龄完全相同 = 重试空转：{ages}"
    assert ages == sorted(ages)


# ---------------------------------------------------------------------------
# 4. 采集侧：quality_status（ML 标签）冻结；盘口活性只进心跳口径（2026-10-27）
# ---------------------------------------------------------------------------


def test_collection_quality_status_frozen_but_heartbeat_uses_book_activity(tmp_path, monkeypatch):
    """quality_status 逐行保持旧口径（ML 标签不可动）；心跳另算 collection_fresh。

    Reviewer 2026-10-27 裁定：`:74` 不得掺入盘口活性，否则训练样本分布被改。
    修 4/8 心跳误报只能改心跳口径 —— 成交冻结但盘口在动 → 心跳 HEALTHY，
    而落库 `quality_status` 仍是 DEGRADED（与原实现逐行相同）。
    """
    store = TradingStore(tmp_path / "trading.db")
    frozen_trade = _quote_at(700)

    class Broker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            return pd.DataFrame([
                {"code": "US.AAPL260720C300000", "bid_price": 30, "ask_price": 30.5,
                 "option_implied_volatility": 0.4, "option_delta": 0.8,
                 "option_open_interest": 100, "volume": 10, "update_time": frozen_trade},
                {"code": "US.AAPL", "last_price": 330},
            ])

    monkeypatch.setattr(
        "futu_quant.option_history.load_gamma_campaigns",
        lambda: [{"code": "US.AAPL260720C300000"}],
    )
    monkeypatch.setattr(
        "futu_quant.option_history.load_all_volatility_campaigns", lambda: []
    )
    monkeypatch.setattr(
        "futu_quant.option_history._BOOK_ACTIVITY", BookActivityTracker(),
    )

    first = collect_option_snapshots(Broker(), store)
    assert first["stored"] == 1
    row = store.option_history("US.AAPL260720C300000")[0]
    assert row["quality_status"] == "DEGRADED", "ML 标签口径被改动（冻结成交应仍 DEGRADED）"
    assert row["quality_reason"].startswith("行情已过期"), row["quality_reason"]
    assert row["book_active_seconds"] is None, "首次观测盘口年龄未知，应为 NULL"
    # 首次无盘口基准 → 心跳也不可用
    first_beat = [h for h in store.heartbeats() if h["strategy_id"] == "option_history"][-1]
    assert first_beat["status"] == "DEGRADED"

    # 第二次采集：盘口有观测基准（年龄≈0）→ 心跳转 HEALTHY，但 quality_status 仍 DEGRADED
    second = collect_option_snapshots(Broker(), store)
    assert second["usable"] == 1
    assert second["good"] == 0, "ML 标签统计不得被盘口活性放大"
    rows = store.option_history("US.AAPL260720C300000")
    assert rows[0]["quality_status"] == "DEGRADED", "ML 标签被盘口活性改动"
    assert rows[0]["book_active_seconds"] is not None
    assert rows[0]["book_active_seconds"] >= 0
    beat = [h for h in store.heartbeats() if h["strategy_id"] == "option_history"][-1]
    assert beat["status"] == "HEALTHY", beat["message"]
    assert "可用 1" in beat["message"]


def test_option_quote_freshness_keeps_legacy_trade_time_semantics():
    """`option_quote_freshness` 保留旧成交时间口径（ML 标签/其它调用方用）。"""
    row = {"bid_price": 1.0, "ask_price": 1.2, "update_time": _quote_at(500)}
    fresh, _, _, degraded = option_quote_freshness(row, maximum_seconds=300)
    assert fresh is False and degraded is False


# ---------------------------------------------------------------------------
# 5. 跨轮次盘口观测的窗口 / fail-closed 边界（Reviewer 五条硬条件）
# ---------------------------------------------------------------------------


class _FakeObservationStore:
    def __init__(self, row):
        self._row = row
        self.raise_error = False

    def latest_option_history(self, code):
        if self.raise_error:
            raise RuntimeError("db down")
        return self._row


def _now_utc():
    return datetime.now(timezone.utc)


def test_book_activity_age_usable_inside_window(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    now = _now_utc()
    store = _FakeObservationStore({
        "book_active_seconds": 120.0,
        "fetched_at": (now - timedelta(seconds=30)).isoformat(),
    })
    age, blocked = gamma.latest_book_activity_age(store, "US.X", now)
    assert blocked is False
    assert 149.0 <= age <= 151.0, f"活年龄应≈150s，实际 {age}"


def test_book_activity_age_blocks_without_history_row(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    age, blocked = gamma.latest_book_activity_age(
        _FakeObservationStore(None), "US.X", _now_utc(),
    )
    assert age is None and blocked is True


def test_book_activity_age_blocks_when_observation_too_old(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    now = _now_utc()
    store = _FakeObservationStore({
        "book_active_seconds": 10.0,
        "fetched_at": (now - timedelta(seconds=901)).isoformat(),
    })
    age, blocked = gamma.latest_book_activity_age(store, "US.X", now)
    assert age is None and blocked is True


def test_book_activity_age_blocks_on_negative_clock_skew(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    now = _now_utc()
    store = _FakeObservationStore({
        "book_active_seconds": 10.0,
        "fetched_at": (now + timedelta(seconds=60)).isoformat(),
    })
    age, blocked = gamma.latest_book_activity_age(store, "US.X", now)
    assert age is None and blocked is True, "时钟回拨不得 clamp 成 0 放行"


def test_book_activity_null_falls_back_to_legacy_300s_gate(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    now = _now_utc()
    store = _FakeObservationStore({"book_active_seconds": None, "fetched_at": now.isoformat()})
    age, blocked = gamma.latest_book_activity_age(store, "US.X", now)
    assert age is None and blocked is False, "NULL 应回落 300s 旧口径（不是放行、不是 BLOCK）"


def test_book_basis_blocked_forces_fail_closed(monkeypatch):
    gamma = _patch_gamma_module(monkeypatch)
    frozen = _quote_at(700)
    broker = _SequenceBroker([(9.75, 9.95, frozen)] * 3)
    _, _, check = gamma.refreshed_trade_quotes(
        broker, "US.AMD261030P520000", "US.AMD", datetime.now(NY), 12.0,
        retries=2, wait_seconds=0, maximum_stale_seconds=600,
        book_active_seconds=120.0, book_basis_blocked=True,
    )
    assert check["option_fresh"] is False
    assert "观测不可信" in check["option_reason"]

