from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from zoneinfo import ZoneInfo

import pandas as pd


CHINA_ZONE = ZoneInfo("Asia/Shanghai")
ACTION_LABELS = {
    "BUY": "买入",
    "HOLD": "继续观察",
    "SELL_ALL": "全部卖出",
    "SELL_PARTIAL": "分批卖出",
    "BLOCKED": "执行阻断",
}


@dataclass(frozen=True)
class CryptoStrategyReview:
    session_date: date
    metrics: dict[str, object]
    narrative: list[str]
    decisions: pd.DataFrame
    executions: pd.DataFrame
    radar: pd.DataFrame
    holdings: pd.DataFrame
    blockers: pd.DataFrame


def _payload(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _session_bounds(session_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(session_date, time.min, CHINA_ZONE).astimezone(timezone.utc)
    end = datetime.combine(session_date, time.max, CHINA_ZONE).astimezone(timezone.utc)
    return start, end


def _within_session(value, start: datetime, end: datetime) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return start <= timestamp.astimezone(timezone.utc) <= end
    except (TypeError, ValueError):
        return False


def _china_time(value) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(CHINA_ZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "")


def _execution_status(order: dict) -> str:
    status = str(order.get("status") or order.get("state") or "").upper()
    if status == "SHADOW_FILLED":
        return "Shadow 已成交"
    if status in {"FILLED", "FILLED_PART", "PARTIALLY_FILLED"}:
        return "模拟盘已成交" if status == "FILLED" else "模拟盘部分成交"
    if status in {"LIVE", "SUBMITTED", "PENDING"}:
        return "模拟盘已提交"
    return status or "已生成执行结果"


def _frame_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无记录。"
    values = frame.fillna("").astype(str)
    return "\n".join([
        "| " + " | ".join(values.columns) + " |",
        "| " + " | ".join("---" for _ in values.columns) + " |",
        *[
            "| " + " | ".join(
                str(value).replace("|", "\\|").replace("\n", " ")
                for value in row
            ) + " |"
            for row in values.itertuples(index=False, name=None)
        ],
    ])


def build_crypto_strategy_review(store, session_date: date | str) -> CryptoStrategyReview:
    selected_date = pd.to_datetime(session_date).date()
    start, end = _session_bounds(selected_date)
    all_rows = [
        row for row in store.decisions(limit=50_000)
        if row.get("strategy_id") in {
            "crypto_volatility_spot", "crypto_volatility_radar"
        }
        and _within_session(row.get("created_at"), start, end)
    ]
    strategy_rows = [
        row for row in all_rows
        if row.get("strategy_id") == "crypto_volatility_spot"
    ]
    radar_rows = [
        row for row in all_rows
        if row.get("strategy_id") == "crypto_volatility_radar"
    ]

    decision_records: list[dict] = []
    execution_records: list[dict] = []
    blocker_records: list[dict] = []
    for row in strategy_rows:
        payload = _payload(row.get("payload_json"))
        action = str(row.get("decision") or "")
        decision_records.append({
            "北京时间": _china_time(row.get("created_at")),
            "标的": row.get("symbol"),
            "策略判断": ACTION_LABELS.get(action, action),
            "执行模式": payload.get("mode", ""),
            "原因": row.get("reason", ""),
        })
        order = payload.get("order")
        if isinstance(order, dict) and order:
            execution_records.append({
                "北京时间": _china_time(row.get("created_at")),
                "标的": row.get("symbol"),
                "方向": {"buy": "买入", "sell": "卖出"}.get(
                    str(order.get("side", "")).lower(), order.get("side", "")
                ),
                "数量": order.get("quantity", order.get("sz")),
                "价格": order.get("price", order.get("px")),
                "金额": order.get("notional"),
                "执行状态": _execution_status(order),
                "执行模式": payload.get("mode", ""),
            })
        if action == "BLOCKED" or "阻止" in str(row.get("reason", "")):
            blocker_records.append({
                "北京时间": _china_time(row.get("created_at")),
                "标的": row.get("symbol"),
                "原因": row.get("reason", ""),
            })

    radar_records = []
    for row in radar_rows:
        payload = _payload(row.get("payload_json"))
        radar_records.append({
            "北京时间": _china_time(row.get("created_at")),
            "标的": row.get("symbol"),
            "雷达判断": ACTION_LABELS.get(str(row.get("decision", "")), row.get("decision")),
            "信号强度": payload.get("signal_strength", ""),
            "自动加入自选": "是" if payload.get("added_to_watchlist") else "否",
            "原因": row.get("reason", ""),
        })

    holdings = pd.DataFrame([
        {
            "标的": row.get("symbol"),
            "当前数量": row.get("quantity"),
            "开仓均价": row.get("average_cost"),
            "更新时间": _china_time(row.get("updated_at")),
        }
        for row in store.virtual_allocations()
        if row.get("strategy_id") == "crypto_volatility_spot"
        and float(row.get("quantity") or 0) > 0
    ])
    decisions = pd.DataFrame(decision_records)
    executions = pd.DataFrame(execution_records)
    radar = pd.DataFrame(radar_records)
    blockers = pd.DataFrame(blocker_records)
    actionable = sum(
        str(row.get("decision")) in {"BUY", "SELL_ALL", "SELL_PARTIAL"}
        for row in strategy_rows
    )
    auto_added = sum(
        bool(_payload(row.get("payload_json")).get("added_to_watchlist"))
        for row in radar_rows
    )
    execution_rate = len(executions) / actionable if actionable else 0.0
    metrics = {
        "策略判断": len(strategy_rows),
        "交易信号": actionable,
        "执行记录": len(executions),
        "信号执行率": execution_rate,
        "执行阻断": len(blockers),
        "雷达扫描": len(radar_rows),
        "雷达自动加入": auto_added,
        "当前持仓": len(holdings),
    }
    narrative = [
        (
            f"本日策略完成 {len(strategy_rows)} 次判断，产生 {actionable} 次可交易信号，"
            f"形成 {len(executions)} 条执行记录，信号执行率为 {execution_rate:.1%}。"
        ),
        (
            f"自动雷达检查 {len(radar_rows)} 个候选，自动加入策略自选 {auto_added} 个；"
            f"当前仍持有 {len(holdings)} 个币种。"
        ),
    ]
    if blockers.empty:
        narrative.append("本日没有记录到策略执行阻断。")
    else:
        narrative.append(f"本日有 {len(blockers)} 次执行阻断，应优先检查阻断原因明细。")
    return CryptoStrategyReview(
        selected_date, metrics, narrative, decisions, executions,
        radar, holdings, blockers,
    )


def crypto_review_markdown(review: CryptoStrategyReview) -> str:
    return "\n\n".join([
        f"# 币策略复盘与执行质量 {review.session_date}",
        "## 整体结论",
        *[f"- {item}" for item in review.narrative],
        "## 核心指标",
        "\n".join(f"- {key}：{value}" for key, value in review.metrics.items()),
        "## 执行记录",
        _frame_table(review.executions),
        "## 自动雷达",
        _frame_table(review.radar),
        "## 当前持仓",
        _frame_table(review.holdings),
        "## 阻断原因",
        _frame_table(review.blockers),
        "## 策略判断",
        _frame_table(review.decisions),
    ])
