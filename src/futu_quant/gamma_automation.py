from __future__ import annotations

from datetime import datetime, timedelta


def decide_gamma_action(
    now: datetime,
    event_time: datetime,
    quantity: int,
    stages_filled: int,
    max_stages: int,
    entry_hours: int,
    pnl_ratio: float,
    drawdown_from_peak: float,
    directional_confirmation: bool,
    profit_target: float,
    stop_loss: float,
    trailing_stop: float,
    max_hold_hours: float = 24,
) -> tuple[str, str]:
    if quantity > 0 and pnl_ratio <= -stop_loss:
        return "SELL", "触发硬止损"
    if now >= event_time:
        if quantity <= 0:
            return "HOLD", "入场窗口已结束且当前无持仓"
        if pnl_ratio >= profit_target:
            return "SELL", "达到止盈目标"
        if drawdown_from_peak >= trailing_stop:
            return "SELL", "触发峰值回撤退出"
        if now >= event_time + timedelta(hours=max_hold_hours):
            return "SELL", f"触发财报后{max_hold_hours:g}小时退出"
        return "HOLD", "事件后监控中，禁止继续加仓"
    if now < event_time - timedelta(hours=entry_hours):
        return "HOLD", "尚未进入允许入场的时间窗口"
    if stages_filled == 0:
        return ("BUY", "自动方向确认后首次入场") if directional_confirmation else (
            "HOLD", "自动方向尚未确认"
        )
    if stages_filled < max_stages and directional_confirmation and pnl_ratio >= 0.10:
        return "BUY", "方向持续确认，仅对盈利持仓金字塔加仓"
    return "HOLD", "继续观察，亏损持仓禁止摊低成本"
