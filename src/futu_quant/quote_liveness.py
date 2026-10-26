"""盘口活性判据（2026-10-26）。

背景：Futu 期权快照的 `update_time` 是**成交驱动**的——深度 ITM 合约十几分钟没有成交，
该字段就冻结，但 bid/ask 其实在实时跳动。用 `update_time` 判断「盘口新鲜度」会误拦开仓
（2026-10-26 当日 91 条拦截里 82 条是这个原因）。

本模块只做**观测**：比较两次快照的 bid/ask/last，判断盘口是否在动、距上次变化多久。
不改 `option_history.quality_status` 口径（GOOD/DEGRADED 决定 ML 训练样本，不可动）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def book_signature(row: Mapping[str, Any] | Any | None) -> tuple[float, float, float]:
    """从快照/历史行里取 (bid, ask, last)；兼容 `bid_price` 与 `bid` 两种列名。"""
    if row is None:
        return (0.0, 0.0, 0.0)

    def value(*keys: str) -> float:
        for key in keys:
            try:
                raw = row.get(key)  # type: ignore[union-attr]
            except AttributeError:
                return 0.0
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return 0.0

    return (
        value("bid_price", "bid"),
        value("ask_price", "ask"),
        value("last_price", "last"),
    )


def option_book_moved(
    previous: Mapping[str, Any] | Any | None,
    current: Mapping[str, Any] | Any | None,
    *, tolerance: float = 0.0,
) -> bool:
    """两次快照之间盘口（bid/ask/last 任一）是否发生变化。

    `current` 没有有效盘口时返回 False（不能把「无数据」当成「在动」）。
    """
    if previous is None or current is None:
        return False
    current_signature = book_signature(current)
    if not any(current_signature):
        return False
    previous_signature = book_signature(previous)
    return any(
        abs(now_value - old_value) > tolerance
        for old_value, now_value in zip(previous_signature, current_signature)
    )


class BookActivityTracker:
    """记住每个合约上次看到的盘口，用「距上次变化秒数」当盘口年龄。

    - 首次观测：年龄未知（None），调用方应回落到成交时间判据；
    - 本次盘口变了：年龄 = 0.0；
    - 本次没变：年龄 = 距上次变化的秒数。
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[tuple[float, float, float], datetime]] = {}

    def observe(
        self, code: str, row: Mapping[str, Any] | Any | None,
        now: datetime | None = None,
    ) -> float | None:
        current = now or datetime.now(timezone.utc)
        signature = book_signature(row)
        if not any(signature):
            return None
        previous = self._seen.get(code)
        if previous is None:
            self._seen[code] = (signature, current)
            return None
        if previous[0] != signature:
            self._seen[code] = (signature, current)
            return 0.0
        return max(0.0, (current - previous[1]).total_seconds())

    def reset(self) -> None:
        self._seen.clear()
