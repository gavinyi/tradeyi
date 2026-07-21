from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import sqrt
from zoneinfo import ZoneInfo

import pandas as pd

from .gamma_risk import market_number, normalize_iv, realized_volatility


def volatility_cooldown_hours(reason: str, configured_hours: float = 24) -> float:
    if "止盈" in reason:
        return min(configured_hours, 8.0)
    if any(marker in reason for marker in ("波动开始收缩", "隐含波动率", "Gamma/Theta")):
        return min(configured_hours, 6.0)
    return configured_hours


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
    candidates: pd.DataFrame, spot: float, rv: float, iv_percentile: float = 0.5,
    as_of: date | None = None,
    *, signal_active: bool = False, strong_signal: bool = False,
    max_premium_per_stage: float | None = None, contracts_per_stage: int = 1,
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
        result["adaptive_risk_tier"] = pd.Series(dtype=str)
        return result
    today = as_of or date.today()
    result = candidates.copy()
    if strong_signal:
        iv_rv_limit, risk_tier = 2.0, "强波动放宽"
    elif signal_active:
        iv_rv_limit, risk_tier = 1.8, "动态波动放宽"
    else:
        iv_rv_limit, risk_tier = 1.5, "常规"
    scores, eligible, reasons, dtes, iv_rv_values = [], [], [], [], []
    premiums, expected_return_flags, premium_flags = [], [], []
    for _, row in result.iterrows():
        expiry = pd.to_datetime(row.get("strike_time"), errors="coerce")
        dte = max(0, (expiry.date() - today).days) if not pd.isna(expiry) else 0
        delta = abs(market_number(row.get("option_delta")))
        spread = market_number(row.get("spread_pct"), 100)
        oi = market_number(row.get("option_open_interest"))
        volume = market_number(row.get("volume"))
        iv = normalize_iv(row.get("option_implied_volatility", 0))
        iv_rv = iv / rv if rv > 0 else 99.0
        expected_return = market_number(row.get("model_expected_return"), -99.0)
        expected_return_ok = expected_return > 0
        multiplier = market_number(row.get("option_contract_multiplier"), 100.0)
        ask = market_number(row.get("ask_price"), market_number(row.get("mid_price")))
        premium = ask * multiplier * max(1, int(contracts_per_stage))
        premium_ok = (
            max_premium_per_stage is None
            or (ask > 0 and premium <= float(max_premium_per_stage))
        )
        max_spread = 12.0 if dte == 0 else 8.0
        liquid = spread <= max_spread and oi >= 500 and volume >= 1
        cost_ok = iv_rv <= iv_rv_limit and iv_percentile <= .75
        delta_ok = .30 <= delta <= .80
        is_eligible = bool(
            liquid and cost_ok and delta_ok and expected_return_ok and premium_ok
        )
        delta_score = max(0.0, 30 - abs(delta - .525) * 100)
        dte_score = 25 if 7 <= dte <= 21 else 15 if 2 <= dte <= 30 else 5
        liquidity_score = max(0.0, 20 - spread) + min(10.0, oi / 500)
        gamma = abs(market_number(row.get("option_gamma")))
        theta = abs(market_number(row.get("option_theta")))
        efficiency_score = min(15.0, (gamma / theta if theta > 0 else 0) * 50)
        cost_score = max(0.0, 15 * (1.5 - min(1.5, iv_rv)) / 1.5)
        score = delta_score + dte_score + liquidity_score + efficiency_score + cost_score
        reason_parts = []
        if spread > max_spread:
            reason_parts.append(f"价差 {spread:.1f}% 超过 {max_spread:.0f}%")
        if oi < 500:
            reason_parts.append(f"未平仓量 {oi:.0f} 低于 500")
        if volume < 1:
            reason_parts.append("当日无成交量")
        if iv_rv > iv_rv_limit:
            reason_parts.append(
                f"IV/RV {iv_rv:.2f} 超过{risk_tier}门槛 {iv_rv_limit:.2f}"
            )
        if iv_percentile > .75:
            reason_parts.append(f"IV百分位 {iv_percentile:.0%} 超过 75%")
        if not delta_ok:
            reason_parts.append(f"Delta {delta:.2f} 不在 0.30–0.80")
        if not expected_return_ok:
            reason_parts.append(f"模型预期收益 {expected_return:.2%} 不大于 0")
        if not premium_ok:
            reason_parts.append(
                f"本批权利金 ${premium:,.0f} 超过预算 ${float(max_premium_per_stage):,.0f}"
            )
        scores.append(score)
        eligible.append(is_eligible)
        reasons.append("；".join(reason_parts) or "全部条件通过")
        dtes.append(dte)
        iv_rv_values.append(iv_rv)
        premiums.append(premium)
        expected_return_flags.append(expected_return_ok)
        premium_flags.append(premium_ok)
    result["volatility_quality_score"] = scores
    result["volatility_eligible"] = eligible
    result["volatility_filter_reason"] = reasons
    result["days_to_expiry"] = dtes
    result["iv_rv"] = iv_rv_values
    result["iv_rv_limit"] = iv_rv_limit
    result["premium_per_stage"] = premiums
    result["expected_return_ok"] = expected_return_flags
    result["premium_budget_ok"] = premium_flags
    result["adaptive_risk_tier"] = risk_tier
    sort_columns = ["volatility_eligible", "volatility_quality_score"]
    if "model_expected_return" in result:
        sort_columns.append("model_expected_return")
    return result.sort_values(
        sort_columns, ascending=[False] * len(sort_columns),
    ).reset_index(drop=True)


