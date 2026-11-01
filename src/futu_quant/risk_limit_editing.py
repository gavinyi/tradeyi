"""风险与限额的「可编辑项」元数据与校验 —— **纯逻辑，可单测，不含 Streamlit**。

为什么单独成模块（仓库约定）：`pages/` 只做薄视图，业务逻辑必须在库里。
P0-UI-2 的「风险与限额」页需要 5 个可编辑项，它们的键名映射、区间校验、「只写变化项」
的判断都是业务规则，放在页面里既测不到、也容易被抄成第二份。

口径来源（PRD §12 B 段）：单笔开仓、日累计开仓、单笔减仓、日累计减仓、日亏损急停线，
**正好 5 项，不得私自扩项**；扩项要重走 PRD。

铁律：
- 键名与 `RiskLimits` 属性名一一对应，页面不得自己拼第二套解析；
- 生效值唯一来源是 `resolve_risk_limits()` 返回的 `RiskLimits`，本模块只负责取值与校验；
- 本模块**不写库**：写库仍走 `futu_quant.config.save_runtime_settings`（唯一写路径）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskEditItem:
    """一个可编辑的风险限额项。"""

    key: str            # runtime_config / .env 键名
    label: str          # 中文业务名（用户看到的就是它）
    attr: str           # `RiskLimits` 上的生效值属性名
    minimum: float
    maximum: float
    step: float
    impact: str         # 它控制什么（PRD AC4 四要素之一）


#: PRD §12 B 段锁定的 5 项，**顺序即展示顺序**。
EDITABLE_ITEMS: tuple[RiskEditItem, ...] = (
    RiskEditItem(
        "RISK_MAX_ORDER_NOTIONAL", "单笔最大开仓金额", "max_order_notional",
        100.0, 10_000_000.0, 100.0,
        "单笔买入不超过这个金额；超过即被风控拒绝下单。",
    ),
    RiskEditItem(
        "RISK_MAX_DAILY_NOTIONAL", "每日累计开仓上限", "max_daily_notional",
        100.0, 100_000_000.0, 100.0,
        "当日所有买入累计不超过这个金额；用完当天不再开新仓（不影响平仓）。",
    ),
    RiskEditItem(
        "RISK_MAX_REDUCTION_ORDER_NOTIONAL", "单笔最大减仓金额", "max_reduction_order_notional",
        100.0, 100_000_000.0, 100.0,
        "单笔卖出（减仓）上限；为防止平仓被卡，这个值通常远大于开仓额度。",
    ),
    RiskEditItem(
        "RISK_MAX_DAILY_REDUCTION_NOTIONAL", "每日累计减仓上限", "max_daily_reduction_notional",
        100.0, 500_000_000.0, 100.0,
        "当日减仓累计上限，与开仓额度是两个独立口径（分开算，不互相占用）。",
    ),
    RiskEditItem(
        "RISK_MAX_DAILY_LOSS", "账户当日亏损急停线", "max_daily_loss",
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
    """把界面输入转换成待写入的键值对；**只返回真正变化的项**。

    `current` 为 `None` 时全部写入（首次配置）；有 `current` 时跳过无变化项，
    没有变化就返回 `{}` —— 调用方据此提示「未做修改」，而不是写一遍相同的值。
    """
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
