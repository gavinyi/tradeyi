from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
import re
from zoneinfo import ZoneInfo

import pandas as pd


NY = ZoneInfo("America/New_York")
CHINA = ZoneInfo("Asia/Shanghai")
TRADE_DECISIONS = {"BUY", "SELL"}
ACTIVE_INTENT_STATUSES = {
    "SHADOW", "SHADOW_PART", "SHADOW_FILLED", "SUBMITTED", "FILLED_PART", "FILLED",
}
OPTION_CODE_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$", re.IGNORECASE)
SIDE_LABELS = {
    "BUY": "买入",
    "SELL": "卖出",
    "BUY_BACK": "买入平仓",
    "SELL_SHORT": "卖出开空",
}


@dataclass(frozen=True)
class DiagnosticWindow:
    session_date: date
    start_utc: datetime
    end_utc: datetime

    @property
    def start_iso(self) -> str:
        return self.start_utc.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end_utc.isoformat()

    @property
    def china_label(self) -> str:
        start = self.start_utc.astimezone(CHINA)
        end = self.end_utc.astimezone(CHINA)
        return f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M}"


def latest_us_session_date(now: datetime | None = None) -> date:
    current = now or datetime.now(NY)
    if current.tzinfo is None:
        current = current.replace(tzinfo=NY)
    return current.astimezone(NY).date()


def diagnostic_window(session_date: date) -> DiagnosticWindow:
    start = datetime.combine(session_date, time(9, 0), tzinfo=NY)
    end = datetime.combine(session_date, time(20, 30), tzinfo=NY)
    return DiagnosticWindow(
        session_date=session_date,
        start_utc=start.astimezone(timezone.utc),
        end_utc=end.astimezone(timezone.utc),
    )


