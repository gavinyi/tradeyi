from __future__ import annotations

from dataclasses import dataclass
import json

import pandas as pd

from .connectivity_protection import PROTECTION_EVENT_TYPES, china_label, json_payload
from .option_lifecycle import parse_option_code
from .strategy_quality import (
    build_action_items,
    build_evidence_alignment,
    build_option_lifecycle_frame,
    build_trade_quality_scores,
)
from .trade_diagnostics import DiagnosticWindow, build_diagnostic_report


REVIEW_STRATEGIES = ("volatility_long_gamma", "core_satellite")


@dataclass(frozen=True)
class StrategyReview:
    window: DiagnosticWindow
    metrics: dict[str, object]
    narrative: list[str]
    orders: pd.DataFrame
    blockers: pd.DataFrame
    option_pnl_breakdown: pd.DataFrame
    option_lifecycle: pd.DataFrame
    quality_scores: pd.DataFrame
    action_items: pd.DataFrame
    evidence_alignment: pd.DataFrame
    option_quality: pd.DataFrame
    core_quality: pd.DataFrame
    runtime_health: pd.DataFrame
    interruptions: pd.DataFrame


def _json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _within(row: dict, window: DiagnosticWindow, column: str = "created_at") -> bool:
    value = str(row.get(column, ""))
    return bool(value and window.start_iso <= value <= window.end_iso)


OPTION_METRIC_KEYS = {
    "pnl_ratio",
    "position_pnl_amount",
    "drawdown",
    "peak_pnl_ratio",
    "mfe_pnl_ratio",
    "profit_capture_ratio",
    "profit_giveback",
    "executable_mark",
    "tiered_profit_lock_floor",
    "tiered_profit_lock_label",
    "profit_protection_profile",
    "position_quantity",
}


def _decision_sort_key(row: dict) -> tuple[str, int]:
    return str(row.get("created_at") or ""), int(row.get("id") or 0)


def _has_option_metrics(row: dict) -> bool:
    payload = _json(row.get("payload_json"))
    return any(key in payload for key in OPTION_METRIC_KEYS)


