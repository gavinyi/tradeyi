"""风险与限额的「可编辑项」元数据、校验与**唯一写路径** —— 不含 Streamlit。

为什么单独成模块（仓库约定）：`pages/` 只做薄视图，业务规则必须在库里。

## 为什么写租户层，而不是写 `ENV_SETTINGS`

风控实际读取顺序（`risk_limits._resolve_uncached`）是：

1. 账户级 `real_account_config:{account}` 的 `risk` 段；
2. 租户级 `risk_limits:{tenant_id}`（无前缀旧键兜底）；
3. env 兜底（`RiskLimits.load()`）。

`futu_quant.config.save_runtime_settings` 写的是 `ENV_SETTINGS` + 镜像 `.env`，
即**最低优先级的 env 兜底**。生产库 `risk_limits:{tenant}` 两条租户键都存在，
于是租户层永远压过 env —— 页面上改完会显示"保存成功"，风控却仍在用旧值。
（2026-11-01 Reviewer 抓出的正是这条。测试只验"写进去了没"，验不出"写了有没有用"。）

因此本模块写**租户层**，并且：

- 写的是**完整 10 字段 risk 段**：`_risk_section_to_limits` 对"部分配置"是
  fail-closed 抛错的（P0-7 铁律），只写 5 项会把整个风控打成不可用；
- 未改动的字段用**当前生效值**补齐，保证写完的语义是"只改这 5 项，其余不动"；
- 写前必须确认生效来源不是**账户级**：账户级在本页之下游，改租户层对它无效，
  这种情况页面必须禁用保存并指路（见 `source_is_writable_here`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .risk_limits import (
    ACCOUNT_RISK_FIELDS,
    ACCOUNT_RISK_FIELD_TO_LIMITS,
    risk_limits_tenant_key,
)


@dataclass(frozen=True)
class RiskEditItem:
    """一个可编辑的风险限额项（`key` 是 risk 段字段名，不是 env 键名）。"""

    key: str            # risk 段字段名（写入目标）
    label: str          # 中文业务名（用户看到的就是它）
    attr: str           # `RiskLimits` 上的生效值属性名
    minimum: float
    maximum: float
    step: float
    impact: str         # 它控制什么（PRD AC4 四要素之一）


#: PRD §12 B 段锁定的 5 项，**顺序即展示顺序**，不得私自扩项（扩项要重走 PRD）。
#:
#: 注意 `max_entry_notional` / `max_entry_daily_notional` 与实盘门禁顶层的
#: `max_order_notional` / `max_daily_notional` **同名不同层**：前者是风控引擎（全模式），
#: 后者是实盘门禁（REAL only）。这里只碰前者，②类顶层键一律只读。
EDITABLE_ITEMS: tuple[RiskEditItem, ...] = (
    RiskEditItem(
        "max_entry_notional", "单笔最大开仓金额", "max_order_notional",
        100.0, 10_000_000.0, 100.0,
        "单笔买入不超过这个金额；超过即被风控拒绝下单。",
    ),
    RiskEditItem(
        "max_entry_daily_notional", "每日累计开仓上限", "max_daily_notional",
        100.0, 100_000_000.0, 100.0,
        "当日所有买入累计不超过这个金额；用完当天不再开新仓（不影响平仓）。",
    ),
    RiskEditItem(
        "max_reduction_order_notional", "单笔最大减仓金额", "max_reduction_order_notional",
        100.0, 100_000_000.0, 100.0,
        "单笔卖出（减仓）上限；为防止平仓被卡，这个值通常远大于开仓额度。",
    ),
    RiskEditItem(
        "max_daily_reduction_notional", "每日累计减仓上限", "max_daily_reduction_notional",
        100.0, 500_000_000.0, 100.0,
        "当日减仓累计上限，与开仓额度是两个独立口径（分开算，不互相占用）。",
    ),
    RiskEditItem(
        "max_daily_loss", "账户当日亏损急停线", "max_daily_loss",
        100.0, 10_000_000.0, 100.0,
        "当日亏损达到该金额即触发急停，禁止继续开仓（平仓不受限）。",
    ),
)

EDITABLE_KEYS: tuple[str, ...] = tuple(item.key for item in EDITABLE_ITEMS)


def effective_values(limits: object) -> dict[str, float]:
    """从 `RiskLimits` 取 5 项的**当前生效值**（唯一来源，不看 env）。

    取不到的项直接不出现在结果里 —— 由页面显式提示「未统计」，绝不填 0 冒充。
    """
    values: dict[str, float] = {}
    for item in EDITABLE_ITEMS:
        try:
            values[item.key] = float(getattr(limits, item.attr))
        except (AttributeError, TypeError, ValueError):
            continue
    return values


def source_is_writable_here(source: str, tenant_id: str) -> bool:
    """当前生效来源是否可由本页写入。

    只有**账户级 `risk` 段**不可写（它在租户层之上，改了租户层也不生效）。
    env 兜底 / 旧无前缀键可以写 —— 写租户层即成为生效层，这是迁移期正确的收口方向。
    """
    if not tenant_id:
        return False
    if source.startswith("real_account_config:"):
        return False
    return True


def validate_value(item: RiskEditItem, value: float) -> float:
    """校验单个输入：必须是有限数字且落在业务区间内，否则抛 `ValueError`。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{item.label} 必须是数字") from exc
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"{item.label} 必须是有限数字")
    if number < item.minimum or number > item.maximum:
        raise ValueError(
            f"{item.label} 必须在 {item.minimum:,.0f} ~ {item.maximum:,.0f} 之间"
        )
    return number


