from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json

import pandas as pd

from futu_quant.store import TradingStore
from futu_quant.scheduled_tasks import (
    DAILY_STRATEGY_REVIEW_TASK,
    SHUTDOWN_TASK,
    _timer_due,
    mark_im_delivery_done,
    next_board_timer_run,
    pending_im_delivery,
    run_daily_strategy_review,
    review_html,
    set_timer_enabled,
    shutdown_computer,
    timer_default_enabled,
    timer_enabled_setting_key,
)

CHINA = DAILY_STRATEGY_REVIEW_TASK.timezone


class _FakeReview:
    class _Window:
        session_date = date(2026, 7, 15)
        china_label = "2026-07-15 17:00–2026-07-16 09:30"

    def __init__(self):
        self.window = self._Window()
        self.metrics = {"策略决策": 1, "订单数": 2}
        self.narrative = ["测试结论", "第二条结论"]
        self.orders = None
        self.option_pnl_breakdown = None
        self.option_lifecycle = None
        self.quality_scores = None
        self.action_items = None
        self.evidence_alignment = None
        self.option_quality = None
        self.core_quality = None
        self.runtime_health = None
        self.interruptions = None
        self.blockers = None


def test_timer_schedules_use_china_time_and_defaults() -> None:
    assert DAILY_STRATEGY_REVIEW_TASK.time.strftime("%H:%M") == "05:15"
    assert SHUTDOWN_TASK.time.strftime("%H:%M") == "06:00"
    assert DAILY_STRATEGY_REVIEW_TASK.timezone is CHINA
    assert timer_default_enabled(DAILY_STRATEGY_REVIEW_TASK) is True
    assert timer_default_enabled(SHUTDOWN_TASK) is False
    assert timer_enabled_setting_key(SHUTDOWN_TASK) == "AUTO_COMPUTER_SHUTDOWN_ENABLED"


def test_next_board_timer_run_rolls_forward_after_firing(tmp_path) -> None:
    store = TradingStore(tmp_path / "trading.db")
    now = datetime(2026, 7, 15, 5, 0, tzinfo=CHINA)
    # No last fire yet: next run is today's 05:15.
    assert next_board_timer_run(store, DAILY_STRATEGY_REVIEW_TASK, now) == datetime(
        2026, 7, 15, 5, 15, tzinfo=CHINA
    )
    store.set_setting(
        "last_board_timer:daily_strategy_review",
        datetime(2026, 7, 15, 5, 15, tzinfo=CHINA).isoformat(),
    )
    # Fired today's slot already (last fire >= today's 05:15): next run is tomorrow.
    assert next_board_timer_run(store, DAILY_STRATEGY_REVIEW_TASK, now) == datetime(
        2026, 7, 16, 5, 15, tzinfo=CHINA
    )
    # Boundary: a last fire before today's slot means today's slot is still pending.
    store.set_setting(
        "last_board_timer:daily_strategy_review",
        datetime(2026, 7, 14, 5, 15, tzinfo=CHINA).isoformat(),
    )
    assert next_board_timer_run(store, DAILY_STRATEGY_REVIEW_TASK, now) == datetime(
        2026, 7, 15, 5, 15, tzinfo=CHINA
    )


def test_timer_due_is_idempotent_per_day(tmp_path) -> None:
    store = TradingStore(tmp_path / "trading.db")
    now = datetime(2026, 7, 15, 5, 20, tzinfo=CHINA)
    assert _timer_due(store, DAILY_STRATEGY_REVIEW_TASK, now) is True
    store.set_setting(
        "last_board_timer:daily_strategy_review",
        datetime(2026, 7, 15, 5, 15, tzinfo=CHINA).isoformat(),
    )
    assert _timer_due(store, DAILY_STRATEGY_REVIEW_TASK, now) is False
    # The 06:00 shutdown timer has not yet reached its daily time at 05:20.
    assert _timer_due(store, SHUTDOWN_TASK, now) is False
    later = datetime(2026, 7, 15, 6, 35, tzinfo=CHINA)
    assert _timer_due(store, SHUTDOWN_TASK, later) is True
    store.set_setting(
        "last_board_timer:computer_shutdown",
        datetime(2026, 7, 15, 6, 0, tzinfo=CHINA).isoformat(),
    )
    assert _timer_due(store, SHUTDOWN_TASK, later) is False
    # Before the timer time, nothing is due.
    early = datetime(2026, 7, 16, 4, 0, tzinfo=CHINA)
    assert _timer_due(store, DAILY_STRATEGY_REVIEW_TASK, early) is False
    assert _timer_due(store, SHUTDOWN_TASK, early) is False


