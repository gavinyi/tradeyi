from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


NEW_YORK = ZoneInfo("America/New_York")


def _quote_trade_age(row, current: datetime) -> tuple[float | None, str]:
    """成交时间（`update_time`）年龄；缺失/不可解析返回 ``(None, 原因)``。

    ⚠️ 该时间戳是**成交驱动**的：期权深度 ITM 十几分钟不成交即冻结，
    不代表行情没在刷新（2026-10-27 Ops 取证：同采集批 5 个合约 bid/ask 全变，
    但按 `update_time` 算出的年龄 27–1766s）。所以它只提供「成交发生」语义。
    """
    raw = row.get("update_time") or row.get("updated_time") or row.get("time_key")
    if not raw:
        return None, "行情没有更新时间"
    timestamp = pd.to_datetime(raw, errors="coerce")
    if pd.isna(timestamp):
        return None, f"无法识别行情更新时间：{raw}"
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(NEW_YORK, ambiguous="NaT", nonexistent="shift_forward")
    else:
        timestamp = timestamp.tz_convert(NEW_YORK)
    return max(0.0, (current - timestamp.to_pydatetime()).total_seconds()), ""


def quote_freshness(
    row, now: datetime | None = None, max_age_seconds: int = 900, *,
    book_active_age_seconds: float | None = None,
) -> tuple[bool, str, float]:
    """行情新鲜度：**主基准 = min(成交年龄, 盘口变化年龄)**。

    传 ``book_active_age_seconds``（距上次观测到盘口变化的秒数）时按两者较小者判定，
    `update_time` 只保留「成交发生」语义；不传则退化为旧的纯成交时间口径。
    """
    current = now or datetime.now(NEW_YORK)
    trade_age, no_trade_reason = _quote_trade_age(row, current)
    book_age: float | None = None
    if book_active_age_seconds is not None:
        candidate = float(book_active_age_seconds)
        # NaN（盘口全是 NaN 时 BookActivityTracker 可能给 NaN）→ 视为「无盘口依据」
        book_age = None if candidate != candidate else max(0.0, candidate)
    if trade_age is None and book_age is None:
        return False, no_trade_reason, float("inf")
    if book_age is not None and (trade_age is None or book_age < trade_age):
        age, basis = book_age, "，按盘口变化年龄判定"
    else:
        age, basis = trade_age, ""
    return age <= max_age_seconds, (
        f"行情新鲜，延迟 {age:.0f} 秒{basis}" if age <= max_age_seconds
        else f"行情已过期，延迟 {age:.0f} 秒{basis}，超过 {max_age_seconds} 秒"
    ), age


def option_quote_freshness(
    row, now: datetime | None = None, fresh_seconds: int = 180,
    maximum_seconds: int = 300, maximum_spread_pct: float = 20.0,
    *, book_active_age_seconds: float | None = None,
) -> tuple[bool, str, float, bool]:
    """Allow a quiet option quote to degrade gracefully when its book is usable.

    `maximum_seconds` 默认 **300**（P0-5 §5.1，原为 900）—— 候选层与入场层**在时间上分离**
    （扫描 → 建 campaign → 下一轮评估 → 选结构 → 下单），候选在扫描时新鲜**不代表下单时新鲜**。
    调用方应从 `candidate_quality` 的**同源配置**传入，不要各写各的。

    ⚠️ 本函数的 `maximum_spread_pct`（默认 20）是**盘口 sanity 检查**，
    **不是入场价差门禁** —— 入场价差门禁由 campaign 的 `max_spread_pct`（VOLATILITY 默认 **12**）
    与配置 `entry_max_spread_pct`（默认 **12**，与真实门禁一致）共同表达。
    两者语义不同，**不要拿这个 20 当入场上限**（那会把真金门禁从 12% 放宽到 20%）。
    """
    _, _, age = quote_freshness(
        row, now, maximum_seconds, book_active_age_seconds=book_active_age_seconds,
    )
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


def option_quote_activity(
    row, now: datetime | None = None, *, book_moved: bool = False,
    book_active_seconds: float | None = None,
    fresh_seconds: int = 180, maximum_seconds: int = 600,
    maximum_spread_pct: float = 20.0,
) -> tuple[bool, str, float, bool]:
    """入场 / 影子撮合专用：**盘口活性为主判据**，成交时间只作下限。

    为什么不能用 `option_quote_freshness`：Futu 期权快照的 `update_time` 是成交驱动的，
    深度 ITM 合约十几分钟不成交就冻结，但 bid/ask 实时在动（2026-10-26 实测：
    AVGO 冻结 11.5 分钟期间 bid 从 10.85 跌到 9.80），旧判据会把这些单全部误拦。

    判定顺序：
    1. 有效年龄（min 成交年龄 / 盘口变化年龄）≤ ``fresh_seconds`` → 新鲜；
    2. 两次快照间盘口有变化（``book_moved``）→ 新鲜，理由写明「成交停滞但盘口活跃」；
    3. ``book_active_seconds``（距上次观测到盘口变化的秒数）≤ ``maximum_seconds`` → 新鲜；
    4. 有效年龄 ≤ ``maximum_seconds`` 且双边盘口可用 → **降级通过**（兜住深度 ITM）；
    5. 其余不通过。

    返回 ``(fresh, reason, age, degraded)``：``age`` 是**有效年龄**（传了盘口年龄时取
    min(成交, 盘口)，否则就是成交年龄）；``degraded`` 语义与旧函数一致（降级通过为 True）。
    """
    _, age_reason, age = quote_freshness(
        row, now, maximum_seconds, book_active_age_seconds=book_active_seconds,
    )
    if age <= fresh_seconds:
        return True, age_reason, age, False
    if book_moved:
        return True, (
            f"成交停滞 {age:.0f} 秒，但盘口在两次快照间有变化（盘口活跃，放行）"
        ), age, False
    if book_active_seconds is not None and book_active_seconds <= maximum_seconds:
        return True, (
            f"成交停滞 {age:.0f} 秒，但盘口 {book_active_seconds:.0f} 秒内有变化"
            f"（≤{maximum_seconds} 秒，盘口活跃，放行）"
        ), age, False
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
    book_age_text = (
        "未知" if book_active_seconds is None else f"{book_active_seconds:.0f} 秒"
    )
    return False, (
        f"行情已过期，成交延迟 {age:.0f} 秒，超过 {maximum_seconds} 秒；"
        f"盘口变化年龄 {book_age_text}；"
        f"买一 {bid:.2f}、卖一 {ask:.2f}、价差 {spread_pct:.1f}%"
    ), age, False


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