def build_changes(
    values: dict[str, float], current: dict[str, float] | None = None,
) -> dict[str, str]:
    """把界面输入转换成待写入的 change 集合；**只返回真正变化的项**。"""
    changes: dict[str, str] = {}
    for item in EDITABLE_ITEMS:
        if item.key not in values:
            continue
        number = validate_value(item, values[item.key])
        if current is not None and item.key in current:
            if abs(float(current[item.key]) - number) < 1e-9:
                continue
        changes[item.key] = str(number)
    return changes


def build_tenant_risk_payload(
    limits: object, changes: dict[str, str],
) -> dict[str, float]:
    """构造**完整 10 字段**的租户级 risk 段：改动项取新值，其余取当前生效值。

    为什么不只写 5 项：`_risk_section_to_limits` 遇到"部分配置"直接 fail-closed，
    只写 5 项会让整个风控不可用（这才是真事故）。
    """
    payload: dict[str, float] = {}
    for field_name, kind in ACCOUNT_RISK_FIELDS:
        attr = ACCOUNT_RISK_FIELD_TO_LIMITS.get(field_name, field_name)
        if field_name in changes:
            value = float(changes[field_name])
        else:
            raw = getattr(limits, attr, None)
            if raw is None:
                raise ValueError(
                    f"缺少 {field_name} 的当前生效值，无法构造完整 risk 段"
                    "（不完整的 risk 段会被 fail-closed 拒绝，宁可不写）"
                )
            value = float(raw)
        payload[field_name] = int(value) if kind is int else value
    return payload


def save_tenant_risk_limits(
    store, tenant_id: str, payload: dict[str, float], actor: str = "streamlit_admin:panel",
) -> str:
    """把完整 risk 段写入**租户层**（`risk_limits:{tenant_id}`），返回写入的键名。

    **为什么是 `set_setting` 而不是 `mutate_runtime_config`**（2026-11-01 实测）：
    风控解析链走 `store.get_setting()` → 读 `system_settings` 表；而
    `mutate_runtime_config()` 写的是**另一张表** `runtime_config`。用它写租户级风控，
    行确实进了库，但 `resolve_risk_limits` 完全看不到 —— 又是一个「写进去了但没生效」。
    版本/actor 留痕由调用方写 `RISK_LIMITS_UPDATED` 审计补齐。

    注意：`payload` 必须是**完整 10 字段**（`build_tenant_risk_payload` 保证），
    否则 `_risk_section_to_limits` 会按 P0-7 铁律 fail-closed 拒绝。
    """
    if not tenant_id:
        raise ValueError("缺少 tenant_id，拒绝写租户级风控配置")
    key = risk_limits_tenant_key(tenant_id)
    store.set_setting(key, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return key


def payload_preview(payload: dict[str, float]) -> str:
    """把 payload 压成一行可审计的 JSON（用于日志/审计 payload）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