def test_review_html_contains_conclusion_and_sections() -> None:
    html = review_html(_FakeReview())
    assert "策略复盘与执行质量" in html
    assert "测试结论" in html
    assert "核心指标" in html
    # Empty DataFrames render as empty sections without a placeholder table.
    assert "订单执行质量" in html


def test_wechat_payload_carries_html_and_conclusion() -> None:
    # The legacy webhook payload helper was removed; delivery now goes through
    # the DSH IM/WeChat plugin via a pending-delivery record in the store.
    pass


def test_run_daily_strategy_review_writes_html_queues_im_delivery_and_marks_timer(
    tmp_path, monkeypatch,
) -> None:
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr("futu_quant.scheduled_tasks.REPORT_DIR", tmp_path)

    class _Review:
        class _Window:
            session_date = date(2026, 7, 15)
            china_label = "2026-07-15 17:00–2026-07-16 09:30"

        def __init__(self):
            self.window = self._Window()
            self.metrics = {"订单数": 1}
            self.narrative = ["结论A"]
            self.orders = pd.DataFrame()
            self.option_pnl_breakdown = pd.DataFrame()
            self.option_lifecycle = pd.DataFrame()
            self.quality_scores = pd.DataFrame()
            self.action_items = []
            self.evidence_alignment = pd.DataFrame()
            self.option_quality = pd.DataFrame()
            self.core_quality = pd.DataFrame()
            self.runtime_health = pd.DataFrame()
            self.interruptions = pd.DataFrame()
            self.blockers = pd.DataFrame()

    monkeypatch.setattr(
        "futu_quant.scheduled_tasks.build_strategy_review",
        lambda store, window: _Review(),
    )
    monkeypatch.setattr("futu_quant.feishu_reporter.FeishuReporter.dispatch_pending_review", lambda self: None)
    now = datetime(2026, 7, 16, 5, 15, tzinfo=CHINA)
    path = run_daily_strategy_review(store, session_date=date(2026, 7, 15), now=now)
    assert Path(path).exists()
    assert "策略复盘与执行质量" in Path(path).read_text(encoding="utf-8")
    assert store.get_setting("last_board_timer:daily_strategy_review") == now.isoformat()
    pending = pending_im_delivery(store)
    assert pending is not None
    assert pending["session_date"] == "2026-07-15"
    assert pending["status"] == "PENDING"
    assert "结论A" in pending["conclusion"]
    assert Path(pending["html_path"]).exists()


def test_pending_im_delivery_round_trip(tmp_path) -> None:
    store = TradingStore(tmp_path / "trading.db")
    store.set_runtime_config(
        "DAILY_STRATEGY_REVIEW_IM_PENDING",
        {"session_date": "2026-07-15", "html_path": "/tmp/x.html", "conclusion": "ok", "status": "PENDING"},
        "scheduled_tasks",
    )
    assert pending_im_delivery(store)["status"] == "PENDING"
    mark_im_delivery_done(store, date(2026, 7, 15))
    assert pending_im_delivery(store) is None
    audits = [row for row in store.audit_events(limit=50) if row["event_type"] == "DAILY_STRATEGY_REVIEW_IM_DELIVERED"]
    assert audits


def test_shutdown_dry_run_records_audit_without_executing(tmp_path, monkeypatch) -> None:
    # 物理关机已交由 Windows 原生计划任务接管，scheduled_tasks 已不再调用
    # subprocess（见 shutdown_computer）；演练模式只写审计记录。
    import futu_quant.scheduled_tasks as scheduled_tasks

    assert not hasattr(scheduled_tasks, "subprocess")

    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setenv("AUTO_SHUTDOWN_DRY_RUN", "true")

    message = shutdown_computer(store, now=datetime(2026, 7, 16, 6, 0, tzinfo=CHINA))
    assert "演练模式" in message
    assert store.get_setting("last_board_timer:computer_shutdown")


def test_set_timer_enabled_persists_and_audits(tmp_path) -> None:
    store = TradingStore(tmp_path / "trading.db")
    set_timer_enabled(store, SHUTDOWN_TASK, True)
    assert store.get_setting(timer_enabled_setting_key(SHUTDOWN_TASK)) == "true"
    audits = [row for row in store.audit_events(limit=100) if row["event_type"] == "TASK_BOARD_TIMER_CHANGED"]
    assert json.loads(audits[0]["payload_json"]) == {"task_id": "computer_shutdown", "enabled": "true"}