def _split_blockers(blockers: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Keep expired cleanup residue visible without treating it as strategy blockage."""
    if blockers.empty or "未下单原因" not in blockers.columns:
        empty = blockers.copy()
        return empty, pd.DataFrame(), empty
    result = blockers.copy()
    cleanup_mask = result["未下单原因"].astype(str).eq("到期/失效清理项")
    result["处理类型"] = cleanup_mask.map({
        True: "到期/失效清理项",
        False: "交易阻塞",
    })
    trading = result[~cleanup_mask].reset_index(drop=True)
    cleanup = result[cleanup_mask].reset_index(drop=True)
    return trading, cleanup, result.reset_index(drop=True)


TASK_LABELS = {
    "shadow_matching": "Shadow 成交匹配",
    "order_reconciliation": "订单对账",
    "ledger_reconciliation": "三账对账",
    "option_combo_monitor": "期权组合执行监控",
    "option_lifecycle": "期权生命周期",
    "option_history": "期权历史采集",
    "earnings_gamma": "财报 Gamma",
    "volatility_long_gamma": "波动率期权",
    "core_satellite": "Core Satellite",
    "daily_report": "每日运行报告",
    "earnings_calendar": "财报日历",
    "system_health": "系统健康",
}


def _multiplier(symbol: str) -> float:
    return 100.0 if parse_option_code(str(symbol)) is not None else 1.0


def _reason_bucket(reason: str) -> str:
    text = str(reason or "")
    if "行情" in text or "盘口" in text or "新鲜度" in text:
        return "行情/盘口质量"
    if "冷却" in text:
        return "退出冷却"
    if "方向" in text or "动态波动" in text:
        return "信号/方向未确认"
    if "IV/RV" in text or "预期收益" in text or "权利金" in text:
        return "合约质量/成本"
    if "风控" in text:
        return "风控限制"
    return text[:40] or "其它"


def _orders_frame(intents: list[dict]) -> pd.DataFrame:
    rows = []
    for row in intents:
        metadata = _json(row.get("metadata_json"))
        multiplier = float(metadata.get("multiplier") or _multiplier(row.get("symbol", "")))
        filled_qty = float(row.get("filled_quantity") or 0)
        avg_price = float(row.get("average_fill_price") or 0)
        limit_price = float(row.get("limit_price") or 0)
        qty = float(row.get("quantity") or 0)
        rows.append({
            "策略": row.get("strategy_id"),
            "代码": row.get("symbol"),
            "方向": {"BUY": "买入", "SELL": "卖出"}.get(str(row.get("side")), row.get("side")),
            "数量": qty,
            "限价": limit_price,
            "状态": row.get("status"),
            "已成交": filled_qty,
            "成交均价": avg_price if filled_qty else None,
            "计划金额": qty * limit_price * multiplier,
            "成交金额": filled_qty * avg_price * multiplier,
            "原因": metadata.get("reason") or metadata.get("trade_reason") or row.get("error", ""),
            "created_at": row.get("created_at"),
        })
    return pd.DataFrame(rows)


def _option_quality(decisions: list[dict], fills: list[dict]) -> pd.DataFrame:
    fill_map: dict[str, dict[str, float]] = {}
    for row in reversed(fills):
        if row.get("strategy_id") != "volatility_long_gamma":
            continue
        symbol = str(row.get("symbol"))
        side = str(row.get("side"))
        item = fill_map.setdefault(symbol, {"buy_qty": 0.0, "buy_amt": 0.0, "sell_qty": 0.0, "sell_amt": 0.0})
        qty = float(row.get("filled_quantity") or row.get("quantity") or 0)
        price = float(row.get("average_fill_price") or row.get("price") or 0)
        if side == "BUY":
            item["buy_qty"] += qty
            item["buy_amt"] += qty * price
        elif side == "SELL":
            item["sell_qty"] += qty
            item["sell_amt"] += qty * price

    latest: dict[str, dict] = {}
    latest_with_metrics: dict[str, dict] = {}
    for row in sorted(decisions, key=_decision_sort_key):
        if row.get("strategy_id") != "volatility_long_gamma" or not row.get("symbol"):
            continue
        symbol = str(row["symbol"])
        latest[symbol] = row
        if _has_option_metrics(row):
            latest_with_metrics[symbol] = row

    rows = []
    for symbol, decision in sorted(latest.items()):
        metric_decision = latest_with_metrics.get(symbol, decision)
        payload = _json(metric_decision.get("payload_json"))
        fills_for_symbol = fill_map.get(symbol, {})
        if (
            not fills_for_symbol
            and decision.get("decision") in {"BLOCKED", "CANDIDATE_BLOCKED"}
            and "合约已于" in str(decision.get("reason") or "")
        ):
            continue
        buy_qty = fills_for_symbol.get("buy_qty", 0.0)
        sell_qty = fills_for_symbol.get("sell_qty", 0.0)
        net_qty = buy_qty - sell_qty
        buy_avg = fills_for_symbol.get("buy_amt", 0.0) / buy_qty if buy_qty else None
        sell_avg = fills_for_symbol.get("sell_amt", 0.0) / sell_qty if sell_qty else None
        realized = (
            (sell_avg - buy_avg) * sell_qty * 100
            if buy_avg is not None and sell_avg is not None else 0.0
        )
        profile = payload.get("profit_protection_profile") or {}
        peak_pnl = payload.get("mfe_pnl_ratio", payload.get("peak_pnl_ratio"))
        current_quantity = net_qty if fills_for_symbol else payload.get("position_quantity", net_qty)
        if abs(float(current_quantity or 0)) <= 1e-9:
            current_quantity = 0.0
        rows.append({
            "代码": symbol,
            "最新动作": decision.get("decision"),
            "最新原因": decision.get("reason"),
            "指标时间": metric_decision.get("created_at"),
            "指标原因": metric_decision.get("reason"),
            "当前持仓": current_quantity,
            "当前盈亏": payload.get("pnl_ratio"),
            "当前盈亏金额": payload.get("position_pnl_amount"),
            "最大浮盈MFE": peak_pnl,
            "峰值盈利": peak_pnl,
            "浮盈捕获率": payload.get("profit_capture_ratio"),
            "盈利回吐": payload.get("profit_giveback"),
            "可成交估值": payload.get("executable_mark"),
            "阶梯锁盈线": payload.get("tiered_profit_lock_floor"),
            "阶梯档位": payload.get("tiered_profit_lock_label"),
            "保护档": profile.get("tier"),
            "保护启动": profile.get("profit_protection_start"),
            "回吐阈值": profile.get("profit_protection_giveback"),
            "锁定盈利": profile.get("minimum_locked_profit"),
            "买入均价": buy_avg,
            "卖出均价": sell_avg,
            "已实现盈亏": realized,
        })
    return pd.DataFrame(rows)


def _option_pnl_breakdown(fills: list[dict]) -> pd.DataFrame:
    """Summarize filled option round trips by contract for daily review.

    This is intentionally a trading-review view, not a tax lot ledger: it uses
    the fills that occurred inside the selected review window and gives the
    trader a quick read of which contracts made or lost money overnight.
    """
    rows_by_symbol: dict[str, dict[str, object]] = {}
    for row in sorted(fills, key=lambda item: (str(item.get("created_at", "")), int(item.get("id") or 0))):
        if row.get("strategy_id") != "volatility_long_gamma":
            continue
        symbol = str(row.get("symbol") or "")
        if parse_option_code(symbol) is None:
            continue
        side = str(row.get("side") or "").upper()
        qty = float(row.get("filled_quantity") or 0)
        price = float(row.get("average_fill_price") or 0)
        fees = float(row.get("fees") or 0)
        if qty <= 0 or price <= 0 or side not in {"BUY", "SELL"}:
            continue
        item = rows_by_symbol.setdefault(symbol, {
            "代码": symbol,
            "买入数量": 0.0,
            "买入金额": 0.0,
            "卖出数量": 0.0,
            "卖出金额": 0.0,
            "费用": 0.0,
            "成交次数": 0,
            "首次成交": row.get("created_at"),
            "最后成交": row.get("created_at"),
        })
        item["成交次数"] = int(item["成交次数"]) + 1
        item["费用"] = float(item["费用"]) + fees
        item["最后成交"] = row.get("created_at")
        if side == "BUY":
            item["买入数量"] = float(item["买入数量"]) + qty
            item["买入金额"] = float(item["买入金额"]) + qty * price * 100
        else:
            item["卖出数量"] = float(item["卖出数量"]) + qty
            item["卖出金额"] = float(item["卖出金额"]) + qty * price * 100

    rows = []
    for item in rows_by_symbol.values():
        buy_qty = float(item["买入数量"])
        sell_qty = float(item["卖出数量"])
        closed_qty = min(buy_qty, sell_qty)
        buy_avg = float(item["买入金额"]) / buy_qty / 100 if buy_qty else None
        sell_avg = float(item["卖出金额"]) / sell_qty / 100 if sell_qty else None
        gross_pnl = (
            (float(sell_avg) - float(buy_avg)) * closed_qty * 100
            if buy_avg is not None and sell_avg is not None else 0.0
        )
        fees = float(item["费用"])
        net_pnl = gross_pnl - fees if closed_qty > 0 else 0.0
        open_qty = buy_qty - sell_qty
        if closed_qty <= 0 and open_qty > 0:
            result = "持仓中"
        elif net_pnl > 0:
            result = "盈利"
        elif net_pnl < 0:
            result = "亏损"
        else:
            result = "持平"
        rows.append({
            **item,
            "平仓数量": closed_qty,
            "剩余持仓": open_qty,
            "买入均价": buy_avg,
            "卖出均价": sell_avg,
            "毛盈亏": gross_pnl,
            "净盈亏": net_pnl,
            "结果": result,
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values("净盈亏", ascending=False).reset_index(drop=True)


def _core_quality(intents: list[dict]) -> pd.DataFrame:
    rows = []
    for row in intents:
        if row.get("strategy_id") != "core_satellite":
            continue
        metadata = _json(row.get("metadata_json"))
        rows.append({
            "代码": row.get("symbol"),
            "方向": {"BUY": "买入", "SELL": "卖出"}.get(str(row.get("side")), row.get("side")),
            "数量": row.get("quantity"),
            "成交均价": row.get("average_fill_price"),
            "当前权重": metadata.get("current_weight"),
            "目标权重": metadata.get("target_weight"),
            "完整目标金额": metadata.get("desired_notional"),
            "本次计划金额": metadata.get("planned_notional"),
            "调仓原因": metadata.get("reason") or metadata.get("trade_reason"),
            "节奏原因": metadata.get("throttle_reason"),
        })
    return pd.DataFrame(rows)


def _runtime_health(store, window: DiagnosticWindow) -> pd.DataFrame:
    rows = [
        row for row in store.task_runs(limit=10_000)
        if _within(row, window, "started_at")
    ]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    grouped_rows = []
    for task_id, group in frame.groupby("task_id", dropna=False):
        ordered = group.sort_values("started_at")
        latest = ordered.iloc[-1]
        statuses = group["status"].astype(str).str.upper()
        problematic = ordered[
            ordered["status"].astype(str).str.upper().isin(
                ["ERROR", "DEGRADED", "SLOW", "OVERDUE", "INTERRUPTED"],
            )
        ]
        problem = problematic.iloc[-1] if not problematic.empty else latest
        degraded = int(statuses.isin(["DEGRADED", "SLOW", "OVERDUE", "INTERRUPTED"]).sum())
        errors = int(statuses.eq("ERROR").sum())
        grouped_rows.append({
            "任务": TASK_LABELS.get(str(task_id), str(task_id)),
            "任务代码": str(task_id),
            "执行次数": int(len(group)),
            "成功次数": int(statuses.eq("SUCCESS").sum()),
            "退化/超时": degraded,
            "异常次数": errors,
            "最近状态": str(latest.get("status", "")),
            "最近消息": str(latest.get("message") or ""),
            "问题摘要": str(problem.get("message") or problem.get("status") or ""),
            "最后时间": latest.get("started_at"),
        })
    result = pd.DataFrame(grouped_rows)
    return result.sort_values(
        ["异常次数", "退化/超时", "最后时间"], ascending=[False, False, False],
    ).reset_index(drop=True)


def _strategy_interruptions(store, window: DiagnosticWindow) -> pd.DataFrame:
    rows = []
    start_types = set(PROTECTION_EVENT_TYPES) | {"BROKER_CONNECTIVITY_FAILURE"}
    recovery_types = {"PROTECTIVE_RECOVERY_CONFIRMED", "BROKER_CONNECTIVITY_RECOVERED"}
    for row in store.operational_events(limit=20_000):
        event_type = str(row.get("event_type") or "").upper()
        if event_type not in start_types | recovery_types:
            continue
        if not _within(row, window):
            continue
        payload = json_payload(row.get("payload_json"))
        rows.append({
            "时间（中国）": china_label(row.get("created_at")),
            "事件": {
                "BROKER_CONNECTIVITY_FAILURE": "OpenD/券商连接失败",
                "PROTECTIVE_RESTART_REQUESTED": "持仓保护请求重启服务",
                "PROTECTIVE_RADAR_RESTART_REQUESTED": "持仓保护请求重启雷达",
                "PROTECTIVE_SERVICE_RESTART_REQUESTED": "持仓保护服务重启",
                "PROTECTIVE_MODE_ACTIVE": "持仓保护模式激活",
                "PROTECTIVE_RECOVERY_CONFIRMED": "持仓保护恢复确认",
                "BROKER_CONNECTIVITY_RECOVERED": "OpenD/券商连接恢复",
            }.get(event_type, event_type),
            "状态": str(row.get("status") or ""),
            "任务": TASK_LABELS.get(str(payload.get("task") or payload.get("trigger_task") or row.get("component") or ""), str(payload.get("task") or payload.get("trigger_task") or row.get("component") or "")),
            "未平期权数": int(payload.get("open_option_count") or 0),
            "失败次数": int(payload.get("failures") or 0),
            "说明": str(payload.get("message") or payload.get("reason") or row.get("message") or ""),
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values("时间（中国）", ascending=False).reset_index(drop=True)


def build_strategy_review(store, window: DiagnosticWindow) -> StrategyReview:
    diagnostic = build_diagnostic_report(store, window)
    decisions = [
        row for row in store.decisions(limit=50_000)
        if _within(row, window) and row.get("strategy_id") in REVIEW_STRATEGIES
    ]
    intents = [
        row for row in store.intents(limit=20_000)
        if _within(row, window) and row.get("strategy_id") in REVIEW_STRATEGIES
    ]
    # PAPER fills live on order_intents; shadow_fills only covers the Shadow
    # simulator. Use the unified filled-intent ledger for both modes so review
    # P&L cannot silently show zero after real paper fills.
    fills = [
        row for row in store.intents(limit=50_000)
        if _within(row, window) and row.get("strategy_id") in REVIEW_STRATEGIES
        and float(row.get("filled_quantity") or 0) > 0
    ]

    orders = _orders_frame(intents)
    blockers = diagnostic["blockers"].copy()
    trading_blockers, cleanup_blockers, blockers = _split_blockers(blockers)
    option_pnl_breakdown = _option_pnl_breakdown(fills)
    option_quality = _option_quality(decisions, fills)
    core_quality = _core_quality(intents)
    runtime_health = _runtime_health(store, window)
    interruptions = _strategy_interruptions(store, window)
    option_lifecycle = build_option_lifecycle_frame(option_pnl_breakdown, option_quality)
    quality_scores = build_trade_quality_scores(
        option_lifecycle, orders, trading_blockers, runtime_health,
    )
    action_items = build_action_items(
        quality_scores, option_lifecycle, runtime_health, trading_blockers,
    )
    evidence_alignment = build_evidence_alignment(store, REVIEW_STRATEGIES)

    decision_counts = pd.DataFrame(decisions)
    if not decision_counts.empty:
        decision_summary = decision_counts.groupby(["strategy_id", "decision"]).size()
    else:
        decision_summary = pd.Series(dtype=int)
    filled_orders = 0 if orders.empty else int(pd.to_numeric(orders["已成交"], errors="coerce").fillna(0).gt(0).sum())
    blocked_count = int(decision_summary.xs("BLOCKED", level="decision").sum()) if (
        not decision_summary.empty and "BLOCKED" in decision_summary.index.get_level_values("decision")
    ) else 0
    expired_cleanup_count = sum(
        1 for row in decisions
        if row.get("decision") in {"BLOCKED", "CANDIDATE_BLOCKED"}
        and "合约已于" in str(row.get("reason") or "")
    )
    realized_option_pnl = (
        float(option_pnl_breakdown["净盈亏"].sum()) if not option_pnl_breakdown.empty else 0.0
    )
    open_option_count = (
        int(pd.to_numeric(option_quality.get("当前持仓", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum())
        if not option_quality.empty else 0
    )
    capture_values = (
        pd.to_numeric(option_quality.get("浮盈捕获率", pd.Series(dtype=float)), errors="coerce")
        if not option_quality.empty else pd.Series(dtype=float)
    ).dropna()
    metrics = {
        "策略决策": len(decisions),
        "订单数": len(intents),
        "成交订单": filled_orders,
        "阻断次数": blocked_count,
        "交易阻塞类型数": len(trading_blockers),
        "到期/失效清理项": expired_cleanup_count,
        "期权已实现盈亏": realized_option_pnl,
        "期权未平持仓": open_option_count,
        "Core调仓订单": len(core_quality),
        "盈利期权合约": (
            int(pd.to_numeric(option_pnl_breakdown.get("净盈亏", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum())
            if not option_pnl_breakdown.empty else 0
        ),
        "亏损期权合约": (
            int(pd.to_numeric(option_pnl_breakdown.get("净盈亏", pd.Series(dtype=float)), errors="coerce").fillna(0).lt(0).sum())
            if not option_pnl_breakdown.empty else 0
        ),
        "期权毛盈利": (
            float(pd.to_numeric(option_pnl_breakdown.get("净盈亏", pd.Series(dtype=float)), errors="coerce").fillna(0).clip(lower=0).sum())
            if not option_pnl_breakdown.empty else 0.0
        ),
        "期权毛亏损": (
            float(pd.to_numeric(option_pnl_breakdown.get("净盈亏", pd.Series(dtype=float)), errors="coerce").fillna(0).clip(upper=0).sum())
            if not option_pnl_breakdown.empty else 0.0
        ),
        "平均浮盈捕获率": float(capture_values.mean()) if not capture_values.empty else 0.0,
        "低捕获合约数": int(capture_values.lt(.50).sum()) if not capture_values.empty else 0,
        "高优先级行动项": (
            int((action_items["优先级"] == "高").sum()) if not action_items.empty else 0
        ),
        "最低质量评分": (
            int(pd.to_numeric(quality_scores["总评分"], errors="coerce").min())
            if not quality_scores.empty else 100
        ),
        "退化任务": (
            int(pd.to_numeric(runtime_health.get("退化/超时", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
            if not runtime_health.empty else 0
        ),
        "异常任务": (
            int(pd.to_numeric(runtime_health.get("异常次数", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
            if not runtime_health.empty else 0
        ),
        "策略中断事件": len(interruptions),
        "持仓保护恢复": (
            int(interruptions["事件"].astype(str).str.contains("恢复").sum())
            if not interruptions.empty else 0
        ),
    }
    narrative = []
    if not interruptions.empty:
        latest_interruption = interruptions.iloc[0]
        active_count = int(
            len(interruptions[~interruptions["事件"].astype(str).str.contains("恢复")])
        )
        narrative.append(
            f"运行中断追踪发现 {len(interruptions)} 条 OpenD/持仓保护事件；"
            f"最近一次为 {latest_interruption['时间（中国）']} 的"
            f"“{latest_interruption['事件']}”。若首页仍标红，应先确认恢复事件与对账已完成。"
        )
        if active_count and int(metrics["持仓保护恢复"]) == 0:
            narrative.append("本窗口出现连接中断但未看到恢复确认，持仓保护链路需要继续追踪。")
    if not runtime_health.empty:
        degraded_rows = runtime_health[
            pd.to_numeric(runtime_health["退化/超时"], errors="coerce").fillna(0).gt(0)
            | pd.to_numeric(runtime_health["异常次数"], errors="coerce").fillna(0).gt(0)
        ]
        if degraded_rows.empty:
            narrative.append("运行健康层面没有发现任务异常或退化。")
        else:
            top_runtime = degraded_rows.iloc[0]
            narrative.append(
                f"运行健康层面有 {int(metrics['退化任务'])} 次退化/超时、"
                f"{int(metrics['异常任务'])} 次异常；最需要关注的是"
                f"“{top_runtime['任务']}”：{top_runtime['问题摘要'] or top_runtime['最近状态']}。"
            )
    if not option_quality.empty:
        pnl_values = (
            pd.to_numeric(option_pnl_breakdown["净盈亏"], errors="coerce").fillna(0)
            if not option_pnl_breakdown.empty else pd.Series(dtype=float)
        )
        winners = option_pnl_breakdown[pnl_values > 0] if not option_pnl_breakdown.empty else pd.DataFrame()
        losers = option_pnl_breakdown[pnl_values < 0] if not option_pnl_breakdown.empty else pd.DataFrame()
        open_rows = option_quality[pd.to_numeric(option_quality["当前持仓"], errors="coerce").fillna(0) > 0]
        narrative.append(
            f"波动率期权产生 {len(option_quality)} 个合约记录，"
            f"已实现盈亏约 ${realized_option_pnl:,.2f}，"
            f"当前仍有 {len(open_rows)} 个未平合约。"
        )
        if not option_pnl_breakdown.empty:
            win_rate = len(winners) / max(1, len(winners) + len(losers))
            best = option_pnl_breakdown.iloc[0]
            worst = option_pnl_breakdown.iloc[-1]
            narrative.append(
                f"按合约成交表现看，盈利 {len(winners)} 个、亏损 {len(losers)} 个，"
                f"胜率约 {win_rate:.0%}；最佳是 {best['代码']}（约 ${float(best['净盈亏']):,.0f}），"
                f"最弱是 {worst['代码']}（约 ${float(worst['净盈亏']):,.0f}）。"
            )
        if not capture_values.empty:
            narrative.append(
                f"止盈质量看，平均浮盈捕获率约 {float(capture_values.mean()):.0%}，"
                f"低于 50% 的合约 {int(capture_values.lt(.50).sum())} 个；"
                "这用于识别“冲高后回落才卖”的问题。"
            )
        if not winners.empty:
            narrative.append(f"盈利退出合约 {len(winners)} 个，说明自动退出链路能跑通。")
    if not quality_scores.empty:
        worst_quality = quality_scores.iloc[0]
        narrative.append(
            f"质量评分最低的是 {worst_quality['对象']}，总分 {int(worst_quality['总评分'])}，"
            f"主要短板是“{worst_quality['主要短板']}”。"
        )
    if not action_items.empty:
        high_items = int((action_items["优先级"] == "高").sum())
        if high_items:
            narrative.append(f"复盘生成 {high_items} 个高优先级处理项，建议先处理再继续扩大交易。")
    if not cleanup_blockers.empty:
        count = int(pd.to_numeric(cleanup_blockers.get("次数", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
        narrative.append(
            f"发现 {count} 条到期/失效残留记录，已从核心交易阻塞统计中剔除；"
            "请让自动清理任务或页面清理入口处理。"
        )
    if not trading_blockers.empty:
        top = trading_blockers.iloc[0]
        narrative.append(f"最大阻塞来源是“{top['未下单原因']}”，出现 {int(top['次数'])} 次。")
    if not core_quality.empty:
        buys = int((core_quality["方向"] == "买入").sum())
        sells = int((core_quality["方向"] == "卖出").sum())
        narrative.append(f"Core Satellite 昨晚提交 {len(core_quality)} 笔调仓：买入 {buys} 笔，卖出 {sells} 笔。")
    if not narrative:
        narrative.append("本窗口内没有发现核心策略交易记录。")

    return StrategyReview(
        window=window,
        metrics=metrics,
        narrative=narrative,
        orders=orders,
        blockers=blockers,
        option_pnl_breakdown=option_pnl_breakdown,
        option_lifecycle=option_lifecycle,
        quality_scores=quality_scores,
        action_items=action_items,
        evidence_alignment=evidence_alignment,
        option_quality=option_quality,
        core_quality=core_quality,
        runtime_health=runtime_health,
        interruptions=interruptions,
    )


def _review_issue_advice(review: StrategyReview) -> tuple[list[str], list[str]]:
    """Derive compact daily issues and actionable advice for IM delivery."""
    issues: list[str] = []
    advice: list[str] = []

    metrics = review.metrics
    orders_empty = review.orders.empty
    decisions_count = int(metrics.get("策略决策") or 0)
    orders_count = int(metrics.get("订单数") or 0)
    filled_orders = int(metrics.get("成交订单") or 0)
    blocked_count = int(metrics.get("阻断次数") or 0)
    trading_blockers = int(metrics.get("交易阻塞类型数") or 0)
    cleanup_items = int(metrics.get("到期/失效清理项") or 0)
    realized_pnl = float(metrics.get("期权已实现盈亏") or 0.0)

    if review.blockers.empty:
        if orders_count == 0 and filled_orders == 0:
            if decisions_count == 0:
                issues.append("本窗口没有策略决策与订单记录，需要确认策略服务是否持续运行。")
                advice.append("先检查 OpenD / 统一交易服务心跳、行情快照与调度任务是否正常。")
            else:
                issues.append("策略有决策但没有形成订单，主要卡在信号后的下单前置链路。")
                advice.append("优先查看动作项里的首个高优先级阻塞，确认是行情质量、冷却还是合约过期。")
    else:
        blocker_rows = review.blockers.copy()
        if "未下单原因" in blocker_rows.columns:
            top_reasons = []
            for _, row in blocker_rows.head(3).iterrows():
                reason = str(row.get("未下单原因") or "").strip()
                count = str(row.get("次数") or "").strip()
                if reason:
                    top_reasons.append(f"{reason}（{count} 次）" if count else reason)
            if top_reasons:
                issues.append("主要阻塞：" + "；".join(top_reasons[:3]) + "。")
        if cleanup_items > 0:
            issues.append(f"存在 {cleanup_items} 条到期/失效清理项，说明仍有旧合约残留。")
        if trading_blockers > 0:
            advice.append("先按动作项处理交易阻塞，再判断是否放宽盘口价差/新鲜度阈值。")
        if "合约已于" in str(blocker_rows.get("未下单原因", pd.Series(dtype="string")).astype(str).to_dict()):
            advice.append("收缩扫描池，优先剔除已过期/即将到期期权，避免反复空扫。")

    if orders_count and not filled_orders:
        issues.append("订单已生成，但本窗口没有实际成交。")
        advice.append("检查限价设置、追价次数、订单超时与取消原因。")
    elif orders_count and filled_orders:
        fill_rate = filled_orders / orders_count
        if fill_rate < 0.5:
            issues.append(f"成交率偏低：{filled_orders}/{orders_count}。")
            advice.append("检查成交结构是否依赖追价，以及盘口深度是否足够支撑当前手数。")

    if realized_pnl < 0:
        issues.append(f"期权已实现盈亏为 {realized_pnl:.2f} 美元，当日整体亏损。")
        advice.append("检查亏损来源是否集中在某类合约，确认止损/止盈是否过晚。")
    elif realized_pnl > 0:
        issues.append(f"期权已实现盈亏为 {realized_pnl:.2f} 美元，当日有正贡献。")

    if review.runtime_health.empty:
        issues.append("运行健康表没有记录，说明本窗口没有足够的心跳/任务证据。")
        advice.append("先恢复运行层稳定性，再谈策略参数调整。")
    else:
        health = review.runtime_health.copy()
        if "异常次数" in health.columns:
            bad_rows = health[pd.to_numeric(health["异常次数"], errors="coerce").fillna(0) > 0]
            if not bad_rows.empty:
                issues.append(f"运行层有 {len(bad_rows)} 项异常或退化，需要优先处理。")

    if review.action_items.empty:
        advice.append("当前没有生成明确动作项，建议先补充一次人工复盘。")
    else:
        top_actions = review.action_items.copy()
        top_actions["优先级排序"] = top_actions["优先级"].map({"高": 0, "中": 1, "低": 2}).fillna(3)
        top_actions = top_actions.sort_values(["优先级排序", "对象"]).head(3)
        action_lines = [
            f"{row.get('对象', '')}：{row.get('建议动作', row.get('问题', ''))}".strip()
            for _, row in top_actions.iterrows()
        ]
        advice.extend([line for line in action_lines if line])

    if not issues:
        issues.append("当日没有发现显著问题，可保持当前策略配置。")
        advice.append("继续观察成交质量与盘口表现，暂不需要参数调整。")

    dedup_issues = []
    for item in issues:
        if item and item not in dedup_issues:
            dedup_issues.append(item)
    dedup_advice = []
    for item in advice:
        if item and item not in dedup_advice:
            dedup_advice.append(item)
    return dedup_issues[:5], dedup_advice[:5]


def review_advice_markdown(review: StrategyReview) -> str:
    issues, advice = _review_issue_advice(review)
    lines = [
        "## 当日问题与建议",
        "",
        "### 问题",
    ]
    lines.extend([f"- {item}" for item in issues] or ["- 本窗口没有发现显著问题。"])
    lines.append("")
    lines.append("### 建议")
    lines.extend([f"- {item}" for item in advice] or ["- 维持当前配置，继续观察。"])
    return "\n".join(lines)


def review_markdown(review: StrategyReview) -> str:
    def table(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "无记录。"
        values = frame.copy()
        for column in values.columns:
            if values[column].dtype == "float64":
                values[column] = values[column].map(lambda value: "" if pd.isna(value) else f"{value:.4g}")
        values = values.fillna("").astype(str)
        lines = [
            "| " + " | ".join(map(str, values.columns)) + " |",
            "| " + " | ".join("---" for _ in values.columns) + " |",
        ]
        for row in values.itertuples(index=False, name=None):
            lines.append("| " + " | ".join(str(item).replace("|", "\\|") for item in row) + " |")
        return "\n".join(lines)

    lines = [
        f"# 策略复盘报告 {review.window.session_date}",
        "",
        f"中国时间窗口：{review.window.china_label}",
        "",
        "## 一句话结论",
        "",
        *[f"- {item}" for item in review.narrative],
        "",
        "## 核心指标",
        "",
        *[f"- {key}：{value}" for key, value in review.metrics.items()],
        "",
        "## 订单执行质量",
        "",
        table(review.orders),
        "",
        "## 期权盈亏拆解",
        "",
        table(review.option_pnl_breakdown),
        "",
        "## 期权交易生命周期",
        "",
        table(review.option_lifecycle),
        "",
        "## 策略质量评分",
        "",
        table(review.quality_scores),
        "",
        "## 行动项",
        "",
        table(review.action_items),
        "",
        "## 回测/前测证据对照",
        "",
        table(review.evidence_alignment),
        "",
        "## 波动率期权质量面板",
        "",
        table(review.option_quality),
        "",
        "## Core Satellite 调仓质量",
        "",
        table(review.core_quality),
        "",
        "## 运行健康",
        "",
        table(review.runtime_health),
        "",
        "## OpenD 中断与持仓保护追踪",
        "",
        table(review.interruptions),
        "",
        "## 阻塞原因",
        "",
        table(review.blockers),
        "",
        review_advice_markdown(review),
    ]
    return "\n".join(lines)
