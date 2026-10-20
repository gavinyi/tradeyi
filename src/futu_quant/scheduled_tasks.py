from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
import os
from pathlib import Path
import platform
from zoneinfo import ZoneInfo

from .store import TradingStore
from .strategy_review import build_strategy_review, review_markdown
from .trade_diagnostics import CHINA, diagnostic_window, latest_us_session_date


REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "reports"


@dataclass(frozen=True)
class TaskBoardTimer:
    task_id: str
    title: str
    time: time
    timezone: ZoneInfo

    @property
    def label(self) -> str:
        return f"{self.title}（每天 {self.time:%H:%M}，中国时间）"


DAILY_STRATEGY_REVIEW_TASK = TaskBoardTimer(
    "daily_strategy_review", "策略复盘与执行质量", time(5, 15), CHINA,
)
DAILY_UNIVERSE_RANKING_TASK = TaskBoardTimer(
    "daily_universe_ranking", "标的池 ML 精排与轮换", time(5, 30), CHINA,
)
SHUTDOWN_TASK = TaskBoardTimer(
    "computer_shutdown", "电脑关机", time(6, 0), CHINA,
)
WEEKLY_UNIVERSE_RECALL_TASK = TaskBoardTimer(
    "weekly_universe_recall", "周度全市场 Top 50 召回", time(21, 10), CHINA,
)
BOARD_TIMERS = (
    DAILY_STRATEGY_REVIEW_TASK,
    DAILY_UNIVERSE_RANKING_TASK,
    SHUTDOWN_TASK,
    WEEKLY_UNIVERSE_RECALL_TASK,
)


def _task_timer_key(timer: TaskBoardTimer) -> str:
    return f"last_board_timer:{timer.task_id}"


def _timer_due(
    store: TradingStore, timer: TaskBoardTimer,
    now: datetime | None = None, session_date: date | None = None,
) -> bool:
    """05:15 China time is after the prior US session close (04:00 China).

    Each timer is idempotent per review day: after it has fired for today's
    review day, the next firing is the following day.
    """
    now = (now or datetime.now(CHINA)).astimezone(timer.timezone)
    review_day = session_date or now.date()
    if timer.task_id == "weekly_universe_recall" and now.weekday() != 0:
        return False
    if now.time() < timer.time:
        return False
    raw = store.get_setting(_task_timer_key(timer), "")
    if raw:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timer.timezone)
        if last.astimezone(timer.timezone).date() == review_day:
            return False
    if session_date is None:
        # Self-correcting: the service may have been down on the timer day, so
        # a missed 05:15 window can fire on the next service cycle.
        return now.time() >= timer.time
    return True


def _html_table(frame) -> str:
    if frame is None or getattr(frame, "empty", False) or not len(frame):
        return '<p class="empty">无记录。</p>'
    if not hasattr(frame, "fillna"):
        import pandas as pd
        frame = pd.DataFrame(frame)
    view = frame.fillna("").astype(str)
    return view.to_html(index=False, border=0, classes="data-table")


def _metric_html(metrics: dict[str, object]) -> str:
    items = "".join(
        f'<div class="metric"><b>{escape(str(key))}</b><span>{escape(str(value))}</span></div>'
        for key, value in metrics.items()
    )
    return f'<h2>核心指标</h2><div class="metrics">{items}</div>'


