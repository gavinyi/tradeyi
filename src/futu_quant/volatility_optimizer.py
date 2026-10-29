from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import ceil, sqrt
import os
from zoneinfo import ZoneInfo

import pandas as pd

from .gamma_risk import market_number, normalize_iv, realized_volatility
from .volatility_structures import DEFAULT_IV_CAP, resolve_iv_percentile_cap


def _bounded_campaign_float(
    value, *, default: float, low: float, high: float,
) -> float:
    """读 campaign 里的数值字段并夹到 ``[low, high]``；缺失/非法回落默认值。

    用于「策略门槛类」参数（`min_return_cost_ratio`、`fee_slippage_allowance_pct`）：
    - 缺失或非数值 → ``default``（保持既有行为，不给老 campaign 制造新拦截）
    - 越界 → 夹到边界，并在越界方向取**更保守**的一端（门槛向上夹、allowance 向上夹）
      由调用方按语义传入 low/high，这里只负责「不炸、不静默变成离谱值」
    """
    number = market_number(value, default)
    if number <= 0:
        # 显式写 0 / 负数一律视为「没配」：既不当作「关掉闸门」，也不当作「零成本」
        return default
    if number < low:
        return low
    if number > high:
        return high
    return number


def volatility_signal_state(
    state: dict, campaign_state: dict, strategy_id: str, underlying: str, code: str,
    tenant_id: str = "",
) -> dict:
    """Return confirmation state shared by option expressions of one underlying.

    2026-10-27：信号键加**租户段** —— `{strategy}:signal:{tenant}:{UNDERLYING}`。
    原来无租户段时实盘/模拟共用一条计时状态，同标的会互相推进对方确认计时。
    兼容历史无租户键：**只搬不复制**（读到旧键就搬到规范键并删旧键，避免两份并存）。
    **租户缺失 fail-closed**：不写任何共享键（计时无法持久化 → 确认永不通过），
    绝不静默回落成跨租户共享键。
    """
    from .volatility_orphans import (
        legacy_volatility_signal_state_key,
        volatility_signal_state_key,
    )

    normalized_underlying = str(underlying or "").strip().upper()
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        # 无租户上下文：返回游离 dict（不挂到 state 上）→ 计时不持久化，确认不通过。
        # A17（2026-10-27）：**提前 return，跳过下面那段 campaign_state 回填**。
        # 回填会把 campaign 里残留的 `signal_started_at` 抄进游离 dict —— 于是
        # 「计时无法持久化」并不可靠：一次调用就可能带着旧时间戳直接判过确认，
        # fail-closed 不彻底。这里只保留与确认计时无关的表达/epoch 默认值。
        orphan_state: dict = {
            "active_contract_code": code,
            "exit_signal_epoch": None,
            "signal_epoch": 0,
        }
        return orphan_state
    signal_key = volatility_signal_state_key(
        normalized_underlying, tenant_id, prefix=f"{strategy_id}:",
    )
    legacy_key = legacy_volatility_signal_state_key(
        normalized_underlying, prefix=f"{strategy_id}:",
    )
    legacy_value = state.pop(legacy_key, None)  # 只搬不复制：迁移后旧键不残留
    if signal_key not in state and legacy_value:
        state[signal_key] = legacy_value
    signal_state = state.setdefault(signal_key, {})
    if not signal_state.get("signal_started_at") and campaign_state.get("signal_started_at"):
        for key in (
            "signal_started_at", "signal_start_volume_ratio", "signal_active",
            "regime_was_active", "signal_epoch", "exit_signal_epoch",
        ):
            if key in campaign_state:
                signal_state[key] = campaign_state[key]
    signal_state["active_contract_code"] = code
    signal_state.setdefault("exit_signal_epoch", None)
    signal_state.setdefault("signal_epoch", 0)
    return signal_state



VOLATILITY_EXIT_PRIORITY = [
    ("到期/行权风险", "临近到期收盘先退出，避免自动行权或流动性塌陷。"),
    ("硬止损", "单笔亏损超过硬止损立即退出，不等待最短持有时间。"),
    ("整单止盈", "未启用分批或只剩 1 张时，到达目标盈利直接兑现。"),
    ("分批止盈", "2 张以上先分批落袋，至少保留 1 张继续追踪趋势。"),
    ("最低锁定盈利", "峰值盈利达标后，不允许盈利单回落到锁定线以下。"),
    ("盈利回吐保护", "按峰值盈利回吐比例退出，不按期权价格回撤误判。"),
    ("IV/Gamma 衰减", "只有价格或方向也不利时，才用 IV/效率衰减退出。"),
    ("方向失效/最长持有", "方向不再支持或超过最长持有窗口后退出。"),
]


PROFIT_PROTECTION_DEFAULTS = {
    "常规": {"start": .10, "giveback": .20, "lock": .05},
    "动态波动": {"start": .12, "giveback": .22, "lock": .07},
    "强波动": {"start": .10, "giveback": .18, "lock": .06},
    "临近到期": {"start": .10, "giveback": .20, "lock": .05},
}


def volatility_profit_lock_floor(peak_pnl_ratio: float) -> tuple[float, str]:
    """Return a professional tiered profit floor based on max favorable excursion."""
    peak = float(peak_pnl_ratio or 0.0)
    tiers = [
        (1.50, 1.10, "超级主升档"),
        (1.00, 0.70, "翻倍主升档"),
        (.80, .50, "爆发档"),
        (.50, .30, "大盈利档"),
        (.30, .18, "强盈利档"),
        (.18, .12, "标准盈利档"),
        (.10, .06, "轻盈利档"),
    ]
    for threshold, floor, label in tiers:
        if peak >= threshold:
            return floor, label
    return 0.0, "未启动"


def volatility_profit_capture_ratio(
    pnl_ratio: float, peak_pnl_ratio: float,
) -> float | None:
    """How much of the position's max favorable excursion is still captured."""
    peak = float(peak_pnl_ratio or 0.0)
    if peak <= 0:
        return None
    return float(pnl_ratio or 0.0) / peak


def _partial_exit_quantity(quantity: int, fraction: float) -> int:
    """Return a safe partial-exit size that never turns a partial into a full exit."""
    if quantity <= 1 or fraction <= 0:
        return 0
    planned = max(1, int(ceil(quantity * float(fraction))))
    return min(planned, quantity - 1)


def select_partial_exit_batch(
    batches: list[dict], *, mode: str = "FIFO",
) -> dict | None:
    """Select the batch whose risk should be reduced first.

    FIFO/LIFO are deterministic; RISK_PRIORITY exits the batch with the worst
    current risk-adjusted return, while preserving the whole-position guard.
    """
    valid = [item for item in batches if int(item.get("quantity", 0) or 0) > 0]
    if not valid:
        return None
    normalized = str(mode or "FIFO").upper()
    if normalized == "LIFO":
        return valid[-1]
    if normalized in {"RISK", "RISK_PRIORITY", "RISK-FIRST"}:
        return min(
            valid,
            key=lambda item: (
                float(item.get("pnl_ratio", 0) or 0),
                float(item.get("r_multiple", 0) or 0),
                str(item.get("entry_time", "")),
            ),
        )
    return valid[0]


def batch_r_multiple(*, mark: float, entry_price: float, stop_price: float) -> float | None:
    """Return realized/unrealized reward divided by initial per-unit risk."""
    risk = float(entry_price) - float(stop_price)
    if risk <= 0 or entry_price <= 0:
        return None
    return round((float(mark) - float(entry_price)) / risk, 12)


def volatility_batch_metrics(
    *, mark: float, batch: dict, peak_mark: float | None = None,
) -> dict:
    entry = float(batch.get("entry_price", 0) or 0)
    stop = float(batch.get("stop_price", 0) or 0)
    peak = max(float(peak_mark or 0), entry, float(mark))
    pnl = mark / entry - 1 if entry > 0 else 0.0
    peak_pnl = peak / entry - 1 if entry > 0 else 0.0
    return {
        "pnl_ratio": pnl,
        "peak_pnl_ratio": peak_pnl,
        "mfe_pnl_ratio": round(peak_pnl, 12),
        "mae_pnl_ratio": min(float(batch.get("mae_pnl_ratio", pnl) or pnl), pnl),
        "profit_capture_ratio": volatility_profit_capture_ratio(pnl, peak_pnl),
        "r_multiple": batch_r_multiple(mark=mark, entry_price=entry, stop_price=stop),
        "peak_mark": peak,
    }


def volatility_partial_exit_plan(
    *,
    quantity: int,
    pnl_ratio: float,
    campaign_state: dict,
    enabled: bool = True,
    tier1_profit: float = .18,
    tier1_fraction: float = .50,
    tier2_profit: float = .35,
    tier2_fraction: float = .25,
) -> tuple[int, str | None, str | None]:
    """Plan professional staged profit taking for multi-contract long-gamma trades.

    Returns ``(exit_quantity, tier_id, reason)``. A zero quantity means no partial
    exit should be sent and the caller can continue with normal exit logic.
    """
    if not enabled or quantity <= 1:
        return 0, None, None
    filled = set(campaign_state.get("partial_exit_tiers") or [])
    tiers = [
        ("tier1", float(tier1_profit), float(tier1_fraction), "第一档"),
        ("tier2", float(tier2_profit), float(tier2_fraction), "第二档"),
    ]
    for tier_id, threshold, fraction, label in tiers:
        if tier_id in filled or pnl_ratio < threshold:
            continue
        exit_quantity = _partial_exit_quantity(quantity, fraction)
        if exit_quantity <= 0:
            continue
        return (
            exit_quantity,
            tier_id,
            f"分批止盈{label}：当前盈利{pnl_ratio:.1%} ≥ {threshold:.1%}，"
            f"卖出{exit_quantity}张，剩余仓位继续盈利回吐保护",
        )
    return 0, None, None


