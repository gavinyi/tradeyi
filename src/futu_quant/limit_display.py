"""限额展示单一出口：页面与运行时必须读同一份「实际生效金额」。

铁律（Reviewer 2026-11-04 阻断 1）：
页面展示的金额不得由页面自己用 `equity × ratio` 推一遍，也不得在派生层未启用时
展示派生**预览值** —— 那会造成「页面一个值、实际另一个值」。
本模块只做一件事：按当前 source 返回**真正生效**的金额，页面直接渲染它。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .limit_profile import resolve_limit_chain
from .limit_profile_store import load_limit_profile

DERIVED_SOURCE = "derived:limit_profile"


@dataclass(frozen=True)
class DisplayLimits:
    """页面要显示的四个数与来源说明（全部为实际生效值）。"""

    source: str
    derived_enabled: bool
    per_order: float
    daily_entries: float
    daily_loss: float
    capacity_line: float | None
    effective_per_order: float
    layer: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "derived_enabled": self.derived_enabled,
            "per_order": self.per_order,
            "daily_entries": self.daily_entries,
            "daily_loss": self.daily_loss,
            "capacity_line": self.capacity_line,
            "effective_per_order": self.effective_per_order,
            "layer": self.layer,
        }


def effective_per_order(per_order: float, capacity_line: float | None) -> float:
    """实盘单笔实际可下金额 = min(派生单笔, 容量线)；容量线缺失时取派生单笔。"""
    return min(per_order, capacity_line) if capacity_line is not None else per_order


def resolve_display_limits(
    store, tenant_id: str, equity: float, legacy_limits: Any = None
) -> DisplayLimits:
    """返回页面必须显示的生效金额；未启用派生层时返回旧键真实值。"""
    resolution = load_limit_profile(store, tenant_id)
    if resolution.source == DERIVED_SOURCE:
        chain = resolve_limit_chain(store, tenant_id, equity, resolution.profile)
        actual = effective_per_order(chain.per_order, chain.capacity_line)
        layer = (
            "容量线"
            if chain.capacity_line is not None and chain.capacity_line <= chain.per_order
            else "派生层"
        )
        return DisplayLimits(
            source=chain.source,
            derived_enabled=True,
            per_order=chain.per_order,
            daily_entries=chain.daily_entries,
            daily_loss=chain.daily_loss,
            capacity_line=chain.capacity_line,
            effective_per_order=actual,
            layer=layer,
        )
    if legacy_limits is None:
        raise ValueError("派生层未启用时必须提供旧键生效值，禁止用预览值冒充实际值")
    try:
        per_order = float(getattr(legacy_limits, "max_order_notional"))
        daily_entries = float(getattr(legacy_limits, "max_daily_notional"))
        daily_loss = float(getattr(legacy_limits, "max_daily_loss"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"旧键生效值无法解析：{exc}") from exc
    return DisplayLimits(
        source=resolution.source,
        derived_enabled=False,
        per_order=per_order,
        daily_entries=daily_entries,
        daily_loss=daily_loss,
        capacity_line=None,
        effective_per_order=per_order,
        layer="旧键",
    )