def review_html(review) -> str:
    sections = [
        ("一句话结论", None),
        ("订单执行质量", review.orders),
        ("期权盈亏拆解", review.option_pnl_breakdown),
        ("期权交易生命周期", review.option_lifecycle),
        ("策略质量评分", review.quality_scores),
        ("行动项", review.action_items),
        ("回测/前测证据对照", review.evidence_alignment),
        ("波动率期权质量面板", review.option_quality),
        ("Core Satellite 调仓质量", review.core_quality),
        ("运行健康", review.runtime_health),
        ("OpenD 中断与持仓保护追踪", review.interruptions),
        ("阻塞原因", review.blockers),
    ]
    body = []
    for title, frame in sections:
        if frame is None:
            narrative = "".join(f"<li>{escape(str(item))}</li>" for item in review.narrative)
            body.append(
                f'<div class="card"><h2>{escape(title)}</h2>'
                f'<ul>{narrative or "<li>本窗口没有额外结论。</li>"}</ul></div>'
            )
        else:
            body.append(f"<h2>{escape(title)}</h2>{_html_table(frame)}")
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;"
        "color:#1f2937;max-width:1400px;margin:32px auto;padding:0 20px;background:#f8fafc}"
        "h1{color:#0f172a}h2{margin-top:28px;border-left:4px solid #2563eb;padding-left:10px}"
        ".meta{color:#64748b}.metrics{display:flex;flex-wrap:wrap;gap:10px}"
        ".metric{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;min-width:130px}"
        ".metric b,.metric span{display:block}.metric span{font-size:20px;margin-top:6px;color:#2563eb}"
        ".card{background:white;padding:18px;border-radius:10px;box-shadow:0 1px 3px #0001}"
        "table{border-collapse:collapse;width:100%;background:white;font-size:13px}"
        "th,td{border:1px solid #e2e8f0;padding:7px;text-align:left;vertical-align:top}"
        "th{background:#eff6ff}.empty{color:#64748b}"
    )
    return (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>策略复盘与执行质量 {review.window.session_date}</title>'
        f'<style>{style}</style></head><body>'
        f"<h1>策略复盘与执行质量</h1>"
        f'<p class="meta">美东交易日：{escape(str(review.window.session_date))}；'
        f'中国时间窗口：{escape(review.window.china_label)}</p>'
        f"{''.join(body[:1])}{_metric_html(review.metrics)}{''.join(body[1:])}"
        f"</body></html>"
    )


IM_DELIVERY_PENDING_KEY = "DAILY_STRATEGY_REVIEW_IM_PENDING"


def queue_review_for_im_delivery(
    store: TradingStore,
    session_date: date,
    html_path: str,
    conclusion: str,
    markdown_path: str = "",
) -> None:
    """Queue the generated review for delivery via the user's DSH IM/WeChat
    chat plugin.

    The standalone service cannot itself reach the user's IM channel (it has no
    agent session context), so it writes a clearly-named "pending delivery"
    record into the store. The DSH agent / IM plugin side reads this record and
    ships the report body (markdown text) to the chat through the connected
    WeChat channel, then marks it delivered. This keeps the runtime
    authoritative, auditable, and decoupled from the IM transport.
    """
    record = {
        "session_date": session_date.isoformat(),
        "html_path": html_path,
        "conclusion": conclusion,
        "created_at": datetime.now(CHINA).isoformat(),
        "status": "PENDING",
    }
    if markdown_path:
        record["markdown_path"] = markdown_path
    store.set_runtime_config(IM_DELIVERY_PENDING_KEY, record, "scheduled_tasks")
    store.audit(
        "DAILY_STRATEGY_REVIEW_QUEUED_FOR_IM",
        {
            "session_date": session_date.isoformat(),
            "path": html_path,
            "markdown_path": markdown_path,
            "conclusion": conclusion,
        },
    )


def mark_im_delivery_done(store: TradingStore, session_date: date, channel: str = "dsh_im_wechat") -> None:
    """Mark a queued review as delivered through the DSH IM/WeChat channel."""
    value, _ = store.get_runtime_config(IM_DELIVERY_PENDING_KEY) or (None, 0)
    record = dict(value) if isinstance(value, dict) else {}
    record["status"] = "DELIVERED"
    record["delivered_at"] = datetime.now(CHINA).isoformat()
    record["channel"] = channel
    record["session_date"] = session_date.isoformat()
    store.set_runtime_config(IM_DELIVERY_PENDING_KEY, record, "dsh_im_wechat")
    store.audit(
        "DAILY_STRATEGY_REVIEW_IM_DELIVERED",
        {"session_date": session_date.isoformat(), "channel": channel},
    )


def pending_im_delivery(store: TradingStore) -> dict | None:
    value, _ = store.get_runtime_config(IM_DELIVERY_PENDING_KEY) or (None, 0)
    if isinstance(value, dict) and value.get("status") == "PENDING":
        return value
    return None