def volatility_profit_protection_profile(
    campaign: dict[str, object], regime: dict[str, object] | None = None,
) -> dict[str, object]:
    tier = volatility_entry_tier(regime or {})
    try:
        parsed_expiry = pd.to_datetime(campaign.get("strike_time"), errors="coerce")
        if pd.isna(parsed_expiry):
            raise ValueError("missing expiry")
        expiry = parsed_expiry.date()
        days_to_expiry = (expiry - datetime.now(ZoneInfo("America/New_York")).date()).days
    except (TypeError, ValueError):
        days_to_expiry = 99
    if days_to_expiry <= int(campaign.get("near_expiry_protection_days", 1)):
        tier = "临近到期"
    defaults = PROFIT_PROTECTION_DEFAULTS[tier]
    suffix = {
        "常规": "normal",
        "动态波动": "dynamic",
        "强波动": "strong",
        "临近到期": "near_expiry",
    }[tier]
    start = float(campaign.get(
        f"profit_protection_start_{suffix}",
        campaign.get("profit_protection_start", defaults["start"]),
    ))
    giveback = float(campaign.get(
        f"profit_protection_giveback_{suffix}",
        campaign.get("profit_protection_drawdown", defaults["giveback"]),
    ))
    lock = float(campaign.get(
        f"minimum_locked_profit_{suffix}",
        campaign.get("minimum_locked_profit", defaults["lock"]),
    ))
    return {
        "tier": tier,
        "profit_protection_start": start,
        "profit_protection_giveback": giveback,
        "minimum_locked_profit": lock,
    }


def volatility_entry_tier(regime: dict[str, object]) -> str:
    """Return the risk tier used for option expression selection."""
    if bool(regime.get("strong_signal_override")) and bool(regime.get("trigger_confirmed")):
        return "强波动"
    if bool(regime.get("active")):
        return "动态波动"
    return "常规"


def volatility_entry_confirmation(
    *,
    now: datetime,
    signal_started_at: str | None,
    regime: dict[str, object],
    confirmation_minutes: float = 5,
    signal_start_volume_ratio: float | None = None,
    minimum_volume_ratio: float = 0.80,
    minimum_volume_retention: float = 0.65,
    require_active: bool = True,
) -> tuple[bool, str]:
    """Require a short persistence check before opening a new long-gamma trade.

    Buying options immediately after a price shock often means paying the peak IV
    and widest spread. This gate keeps exits unchanged, but delays new entries
    until the dynamic signal has survived one short confirmation window.
    """
    configured_minutes = max(0.0, float(confirmation_minutes or 0))
    if require_active and not bool(regime.get("active")):
        return False, f"动态波动状态未激活：{regime.get('reason', '')}"
    if "volume_ratio" in regime:
        current_volume = float(regime.get("volume_ratio", 0) or 0)
        minimum_volume_ratio = max(0.0, float(minimum_volume_ratio or 0))
        if current_volume < minimum_volume_ratio:
            return (
                False,
                "等待入场稳定性确认："
                f"当前量比 {current_volume:.2f} 低于最低 {minimum_volume_ratio:.2f}",
            )
        if signal_start_volume_ratio is not None:
            start_volume = max(0.0, float(signal_start_volume_ratio or 0))
            retention = max(0.0, min(1.0, float(minimum_volume_retention or 0)))
            retained = current_volume / start_volume if start_volume > 0 else 1.0
            if start_volume > 0 and retained < retention:
                return (
                    False,
                    "等待入场稳定性确认："
                    f"量能保持率 {retained:.0%} 低于 {retention:.0%}，"
                    f"起点量比 {start_volume:.2f}，当前量比 {current_volume:.2f}",
                )
    strong = bool(
        regime.get("strong_signal_override")
        and regime.get("trigger_confirmed")
        and regime.get("volume_confirmed")
    )
    fully_confirmed = bool(
        regime.get("trigger_confirmed")
        and regime.get("volume_confirmed")
        and (regime.get("direction_confirmed") or regime.get("strong_signal_override"))
    )
    if strong:
        minutes, tier = min(configured_minutes, 1.0), "强信号"
    elif fully_confirmed:
        minutes, tier = min(configured_minutes, 2.5), "确认信号"
    else:
        minutes, tier = configured_minutes, "边缘信号"
    if minutes <= 0:
        return True, "已关闭入场二次确认"
    if not signal_started_at:
        return False, f"等待入场二次确认：{tier}刚触发，需要持续 {minutes:g} 分钟"
    try:
        started = datetime.fromisoformat(str(signal_started_at))
    except ValueError:
        return False, f"等待入场二次确认：信号开始时间无效，需要重新记录后持续 {minutes:g} 分钟"
    elapsed = (now - started).total_seconds() / 60
    if elapsed < minutes:
        return (
            False,
            f"等待入场二次确认：{tier}已持续 {elapsed:.1f} 分钟，目标 ≥ {minutes:g} 分钟",
        )
    return True, f"入场二次确认通过：{tier}已持续 {elapsed:.1f} 分钟 ≥ {minutes:g} 分钟"


def volatility_direction_size_factor(regime: dict[str, object]) -> tuple[float, str]:
    """Return a size factor for conflicted strong intraday signals.

    If a sharp intraday move overrides lagging 5d/20d direction, keep the trade
    alive but treat it as lower conviction. This avoids turning every spike into
    a full-sized position.
    """
    if (
        bool(regime.get("strong_signal_override"))
        and bool(regime.get("trigger_confirmed"))
        and not bool(regime.get("direction_confirmed"))
    ):
        return 0.5, "强波动方向覆盖但中短趋势分歧，按半档/单张降级执行"
    return 1.0, "方向一致性正常，不降级仓位"


