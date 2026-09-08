from __future__ import annotations

from dataclasses import dataclass, field
import math

import pandas as pd


@dataclass(frozen=True)
class CryptoSignalProfile:
    fast_window: int
    slow_window: int
    breakout_window: int
    volatility_expansion: float
    zscore_target: float
    min_volume_ratio: float
    regime_filter_enabled: bool = True
    trend_window: int = 120
    min_trend_strength: float = 0.005
    max_chase_atr: float = 8.0
    confirmation_bars: int = 2
    breakout_enabled: bool = True
    pullback_enabled: bool = False
    pullback_depth: float = 0.04
    pullback_max_rsi: float = 65.0
    pullback_max_bollinger: float = 0.80
    pullback_min_trend_return: float = 0.02
    pullback_min_last_return: float = 0.003
    trend_participation_enabled: bool = False
    trend_participation_min_24h_return: float = 0.025
    trend_participation_min_72h_return: float = 0.030
    trend_participation_max_rsi: float = 78.0
    trend_participation_max_bollinger: float = 1.10
    trend_participation_min_volume_ratio: float = 0.60


@dataclass(frozen=True)
class CryptoSignalState:
    direction: str
    active: bool
    strength: str
    score: float
    reason: str
    metrics: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)


def _check(name: str, current, target: str, passed: bool) -> dict:
    return {"name": name, "current": current, "target": target, "passed": bool(passed)}


