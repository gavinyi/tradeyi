from __future__ import annotations

from dataclasses import dataclass
import json

import pandas as pd

from .option_lifecycle import parse_option_code
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
    option_quality: pd.DataFrame
    core_quality: pd.DataFrame
    runtime_health: pd.DataFrame


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
    for row in decisions:
        if row.get("strategy_id") == "volatility_long_gamma" and row.get("symbol"):
            latest[str(row["symbol"])] = row

    rows = []
    for symbol, decision in sorted(latest.items()):
        payload = _json(decision.get("payload_json"))
        fills_for_symbol = fill_map.get(symbol, {})
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
        rows.append({
            "代码": symbol,
            "最新动作": decision.get("decision"),
            "最新原因": decision.get("reason"),
            "当前持仓": payload.get("position_quantity", net_qty),
            "当前盈亏": payload.get("pnl_ratio"),
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
    option_pnl_breakdown = _option_pnl_breakdown(fills)
    option_quality = _option_quality(decisions, fills)
    core_quality = _core_quality(intents)
    runtime_health = _runtime_health(store, window)

    decision_counts = pd.DataFrame(decisions)
    if not decision_counts.empty:
        decision_summary = decision_counts.groupby(["strategy_id", "decision"]).size()
    else:
        decision_summary = pd.Series(dtype=int)
    filled_orders = 0 if orders.empty else int(pd.to_numeric(orders["已成交"], errors="coerce").fillna(0).gt(0).sum())
    blocked_count = int(decision_summary.xs("BLOCKED", level="decision").sum()) if (
        not decision_summary.empty and "BLOCKED" in decision_summary.index.get_level_values("decision")
    ) else 0
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
        "退化任务": (
            int(pd.to_numeric(runtime_health.get("退化/超时", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
            if not runtime_health.empty else 0
        ),
        "异常任务": (
            int(pd.to_numeric(runtime_health.get("异常次数", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
            if not runtime_health.empty else 0
        ),
    }
    narrative = []
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
    if not blockers.empty:
        top = blockers.iloc[0]
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
        option_quality=option_quality,
        core_quality=core_quality,
        runtime_health=runtime_health,
    )


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
        "## 阻塞原因",
        "",
        table(review.blockers),
    ]
    return "\n".join(lines)