def volatility_entry_quality_allowed(
    campaign: dict[str, object],
    *,
    default_min_score: float = 70.0,
    default_min_expected_return: float = 0.02,
    store: object | None = None,
    strategy_id: str | None = None,
    tenant_id: str | None = None,
    symbol: str | None = None,
    code: str | None = None,
    call_path: str = "unknown",
) -> tuple[bool, str, dict[str, float]]:
    """Final pre-trade quality gate for saved volatility campaigns.

    Candidate ranking happens when an activity is created or rolled. This guard
    prevents stale low-quality saved activities from continuing to open trades
    after the optimizer has become stricter.
    """
    tier = str(campaign.get("adaptive_risk_tier", campaign.get("entry_tier", "")))
    tiered_min_expected = volatility_min_expected_return(
        signal_active="动态" in tier or "强" in tier,
        strong_signal="强" in tier,
    )
    # Use the campaign/risk-tier threshold chosen by the same optimizer that
    # selected the saved expression.  A hard-coded 70 here made valid strong-
    # signal alternatives (54–69) impossible to execute.
    min_score = float(campaign.get(
        "minimum_entry_quality_score",
        default_min_score,
    ) or default_min_score)
    if "强" in tier:
        min_score = max(min_score, 60.0)
    elif "动态" in tier:
        min_score = max(min_score, 62.0)
    configured_min_expected = float(campaign.get(
        "minimum_model_expected_return", default_min_expected_return,
    ) or default_min_expected_return)
    expected_policy = str(campaign.get("expected_return_policy", "TIERED")).upper()
    min_expected = (
        configured_min_expected
        if expected_policy == "STRICT"
        else min(configured_min_expected, tiered_min_expected)
    )
    score = market_number(campaign.get("volatility_quality_score"), 0.0)
    expected_return = campaign.get("model_expected_return")
    expected = market_number(expected_return, 0.0)
    spread = market_number(campaign.get("spread_pct"), 100.0)
    oi = market_number(campaign.get("option_open_interest"), 0.0)
    volume = market_number(campaign.get("volume"), 0.0)
    max_spread = min(10.0, float(campaign.get("maximum_entry_spread_pct", 10.0) or 10.0))
    min_oi = float(campaign.get("minimum_entry_open_interest", 800) or 800)
    min_volume = float(campaign.get("minimum_entry_volume", 10) or 10)
    checks = {
        "quality_score": score, "minimum_quality_score": min_score,
        "model_expected_return": expected,
        "minimum_model_expected_return": min_expected,
        "spread_pct": spread, "maximum_spread_pct": max_spread,
        "open_interest": oi, "minimum_open_interest": min_oi,
        "volume": volume, "minimum_volume": min_volume,
    }
    failed = []
    if score < min_score:
        failed.append(f"质量分 {score:.1f} 低于 {min_score:.1f}")
    # Older campaigns may not have model_expected_return stored; do not block
    # solely because the historical field is absent.
    if expected_return is not None and expected < min_expected:
        failed.append(f"模型预期收益 {expected:.2%} 低于 {min_expected:.2%}")
    cost_multiple = market_number(campaign.get("expected_return_to_cost", campaign.get("model_expected_return_to_cost")), 0.0)
    # 费用/滑点 allowance：往返口径的固定加项，可按券商真实费率配置。
    # 原先写死 0.01（1%）；对低价合约占比过大，但**不能随手调小**——它代表
    # 佣金+交易所费用+滑点的真实成本，调小等于放宽净亏交易。
    allowance = _bounded_campaign_float(
        campaign.get("fee_slippage_allowance_pct"),
        default=0.01, low=0.0, high=0.05,
    )
    estimated_cost = market_number(campaign.get("estimated_total_cost_pct"), 0.0)
    if estimated_cost <= 0:
        # Fail closed when a campaign has no explicit cost estimate: use the
        # quoted percentage spread plus the round-trip fee/slippage allowance.
        estimated_cost = spread / 100.0 + allowance
    cost_multiple = expected / estimated_cost if estimated_cost > 0 else 0.0
    # 预期收益/成本倍数：campaign 可配，默认 2.0（= 行业 2× 往返成本口径，不降默认）。
    # 下限 1.5 是安全底线（低于它等于放行「edge 撑不过一次往返」的交易）；
    # 上限 4.0 防止误配成极端值把策略锁死。非法/缺省一律回落 2.0（fail-closed 到默认）。
    min_return_cost_ratio = _bounded_campaign_float(
        campaign.get("min_return_cost_ratio"),
        default=2.0, low=1.5, high=4.0,
    )
    checks["expected_return_to_cost"] = cost_multiple
    checks["estimated_total_cost_pct"] = estimated_cost
    checks["min_return_cost_ratio"] = min_return_cost_ratio
    checks["fee_slippage_allowance_pct"] = allowance
    if expected < estimated_cost * min_return_cost_ratio:
        failed.append(
            f"预期收益 {expected:.2%} 未达到预计总成本 {estimated_cost:.2%} 的"
            f"{min_return_cost_ratio:g}倍"
        )
    if spread > max_spread:
        failed.append(f"价差 {spread:.1f}% 超过 {max_spread:.1f}%")
    if oi < min_oi:
        failed.append(f"未平仓量 {oi:.0f} 低于 {min_oi:.0f}")
    if volume < min_volume:
        failed.append(f"成交量 {volume:.0f} 低于 {min_volume:.0f}")
    allowed = not failed
    reason = ("入场质量闸门未通过：" + "；".join(failed)) if failed else "入场质量闸门通过"
    # 观测落库：每次判定写一条结构化 checks，直接回答「这张合约卡在哪一条」，
    # 而不是从中文 reason 字符串里猜。不新建表，复用 audit_events。
    # 缺字段/留痕失败一律不抛异常——观测不能反噬交易。
    if store is not None:
        try:
            payload = {
                # 调用路径：`entry` = 真实下单前的入场判定（回答「这张合约为什么没下单」），
                # `review` = 每 300 秒无条件跑的存量表达复核（只说明存量 campaign 仍过闸门）。
                # 两者必须可区分，否则真出现 BUY 时入场记录会被复核记录淹没。
                "call_path": str(call_path or "unknown"),
                # 闸门身份：本函数是**质量分闸门**，只管质量分/预期收益/价差/OI/量/成本倍数。
                # 它与 `volatility_structures.py` 的 **IV 百分位闸门**是两道不同的闸，
                # 这里通过**不代表** IV 闸门放行（2026-10-28 曾据此误判，特此留痕）。
                "gate": "ENTRY_QUALITY",
                # 本函数**不覆盖** `volatility_structures.py` 的 IV 百分位闸门。
                # 显式写 False 而不是留空：今晚正是因为「看见 allowed=true 就以为全覆盖」，
                # 拿质量分闸门去否决 IV 闸门，属越界外推。留空 = 让人再犯一次。
                "iv_gate_covered": False,
                "iv_gate_note": (
                    "IV 百分位闸门在 volatility_structures.py 的 _package 内逐候选判定"
                    "（取各腿 maximum_iv_percentile 的最大值）；本记录不含该判定结果。"
                ),
                # 存量 campaign 选定时留下的 IV 事实（原值照录，**不做任何判定**）。
                # 目的：让 `volatility_structures.py` 的 IV 百分位闸门不再是全黑箱 ——
                # 至少能看到「这张 campaign 当初的 IV 百分位是多少、什么口径、多少样本」。
                # 刻意不在这里算 pass/fail：阈值唯一来源是 `resolve_iv_percentile_cap`，
                # 在此复制一份 0.75 就是类 1「阈值重复」缺陷复发。
                "iv_history_percentile": campaign.get("iv_history_percentile"),
                "iv_history_samples": campaign.get("iv_history_samples"),
                "iv_percentile_status": campaign.get("iv_percentile_status"),
                "tenant_id": str(tenant_id or campaign.get("tenant_id") or ""),
                "strategy_id": str(strategy_id or campaign.get("strategy_id") or ""),
                "symbol": str(symbol or campaign.get("symbol") or ""),
                "code": str(code or campaign.get("code") or ""),
                "campaign_id": str(campaign.get("campaign_id", code or campaign.get("code", ""))),
                "allowed": allowed,
                "reason": reason,
                "checks": checks,
                "effective_thresholds": {
                    "minimum_quality_score": min_score,
                    "minimum_model_expected_return": min_expected,
                    "maximum_spread_pct": max_spread,
                    "minimum_open_interest": min_oi,
                    "minimum_volume": min_volume,
                    "min_return_cost_ratio": min_return_cost_ratio,
                },
            }
            store.audit(
                "VOLATILITY_ENTRY_QUALITY",
                payload,
                payload["strategy_id"] or None,
                actor="service:volatility",
            )
        except Exception:  # noqa: BLE001 - 观测失败不能阻断交易
            pass
    if failed:
        return False, reason, checks
    return True, reason, checks


def volatility_combo_entry_allowed(
    campaign: dict[str, object],
    *,
    default_min_score: float = 68.0,
    default_min_expected_return: float = 0.06,
) -> tuple[bool, str, dict[str, float]]:
    """Allow multi-leg automatic structures only for high-conviction setups."""
    score = market_number(campaign.get("volatility_quality_score"), 0.0)
    expected = market_number(campaign.get("model_expected_return"), 0.0)
    spread = market_number(campaign.get("spread_pct"), 100.0)
    oi = market_number(campaign.get("option_open_interest"), 0.0)
    volume = market_number(campaign.get("volume"), 0.0)
    min_score = float(campaign.get(
        "combo_minimum_entry_quality_score", default_min_score,
    ) or default_min_score)
    min_expected = float(campaign.get(
        "combo_minimum_model_expected_return", default_min_expected_return,
    ) or default_min_expected_return)
    max_spread = float(campaign.get("combo_maximum_entry_spread_pct", 4.0) or 4.0)
    min_oi = float(campaign.get("combo_minimum_open_interest", 1500) or 1500)
    min_volume = float(campaign.get("combo_minimum_volume", 30) or 30)
    checks = {
        "quality_score": score, "minimum_quality_score": min_score,
        "model_expected_return": expected,
        "minimum_model_expected_return": min_expected,
        "spread_pct": spread, "maximum_spread_pct": max_spread,
        "open_interest": oi, "minimum_open_interest": min_oi,
        "volume": volume, "minimum_volume": min_volume,
    }
    failed = []
    if score < min_score:
        failed.append(f"质量分 {score:.1f} 低于组合门槛 {min_score:.1f}")
    if expected < min_expected:
        failed.append(f"模型预期收益 {expected:.2%} 低于组合门槛 {min_expected:.2%}")
    if spread > max_spread:
        failed.append(f"主腿价差 {spread:.1f}% 超过组合门槛 {max_spread:.1f}%")
    if oi < min_oi:
        failed.append(f"主腿未平仓量 {oi:.0f} 低于组合门槛 {min_oi:.0f}")
    if volume < min_volume:
        failed.append(f"主腿成交量 {volume:.0f} 低于组合门槛 {min_volume:.0f}")
    if failed:
        return False, "组合结构质量不足，回退单腿买方期权：" + "；".join(failed), checks
    return True, "组合结构质量通过，可使用定义风险组合", checks


