from __future__ import annotations


def heartbeat_display_status(status: str, age_seconds: float, stale_after: float = 120) -> str:
    if age_seconds >= stale_after:
        return "离线（心跳超时）"
    normalized = str(status).upper()
    if normalized == "RUNNING":
        return "正在执行"
    if normalized == "HEALTHY":
        return "正常"
    if normalized == "ERROR":
        return "执行异常"
    if normalized == "STOPPED":
        return "已停止"
    return normalized


def heartbeat_stale_after(strategy_id: str) -> float:
    """Match UI freshness to each task's actual scheduler cadence."""
    if strategy_id in {"earnings_calendar", "correlation_risk", "daily_report"}:
        return 25 * 60 * 60
    if strategy_id == "system_health":
        return 7 * 60
    return 2 * 60