def run_daily_strategy_review(
    store: TradingStore,
    session_date: date | None = None,
    now: datetime | None = None,
) -> str:
    """Generate the daily strategy review HTML and hand it to the DSH agent
    for WeChat delivery.

    This runs inside the standalone ``run_service.py`` process, which has no
    DSH agent / IM channel context. So it writes the HTML report and then
    enqueues a "pending IM delivery" record. A scheduled DSH agent invocation
    picks that record up, uses agent capability to ship the conclusion (and
    optionally the HTML file) to the user's WeChat chat via the connected
    DSH IM plugin, then marks the record delivered so it is not re-sent.
    """
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    session_date = session_date or latest_us_session_date(now)
    window = diagnostic_window(session_date)
    review = build_strategy_review(store, window)
    html = review_html(review)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"strategy_review_{session_date.isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    md_path = REPORT_DIR / f"strategy_review_{session_date.isoformat()}.md"
    md_path.write_text(review_markdown(review), encoding="utf-8")
    conclusion = "\n".join(review.narrative[:8]) or "本窗口没有额外结论。"
    queue_review_for_im_delivery(store, session_date, str(path), conclusion, str(md_path))
    store.set_setting(_task_timer_key(DAILY_STRATEGY_REVIEW_TASK), now.isoformat())

    # 应用内自动触发飞书战报与复盘推送，直达用户机器人会话
    try:
        from futu_quant.feishu_reporter import FeishuReporter
        FeishuReporter(store).dispatch_pending_review()
    except Exception:
        pass

    return str(path)


def shutdown_computer(
    store: TradingStore | None = None,
    dry_run: bool | None = None,
    now: datetime | None = None,
) -> str:
    """每日 06:00（中国时间）关机任务；演练模式只写审计记录。"""
    if dry_run is None:
        dry_run_val = (
            store.get_setting("AUTO_SHUTDOWN_DRY_RUN", "") if store else ""
        ) or os.getenv("AUTO_SHUTDOWN_DRY_RUN", "false")
        dry_run = dry_run_val.strip().lower() == "true"
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    if dry_run:
        message = "关机任务处于演练模式，未执行系统关机。"
        if store is not None:
            store.audit("AUTO_COMPUTER_SHUTDOWN_DRY_RUN", {"message": message})
            store.set_setting(_task_timer_key(SHUTDOWN_TASK), now.isoformat())
        return message
    # 物理关机已按用户要求交由 Windows 原生计划任务接管，Python 侧仅负责安全落盘就绪
    # if platform.system() == "Windows":
    #     command = ["shutdown", "/s", "/t", "0"]
    # elif platform.system() == "Darwin":
    #     command = ["osascript", "-e", 'tell app "System Events" to shut down']
    # else:
    #     command = ["systemctl", "poweroff"]
    message = "盘后数据与复盘已安全落盘，物理关机交由 Windows 计划任务接管执行。"
    if store is not None:
        store.audit("AUTO_COMPUTER_SHUTDOWN_READY", {"platform": platform.system(), "message": message})
        store.set_setting(_task_timer_key(SHUTDOWN_TASK), now.isoformat())
    return message


def board_task_runs(store: TradingStore, limit: int = 300) -> dict[str, list[dict]]:
    runs = [
        row for row in store.task_runs(limit=limit)
        if row["task_id"] in {task.task_id for task in BOARD_TIMERS}
    ]
    return {
        task.task_id: [row for row in runs if row["task_id"] == task.task_id]
        for task in BOARD_TIMERS
    }


def next_board_timer_run(
    store: TradingStore, timer: TaskBoardTimer, now: datetime | None = None,
) -> datetime:
    now = (now or datetime.now(timer.timezone)).astimezone(timer.timezone)
    next_run = datetime.combine(now.date(), timer.time, tzinfo=timer.timezone)
    raw = store.get_setting(_task_timer_key(timer), "")
    if raw:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timer.timezone)
        if last.astimezone(timer.timezone) >= next_run:
            next_run += timedelta(days=1)
    return next_run


def timer_enabled_setting_key(timer: TaskBoardTimer) -> str:
    return f"AUTO_{timer.task_id.upper()}_ENABLED"


def timer_default_enabled(timer: TaskBoardTimer) -> bool:
    return timer.task_id in {
        DAILY_STRATEGY_REVIEW_TASK.task_id,
        DAILY_UNIVERSE_RANKING_TASK.task_id,
        WEEKLY_UNIVERSE_RECALL_TASK.task_id,
    }