def _within(row: dict, window: DiagnosticWindow, column: str = "created_at") -> bool:
    value = str(row.get(column, ""))
    return bool(value and window.start_iso <= value <= window.end_iso)


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _quantity(value) -> int | float | None:
    number = _optional_number(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else number


def _side_label(value) -> str:
    side = str(value or "").upper()
    return SIDE_LABELS.get(side, side)


def _contract_multiplier(symbol: str, metadata: dict) -> float:
    configured = _optional_number(metadata.get("multiplier"))
    if configured is not None and configured > 0:
        return configured
    return 100.0 if OPTION_CODE_PATTERN.match(str(symbol)) else 1.0


def _trade_amounts(
    symbol: str, latest_intent: dict, latest_signal: dict,
) -> dict[str, str | int | float | None]:
    """Return planned and actually filled trade economics for the final diagnosis."""
    signal_payload = _json(latest_signal.get("payload_json"))
    metadata = _json(latest_intent.get("metadata_json"))
    multiplier = _contract_multiplier(symbol, metadata or signal_payload)

    if latest_intent:
        side = latest_intent.get("side")
        quantity = _quantity(latest_intent.get("quantity"))
        limit_price = _optional_number(latest_intent.get("limit_price"))
    else:
        side = latest_signal.get("decision") or signal_payload.get("side")
        quantity = _quantity(
            signal_payload.get("planned_order_quantity", signal_payload.get("quantity"))
        )
        limit_price = next((
            value for key in ("limit_price", "price", "ask_price", "bid_price")
            if (value := _optional_number(signal_payload.get(key))) is not None
        ), None)

    planned_amount = (
        float(quantity) * limit_price * multiplier
        if quantity is not None and limit_price is not None else None
    )
    filled_quantity = _quantity(latest_intent.get("filled_quantity")) if latest_intent else None
    average_fill_price = (
        _optional_number(latest_intent.get("average_fill_price"))
        if latest_intent else None
    )
    actual_amount = (
        float(filled_quantity) * average_fill_price * multiplier
        if filled_quantity and average_fill_price else None
    )
    return {
        "交易方向": _side_label(side),
        "完整目标数量": _quantity(metadata.get("desired_quantity")),
        "当前执行批次": metadata.get("execution_slice"),
        "总批数": metadata.get("total_slices"),
        "计划/订单数量": quantity,
        "限价": limit_price,
        "计划名义金额": planned_amount,
        "完整目标金额": _optional_number(metadata.get("desired_notional")),
        "执行节奏原因": metadata.get("throttle_reason", ""),
        "已成交数量": filled_quantity or 0,
        "成交均价": average_fill_price if filled_quantity else None,
        "实际成交金额": actual_amount if filled_quantity else 0.0,
    }


def _reason_category(reason: str) -> str:
    text = str(reason)
    if "新鲜度" in text or "行情已过期" in text or "盘口不可用" in text:
        return "行情过期或盘口不可用"
    if "预期收益" in text:
        return "模型预期收益未通过"
    if "IV/RV" in text or "IV成本" in text or "隐含波动" in text:
        return "IV成本未通过"
    if "本批权利金" in text or "权利金检查" in text or "premium" in text.lower():
        return "权利金超过预算"
    if "Greeks" in text or "Gamma" in text or "Delta" in text:
        return "Greeks风险未通过"
    if "名义金额" in text:
        return "单笔名义金额超限"
    if "审批" in text:
        return "上线审批未通过"
    if "方向" in text or "动态波动" in text:
        return "信号或方向未确认"
    return text[:60] or "未说明原因"


def _intent_stage(status: str) -> tuple[str, str]:
    status = str(status).upper()
    if status == "REJECTED":
        return "全局风控", "已拒绝"
    if status == "FAILED":
        return "订单提交", "提交失败"
    if status.startswith("SHADOW"):
        return "Shadow执行", "Shadow订单"
    if status in {"SUBMITTED", "SUBMITTING", "WAITING_SUBMIT"}:
        return "券商订单", "等待成交"
    if status == "FILLED_PART":
        return "订单成交", "部分成交"
    if status in {"FILLED", "FILLED_ALL"}:
        return "订单成交", "已成交"
    if "CANCEL" in status:
        return "订单管理", "已撤单"
    return "订单意图", status or "已创建"


def build_diagnostic_report(store, window: DiagnosticWindow) -> dict:
    decisions = [row for row in store.decisions(limit=20_000) if _within(row, window)]
    intents = [row for row in store.intents(limit=10_000) if _within(row, window)]
    audits = [row for row in store.audit_events(limit=20_000) if _within(row, window)]
    fills = [row for row in store.shadow_fills(limit=20_000) if _within(row, window)]

    groups: dict[tuple[str, str], dict] = {}

    def group(strategy: str, symbol: str | None) -> dict:
        key = (str(strategy or "未标记策略"), str(symbol or "组合级"))
        return groups.setdefault(key, {
            "strategy": key[0], "symbol": key[1], "decisions": [], "intents": [],
            "fills": [], "audits": [],
        })

    for row in decisions:
        group(row.get("strategy_id"), row.get("symbol"))["decisions"].append(row)
    for row in intents:
        group(row.get("strategy_id"), row.get("symbol"))["intents"].append(row)
    for row in fills:
        group(row.get("strategy_id"), row.get("symbol"))["fills"].append(row)
    for row in audits:
        payload = _json(row.get("payload_json"))
        symbol = (
            payload.get("underlying") if row.get("strategy_id") == "volatility_scanner"
            else payload.get("symbol") or payload.get("code") or payload.get("underlying")
        )
        strategy = row.get("strategy_id")
        if strategy and symbol:
            group(strategy, symbol)["audits"].append(row)

    summary_rows = []
    blocker_reasons = []
    for value in groups.values():
        group_decisions = sorted(value["decisions"], key=lambda row: row["created_at"])
        group_intents = sorted(value["intents"], key=lambda row: row["created_at"])
        group_fills = sorted(value["fills"], key=lambda row: row["created_at"])
        trade_signals = [
            row for row in group_decisions
            if row["decision"] in TRADE_DECISIONS
            and value["strategy"] != "volatility_scanner"
        ]
        latest_trade_signal = trade_signals[-1] if trade_signals else {}
        blocked = [
            row for row in group_decisions
            if row["decision"] in {"BLOCKED", "CANDIDATE_BLOCKED"}
        ]
        latest = group_decisions[-1] if group_decisions else {}
        latest_intent = group_intents[-1] if group_intents else {}
        auto_created = any(
            row.get("event_type") in {
                "VOLATILITY_SHADOW_CAMPAIGN_AUTO_CREATED",
                "VOLATILITY_AUTO_CAMPAIGN_CREATED",
            }
            for row in value["audits"]
        )
        scanner_trigger_count = sum(
            1 for row in group_decisions
            if value["strategy"] == "volatility_scanner" and row["decision"] == "BUY"
        )
        if latest_intent:
            stage, outcome = _intent_stage(latest_intent.get("status", ""))
            reason = latest_intent.get("error") or latest_intent.get("status", "")
        elif value["strategy"] == "volatility_scanner" and blocked:
            stage, outcome = "候选筛选", "候选被拒绝"
            reason = blocked[-1]["reason"]
        elif value["strategy"] == "volatility_scanner" and auto_created:
            stage, outcome = "自动策略", "已生成"
            reason = "正股触发后已生成自动期权策略"
        elif value["strategy"] == "volatility_scanner" and scanner_trigger_count:
            stage, outcome = "雷达触发", "已触发"
            reason = "正股达到动态波动条件，等待候选或已有活动"
        elif trade_signals:
            stage, outcome = "交易前检查", "未形成订单"
            reason = (
                blocked[-1]["reason"] if blocked
                else f"产生 {len(trade_signals)} 次买卖信号，但没有形成订单意图"
            )
        elif blocked:
            is_candidate = blocked[-1]["decision"] == "CANDIDATE_BLOCKED"
            stage, outcome = (
                ("候选筛选", "候选被拒绝") if is_candidate
                else ("策略校验", "被阻断")
            )
            reason = blocked[-1]["reason"]
        elif latest:
            stage, outcome = "策略信号", "等待信号"
            reason = latest.get("reason", "")
        else:
            stage, outcome, reason = "活动记录", "已记录", "已产生审计事件"
        if outcome in {"已拒绝", "提交失败", "未形成订单", "候选被拒绝", "被阻断"}:
            blocker_reasons.append(reason)
        last_time = max(
            [str(row.get("created_at", "")) for row in (
                group_decisions + group_intents + group_fills + value["audits"]
            )] or [""],
        )
        trade_amounts = _trade_amounts(
            value["symbol"], latest_intent, latest_trade_signal,
        )
        summary_rows.append({
            "策略": value["strategy"], "代码": value["symbol"],
            **trade_amounts,
            "最终阶段": stage, "最终结果": outcome, "最终原因": reason,
            "买卖信号次数": len(trade_signals), "阻断次数": len(blocked),
            "订单意图数": len(group_intents), "Shadow成交数": len(group_fills),
            "最后时间": last_time,
        })

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        severity = {
            "已拒绝": 0, "提交失败": 0, "未形成订单": 1,
            "候选被拒绝": 1, "被阻断": 1, "等待成交": 2,
            "Shadow订单": 2, "部分成交": 2, "已成交": 3, "等待信号": 4,
        }
        summary["_severity"] = summary["最终结果"].map(severity).fillna(5)
        summary = summary.sort_values(
            ["_severity", "最后时间"], ascending=[True, False]
        ).drop(columns="_severity").reset_index(drop=True)

    blocker_counts = pd.Series(
        [_reason_category(reason) for reason in blocker_reasons], dtype="string",
    ).value_counts().rename_axis("未下单原因").reset_index(name="次数")
    scanner = [row for row in decisions if row["strategy_id"] == "volatility_scanner"]
    created_campaigns = [
        row for row in audits
        if row.get("event_type") in {
            "VOLATILITY_SHADOW_CAMPAIGN_AUTO_CREATED",
            "VOLATILITY_AUTO_CAMPAIGN_CREATED",
        }
    ]
    rejected_intents = [row for row in intents if row.get("status") in {"REJECTED", "FAILED"}]
    active_intents = [row for row in intents if row.get("status") in ACTIVE_INTENT_STATUSES]
    metrics = {
        "扫描标的": len({row.get("symbol") for row in scanner if row.get("symbol")}),
        "触发标的": len({
            row.get("symbol") for row in scanner if row.get("decision") == "BUY"
        }),
        "生成自动策略": len(created_campaigns),
        "订单意图": len(intents),
        "已拒绝/失败": len(rejected_intents),
        "已提交或Shadow": len(active_intents),
        "Shadow成交": len(fills),
    }
    return {
        "window": window, "metrics": metrics, "summary": summary,
        "blockers": blocker_counts, "decisions": decisions, "intents": intents,
        "audits": audits, "fills": fills,
    }


def diagnostic_timeline(report: dict, strategy: str, symbol: str) -> pd.DataFrame:
    rows = []
    for row in report["decisions"]:
        if row.get("strategy_id") == strategy and str(row.get("symbol") or "组合级") == symbol:
            rows.append({
                "时间": row["created_at"], "阶段": "策略决策",
                "状态": row["decision"], "说明": row["reason"],
            })
    intent_ids = set()
    for row in report["intents"]:
        if row.get("strategy_id") == strategy and str(row.get("symbol") or "组合级") == symbol:
            intent_ids.add(row["id"])
            rows.append({
                "时间": row["created_at"], "阶段": "订单意图",
                "状态": row["status"], "说明": row.get("error") or (
                    f"{row['side']} {row['quantity']} @ {float(row['limit_price']):.2f}"
                ),
            })
    for row in report["fills"]:
        if row.get("intent_id") in intent_ids:
            rows.append({
                "时间": row["created_at"], "阶段": "Shadow成交",
                "状态": "已撮合", "说明": (
                    f"数量 {row['quantity']}，价格 {float(row['price']):.2f}"
                ),
            })
    for row in report["audits"]:
        payload = _json(row.get("payload_json"))
        audit_symbol = (
            payload.get("underlying") if row.get("strategy_id") == "volatility_scanner"
            else payload.get("symbol") or payload.get("code") or payload.get("underlying")
        )
        if row.get("strategy_id") == strategy and str(audit_symbol or "组合级") == symbol:
            rows.append({
                "时间": row["created_at"], "阶段": "操作审计",
                "状态": row["event_type"], "说明": json.dumps(
                    payload, ensure_ascii=False, default=str,
                ),
            })
    result = pd.DataFrame(rows)
    return result.sort_values("时间").reset_index(drop=True) if not result.empty else result


def diagnostic_markdown(report: dict) -> str:
    def markdown_table(frame: pd.DataFrame) -> str:
        if frame.empty:
            return ""
        values = frame.fillna("").astype(str)
        columns = [str(column) for column in values.columns]
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for row in values.itertuples(index=False, name=None):
            cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    window = report["window"]
    metrics = report["metrics"]
    lines = [
        f"# {window.session_date} 美股交易诊断日报",
        "",
        f"中国时间窗口：{window.china_label}",
        "",
        "## 摘要",
        "",
    ]
    lines.extend(f"- {key}：{value}" for key, value in metrics.items())
    lines.extend(["", "## 策略结果", ""])
    if report["summary"].empty:
        lines.append("本窗口没有策略记录。")
    else:
        lines.append(markdown_table(report["summary"]))
    lines.extend(["", "## 未下单原因", ""])
    if report["blockers"].empty:
        lines.append("没有阻断记录。")
    else:
        lines.append(markdown_table(report["blockers"]))
    return "\n".join(lines)
