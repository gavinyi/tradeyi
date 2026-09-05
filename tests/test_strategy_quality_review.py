from __future__ import annotations

from datetime import date

import pytest
import pyarrow as pa

from futu_quant.store import TradingStore
from futu_quant.strategy_review import build_strategy_review
from futu_quant.trade_diagnostics import diagnostic_window


def _freeze_time(store: TradingStore, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "now", lambda: value)


def _fill_order(store: TradingStore, intent: dict, price: float, monkeypatch: pytest.MonkeyPatch, at: str) -> None:
    broker_id = f"order-{intent['id']}"
    store.update_intent(intent["id"], "SUBMITTED", broker_id)
    _freeze_time(store, at, monkeypatch)
    store.update_by_broker_order(
        broker_id, "FILLED", float(intent["quantity"]), price, 1.0,
    )


def test_strategy_review_builds_lifecycle_quality_and_action_items(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "review.db")
    _freeze_time(store, "2026-08-13T14:00:00+00:00", monkeypatch)
    store.decision(
        "volatility_long_gamma",
        "US.SPCX260814C141000",
        "BUY",
        "动态波动与方向确认后首次入场",
        {
            "pnl_ratio": -0.42,
            "mfe_pnl_ratio": 0.24,
            "profit_capture_ratio": 0.18,
            "position_quantity": 0,
        },
    )
    buy, _ = store.create_intent(
        "volatility_long_gamma",
        "spcx-buy",
        "US.SPCX260814C141000",
        "BUY",
        1,
        10,
        {"reason": "动态波动与方向确认后首次入场"},
    )
    _fill_order(store, buy, 10, monkeypatch, "2026-08-13T14:05:00+00:00")
    _freeze_time(store, "2026-08-13T15:20:00+00:00", monkeypatch)
    sell, _ = store.create_intent(
        "volatility_long_gamma",
        "spcx-sell",
        "US.SPCX260814C141000",
        "SELL",
        1,
        6,
        {"reason": "硬止损退出"},
    )
    _fill_order(store, sell, 6, monkeypatch, "2026-08-13T15:21:00+00:00")

    review = build_strategy_review(store, diagnostic_window(date(2026, 8, 13)))

    assert not review.option_lifecycle.empty
    assert not review.quality_scores.empty
    assert not review.action_items.empty
    lifecycle = review.option_lifecycle.iloc[0]
    assert lifecycle["代码"] == "US.SPCX260814C141000"
    assert lifecycle["最大浮盈MFE"] == pytest.approx(0.24)
    assert lifecycle["浮盈捕获率"] == pytest.approx(0.18)
    assert lifecycle["净盈亏"] == pytest.approx(-402)
    worst = review.quality_scores.iloc[0]
    assert worst["对象"] == "US.SPCX260814C141000"
    assert worst["主要短板"] in {"退出质量", "风控质量"}
    assert int(review.metrics["高优先级行动项"]) >= 1
    assert int(review.metrics["最低质量评分"]) < 70


def test_option_lifecycle_keeps_mfe_when_later_decision_has_no_metrics(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "review_metrics.db")
    _freeze_time(store, "2026-08-13T14:00:00+00:00", monkeypatch)
    store.decision(
        "volatility_long_gamma",
        "US.GOOG260814P345000",
        "SELL",
        "盈利保护触发",
        {
            "pnl_ratio": 0.12,
            "position_pnl_amount": 57.0,
            "mfe_pnl_ratio": 0.26,
            "profit_capture_ratio": 0.73,
            "position_quantity": 1,
        },
    )
    buy, _ = store.create_intent(
        "volatility_long_gamma",
        "goog-buy",
        "US.GOOG260814P345000",
        "BUY",
        1,
        5.0,
        {"reason": "动态波动与方向确认后首次入场"},
    )
    _fill_order(store, buy, 5.0, monkeypatch, "2026-08-13T14:01:00+00:00")
    sell, _ = store.create_intent(
        "volatility_long_gamma",
        "goog-sell",
        "US.GOOG260814P345000",
        "SELL",
        1,
        5.57,
        {"reason": "盈利保护触发"},
    )
    _fill_order(store, sell, 5.57, monkeypatch, "2026-08-13T14:05:00+00:00")
    _freeze_time(store, "2026-08-13T14:10:00+00:00", monkeypatch)
    store.decision(
        "volatility_long_gamma",
        "US.GOOG260814P345000",
        "HOLD",
        "期权表达复核：已维护为当前最佳期权表达",
        {},
    )

    review = build_strategy_review(store, diagnostic_window(date(2026, 8, 13)))
    lifecycle = review.option_lifecycle.iloc[0]

    assert lifecycle["当前盈亏比例"] == pytest.approx(0.12)
    assert lifecycle["当前盈亏金额"] == pytest.approx(57.0)
    assert lifecycle["最大浮盈MFE"] == pytest.approx(0.26)
    assert lifecycle["浮盈捕获率"] == pytest.approx(0.73)
    assert lifecycle["剩余持仓"] == 0
    assert review.metrics["期权未平持仓"] == 0
    assert lifecycle["最新动作"] == "HOLD"
    assert "期权表达复核" in lifecycle["最新原因"]


def test_expired_cleanup_rows_do_not_drive_quality_or_actions(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "review_expired.db")
    _freeze_time(store, "2026-08-13T14:00:00+00:00", monkeypatch)
    for _ in range(3):
        store.decision(
            "volatility_long_gamma",
            "US.AAPL260813C300000",
            "BLOCKED",
            "合约已于 2026-08-13 到期",
        )

    review = build_strategy_review(store, diagnostic_window(date(2026, 8, 13)))

    assert review.metrics["到期/失效清理项"] == 3
    assert review.blockers.iloc[0]["处理类型"] == "到期/失效清理项"
    assert review.quality_scores.empty
    assert review.action_items.empty


def test_quality_scores_are_arrow_serializable_with_mixed_row_types(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "review_arrow.db")
    _freeze_time(store, "2026-08-13T14:00:00+00:00", monkeypatch)
    store.decision(
        "volatility_long_gamma",
        "US.SPCX260814C141000",
        "BUY",
        "动态波动与方向确认后首次入场",
        {
            "pnl_ratio": -0.42,
            "mfe_pnl_ratio": 0.24,
            "profit_capture_ratio": 0.18,
            "position_quantity": 0,
        },
    )
    buy, _ = store.create_intent(
        "volatility_long_gamma",
        "spcx-buy",
        "US.SPCX260814C141000",
        "BUY",
        1,
        10,
        {"reason": "动态波动与方向确认后首次入场"},
    )
    _fill_order(store, buy, 10, monkeypatch, "2026-08-13T14:05:00+00:00")
    store.decision(
        "volatility_long_gamma",
        "US.META260807P550000",
        "BUY",
        "动态波动与方向确认后首次入场",
        {"planned_order_quantity": 0},
    )
    run_id = store.start_task_run("option_history", 60, None)
    store.finish_task_run(run_id, "ERROR", 1.0, "富途连接断开")

    review = build_strategy_review(store, diagnostic_window(date(2026, 8, 13)))

    assert not review.quality_scores.empty
    pa.Table.from_pandas(review.quality_scores)


def test_strategy_review_evidence_alignment_handles_missing_records(tmp_path):
    store = TradingStore(tmp_path / "evidence.db")

    review = build_strategy_review(store, diagnostic_window(date(2026, 8, 13)))

    assert set(review.evidence_alignment["策略"]) == {"volatility_long_gamma", "core_satellite"}
    assert review.evidence_alignment["结论"].str.contains("缺少回测/前测证据").any()