def volatility_hard_loss_exit_reason(
    *,
    pnl_ratio: float,
    pnl_amount: float,
    max_loss_usd: float = 300.0,
    max_loss_ratio: float = 0.25,
) -> str | None:
    """Absolute per-position kill switch for long-gamma option campaigns.

    **口径必须与 campaign 配置一致**：本函数默认值与
    `run_gamma_strategy` 的回落值此前分别是 100 与 300（同一件事两个数，靠
    「键不存在」决定用哪个），属静默分歧。现统一为 300 / 0.25，并且
    **下单前强制校验 campaign 必须显式带这两个键**（缺省即拒绝开仓并记 ERROR），
    不再依赖任何回落值。

    **两层是 OR（取较紧者先生效），不是「先相对、不够再绝对」**：

    - `max_loss_ratio`（相对止损，默认 -25%）：随仓位规模自动缩放，小仓位上先生效；
    - `max_loss_usd`（绝对上限，默认 $300）：大仓位上先生效，防极端跳空下相对止损失守。

    例：1 张 $294 权利金的仓位，-25% ≈ $75 就先触发，$300 那层够不着；
    1 张 $2,076 权利金的仓位，$300（≈14%）比 -25% 更紧，故 $300 生效。
    两层互补，**较紧的那层决定实际止损位** —— 小仓位实际止损比 $300 更紧，属保守一侧。

    **盈亏口径**：`pnl_amount` 是**整仓美元盈亏**
    （`(executable_mark − cost) × quantity × multiplier`），与 `max_loss_usd` 同口径。
    切勿按「每张权利金」重写 —— 那会差 100 倍（合约乘数）。
    """
    usd_limit = max(0.0, float(max_loss_usd or 0.0))
    ratio_limit = max(0.0, float(max_loss_ratio or 0.0))
    usd_hit = usd_limit > 0 and float(pnl_amount) <= -usd_limit
    ratio_hit = ratio_limit > 0 and float(pnl_ratio) <= -ratio_limit
    if not (usd_hit or ratio_hit):
        return None
    reasons = []
    if usd_hit:
        reasons.append(f"亏损 ${abs(float(pnl_amount)):,.2f} ≥ ${usd_limit:,.2f}")
    if ratio_hit:
        reasons.append(f"亏损比例 {abs(float(pnl_ratio)):.1%} ≥ {ratio_limit:.1%}")
    return "单笔硬止损触发：" + "；".join(reasons)


def volatility_time_decay_stop(
    *,
    pnl_ratio: float,
    opened_at: datetime,
    now: datetime,
    hard_stop_loss: float = 0.25,
    floor_stop_loss: float = 0.12,
    start_minutes: float = 15,
    full_decay_minutes: float = 90,
    enabled: bool = True,
) -> tuple[float, float, str | None]:
    """Tighten loss tolerance as a long-gamma option ages.

    Long option premium decays quickly.  A static 25% stop can be too slow for
    weak intraday signals: after the trade has had enough time to work, the
    allowed loss line gradually decays toward a lower floor.  Hard-stop remains
    the final kill switch; this is an earlier risk reducer.
    """
    hard = max(0.0, float(hard_stop_loss or 0.0))
    floor = max(0.0, min(hard, float(floor_stop_loss or 0.0)))
    start = max(0.0, float(start_minutes or 0.0))
    full = max(start + 1.0, float(full_decay_minutes or 0.0))
    if not enabled or hard <= 0:
        return hard, 0.0, None
    held_minutes = max(0.0, (now - opened_at).total_seconds() / 60)
    if held_minutes <= start:
        threshold = hard
    else:
        progress = min(1.0, (held_minutes - start) / (full - start))
        threshold = hard - (hard - floor) * progress
    loss_ratio = max(0.0, -float(pnl_ratio or 0.0))
    if floor > 0 and loss_ratio >= threshold:
        return threshold, held_minutes, (
            f"时间衰减动态止损触发：已持有{held_minutes:.0f}分钟，"
            f"亏损比例{loss_ratio:.1%} ≥ 动态阈值{threshold:.1%}"
            f"（硬止损{hard:.1%}，最低阈值{floor:.1%}）"
        )
    return threshold, held_minutes, None


def volatility_weekend_carry_exit_reason(
    *,
    now: datetime,
    pnl_ratio: float,
    opened_at: datetime,
    min_profit_cushion: float = 0.20,
    pre_close_minutes: float = 30.0,
    enabled: bool = True,
) -> str | None:
    """Exit long option positions before Friday close unless a large profit cushion exists.

    Options carried over weekends suffer 48 hours of pure Theta decay with zero
    trading flexibility, and are exposed to unpredictable Monday gap opens.
    If the position has not built a solid safety cushion (default 20%),
    exit before Friday regular close to protect capital.
    """
    if not enabled:
        return None
    ny_tz = ZoneInfo("America/New_York")
    ny_now = now.astimezone(ny_tz) if now.tzinfo else now.replace(tzinfo=ny_tz)

    # 星期五是 weekday == 4
    if ny_now.weekday() != 4:
        return None

    close_time = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
    minutes_to_close = (close_time - ny_now).total_seconds() / 60.0

    if 0.0 <= minutes_to_close <= max(5.0, float(pre_close_minutes or 30.0)):
        current_pnl = float(pnl_ratio or 0.0)
        cushion = max(0.0, float(min_profit_cushion or 0.20))
        if current_pnl < cushion:
            return (
                f"周五收盘前避险退出：当前盈利{current_pnl:.1%}未达周末安全垫{cushion:.1%}，"
                f"距收盘{minutes_to_close:.0f}分钟主动清仓以防周末时间衰减与周一跳空风险"
            )
    return None


def volatility_min_expected_return(
    *, signal_active: bool = False, strong_signal: bool = False, store=None,
) -> float:
    """Minimum model expected return by volatility tier.

    In paper-trading validation we still reject negative expectancy, but a
    confirmed strong-volatility setup should not be blocked by the same 2% bar
    used for ordinary trades.
    """
    if strong_signal:
        key, env_key, fallback = (
            "volatility_expected_return_min_strong",
            "VOLATILITY_EXPECTED_RETURN_MIN_STRONG",
            "0.0",
        )
    elif signal_active:
        key, env_key, fallback = (
            "volatility_expected_return_min_dynamic",
            "VOLATILITY_EXPECTED_RETURN_MIN_DYNAMIC",
            "0.005",
        )
    else:
        key, env_key, fallback = (
            "volatility_expected_return_min_normal",
            "VOLATILITY_EXPECTED_RETURN_MIN_NORMAL",
            "0.02",
        )
    configured = store.get_setting(key, "") if store is not None else ""
    return float(configured or os.getenv(env_key, fallback))


def tiered_premium_budget(base_budget: float, regime: dict[str, object], store=None) -> tuple[float, str]:
    """Scale budget by signal strength; hard risk gates remain unchanged."""
    tier = volatility_entry_tier(regime)
    defaults = {
        "常规": ("volatility_tier_normal_budget_factor", "VOLATILITY_TIER_NORMAL_BUDGET_FACTOR", "1.0"),
        "动态波动": ("volatility_tier_dynamic_budget_factor", "VOLATILITY_TIER_DYNAMIC_BUDGET_FACTOR", "1.5"),
        "强波动": ("volatility_tier_strong_budget_factor", "VOLATILITY_TIER_STRONG_BUDGET_FACTOR", "2.0"),
    }
    key, env_key, fallback = defaults[tier]
    configured = store.get_setting(key, "") if store is not None else ""
    factors = {tier: float(configured or os.getenv(env_key, fallback))}
    for other, (_, other_env, other_fallback) in defaults.items():
        if other not in factors:
            factors[other] = float(os.getenv(other_env, other_fallback))
    factor = factors[tier]
    return float(base_budget) * factor, tier


