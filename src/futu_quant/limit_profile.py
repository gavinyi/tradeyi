"""限额派生阶段 1：统一计算与诊断（不接管现有读侧）。"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class LimitProfile:
    per_order_ratio: float = 0.20  # 2026-11-05 用户拍板：与容量线 20% 同步放宽，杜绝「预检放行、下单被拒」
    daily_entries_ratio: float = 0.06
    daily_loss_ratio: float = 0.20  # 2026-11-05 用户拍板：日亏预算同步 20%
    floor_per_order: float = 100.0
    floor_daily_entries: float = 200.0
    floor_daily_loss: float = 200.0
    hard_cap_per_order: float = 50_000.0
    hard_cap_daily_entries: float = 100_000.0
    hard_cap_daily_loss: float = 50_000.0
    capacity_ratio: float | None = None


@dataclass(frozen=True)
class LimitChain:
    tenant_id: str
    equity: float
    per_order: float
    batch: float
    daily_entries: float
    daily_loss: float
    capacity_line: float | None
    source: str = "derived:diagnostic"
    clamped: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant": self.tenant_id,
            "equity": self.equity,
            "per_order": self.per_order,
            "batch": self.batch,
            "daily_entries": self.daily_entries,
            "daily_loss": self.daily_loss,
            "capacity_line": self.capacity_line,
            "source": self.source,
            "clamped": dict(self.clamped or {}),
        }


def _finite_nonnegative(value: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if not isfinite(result) or result < 0:
        raise ValueError(f"{name} 必须是非负有限数字")
    return result


def _derive(
    equity: float, ratio: float, floor: float, hard_cap: float, name: str
) -> tuple[float, str | None]:
    raw = equity * _finite_nonnegative(ratio, f"{name}.ratio")
    floor_value = _finite_nonnegative(floor, f"{name}.floor")
    cap = _finite_nonnegative(hard_cap, f"{name}.hard_cap")
    if cap < floor_value:
        raise ValueError(f"{name} 的硬顶不能小于地板")
    if raw < floor_value:
        return floor_value, f"floor:{floor_value:g}"
    if raw > cap:
        return cap, f"hard_cap:{cap:g}"
    return raw, None


def resolve_limit_chain(
    store, tenant_id: str, equity: float, profile: LimitProfile | None = None
) -> LimitChain:
    del store
    if not tenant_id:
        raise ValueError("tenant_id 不能为空")
    amount = _finite_nonnegative(equity, "equity")
    if amount <= 0:
        raise ValueError("equity 必须大于 0；无法确认权益时禁止生成放行限额")
    cfg = profile or LimitProfile()
    per_order, per_order_clamp = _derive(
        amount, cfg.per_order_ratio, cfg.floor_per_order,
        cfg.hard_cap_per_order, "per_order"
    )
    daily_entries, entries_clamp = _derive(
        amount, cfg.daily_entries_ratio, cfg.floor_daily_entries,
        cfg.hard_cap_daily_entries, "daily_entries"
    )
    daily_loss, loss_clamp = _derive(
        amount, cfg.daily_loss_ratio, cfg.floor_daily_loss,
        cfg.hard_cap_daily_loss, "daily_loss"
    )
    # 容量线唯一口径（2026-11-05 用户拍板 20%）：派生层与预检同源同值，
    # 默认 = 权益 × CAPACITY_EQUITY_RATIO，再受 profile.capacity_ratio 与
    # 派生日亏预算（loss_limit 语义）双重钳制 —— 预检不得比下单门禁更宽。
    capacity = None
    capacity_clamp = None
    from .capacity_precheck import CAPACITY_EQUITY_RATIO

    capacity = amount * CAPACITY_EQUITY_RATIO
    if cfg.capacity_ratio is not None:
        capacity_ratio = _finite_nonnegative(cfg.capacity_ratio, "capacity.ratio")
        if capacity_ratio > 1.0:
            capacity_ratio = 1.0
            capacity_clamp = "hard_cap:1"
        ratio_cap = amount * capacity_ratio
        if ratio_cap < capacity:
            capacity = ratio_cap
            capacity_clamp = capacity_clamp or f"capacity_ratio:{capacity_ratio:g}"
    if daily_loss < capacity:
        capacity = daily_loss
        capacity_clamp = capacity_clamp or "loss_limit:min"
    clamps = {
        key: value for key, value in {
            "per_order": per_order_clamp,
            "daily_entries": entries_clamp,
            "daily_loss": loss_clamp,
            "capacity_line": capacity_clamp,
        }.items() if value
    }
    return LimitChain(
        tenant_id, amount, per_order, per_order, daily_entries, daily_loss,
        capacity, clamped=clamps
    )


def resolve_batch_budget(
    store, tenant_id: str, legacy_budget: float, equity: float | None
) -> tuple[float, str]:
    """批预算层（第 5 层）：派生启用后取派生链 `batch`，旧键金额只读不生效。

    Reviewer 2026-11-04 阻断 1：`volatility_roll.py` 原先只读
    `campaign.max_premium_per_stage`（`VOLATILITY_RADAR_BUDGETS:{tenant}`），
    98 万模拟盘零成交正是从这一层开始的 —— 七层只接了六层。
    拿不到权益时返回 0（等价一张不买，fail-closed），**绝不回落到旧键金额**。
    """
    if not tenant_id:
        return float(legacy_budget), "legacy:no_tenant"
    from .limit_profile_store import load_limit_profile

    resolution = load_limit_profile(store, tenant_id)
    if resolution.source != "derived:limit_profile":
        return float(legacy_budget), resolution.source
    try:
        amount = _finite_nonnegative(equity if equity is not None else 0.0, "equity")
    except (TypeError, ValueError):
        return 0.0, "derived:missing_equity"
    if amount <= 0:
        return 0.0, "derived:missing_equity"
    chain = resolve_limit_chain(store, tenant_id, amount, resolution.profile)
    return float(chain.batch), resolution.source


def validate_profile(profile: LimitProfile, equity: float) -> tuple[bool, str]:
    """校验待发布百分比；非法值一律拒绝，不静默夹到边界。

    注意：**超过硬顶不是错误** —— 硬顶就是用来截断的（模拟盘 6% = $58,941.37
    实际生效 $50,000），拒绝它会让「按百分比设置、由硬顶兜底」的口径失效。
    低于地板则说明设得过小（等于没放开），直接拒绝而不是偷偷抬到地板。
    """
    try:
        amount = _finite_nonnegative(equity, "equity")
        if amount <= 0:
            return False, "权益必须为正，当前无法生成生效限额"
        for field in ("per_order_ratio", "daily_entries_ratio", "daily_loss_ratio"):
            ratio = _finite_nonnegative(getattr(profile, field), field)
            base = field.replace("_ratio", "")
            floor = _finite_nonnegative(getattr(profile, "floor_" + base), field)
            cap = _finite_nonnegative(getattr(profile, "hard_cap_" + base), field)
            if cap < floor:
                return False, f"{base} 的硬顶不能小于地板"
            value = amount * ratio
            if value < floor:
                return False, f"{field} 派生值 ${value:,.2f} 低于地板 ${floor:,.2f}"
        if profile.capacity_ratio is not None:
            ratio = _finite_nonnegative(profile.capacity_ratio, "capacity_ratio")
            if ratio > 1.0:
                return False, "capacity_ratio 不能超过 100%"
        return True, ""
    except (TypeError, ValueError) as exc:
        return False, str(exc)


def classify_changes(old: LimitChain, new: LimitChain) -> dict[str, str]:
    """按实际生效金额分类；变松必须触发额外确认。"""
    result: dict[str, str] = {}
    for name in ("per_order", "batch", "daily_entries", "daily_loss", "capacity_line"):
        before, after = getattr(old, name), getattr(new, name)
        if before is None or after is None:
            # 容量线可空：新增约束=变紧，去掉约束=变松，都为空=不变。
            result[name] = (
                "unchanged" if before is None and after is None
                else "tighten" if before is None else "loosen"
            )
            continue
        result[name] = "loosen" if after > before else "tighten" if after < before else "unchanged"
    return result


def effective_limit(chain: LimitChain, notional: float) -> tuple[bool, str | None, float]:
    requested = _finite_nonnegative(notional, "notional")
    limits = [
        ("per_order", chain.per_order),
        ("batch", chain.batch),
        ("daily_entries", chain.daily_entries),
    ]
    if chain.capacity_line is not None:
        limits.append(("capacity_line", chain.capacity_line))
    name, amount = min(limits, key=lambda item: item[1])
    return requested <= amount, (None if requested <= amount else name), amount


def diagnostic_payload(
    chain: LimitChain,
    requested: float | None = None,
    actual: Mapping[str, float] | None = None,
) -> dict[str, object]:
    payload = chain.as_dict()
    if actual is not None:
        payload["actual"] = dict(actual)
    if requested is not None:
        allowed, rejected_by, limit = effective_limit(chain, requested)
        payload.update({
            "requested": float(requested),
            "allowed": allowed,
            "rejected_by": rejected_by,
            "binding_limit": limit,
        })
    return payload