def volatility_exit_reason(
    regime: dict, current_iv: float, entry_iv: float, current_efficiency: float,
    entry_efficiency: float, iv_decline_ratio: float = .20,
    efficiency_decline_ratio: float = .40,
    contraction_confirmations_required: int = 3,
) -> str | None:
    if (
        regime.get("contraction")
        and not regime.get("active", False)
        and int(regime.get("contraction_confirmations", 0))
        >= int(contraction_confirmations_required)
    ):
        return "实现波动开始收缩，退出波动率仓位"
    if entry_iv > 0 and current_iv <= entry_iv * (1 - iv_decline_ratio):
        return f"隐含波动率较实际入场下降{iv_decline_ratio:.0%}，退出波动率仓位"
    if (
        entry_efficiency > 0
        and current_efficiency <= entry_efficiency * (1 - efficiency_decline_ratio)
    ):
        return (
            f"Gamma/Theta效率较实际入场下降{efficiency_decline_ratio:.0%}，"
            "退出波动率仓位"
        )
    return None


def decide_volatility_action(
    *, now: datetime, opened_at: datetime, quantity: int, stages_filled: int,
    max_stages: int, pnl_ratio: float, peak_pnl_ratio: float,
    drawdown_from_peak: float, directional_confirmation: bool,
    signal_active: bool, profit_target: float, stop_loss: float,
    trailing_stop: float, max_hold_hours: float, decay_reason: str | None = None,
    minimum_hold_minutes: float = 30,
) -> tuple[str, str]:
    """Decide one volatility campaign action without earnings-event semantics."""
    if quantity <= 0:
        if signal_active and directional_confirmation:
            return "BUY", "动态波动与方向确认后首次入场"
        return "HOLD", "动态波动或方向条件尚未确认"
    if pnl_ratio <= -stop_loss:
        return "SELL", "触发波动率策略硬止损"
    if pnl_ratio >= profit_target:
        return "SELL", "达到波动率策略止盈目标"
    if peak_pnl_ratio > 0 and drawdown_from_peak >= trailing_stop:
        return "SELL", "触发波动率策略盈利峰值回撤退出"
    held = now - opened_at
    if held >= timedelta(hours=max_hold_hours):
        return "SELL", f"波动率仓位已持有{max_hold_hours:g}小时，触发最长持有退出"
    if decay_reason and held >= timedelta(minutes=minimum_hold_minutes):
        return "SELL", decay_reason
    if (
        stages_filled < max_stages
        and signal_active
        and directional_confirmation
        and pnl_ratio >= .10
    ):
        return "BUY", "动态波动持续确认，仅对盈利持仓金字塔加仓"
    return "HOLD", "波动率仓位继续监控，亏损持仓禁止摊低成本"


def sync_volatility_position_cycle(
    state: dict, *, quantity: int, cost: float, last: float, current_iv: float,
    current_efficiency: float, now: datetime,
) -> str:
    """Synchronize per-position baselines and never leak them across entry cycles."""
    previous_quantity = int(state.get("previous_position_quantity", 0) or 0)
    if quantity <= 0:
        transition = "CLOSED" if previous_quantity > 0 else "FLAT"
        for key in (
            "position_opened_at", "peak", "entry_iv_actual",
            "entry_gamma_theta_efficiency_actual", "contraction_confirmations",
            "pending_exit_reason",
        ):
            state.pop(key, None)
        state["previous_position_quantity"] = 0
        return transition
    if previous_quantity <= 0 or not state.get("position_opened_at"):
        state["position_opened_at"] = now.isoformat()
        state["peak"] = max(float(cost), float(last))
        state["entry_iv_actual"] = float(current_iv)
        state["entry_gamma_theta_efficiency_actual"] = float(current_efficiency)
        state["contraction_confirmations"] = 0
        transition = "OPENED"
    else:
        state["peak"] = max(float(state.get("peak", cost)), float(last))
        transition = "ACTIVE"
    state["previous_position_quantity"] = int(quantity)
    return transition