def set_timer_enabled(store: TradingStore, timer: TaskBoardTimer, enabled: bool) -> None:
    store.set_setting(timer_enabled_setting_key(timer), str(enabled).lower())
    store.audit("TASK_BOARD_TIMER_CHANGED", {
        "task_id": timer.task_id, "enabled": str(enabled).lower(),
    })


def deliver_pending_im_review(
    store: TradingStore,
    session_date: date | None = None,
    mark_done: bool = True,
) -> dict | None:
    """Read the pending-delivery record and prepare it for DSH IM/WeChat delivery.

    Returns a dict with the record's fields plus an ``html`` key (the file's
    text) so the DSH scheduled agent can hand it off without re-reading the
    store. When ``mark_done`` is True the record is also flipped to
    DELIVERED so a subsequent DSH agent run does not re-send the same
    report. Call this from the DSH scheduled agent right before invoking
    the IM channel; call ``mark_im_delivery_done`` again on success if you
    want a separate, explicit confirmation step.
    """
    pending = pending_im_delivery(store)
    if pending is None:
        return None
    result = dict(pending)
    if session_date is not None:
        result["session_date"] = session_date.isoformat()
    try:
        html_path = Path(result.get("html_path", ""))
        if html_path.exists():
            result["html"] = html_path.read_text(encoding="utf-8")
    except OSError:
        result["html"] = ""
    try:
        md_path = Path(result.get("markdown_path", ""))
        if md_path.exists():
            result["markdown"] = md_path.read_text(encoding="utf-8")
    except OSError:
        result["markdown"] = ""
    if mark_done:
        mark_im_delivery_done(
            store,
            date.fromisoformat(result.get("session_date", datetime.now(CHINA).date().isoformat())),
        )
    return result


def run_daily_universe_ranking(
    store: TradingStore | None = None, now: datetime | None = None,
) -> str:
    """每日离线执行标的沙盒自评、ML精排打分与外援轮换仲裁。"""
    from futu_quant.universe.diversity_filter import DiversityFilter
    from futu_quant.universe.incubator_engine import IncubatorEngine
    from futu_quant.universe.lifecycle_manager import LifecycleManager
    from futu_quant.universe.ranking_pipeline import RankingPipeline

    store = store or TradingStore()
    incubator = IncubatorEngine(store=store)
    ranking = RankingPipeline(store=store)
    diversity = DiversityFilter(max_satellites=5)
    lifecycle = LifecycleManager(store=store)

    candidates = store.candidate_symbols(status="ACTIVE")
    if not candidates:
        return "当前无活跃候选标的"

    symbols = [c["symbol"] for c in candidates]
    for s in symbols:
        incubator.evaluate_daily_metrics(s)

    ranked_df = ranking.rank_candidates(symbols)
    # 自适应弹性容量: 优质标的充沛时自动扩容至最多 10 只
    diverse_top = diversity.select_diverse_portfolio(ranked_df, adaptive_capacity=True)
    effective_cap = diversity.resolve_capacity(ranked_df)
    candidate_scores = dict(zip(ranked_df["symbol"], ranked_df["ml_score"]))

    promoted = lifecycle.evaluate_promotions(
        diverse_top,
        candidate_scores=candidate_scores,
        max_capacity=effective_cap,
    )
    lifecycle.evaluate_evictions()
    active_pool = lifecycle.sync_to_runtime_universe()

    if now is not None:
        store.set_setting(_task_timer_key(DAILY_UNIVERSE_RANKING_TASK), now.isoformat())

    return f"ML 标的精排与轮换完成，当前生效池共 {len(active_pool)} 只，其中外援 {len(promoted)} 只。"


def run_weekly_universe_recall(
    store: TradingStore | None = None, now: datetime | None = None,
) -> str:
    """每周一美股开盘前自动执行全市场期权流动性 Top 50 召回。"""
    from futu_quant.universe.recall_scanner import RecallScanner

    store = store or TradingStore()
    scanner = RecallScanner(store=store)
    week_start = (now.date() if now else datetime.now(CHINA).date()).isoformat()
    candidates = scanner.run_weekly_recall(week_start)

    if now is not None:
        store.set_setting(_task_timer_key(WEEKLY_UNIVERSE_RECALL_TASK), now.isoformat())

    return f"周度全市场 Top 50 期权流动性召回完成，入选 {len(candidates)} 只候选标的。"