def loss_adaptive_premium_budget(
    base_budget: float, store, symbol: str, lookback_hours: float = 24,
) -> tuple[float, str]:
    """Reduce a symbol's next budget after recent realized losses.

    This is a conservative execution throttle, not a signal override: one
    recent losing exit gets a 25% reduction; two or more losing exits get 50%.
    The strategy may still trade if all normal entry gates pass.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(lookback_hours))).isoformat()
    get_setting = getattr(store, "get_setting", lambda _key, default="": default)
    cooldown_after = int(get_setting("volatility_loss_cooldown_after", "3") or 3)
    factor_one = float(get_setting("volatility_loss_budget_factor_one", "0.75") or .75)
    factor_two = float(get_setting("volatility_loss_budget_factor_two", "0.50") or .50)
    normalized_symbol = "".join(
        char for char in str(symbol).replace("US.", "").upper() if char.isalnum()
    )
    rows = [row for row in store.intents(limit=20_000)
            if row.get("strategy_id") == "volatility_long_gamma"
            and (
                row.get("symbol") == symbol
                or normalized_symbol in "".join(
                    char for char in str(row.get("symbol", "")).upper()
                    if char.isalnum()
                )
            )
            and row.get("created_at", "") >= cutoff
            and row.get("status") == "FILLED" and float(row.get("filled_quantity") or 0) > 0]
    buys: list[tuple[float, float]] = []
    exit_pnls: list[float] = []
    for row in sorted(rows, key=lambda item: item.get("created_at", "")):
        qty = float(row.get("filled_quantity") or 0)
        price = float(row.get("average_fill_price") or 0)
        if row.get("side") == "BUY":
            buys.append((qty, price))
        elif row.get("side") == "SELL":
            remaining, pnl = qty, 0.0
            while remaining > 0 and buys:
                buy_qty, buy_price = buys[0]
                take = min(remaining, buy_qty)
                pnl += (price - buy_price) * take * 100
                remaining -= take
                buy_qty -= take
                if buy_qty <= 0:
                    buys.pop(0)
                else:
                    buys[0] = (buy_qty, buy_price)
            exit_pnls.append(pnl)
    losses = sum(1 for pnl in exit_pnls if pnl < 0)
    consecutive_losses = 0
    for pnl in reversed(exit_pnls):
        if pnl < 0:
            consecutive_losses += 1
        else:
            break
    if cooldown_after > 0 and consecutive_losses >= cooldown_after:
        return (
            0.0,
            f"近{lookback_hours:g}小时连续亏损{consecutive_losses}次，"
            "进入同标的防守冷却，暂停新增风险",
        )
    factor = factor_two if consecutive_losses >= 2 else factor_one if consecutive_losses == 1 else 1.0
    return (
        float(base_budget) * factor,
        f"近{lookback_hours:g}小时亏损退出{losses}次，"
        f"连续亏损{consecutive_losses}次，预算系数{factor:.0%}",
    )


def volatility_cooldown_hours(
    reason: str, configured_hours: float = 24, overrides: dict[str, float] | None = None,
) -> float:
    values = {
        "take_profit": 4.0,
        "profit_protection": 8.0,
        "stop_loss": configured_hours,
        "expiry": 0.0,
        "decay": 8.0,
        "contraction": 6.0,
        "default": configured_hours,
    }
    if overrides:
        values.update({key: float(value) for key, value in overrides.items()})
    if "分批止盈" in reason or "替代分批" in reason or "主动落袋" in reason:
        return 0.0
    if "到期" in reason or "行权" in reason:
        return values["expiry"]
    if "硬止损" in reason:
        return values["stop_loss"]
    if "盈利保护" in reason or "最低锁定盈利" in reason:
        return values["profit_protection"]
    if "止盈" in reason:
        return values["take_profit"]
    if any(marker in reason for marker in ("波动开始收缩", "隐含波动率", "Gamma/Theta")):
        return values["contraction"] if "波动开始收缩" in reason else values["decay"]
    return values["default"]


def same_signal_epoch_reentry_allowed(
    *, last_exit_epoch: int | None, current_epoch: int | None,
) -> tuple[bool, str]:
    """Block re-entry while the market is still in the signal that caused exit."""
    if last_exit_epoch is None:
        return False, "缺少上次退出信号周期，按安全规则禁止重入"
    if current_epoch is None:
        return False, "当前信号周期未知，按安全规则禁止重入"
    if int(last_exit_epoch) == int(current_epoch):
        return False, f"仍处于上次退出的信号周期 epoch={int(current_epoch)}"
    return True, f"检测到新的信号周期 epoch={int(current_epoch)}"


def volatility_cooldown_reentry_allowed(
    *, remaining_hours: float, last_exit_epoch: int | None, current_epoch: int | None,
) -> tuple[bool, str]:
    """Allow a re-entry once the time cooldown has actually expired.

    Older campaign state may not contain the exit epoch.  That missing field
    must not turn an already-expired cooldown into a permanent block.
    """
    if float(remaining_hours) <= 0:
        return True, "退出冷却已结束，允许重新评估入场"
    allowed, reason = same_signal_epoch_reentry_allowed(
        last_exit_epoch=last_exit_epoch, current_epoch=current_epoch,
    )
    return allowed, reason


def volatility_cooldown_bypass_allowed(
    reason: str,
    regime: dict,
    *,
    enabled: bool = True,
    allow_stop_loss: bool = False,
    min_zscore: float = 3.0,
    min_intraday_move: float = 0.035,
    min_atr_ratio: float = 2.0,
    min_volume_ratio: float = 1.50,
    signal_tier: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a fresh volatility shock can override an exit cooldown."""
    if not enabled:
        return False, "强信号冷却豁免未开启"
    if "硬止损" in str(reason) and not allow_stop_loss:
        return False, "上次为硬止损退出，默认不允许强信号豁免冷却"
    if not bool(regime.get("active")):
        return False, "动态波动状态未激活"
    volume_ratio = float(regime.get("volume_ratio", 0) or 0)
    if volume_ratio < min_volume_ratio:
        return False, f"量比 {volume_ratio:.2f} 未达到强信号豁免门槛 {min_volume_ratio:.2f}"
    zscore = float(regime.get("zscore_20d", 0) or 0)
    intraday = abs(float(regime.get("intraday_return", 0) or 0))
    atr_ratio = float(regime.get("atr_ratio", 0) or 0)
    strong_checks = [
        zscore >= min_zscore,
        intraday >= min_intraday_move,
        atr_ratio >= min_atr_ratio,
    ]
    # Cooldown bypass is deliberately stricter than entry activation.  A
    # single intraday spike should not immediately re-open a position that
    # just took profit; require two independent volatility confirmations.
    confirmations = sum(bool(value) for value in strong_checks)
    tier = signal_tier or volatility_entry_tier(regime)
    required = 1 if tier == "强波动" and bool(regime.get("volume_confirmed")) else 2
    if confirmations < required:
        return (
            False,
            "强信号豁免未达标："
            f"Z值 {zscore:.2f}/{min_zscore:.2f}，"
            f"日内波动 {intraday:.2%}/{min_intraday_move:.2%}，"
            f"ATR倍数 {atr_ratio:.2f}/{min_atr_ratio:.2f}；"
            f"当前分层 {tier}，仅 {confirmations}/3 项达标，至少需要 {required} 项",
        )
    return (
        True,
        "强波动信号提前解除冷却："
        f"Z值 {zscore:.2f}，日内波动 {intraday:.2%}，"
        f"ATR倍数 {atr_ratio:.2f}，量比 {volume_ratio:.2f}",
    )


def _session_progress() -> float:
    now = datetime.now(ZoneInfo("America/New_York"))
    opened = datetime.combine(now.date(), time(9, 30), tzinfo=now.tzinfo)
    closed = datetime.combine(now.date(), time(16, 0), tzinfo=now.tzinfo)
    if now <= opened:
        return 1.0
    if now >= closed:
        return 1.0
    return max(0.10, min(1.0, (now - opened).total_seconds() / (closed - opened).total_seconds()))


def volatility_threshold_profile(candles: pd.DataFrame) -> dict[str, float]:
    close = candles["close"].astype(float)
    returns = close.pct_change().dropna().tail(60)
    annual_rv = float(returns.std() * sqrt(252)) if len(returns) >= 20 else .30
    return {
        "annual_realized_volatility": annual_rv,
        "entry_zscore": 2.3 if annual_rv >= .60 else 2.1 if annual_rv >= .40 else 1.8,
        "minimum_underlying_move": max(.015, min(.04, annual_rv / sqrt(252) * 1.5)),
    }


def complete_volatility_activation(
    owner, candles: pd.DataFrame, profile: dict[str, float], market_move: float,
) -> dict[str, object]:
    """完整激活判定：基础波动条件 + 相对 SPY/绝对涨跌独立性门。"""
    regime = volatility_regime(
        owner, candles, float(profile["entry_zscore"]),
        float(profile["minimum_underlying_move"]),
    )
    relative_strength = float(regime["intraday_return"]) - float(market_move)
    market_independent = (
        abs(relative_strength) >= .005 or abs(float(regime["intraday_return"])) >= .03
    )
    regime["market_return"] = float(market_move)
    regime["relative_strength"] = relative_strength
    regime["market_independent"] = market_independent
    if regime["active"] and not market_independent:
        regime["active"] = False
        regime["reason"] = (
            f"主要随大盘同步波动：相对SPY仅 {relative_strength:+.2%}，目标绝对值≥0.50%"
        )
    return regime


