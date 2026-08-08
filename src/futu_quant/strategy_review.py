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
    option_quality: pd.DataFrame
    core_quality: pd.DataFrame


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
        rows.append({
            "代码": symbol,
            "最新动作": decision.get("decision"),
            "最新原因": decision.get("reason"),
            "当前持仓": payload.get("position_quantity", net_qty),
            "当前盈亏": payload.get("pnl_ratio"),
            "峰值盈利": payload.get("peak_pnl_ratio"),
            "盈利回吐": payload.get("profit_giveback"),
            "保护档": profile.get("tier"),
            "保护启动": profile.get("profit_protection_start"),
            "回吐阈值": profile.get("profit_protection_giveback"),
            "锁定盈利": profile.get("minimum_locked_profit"),
            "买入均价": buy_avg,
            "卖出均价": sell_avg,
            "已实现盈亏": realized,
        })
    return pd.DataFrame(rows)


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
    option_quality = _option_quality(decisions, fills)
    core_quality = _core_quality(intents)

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
        float(option_quality["已实现盈亏"].sum()) if not option_quality.empty else 0.0
    )
    open_option_count = (
        int(pd.to_numeric(option_quality.get("当前持仓", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum())
        if not option_quality.empty else 0
    )
    metrics = {
        "策略决策": len(decisions),
        "订单数": len(intents),
        "成交订单": filled_orders,
        "阻断次数": blocked_count,
        "期权已实现盈亏": realized_option_pnl,
        "期权未平持仓": open_option_count,
        "Core调仓订单": len(core_quality),
    }
    narrative = []
    if not option_quality.empty:
        winners = option_quality[pd.to_numeric(option_quality["已实现盈亏"], errors="coerce").fillna(0) > 0]
        open_rows = option_quality[pd.to_numeric(option_quality["当前持仓"], errors="coerce").fillna(0) > 0]
        narrative.append(
            f"波动率期权产生 {len(option_quality)} 个合约记录，"
            f"已实现盈亏约 ${realized_option_pnl:,.2f}，"
            f"当前仍有 {len(open_rows)} 个未平合约。"
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
        option_quality=option_quality,
        core_quality=core_quality,
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
        "## 波动率期权质量面板",
        "",
        table(review.option_quality),
        "",
        "## Core Satellite 调仓质量",
        "",
        table(review.core_quality),
        "",
        "## 阻塞原因",
        "",
        table(review.blockers),
    ]
    return "\n".join(lines)