def build_crypto_signal(frame: pd.DataFrame, profile: CryptoSignalProfile) -> CryptoSignalState:
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    high = frame.get("high", close).astype(float)
    low_series = frame.get("low", close).astype(float)
    returns = close.pct_change().dropna()
    annualizer = math.sqrt(24 * 365)
    rv_fast = float((returns.tail(profile.fast_window).pow(2).mean() ** .5) * annualizer)
    rv_slow = float((returns.tail(profile.slow_window).pow(2).mean() ** .5) * annualizer)
    expansion = rv_fast / rv_slow if rv_slow > 0 else 0.0
    absolute = returns.abs()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, math.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if not math.isfinite(rsi):
        rsi = 50.0
    bollinger_window = min(20, len(close))
    bollinger_mid = close.rolling(bollinger_window).mean()
    bollinger_std = close.rolling(bollinger_window).std(ddof=0)
    bollinger_upper = float((bollinger_mid + 2 * bollinger_std).iloc[-1])
    bollinger_lower = float((bollinger_mid - 2 * bollinger_std).iloc[-1])
    bollinger_position = (
        (float(close.iloc[-1]) - bollinger_lower) / (bollinger_upper - bollinger_lower)
        if bollinger_upper > bollinger_lower else 0.5
    )
    baseline = absolute.tail(profile.slow_window)
    std = float(baseline.std(ddof=0))
    zscore_abs = (float(absolute.iloc[-1]) - float(baseline.mean())) / std if std > 0 else 0.0
    signed_recent = returns.tail(profile.slow_window)
    signed_std = float(signed_recent.std(ddof=0))
    zscore_signed = (
        float((returns.iloc[-1] - signed_recent.mean()) / signed_std)
        if signed_std > 0 else 0.0
    )
    ema_fast = float(close.ewm(span=12, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=min(48, profile.slow_window), adjust=False).mean().iloc[-1])
    trend_span = min(max(profile.trend_window, profile.slow_window), max(len(close), 2))
    ema_trend = float(close.ewm(span=trend_span, adjust=False).mean().iloc[-1])
    trend_window = min(profile.trend_window, max(len(close) - 1, 2))
    trend_return = float(close.iloc[-1] / close.iloc[-trend_window] - 1)
    momentum_24h = float(close.iloc[-1] / close.iloc[-min(24, len(close) - 1)] - 1)
    momentum_72h = float(close.iloc[-1] / close.iloc[-min(72, len(close) - 1)] - 1)
    regime = "RANGE"
    if (
        ema_fast > ema_slow
        and trend_return >= profile.min_trend_strength
        and (ema_slow >= ema_trend or abs(zscore_signed) >= profile.zscore_target)
    ):
        regime = "TREND_UP"
    elif (
        ema_fast < ema_slow
        and trend_return <= -profile.min_trend_strength
        and (ema_slow <= ema_trend or abs(zscore_signed) >= profile.zscore_target)
    ):
        regime = "TREND_DOWN"
    elif expansion >= profile.volatility_expansion * 1.5 or abs(zscore_signed) >= profile.zscore_target * 1.5:
        regime = "HIGH_VOL_RANGE"
    true_range = pd.concat(
        [
            (high - low_series).abs(),
            (high - close.shift()).abs(),
            (low_series - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(true_range.tail(profile.breakout_window).mean())
    atr_ratio = atr / float(close.iloc[-1]) if close.iloc[-1] else 0.0
    ema_distance_atr = abs(float(close.iloc[-1]) - ema_slow) / atr if atr > 0 else 0.0
    prior = close.iloc[-profile.breakout_window - 1:-1]
    low, high, last = float(prior.min()), float(prior.max()), float(close.iloc[-1])
    breakout_up = (last - low) / (high - low) if high > low else 0.5
    breakout_down = (high - last) / (high - low) if high > low else 0.5
    confirm_bars = max(1, int(profile.confirmation_bars))
    recent = close.tail(confirm_bars)
    breakout_up_confirmed = bool((recent >= high).sum() >= min(confirm_bars, len(recent)))
    breakout_down_confirmed = bool((recent <= low).sum() >= min(confirm_bars, len(recent)))
    volume_mean = float(volume.iloc[-profile.breakout_window - 1:-1].mean())
    volume_ratio = float(volume.iloc[-1]) / volume_mean if volume_mean > 0 else 0.0
    volatility_ok = expansion >= profile.volatility_expansion or abs(zscore_signed) >= profile.zscore_target or zscore_abs >= profile.zscore_target
    regime_long_ok = (not profile.regime_filter_enabled) or regime == "TREND_UP"
    regime_short_ok = (not profile.regime_filter_enabled) or regime == "TREND_DOWN"
    chase_ok = ema_distance_atr <= profile.max_chase_atr
    long_breakout_ok = profile.breakout_enabled and breakout_up >= .80 and breakout_up_confirmed
    short_breakout_ok = profile.breakout_enabled and breakout_down >= .80 and breakout_down_confirmed
    pullback_ok = (
        profile.pullback_enabled
        and regime == "TREND_UP"
        and trend_return >= profile.pullback_min_trend_return
        and abs(last / ema_fast - 1) <= profile.pullback_depth
        and returns.iloc[-1] >= profile.pullback_min_last_return
        and last >= ema_fast * (1 - profile.pullback_depth * 0.5)
        and volume_ratio >= max(0.5, profile.min_volume_ratio * 0.6)
        and rsi <= profile.pullback_max_rsi
        and bollinger_position <= profile.pullback_max_bollinger
    )
    trend_participation_ok = (
        profile.trend_participation_enabled
        and regime == "TREND_UP"
        and ema_fast > ema_slow >= ema_trend
        and trend_return >= profile.min_trend_strength
        and momentum_24h >= profile.trend_participation_min_24h_return
        and momentum_72h >= profile.trend_participation_min_72h_return
        and last >= ema_fast
        and chase_ok
        and volume_ratio >= profile.trend_participation_min_volume_ratio
        and rsi <= profile.trend_participation_max_rsi
        and bollinger_position <= profile.trend_participation_max_bollinger
    )
    long_ok = (
        volatility_ok and regime_long_ok and chase_ok
        and ema_fast > ema_slow and volume_ratio >= profile.min_volume_ratio
        and long_breakout_ok
    ) or pullback_ok or trend_participation_ok
    short_ok = (
        volatility_ok and regime_short_ok and chase_ok
        and ema_fast < ema_slow and short_breakout_ok
        and volume_ratio >= profile.min_volume_ratio
    )
    direction = "LONG" if long_ok else "SHORT" if short_ok else "NONE"
    entry_source = (
        "趋势参与" if trend_participation_ok
        else "回调" if pullback_ok
        else "突破" if long_breakout_ok or short_breakout_ok
        else "无"
    )
    breakout = breakout_up if direction != "SHORT" else breakout_down
    score = (
        min(30.0, expansion * 15)
        + min(25.0, abs(zscore_signed) * 10)
        + min(20.0, volume_ratio * 8)
        + min(15.0, breakout * 15)
        + (10.0 if direction != "NONE" else 0.0)
    )
    checks = [
        _check(
            "波动激活",
            f"{expansion:.2f} / {zscore_signed:.2f}",
            f"扩张≥{profile.volatility_expansion:.2f} 或 |Z|≥{profile.zscore_target:.2f}",
            volatility_ok,
        ),
        _check(
            "方向与入场形态",
            f"EMA差 {ema_fast / ema_slow - 1:+.2%}；上破 {breakout_up:.0%}；下破 {breakout_down:.0%}",
            "做多：突破 / 回调 / 趋势参与；做空：趋势向下且确认跌破",
            (long_ok or short_ok),
        ),
        _check("成交量确认", f"{volume_ratio:.2f}", f"≥ {profile.min_volume_ratio:.2f}", volume_ratio >= profile.min_volume_ratio),
        _check(
            "市场状态",
            f"{regime}；趋势 {trend_return:+.2%}",
            f"趋势强度≥{profile.min_trend_strength:.2%}",
            regime in {"TREND_UP", "TREND_DOWN"} or not profile.regime_filter_enabled,
        ),
        _check(
            "反追价过滤",
            f"{ema_distance_atr:.2f} ATR",
            f"≤ {profile.max_chase_atr:.2f} ATR",
            chase_ok,
        ),
        _check(
            "突破确认",
            f"上破确认 {breakout_up_confirmed}；下破确认 {breakout_down_confirmed}",
            f"连续 {confirm_bars} 根确认",
            breakout_up_confirmed or breakout_down_confirmed or pullback_ok,
        ),
        _check(
            "回调质量",
            (
                f"趋势 {trend_return:+.2%}；单K {returns.iloc[-1]:+.2%}；"
                f"RSI {rsi:.1f}；布林位置 {bollinger_position:.0%}"
            ),
            (
                f"趋势≥{profile.pullback_min_trend_return:.2%}；"
                f"单K≥{profile.pullback_min_last_return:.2%}；"
                f"RSI≤{profile.pullback_max_rsi:.0f}；"
                f"布林≤{profile.pullback_max_bollinger:.0%}"
            ),
            (
                trend_return >= profile.pullback_min_trend_return
                and returns.iloc[-1] >= profile.pullback_min_last_return
                and rsi <= profile.pullback_max_rsi
                and bollinger_position <= profile.pullback_max_bollinger
            ),
        ),
        _check(
            "趋势参与",
            (
                f"24h {momentum_24h:+.2%}；72h {momentum_72h:+.2%}；"
                f"RSI {rsi:.1f}；量比 {volume_ratio:.2f}"
            ),
            (
                f"24h≥{profile.trend_participation_min_24h_return:.2%}；"
                f"72h≥{profile.trend_participation_min_72h_return:.2%}；"
                f"RSI≤{profile.trend_participation_max_rsi:.0f}"
            ),
            trend_participation_ok,
        ),
    ]
    metrics = {
        "rv_fast": rv_fast, "rv_slow": rv_slow,
        "volatility_expansion": expansion, "zscore": zscore_signed,
        "zscore_abs": zscore_abs, "breakout": breakout,
        "breakout_up": breakout_up, "breakout_down": breakout_down,
        "volume_ratio": volume_ratio, "score": round(score, 2),
        "regime": regime, "trend_return": trend_return,
        "momentum_24h": momentum_24h,
        "momentum_72h": momentum_72h,
        "atr_ratio": atr_ratio, "ema_distance_atr": ema_distance_atr,
        "breakout_up_confirmed": breakout_up_confirmed,
        "breakout_down_confirmed": breakout_down_confirmed,
        "pullback_ok": pullback_ok,
        "trend_participation_ok": trend_participation_ok,
        "entry_source": entry_source,
        "pullback_min_trend_return": profile.pullback_min_trend_return,
        "pullback_min_last_return": profile.pullback_min_last_return,
        "rsi": rsi, "bollinger_position": bollinger_position,
        "last": last,
    }
    if direction == "NONE":
        failed = "、".join(item["name"] for item in checks if not item["passed"])
        return CryptoSignalState(direction, False, "观察", round(score, 2), f"入场条件未满足：{failed}", metrics, checks)
    strength = "强信号" if score >= 80 else "标准"
    return CryptoSignalState(direction, True, strength, round(score, 2), f"{strength}：波动、方向、突破和量能确认", metrics, checks)