def volatility_regime(
    owner, candles: pd.DataFrame, entry_z: float = 2.0,
    minimum_move: float = .02,
) -> dict[str, object]:
    bars = candles.sort_values("time_key").copy() if "time_key" in candles else candles.copy()
    close = bars["close"].astype(float)
    high = bars.get("high", close).astype(float)
    low = bars.get("low", close).astype(float)
    returns = close.pct_change()
    last = market_number(owner.get("last_price"), market_number(close.iloc[-1]) if len(close) else 0)
    previous = market_number(owner.get("prev_close_price"), market_number(close.iloc[-2]) if len(close) >= 2 else 0)
    intraday = last / previous - 1 if previous > 0 else 0.0
    sigma20 = float(returns.tail(20).std()) if len(returns.dropna()) >= 20 else 0.0
    zscore = abs(intraday) / sigma20 if sigma20 > 0 else 0.0
    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low, (high - previous_close).abs(), (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr20 = float(true_range.tail(20).mean())
    atr_ratio = abs(last - previous) / atr20 if atr20 > 0 else 0.0
    volume = bars.get("volume", pd.Series(dtype=float)).astype(float)
    average_daily_volume = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else 0.0
    progress = _session_progress()
    expected_volume = average_daily_volume * progress
    volume_ratio = float(volume.iloc[-1] / expected_volume) if expected_volume > 0 else 1.0
    rv10 = float(returns.tail(10).std() * sqrt(252)) if len(returns.dropna()) >= 10 else 0.0
    rv60 = realized_volatility(bars, 60)
    momentum5 = last / float(close.iloc[-6]) - 1 if len(close) >= 6 else 0.0
    trend20 = last / float(close.tail(20).mean()) - 1 if len(close) >= 20 else 0.0
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in (intraday, momentum5, trend20)]
    direction_score = sum(signs)
    dynamic_trigger = zscore >= entry_z or atr_ratio >= 1.5
    absolute_trigger = abs(intraday) >= minimum_move
    strong_override = zscore >= 3.0 or atr_ratio >= 2.0 or abs(intraday) >= .03
    volume_target = 0.80 if strong_override else 1.20
    volume_confirmed = volume_ratio >= volume_target if len(volume) >= 20 else True
    trigger_confirmed = dynamic_trigger or absolute_trigger
    direction_confirmed = abs(direction_score) >= 2
    direction_gate = direction_confirmed or strong_override
    active = bool(trigger_confirmed and volume_confirmed and direction_gate)
    contraction = bool(rv10 > 0 and rv60 > 0 and rv10 < rv60 * 0.70 and zscore < 1.2)
    checks = [
        {
            "检查项": "当日绝对涨跌（触发条件之一）", "当前值": f"{abs(intraday):.2%}",
            "目标值": f"≥ {minimum_move:.2%}", "结果": "达标" if absolute_trigger else "未达标",
        },
        {
            "检查项": "20日波动Z值（触发条件之一）", "当前值": f"{zscore:.2f}",
            "目标值": f"≥ {entry_z:.2f}", "结果": "达标" if zscore >= entry_z else "未达标",
        },
        {
            "检查项": "ATR倍数（触发条件之一）", "当前值": f"{atr_ratio:.2f}",
            "目标值": "≥ 1.50", "结果": "达标" if atr_ratio >= 1.5 else "未达标",
        },
        {
            "检查项": "盘中进度校正量比", "当前值": f"{volume_ratio:.2f}",
            "目标值": f"≥ {volume_target:.2f}" + ("（强信号放宽）" if strong_override else ""),
            "结果": "达标" if volume_confirmed else "未达标",
        },
        {
            "检查项": "方向一致性", "当前值": f"{direction_score:+d}",
            "目标值": "绝对值 ≥ 2（强波动可由日内方向覆盖）",
            "结果": "强信号覆盖" if strong_override and not direction_confirmed
            else "达标" if direction_confirmed else "未达标",
        },
    ]
    failed = []
    if not trigger_confirmed:
        failed.append("涨跌幅、Z值、ATR三个触发条件均未达到")
    if not volume_confirmed:
        failed.append(f"量比 {volume_ratio:.2f} 未达到 {volume_target:.2f}")
    if not direction_gate:
        failed.append(f"方向分 {direction_score:+d} 未达到绝对值2")
    return {
        "active": active, "contraction": contraction, "intraday_return": intraday,
        "zscore_20d": zscore, "atr_ratio": atr_ratio, "volume_ratio": volume_ratio,
        "session_progress": progress,
        "rv10": rv10, "rv60": rv60, "momentum_5d": momentum5, "trend_20d": trend20,
        "direction_score": float(direction_score),
        "strong_signal_override": strong_override,
        "trigger_confirmed": trigger_confirmed,
        "volume_confirmed": volume_confirmed,
        "direction_confirmed": direction_confirmed,
        "direction_gate": direction_gate,
        "entry_zscore_target": entry_z,
        "minimum_move_target": minimum_move,
        "checks": checks,
        "reason": "；".join(failed) if failed else "全部动态波动条件已通过",
    }


def rank_volatility_contracts(
    candidates: pd.DataFrame, spot: float, rv: float, iv_percentile: float | None = None,
    as_of: date | None = None,
    *, signal_active: bool = False, strong_signal: bool = False,
    max_premium_per_stage: float | None = None, contracts_per_stage: int = 1,
    store=None,
    campaign: dict | None = None,
) -> pd.DataFrame:
    if candidates.empty:
        result = candidates.copy()
        # Downstream radar/roll code can safely inspect an empty ranking result.
        result["volatility_quality_score"] = pd.Series(dtype=float)
        result["volatility_eligible"] = pd.Series(dtype=bool)
        result["volatility_filter_reason"] = pd.Series(dtype=str)
        result["days_to_expiry"] = pd.Series(dtype=int)
        result["iv_rv"] = pd.Series(dtype=float)
        result["iv_rv_limit"] = pd.Series(dtype=float)
        result["premium_per_stage"] = pd.Series(dtype=float)
        result["minimum_model_expected_return"] = pd.Series(dtype=float)
        result["budget_alternative"] = pd.Series(dtype=bool)
        result["adaptive_risk_tier"] = pd.Series(dtype=str)
        return result
    today = as_of or datetime.now(ZoneInfo("America/New_York")).date()
    result = candidates.copy()
    if strong_signal:
        iv_rv_limit, risk_tier = 1.8, "强波动精选"
    elif signal_active:
        iv_rv_limit, risk_tier = 1.65, "动态波动精选"
    else:
        iv_rv_limit, risk_tier = 1.5, "常规"
    min_expected_return = volatility_min_expected_return(
        signal_active=signal_active, strong_signal=strong_signal, store=store,
    )
    scores, eligible, reasons, dtes, iv_rv_values = [], [], [], [], []
    premiums, expected_return_flags, premium_flags = [], [], []
    budget_alternatives: list[bool] = []
    for _, row in result.iterrows():
        expiry = pd.to_datetime(row.get("strike_time"), errors="coerce")
        dte = max(0, (expiry.date() - today).days) if not pd.isna(expiry) else 0
        delta = abs(market_number(row.get("option_delta")))
        spread = market_number(row.get("spread_pct"), 100)
        oi = market_number(row.get("option_open_interest"))
        volume = market_number(row.get("volume"))
        iv = normalize_iv(row.get("option_implied_volatility", 0))
        raw_iv_percentile = row.get("iv_history_percentile", iv_percentile)
        row_iv_percentile = market_number(raw_iv_percentile, float("nan"))
        # 口径：只有「同一合约自身历史」算出的百分位才可比历史上限。
        # option_expectancy 在历史样本不足时会回落成链内横截面排名（CHAIN_PROXY），
        # 那是「本合约在当前期权链里贵不贵」，与自身历史位置无关 —— 拿它比 75% 上限
        # 会把所有新合约/0DTE 系统性挡死（10-29 实测 90%~96% 全部属此类）。
        row_iv_status = str(row.get("iv_percentile_status") or "").upper()
        iv_value_present = bool(pd.notna(row_iv_percentile))
        historical_percentile = bool(iv_value_present and row_iv_status == "HISTORICAL")
        has_iv_percentile = iv_value_present
        iv_rv = iv / rv if rv > 0 else 99.0
        expected_return = market_number(row.get("model_expected_return"), -99.0)
        expected_return_ok = expected_return >= min_expected_return
        multiplier = market_number(row.get("option_contract_multiplier"), 100.0)
        ask = market_number(row.get("ask_price"), market_number(row.get("mid_price")))
        premium = ask * multiplier * max(1, int(contracts_per_stage))
        premium_ok = (
            max_premium_per_stage is None
            or (ask > 0 and premium <= float(max_premium_per_stage))
        )
        max_spread = 8.0 if dte == 0 else 6.0 if strong_signal else 5.0
        min_oi = 800 if strong_signal else 1000
        min_volume = 10 if strong_signal else 15
        liquid = spread <= max_spread and oi >= min_oi and volume >= min_volume
        # 入场 IV 百分位上限：与 `volatility_structures._package` 共用同一解析器，
        # 避免同一道闸门出现两个真值源（此处原先写死 .75）。
        iv_cap = resolve_iv_percentile_cap(campaign)
        # 闸门 fail-closed：**字段缺失 = 证据不足 = 拦**；但历史样本不足时的链内代理
        # 不是「历史高位」，只跳过百分位这一道，IV/RV 与绝对 IV 上限仍然生效。
        iv_percentile_ok = bool(
            iv_value_present
            and (not historical_percentile or row_iv_percentile <= iv_cap)
        )
        cost_ok = iv_rv <= iv_rv_limit and iv_percentile_ok
        delta_ok = .30 <= delta <= .80
        delta_score = max(0.0, 30 - abs(delta - .525) * 100)
        dte_score = 25 if 7 <= dte <= 21 else 15 if 2 <= dte <= 30 else 5
        liquidity_score = max(0.0, 20 - spread) + min(10.0, oi / 500)
        gamma = abs(market_number(row.get("option_gamma")))
        theta = abs(market_number(row.get("option_theta")))
        efficiency_score = min(15.0, (gamma / theta if theta > 0 else 0) * 50)
        cost_score = max(0.0, 15 * (1.5 - min(1.5, iv_rv)) / 1.5)
        score = delta_score + dte_score + liquidity_score + efficiency_score + cost_score
        min_score = 54.0 if strong_signal else 58.0 if signal_active else 60.0
        alternative_min_score = min_score - (6.0 if strong_signal else 4.0 if signal_active else 0.0)
        budget_alternative = bool(
            signal_active
            and liquid and cost_ok and delta_ok and expected_return_ok and premium_ok
            and score >= alternative_min_score
            and score < min_score
        )
        is_eligible = bool(
            liquid and cost_ok and delta_ok and expected_return_ok and premium_ok
            and (score >= min_score or budget_alternative)
        )
        reason_parts = []
        if spread > max_spread:
            reason_parts.append(f"价差 {spread:.1f}% 超过 {max_spread:.0f}%")
        if oi < min_oi:
            reason_parts.append(f"未平仓量 {oi:.0f} 低于 {min_oi}")
        if volume < min_volume:
            reason_parts.append(f"成交量 {volume:.0f} 低于 {min_volume}")
        if iv_rv > iv_rv_limit:
            reason_parts.append(
                f"IV/RV {iv_rv:.2f} 超过{risk_tier}门槛 {iv_rv_limit:.2f}"
            )
        if not has_iv_percentile:
            reason_parts.append(
                f"IV 百分位缺失（证据不足，按 fail-closed 拦下，生效上限 {iv_cap:.0%}）"
            )
        elif historical_percentile and row_iv_percentile > iv_cap:
            reason_parts.append(
                f"IV历史百分位 {row_iv_percentile:.0%} 超过 {iv_cap:.0%}"
            )
        # 实时 IV 闸门观测：记录**当前候选**的实时百分位与生效阈值。
        # 必要性（2026-10-29 Ops 指出）：`VOLATILITY_ENTRY_QUALITY` 里照录的是
        # campaign 选定时存下的旧值（如 NVDA 0.7382），而 `:87`/此处拦的是
        # 候选**此刻**的值（88%~97%）——不记实时值，0 单真因永远是黑箱。
        # 只在**被拦下**时记（Reviewer 2026-10-29 条件）：放行的候选已有
        # `VOLATILITY_ENTRY_QUALITY` 覆盖；逐候选全量写会日增数千条、属热路径写放大。
        if store is not None and not iv_percentile_ok:
            try:
                store.audit(
                    "VOLATILITY_IV_GATE",
                    {
                        "tenant_id": str((campaign or {}).get("tenant_id") or ""),
                        "strategy_id": str((campaign or {}).get("strategy_id") or ""),
                        "code": str(row.get("code") or ""),
                        "iv_percentile_live": (
                            None if not has_iv_percentile else float(row_iv_percentile)
                        ),
                        "iv_percentile_status": str(row.get("iv_percentile_status") or ""),
                        "iv_cap_effective": float(iv_cap),
                        "iv_cap_is_default": bool(iv_cap == DEFAULT_IV_CAP),
                        "iv_percentile_ok": bool(iv_percentile_ok),
                        "passed": bool(iv_percentile_ok),
                    },
                    str((campaign or {}).get("strategy_id") or "") or None,
                    actor="service:volatility",
                )
            except Exception:  # noqa: BLE001 - 观测失败不得阻断选合约
                pass
        if not delta_ok:
            reason_parts.append(f"Delta {delta:.2f} 不在 0.30–0.80")
        if not expected_return_ok:
            reason_parts.append(
                f"模型预期收益 {expected_return:.2%} 低于{risk_tier}门槛 {min_expected_return:.2%}"
            )
        if not premium_ok:
            reason_parts.append(
                f"本批权利金 ${premium:,.0f} 超过预算 ${float(max_premium_per_stage):,.0f}"
            )
        if budget_alternative:
            reason_parts.append(
                f"预算替代表达放行：质量分 {score:.1f} 低于精选 {min_score:.0f}，但高于替代门槛 {alternative_min_score:.0f}"
            )
        elif score < min_score:
            reason_parts.append(f"综合质量分 {score:.1f} 低于 {min_score:.0f}")
        scores.append(score)
        eligible.append(is_eligible)
        reasons.append("；".join(reason_parts) or "全部条件通过")
        dtes.append(dte)
        iv_rv_values.append(iv_rv)
        premiums.append(premium)
        expected_return_flags.append(expected_return_ok)
        premium_flags.append(premium_ok)
        budget_alternatives.append(budget_alternative)
    result["volatility_quality_score"] = scores
    result["volatility_eligible"] = eligible
    result["volatility_filter_reason"] = reasons
    result["days_to_expiry"] = dtes
    result["iv_rv"] = iv_rv_values
    result["iv_rv_limit"] = iv_rv_limit
    result["premium_per_stage"] = premiums
    result["minimum_model_expected_return"] = min_expected_return
    result["expected_return_ok"] = expected_return_flags
    result["premium_budget_ok"] = premium_flags
    result["budget_alternative"] = budget_alternatives
    result["volatility_min_quality_score"] = [
        54.0 if strong_signal else 58.0 if signal_active else 60.0
    ] * len(result)
    result["adaptive_risk_tier"] = risk_tier
    sort_columns = [
        "volatility_eligible", "premium_budget_ok",
        "budget_alternative", "volatility_quality_score",
    ]
    ascending = [False, False, True, False]
    if "model_expected_return" in result:
        sort_columns.append("model_expected_return")
        ascending.append(False)
    return result.sort_values(
        sort_columns, ascending=ascending,
    ).reset_index(drop=True)


