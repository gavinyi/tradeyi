from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


NEW_YORK = ZoneInfo("America/New_York")


def quote_freshness(row, now: datetime | None = None, max_age_seconds: int = 900) -> tuple[bool, str, float]:
    current = now or datetime.now(NEW_YORK)
    raw = row.get("update_time") or row.get("updated_time") or row.get("time_key")
    if not raw:
        return False, "行情没有更新时间", float("inf")
    timestamp = pd.to_datetime(raw, errors="coerce")
    if pd.isna(timestamp):
        return False, f"无法识别行情更新时间：{raw}", float("inf")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(NEW_YORK)
    else:
        timestamp = timestamp.tz_convert(NEW_YORK)
    age = max(0.0, (current - timestamp.to_pydatetime()).total_seconds())
    return age <= max_age_seconds, (
        f"行情新鲜，延迟 {age:.0f} 秒" if age <= max_age_seconds
        else f"行情已过期，延迟 {age:.0f} 秒，超过 {max_age_seconds} 秒"
    ), age


def option_quote_freshness(
    row, now: datetime | None = None, fresh_seconds: int = 180,
    maximum_seconds: int = 900, maximum_spread_pct: float = 20.0,
) -> tuple[bool, str, float, bool]:
    """Allow a quiet option quote to degrade gracefully when its book is usable."""
    _, _, age = quote_freshness(row, now, maximum_seconds)
    if age <= fresh_seconds:
        return True, f"行情新鲜，延迟 {age:.0f} 秒", age, False
    bid = float(row.get("bid_price") or 0)
    ask = float(row.get("ask_price") or 0)
    middle = (bid + ask) / 2
    spread_pct = (ask - bid) / middle * 100 if middle > 0 and ask >= bid else float("inf")
    book_usable = bid > 0 and ask > 0 and ask >= bid and spread_pct <= maximum_spread_pct
    if age <= maximum_seconds and book_usable:
        return True, (
            f"成交不活跃，行情延迟 {age:.0f} 秒；双边盘口仍可用，"
            f"价差 {spread_pct:.1f}%（允许≤{maximum_spread_pct:.1f}%），降级通过"
        ), age, True
    if age > maximum_seconds:
        return False, (
            f"行情已过期，延迟 {age:.0f} 秒，超过 {maximum_seconds} 秒"
        ), age, False
    return False, (
        f"行情延迟 {age:.0f} 秒且盘口不可用：买一 {bid:.2f}、"
        f"卖一 {ask:.2f}、价差 {spread_pct:.1f}%"
    ), age, False


def option_book_status(row, maximum_spread_pct: float = 20.0) -> tuple[bool, str, float]:
    bid = float(row.get("bid_price") or 0)
    ask = float(row.get("ask_price") or 0)
    middle = (bid + ask) / 2
    spread_pct = (ask - bid) / middle * 100 if middle > 0 and ask >= bid else float("inf")
    if bid <= 0 or ask <= 0:
        return False, f"盘口不可用：买一 {bid:.2f}、卖一 {ask:.2f}", spread_pct
    if ask < bid:
        return False, f"盘口倒挂：买一 {bid:.2f}、卖一 {ask:.2f}", spread_pct
    if spread_pct > maximum_spread_pct:
        return False, (
            f"盘口价差过大：买一 {bid:.2f}、卖一 {ask:.2f}、"
            f"价差 {spread_pct:.1f}% > {maximum_spread_pct:.1f}%"
        ), spread_pct
    return True, (
        f"盘口可用：买一 {bid:.2f}、卖一 {ask:.2f}、价差 {spread_pct:.1f}%"
    ), spread_pct


def option_quote_freshness_for_exit(
    row,
    now: datetime | None = None,
    maximum_spread_pct: float = 20.0,
    price_deviation_anchor_pct: float = 0.40,
) -> tuple[bool, str, float]:
    """平仓/止损减仓出口专属盘口新鲜度降级校验。

    四条安全护栏保证：
    1. 仅限平仓类减仓动作，严禁用于入场开仓；
    2. 双边盘口（买一/卖一 > 0 且 ask >= bid）+ 价差 <= 阈值；
    3. 价格合理性锚：仅防单向极端暴跌闪崩（买一低于最后成交价达阈值时拦截；买一高于最后成交价属有利平仓，绝不拦截）；
    4. 允许在成交时长滞后时，基于健康双边盘口放行退出。
    """
    bid = float(row.get("bid_price") or 0)
    ask = float(row.get("ask_price") or 0)
    last = float(row.get("last_price") or 0)
    middle = (bid + ask) / 2
    spread_pct = (ask - bid) / middle * 100 if middle > 0 and ask >= bid else float("inf")

    if bid <= 0 or ask <= 0:
        return False, f"平仓盘口不可用：买一 {bid:.2f}、卖一 {ask:.2f}", spread_pct
    if ask < bid:
        return False, f"平仓盘口倒挂：买一 {bid:.2f}、卖一 {ask:.2f}", spread_pct
    if spread_pct > maximum_spread_pct:
        return False, f"平仓盘口价差过大：{spread_pct:.1f}% > {maximum_spread_pct:.1f}%", spread_pct

    # 单向防暴跌滑点拦截：仅当 bid 明显低于 last 时校验下行偏离，有利滑点（bid >= last）严禁拦截
    if last > 0 and price_deviation_anchor_pct > 0 and bid < last:
        downside_dev = (last - bid) / last
        if downside_dev > price_deviation_anchor_pct:
            return False, (
                f"平仓买一价 ${bid:.2f} 下行偏离最后成交价 ${last:.2f} 达 {downside_dev:.1%} "
                f"> {price_deviation_anchor_pct:.1%}，防极端暴跌抽单保护拦截"
            ), spread_pct

    return True, f"双边盘口健康可用（买一 {bid:.2f}、卖一 {ask:.2f}、价差 {spread_pct:.1f}%），允许出口降级平仓", spread_pct
