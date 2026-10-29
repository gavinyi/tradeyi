"""波动率信号链断链判定（纯函数，供 scanner/campaign 共用）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


DEFAULT_INTENT_TIMEOUT_SECONDS = 900.0


def campaign_intent_chain_status(
    campaign: dict[str, Any],
    *,
    now: datetime,
    intents: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    tenant_id: str,
    timeout_seconds: float = DEFAULT_INTENT_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """返回是否为真正断链；正常门禁留痕、已有 intent、未超时均不告警。"""
    trace_id = str(campaign.get("activation_trace_id") or "").strip()
    created_raw = campaign.get("auto_created_at")
    if not trace_id or not created_raw:
        return False, "缺少新链路追踪元数据，不将存量 campaign 误报为断链"
    try:
        created = datetime.fromisoformat(str(created_raw))
        if created.tzinfo is None:
            raise ValueError("auto_created_at 无时区")
        age = (now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError) as exc:
        return False, f"campaign 链路时间无效：{exc}"
    if age < 0 or age < timeout_seconds:
        return False, f"campaign→intent 等待中（{max(age, 0):.1f}s）"
    code = str(campaign.get("code") or "")
    scoped_intents = [
        row for row in intents
        if str(row.get("tenant_id") or "") == tenant_id
        and str(row.get("strategy_id") or "") == "volatility_long_gamma"
        and str(row.get("symbol") or "") == code
        and str(row.get("created_at") or "") >= str(created_raw)
    ]
    if scoped_intents:
        return False, "已形成订单意图"
    for row in decisions:
        if (
            str(row.get("tenant_id") or "") == tenant_id
            and str(row.get("strategy_id") or "") == "volatility_long_gamma"
            and str(row.get("symbol") or "") == code
            and str(row.get("created_at") or "") >= str(created_raw)
            and str(row.get("decision") or "") in {"BLOCKED", "CLOSED", "HOLD"}
        ):
            return False, "已有正常门禁/策略决策留痕"
    return True, f"campaign 已创建 {age:.1f}s，但无订单意图且无正常门禁留痕（trace_id={trace_id}）"