def volatility_exit_reason(
    regime: dict, current_iv: float, entry_iv: float, current_efficiency: float,
    entry_efficiency: float, iv_decline_ratio: float = .20,
    efficiency_decline_ratio: float = .40,
    contraction_confirmations_required: int = 3,
    pnl_ratio: float = 0.0,
    directional_confirmation: bool = True,
    current_delta: float | None = None,
    minimum_effective_delta: float = .18,
    adverse_signal_confirmations: int = 0,
    adverse_signal_confirmations_required: int = 2,
) -> str | None:
    inactive_contraction_confirmed = (
        regime.get("contraction")
        and not regime.get("active", False)
        and int(regime.get("contraction_confirmations", 0))
        >= int(contraction_confirmations_required)
    )
    if (
        inactive_contraction_confirmed
    ):
        return "实现波动开始收缩，退出波动率仓位"
    adverse_confirmed = (
        int(adverse_signal_confirmations)
        >= int(adverse_signal_confirmations_required)
    )
    if adverse_confirmed and not directional_confirmation and pnl_ratio < 0:
        return (
            f"正股方向连续{int(adverse_signal_confirmations)}轮与持仓表达相反，"
            "提前退出，避免等到期权价格硬止损"
        )
    if (
        adverse_confirmed and current_delta is not None and pnl_ratio < 0
        and abs(float(current_delta)) < float(minimum_effective_delta)
    ):
        return (
            f"期权有效 Delta 已降至 {abs(float(current_delta)):.2f}，低于 "
            f"{float(minimum_effective_delta):.2f}，方向表达失效"
        )
    adverse_context = pnl_ratio < 0 or not directional_confirmation or not regime.get("active", False)
    if (
        entry_iv > 0
        and current_iv <= entry_iv * (1 - iv_decline_ratio)
        and adverse_context
    ):
        return (
            f"隐含波动率较实际入场下降{iv_decline_ratio:.0%}，且价格/方向未确认，"
            "退出波动率仓位"
        )
    if (
        entry_efficiency > 0
        and current_efficiency <= entry_efficiency * (1 - efficiency_decline_ratio)
        and adverse_context
    ):
        return (
            f"Gamma/Theta效率较实际入场下降{efficiency_decline_ratio:.0%}，且价格/方向未确认，"
            "退出波动率仓位"
        )
    return None


def decide_volatility_action(
    *, now: datetime, opened_at: datetime, quantity: int, stages_filled: int,
    max_stages: int, pnl_ratio: float, peak_pnl_ratio: float,
    drawdown_from_peak: float, directional_confirmation: bool,
    signal_active: bool, profit_target: float, stop_loss: float,
    trailing_stop: float, max_hold_hours: float, decay_reason: str | None = None,
    minimum_hold_minutes: float = 30, profit_protection_start: float = .15,
    profit_protection_drawdown: float | None = None,
    profit_giveback_from_peak: float | None = None,
    full_profit_target_enabled: bool = True,
    minimum_locked_profit: float = .0,
    single_contract_take_profit: float = .18,
    tiered_profit_lock_enabled: bool = True,
    time_tighten_minutes: float = 60,
    time_tighten_giveback: float = .12,
    dynamic_stop_enabled: bool = True,
    dynamic_stop_floor: float = .12,
    dynamic_stop_start_minutes: float = 15,
    dynamic_stop_full_decay_minutes: float = 90,
    soft_profit_protection_start: float | None = None,
    soft_profit_lock_floor: float = 0.0,
    soft_profit_giveback: float = .35,
    rapid_giveback_threshold: float = .25,
    rapid_giveback_window_minutes: float = 15,
    peak_reached_at: datetime | None = None,
    weekend_protection_enabled: bool = True,
    weekend_protection_min_profit: float = 0.20,
    weekend_protection_minutes: float = 30.0,
) -> tuple[str, str]:
    """Decide one volatility campaign action without earnings-event semantics."""
    if quantity <= 0:
        if signal_active and directional_confirmation:
            return "BUY", "动态波动与方向确认后首次入场"
        return "HOLD", "动态波动或方向条件尚未确认"
    if pnl_ratio <= -stop_loss:
        return "SELL", "触发波动率策略硬止损"
    _, _, dynamic_stop_reason = volatility_time_decay_stop(
        pnl_ratio=pnl_ratio,
        opened_at=opened_at,
        now=now,
        hard_stop_loss=stop_loss,
        floor_stop_loss=dynamic_stop_floor,
        start_minutes=dynamic_stop_start_minutes,
        full_decay_minutes=dynamic_stop_full_decay_minutes,
        enabled=dynamic_stop_enabled,
    )
    if dynamic_stop_reason:
        return "SELL", dynamic_stop_reason
    if full_profit_target_enabled and not tiered_profit_lock_enabled and pnl_ratio >= profit_target:
        return "SELL", "达到波动率策略止盈目标"
    protection_drawdown = (
        trailing_stop if profit_protection_drawdown is None else profit_protection_drawdown
    )
    protection_active = peak_pnl_ratio >= profit_protection_start
    soft_start = (
        float(soft_profit_protection_start)
        if soft_profit_protection_start is not None
        else max(0.0, float(profit_protection_start) * .80)
    )
    soft_active = peak_pnl_ratio >= soft_start
    profit_giveback = (
        max(0.0, (peak_pnl_ratio - pnl_ratio) / peak_pnl_ratio)
        if profit_giveback_from_peak is None and peak_pnl_ratio > 0
        else max(0.0, float(profit_giveback_from_peak or 0.0))
    )
    held = now - opened_at
    if soft_active and not protection_active:
        rapid = (
            peak_reached_at is not None
            and (now - peak_reached_at).total_seconds() <= max(0.0, rapid_giveback_window_minutes) * 60
            and profit_giveback >= max(0.0, rapid_giveback_threshold)
        )
        if soft_profit_lock_floor > 0 and pnl_ratio <= soft_profit_lock_floor:
            return "SELL", (
                f"软启动锁盈触发：峰值盈利{peak_pnl_ratio:.1%}，当前盈利{pnl_ratio:.1%} "
                f"≤ 软锁盈线{soft_profit_lock_floor:.1%}"
            )
        if rapid:
            return "SELL", (
                f"快速回吐保护触发：峰值盈利{peak_pnl_ratio:.1%}，"
                f"{rapid_giveback_window_minutes:g}分钟内回吐{profit_giveback:.1%}"
            )
        if profit_giveback >= max(0.0, soft_profit_giveback):
            return "SELL", (
                f"软启动回吐保护触发：峰值盈利{peak_pnl_ratio:.1%}，"
                f"盈利回吐{profit_giveback:.1%} ≥ {soft_profit_giveback:.1%}"
            )
    # A single contract cannot be partially realized. Keep it running above
    # the target, but evaluate every lock/giveback rule before deciding HOLD.
    if tiered_profit_lock_enabled and protection_active:
        tier_floor, tier_label = volatility_profit_lock_floor(peak_pnl_ratio)
        effective_floor = max(float(minimum_locked_profit or 0.0), tier_floor)
        if effective_floor > 0 and pnl_ratio <= effective_floor:
            return (
                "SELL",
                f"阶梯锁盈触发：{tier_label}峰值盈利{peak_pnl_ratio:.1%}，"
                f"当前盈利{pnl_ratio:.1%} ≤ 锁盈线{effective_floor:.1%}",
            )
        if (
            held >= timedelta(minutes=max(0.0, float(time_tighten_minutes or 0.0)))
            and peak_pnl_ratio >= .10
            and profit_giveback >= float(time_tighten_giveback or 0.0)
        ):
            return (
                "SELL",
                f"持仓时间收紧止盈：已持有{held.total_seconds() / 60:.0f}分钟，"
                f"峰值盈利{peak_pnl_ratio:.1%}，盈利回吐{profit_giveback:.1%} ≥ "
                f"{float(time_tighten_giveback):.1%}",
            )
    if protection_active and profit_giveback >= protection_drawdown:
        return (
            "SELL",
            f"盈利保护触发：峰值盈利{peak_pnl_ratio:.1%}，"
            f"当前盈利{pnl_ratio:.1%}，"
            f"盈利回吐{profit_giveback:.1%} ≥ {protection_drawdown:.1%}",
        )
    if (
        protection_active
        and minimum_locked_profit > 0
        and pnl_ratio <= minimum_locked_profit
    ):
        return (
            "SELL",
            f"最低锁定盈利触发：峰值盈利{peak_pnl_ratio:.1%}，"
            f"当前盈利{pnl_ratio:.1%} ≤ 锁定线{minimum_locked_profit:.1%}",
        )
    if weekend_protection_enabled:
        weekend_reason = volatility_weekend_carry_exit_reason(
            now=now,
            pnl_ratio=pnl_ratio,
            opened_at=opened_at,
            min_profit_cushion=weekend_protection_min_profit,
            pre_close_minutes=weekend_protection_minutes,
            enabled=True,
        )
        if weekend_reason:
            return "SELL", weekend_reason
    if held >= timedelta(hours=max_hold_hours):
        return "SELL", f"波动率仓位已持有{max_hold_hours:g}小时，触发最长持有退出"
    if decay_reason and held >= timedelta(minutes=minimum_hold_minutes):
        return "SELL", decay_reason
    if quantity == 1 and single_contract_take_profit > 0 and pnl_ratio >= single_contract_take_profit:
        return (
            "HOLD",
            f"单张达到{single_contract_take_profit:.1%}，启动移动止盈；"
            "阶梯锁盈与峰值回撤保护持续生效",
        )
    if (
        stages_filled < max_stages
        and signal_active
        and directional_confirmation
        and pnl_ratio >= .10
    ):
        return "BUY", "动态波动持续确认，仅对盈利持仓金字塔加仓"
    if protection_active:
        return (
            "HOLD",
            f"浮盈{pnl_ratio:.1%}，峰值盈利{peak_pnl_ratio:.1%}，"
            f"盈利回吐{profit_giveback:.1%}未达保护阈值"
            f"{protection_drawdown:.1%}，继续持有",
        )
    if pnl_ratio > 0:
        return (
            "HOLD",
            f"浮盈{pnl_ratio:.1%}，尚未达到盈利保护启动阈值"
            f"{profit_protection_start:.1%}，继续持有",
        )
    return "HOLD", "波动率仓位继续监控，亏损持仓禁止摊低成本"


def sync_volatility_position_cycle(
    state: dict, *, quantity: int, cost: float, last: float, current_iv: float,
    current_efficiency: float, now: datetime,
    allocation_policy: str = "FIFO",
) -> str:
    """Synchronize per-position baselines and never leak them across entry cycles."""
    previous_quantity = int(state.get("previous_position_quantity", 0) or 0)
    if quantity <= 0:
        transition = "CLOSED" if previous_quantity > 0 else "FLAT"
        for key in (
            "position_opened_at", "peak", "entry_iv_actual",
            "entry_gamma_theta_efficiency_actual", "contraction_confirmations",
            "pending_exit_reason", "pending_exit_episode", "selected_exit_tranche_id", "peak_reached_at",
            "partial_exit_tiers", "tranches", "active_batch_id", "initial_risk",
            "adverse_signal_confirmations", "regime_was_active", "signal_active",
        ):
            state.pop(key, None)
        if transition == "CLOSED" and state.get("signal_epoch") is not None:
            state["exit_signal_epoch"] = state.get("signal_epoch")
            state["last_exit_at"] = now.isoformat()
            state["exit_contract_code"] = str(state.get("active_contract_code", ""))
        state["previous_position_quantity"] = 0
        return transition
    if previous_quantity <= 0 or not state.get("position_opened_at"):
        state["position_opened_at"] = now.isoformat()
        state["peak"] = max(float(cost), float(last))
        state["entry_iv_actual"] = float(current_iv)
        state["entry_gamma_theta_efficiency_actual"] = float(current_efficiency)
        state["contraction_confirmations"] = 0
        state["partial_exit_tiers"] = []
        state["tranches"] = [{
            "tranche_id": f"entry-{now.strftime('%Y%m%d%H%M%S')}",
            "entry_time": now.isoformat(), "entry_price": float(cost),
            "stop_price": float(cost) * .65 if cost > 0 else 0.0,
            "quantity": int(quantity), "peak_mark": float(last),
            "mae_pnl_ratio": min(0.0, float(last) / float(cost) - 1) if cost > 0 else 0.0,
            "mfe_pnl_ratio": max(0.0, float(last) / float(cost) - 1) if cost > 0 else 0.0,
        }]
        transition = "OPENED"
    else:
        state["peak"] = max(float(state.get("peak", cost)), float(last))
        tranches = list(state.get("tranches") or [])
        if quantity > previous_quantity:
            added = quantity - previous_quantity
            tranches.append({
                "tranche_id": f"add-{now.strftime('%Y%m%d%H%M%S')}",
                "entry_time": now.isoformat(), "entry_price": float(cost),
                "stop_price": float(cost) * .65 if cost > 0 else 0.0,
                "quantity": int(added), "peak_mark": float(last),
                "mae_pnl_ratio": 0.0, "mfe_pnl_ratio": 0.0,
            })
        elif quantity < previous_quantity:
            remaining = quantity
            ordered = list(range(len(tranches)))
            policy = str(allocation_policy or "FIFO").upper()
            if policy == "LIFO":
                ordered.reverse()
            elif policy == "RISK_PRIORITY":
                ordered.sort(key=lambda i: float(tranches[i].get("initial_risk", 0) or 0), reverse=True)
            for index in ordered:
                tranche = tranches[index]
                used = min(int(tranche.get("quantity", 0)), remaining)
                tranche["quantity"] = used
                remaining -= used
            tranches = [item for item in tranches if int(item.get("quantity", 0)) > 0]
        state["tranches"] = tranches
        transition = "ADDED" if quantity > previous_quantity else "ACTIVE"
    state["previous_position_quantity"] = int(quantity)
    if quantity > 0:
        denominator = float(cost) * max(1, int(quantity))
        state.setdefault("initial_risk", denominator * .35 if denominator > 0 else 0.0)
        current_pnl = float(last) / float(cost) - 1 if cost > 0 else 0.0
        state["mae_pnl_ratio"] = min(float(state.get("mae_pnl_ratio", 0.0)), current_pnl)
        state["mfe_pnl_ratio"] = max(float(state.get("mfe_pnl_ratio", current_pnl)), current_pnl)
    return transition
