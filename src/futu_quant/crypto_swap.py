from __future__ import annotations

from dataclasses import dataclass, field
import math
import os

import pandas as pd

from .crypto_signal import CryptoSignalProfile, build_crypto_signal


@dataclass(frozen=True)
class CryptoSwapConfig:
    entry_enabled: bool = False
    trend_breakout_enabled: bool = True
    mean_reversion_enabled: bool = False
    fast_window: int = 12
    slow_window: int = 72
    breakout_window: int = 24
    volatility_expansion: float = 1.35
    zscore_target: float = 2.0
    min_volume_ratio: float = 0.0
    margin_budget: float = 500.0
    leverage: float = 2.0
    hard_stop_loss: float = 0.05
    long_hard_stop_loss: float = 0.05
    short_hard_stop_loss: float = 0.04
    profit_protection_start: float = 0.10
    long_profit_protection_start: float = 0.10
    short_profit_protection_start: float = 0.08
    profit_giveback: float = 0.30
    long_profit_giveback: float = 0.30
    short_profit_giveback: float = 0.25
    partial_profit_tier1: float = 0.08
    partial_profit_tier1_fraction: float = 0.50
    partial_profit_tier2: float = 0.16
    partial_profit_tier2_fraction: float = 0.25
    min_hold_hours: float = 1.0
    max_hold_hours: float = 72.0
    long_max_hold_hours: float = 72.0
    short_max_hold_hours: float = 48.0
    contraction_ratio: float = 0.75
    contraction_confirmations: int = 3
    funding_exit_rate: float = 0.0015
    emergency_reverse_loss: float = 0.02
    liquidation_buffer_exit: float = 0.15
    regime_filter_enabled: bool = True
    trend_window: int = 120
    min_trend_strength: float = 0.005
    max_chase_atr: float = 8.0
    confirmation_bars: int = 1
    continuation_bars: int = 2
    continuation_min_return: float = 0.005
    long_min_volume_ratio: float = 1.10
    long_min_trend_strength: float = 0.020
    short_min_volume_ratio: float = 1.10
    short_min_trend_strength: float = 0.010
    short_continuation_min_return: float = 0.005
    higher_timeframe_enabled: bool = True
    higher_timeframe_bar: str = "4H"
    higher_timeframe_min_bars: int = 80
    higher_timeframe_min_trend_return: float = 0.0
    mean_reversion_zscore: float = 2.8
    mean_reversion_rsi_overbought: float = 78.0
    mean_reversion_rsi_oversold: float = 22.0
    mean_reversion_bollinger_upper: float = 1.05
    mean_reversion_bollinger_lower: float = -0.05
    mean_reversion_min_volume_ratio: float = 0.80
    mean_reversion_position_scale: float = 0.35
    weak_momentum_exit_enabled: bool = True
    weak_momentum_exit_hours: float = 6.0
    weak_momentum_exit_max_pnl: float = 0.0
    weak_momentum_exit_min_peak_pnl: float = 0.006


@dataclass(frozen=True)
class CryptoSwapDecision:
    action: str
    reason: str
    target_notional: float = 0.0
    metrics: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    quantity_fraction: float = 1.0


def crypto_swap_config_from_env() -> CryptoSwapConfig:
    config = CryptoSwapConfig(
        entry_enabled=os.getenv("CRYPTO_SWAP_ENTRY_ENABLED", "false").lower() == "true",
        trend_breakout_enabled=os.getenv(
            "CRYPTO_SWAP_TREND_BREAKOUT_ENABLED", "true"
        ).lower() == "true",
        mean_reversion_enabled=os.getenv(
            "CRYPTO_SWAP_MEAN_REVERSION_ENABLED", "false"
        ).lower() == "true",
        fast_window=int(os.getenv("CRYPTO_SWAP_FAST_WINDOW", "12")),
        slow_window=int(os.getenv("CRYPTO_SWAP_SLOW_WINDOW", "72")),
        breakout_window=int(os.getenv("CRYPTO_SWAP_BREAKOUT_WINDOW", "24")),
        volatility_expansion=float(os.getenv("CRYPTO_SWAP_VOL_EXPANSION", "1.35")),
        zscore_target=float(os.getenv("CRYPTO_SWAP_ZSCORE", "2.0")),
        min_volume_ratio=float(os.getenv("CRYPTO_SWAP_MIN_VOLUME_RATIO", "0")),
        margin_budget=float(os.getenv("CRYPTO_SWAP_MARGIN_BUDGET", "500")),
        leverage=float(os.getenv("CRYPTO_SWAP_LEVERAGE", "2")),
        hard_stop_loss=float(os.getenv("CRYPTO_SWAP_HARD_STOP", "0.05")),
        long_hard_stop_loss=float(os.getenv("CRYPTO_SWAP_LONG_HARD_STOP", "0.05")),
        short_hard_stop_loss=float(os.getenv("CRYPTO_SWAP_SHORT_HARD_STOP", "0.04")),
        profit_protection_start=float(
            os.getenv("CRYPTO_SWAP_PROFIT_PROTECTION_START", "0.10")
        ),
        long_profit_protection_start=float(
            os.getenv("CRYPTO_SWAP_LONG_PROFIT_PROTECTION_START", "0.10")
        ),
        short_profit_protection_start=float(
            os.getenv("CRYPTO_SWAP_SHORT_PROFIT_PROTECTION_START", "0.08")
        ),
        profit_giveback=float(os.getenv("CRYPTO_SWAP_PROFIT_GIVEBACK", "0.30")),
        long_profit_giveback=float(os.getenv("CRYPTO_SWAP_LONG_PROFIT_GIVEBACK", "0.30")),
        short_profit_giveback=float(os.getenv("CRYPTO_SWAP_SHORT_PROFIT_GIVEBACK", "0.25")),
        partial_profit_tier1=float(os.getenv("CRYPTO_SWAP_PARTIAL_PROFIT_TIER1", "0.08")),
        partial_profit_tier1_fraction=float(os.getenv("CRYPTO_SWAP_PARTIAL_PROFIT_TIER1_FRACTION", "0.50")),
        partial_profit_tier2=float(os.getenv("CRYPTO_SWAP_PARTIAL_PROFIT_TIER2", "0.16")),
        partial_profit_tier2_fraction=float(os.getenv("CRYPTO_SWAP_PARTIAL_PROFIT_TIER2_FRACTION", "0.25")),
        min_hold_hours=float(os.getenv("CRYPTO_SWAP_MIN_HOLD_HOURS", "1")),
        max_hold_hours=float(os.getenv("CRYPTO_SWAP_MAX_HOLD_HOURS", "72")),
        long_max_hold_hours=float(os.getenv("CRYPTO_SWAP_LONG_MAX_HOLD_HOURS", "72")),
        short_max_hold_hours=float(os.getenv("CRYPTO_SWAP_SHORT_MAX_HOLD_HOURS", "48")),
        contraction_ratio=float(os.getenv("CRYPTO_SWAP_CONTRACTION_RATIO", "0.75")),
        contraction_confirmations=int(os.getenv("CRYPTO_SWAP_CONTRACTION_CONFIRMATIONS", "3")),
        funding_exit_rate=float(os.getenv("CRYPTO_SWAP_FUNDING_EXIT_RATE", "0.0015")),
        emergency_reverse_loss=float(os.getenv("CRYPTO_SWAP_EMERGENCY_REVERSE_LOSS", "0.02")),
        liquidation_buffer_exit=float(os.getenv("CRYPTO_SWAP_LIQUIDATION_BUFFER_EXIT", "0.15")),
        regime_filter_enabled=os.getenv("CRYPTO_SWAP_REGIME_FILTER_ENABLED", "true").lower() == "true",
        trend_window=int(os.getenv("CRYPTO_SWAP_TREND_WINDOW", "120")),
        min_trend_strength=float(os.getenv("CRYPTO_SWAP_MIN_TREND_STRENGTH", "0.005")),
        max_chase_atr=float(os.getenv("CRYPTO_SWAP_MAX_CHASE_ATR", "8.0")),
        confirmation_bars=int(os.getenv("CRYPTO_SWAP_CONFIRMATION_BARS", "1")),
        continuation_bars=int(os.getenv("CRYPTO_SWAP_CONTINUATION_BARS", "2")),
        continuation_min_return=float(
            os.getenv("CRYPTO_SWAP_CONTINUATION_MIN_RETURN", "0.005")
        ),
        long_min_volume_ratio=float(
            os.getenv("CRYPTO_SWAP_LONG_MIN_VOLUME_RATIO", "1.10")
        ),
        long_min_trend_strength=float(
            os.getenv("CRYPTO_SWAP_LONG_MIN_TREND_STRENGTH", "0.020")
        ),
        short_min_volume_ratio=float(
            os.getenv("CRYPTO_SWAP_SHORT_MIN_VOLUME_RATIO", "1.10")
        ),
        short_min_trend_strength=float(
            os.getenv("CRYPTO_SWAP_SHORT_MIN_TREND_STRENGTH", "0.010")
        ),
        short_continuation_min_return=float(
            os.getenv("CRYPTO_SWAP_SHORT_CONTINUATION_MIN_RETURN", "0.005")
        ),
        higher_timeframe_enabled=os.getenv(
            "CRYPTO_SWAP_HIGHER_TIMEFRAME_ENABLED", "true"
        ).lower() == "true",
        higher_timeframe_bar=os.getenv("CRYPTO_SWAP_HIGHER_TIMEFRAME_BAR", "4H"),
        higher_timeframe_min_bars=int(
            os.getenv("CRYPTO_SWAP_HIGHER_TIMEFRAME_MIN_BARS", "80")
        ),
        higher_timeframe_min_trend_return=float(
            os.getenv("CRYPTO_SWAP_HIGHER_TIMEFRAME_MIN_TREND_RETURN", "0")
        ),
        mean_reversion_zscore=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_ZSCORE", "2.8")
        ),
        mean_reversion_rsi_overbought=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_RSI_OVERBOUGHT", "78")
        ),
        mean_reversion_rsi_oversold=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_RSI_OVERSOLD", "22")
        ),
        mean_reversion_bollinger_upper=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_BOLLINGER_UPPER", "1.05")
        ),
        mean_reversion_bollinger_lower=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_BOLLINGER_LOWER", "-0.05")
        ),
        mean_reversion_min_volume_ratio=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_MIN_VOLUME_RATIO", "0.80")
        ),
        mean_reversion_position_scale=float(
            os.getenv("CRYPTO_SWAP_MEAN_REVERSION_POSITION_SCALE", "0.35")
        ),
        weak_momentum_exit_enabled=os.getenv(
            "CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_ENABLED", "true"
        ).lower() == "true",
        weak_momentum_exit_hours=float(
            os.getenv("CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_HOURS", "6")
        ),
        weak_momentum_exit_max_pnl=float(
            os.getenv("CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_MAX_PNL", "0")
        ),
        weak_momentum_exit_min_peak_pnl=float(
            os.getenv("CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL", "0.006")
        ),
    )
    if not 6 <= config.fast_window < config.slow_window <= 500:
        raise ValueError("合约策略快窗口必须小于慢窗口")
    if not 1 <= config.leverage <= 3:
        raise ValueError("安全锁：合约杠杆必须在 1 到 3 倍之间")
    if config.margin_budget <= 0 or not 0 < config.hard_stop_loss <= .25:
        raise ValueError("合约保证金预算或止损配置无效")
    if not 0 < config.long_hard_stop_loss <= .25:
        raise ValueError("合约做多硬止损配置无效")
    if not 0 < config.short_hard_stop_loss <= .25:
        raise ValueError("合约做空硬止损配置无效")
    if not 0 < config.partial_profit_tier1 < config.partial_profit_tier2:
        raise ValueError("合约分批止盈第二档必须高于第一档")
    if config.min_hold_hours < 0 or config.max_hold_hours <= config.min_hold_hours:
        raise ValueError("合约最短/最长持有时间配置无效")
    if not 0 < config.emergency_reverse_loss <= config.hard_stop_loss:
        raise ValueError("紧急反向退出亏损阈值必须大于 0 且不高于硬止损")
    if not 0 < config.liquidation_buffer_exit < 1:
        raise ValueError("强平缓冲退出阈值必须在 0% 到 100% 之间")
    if not 24 <= config.trend_window <= 1000:
        raise ValueError("合约趋势窗口必须在 24 到 1000 之间")
    if not 0 <= config.min_trend_strength <= 1:
        raise ValueError("合约趋势强度门槛必须在 0% 到 100% 之间")
    if not 1 <= config.max_chase_atr <= 50:
        raise ValueError("合约反追价 ATR 倍数必须在 1 到 50 之间")
    if not 1 <= config.confirmation_bars <= 6:
        raise ValueError("合约突破确认根数必须在 1 到 6 之间")
    if not 1 <= config.continuation_bars <= 12:
        raise ValueError("合约趋势延续确认根数必须在 1 到 12 之间")
    if not 0 <= config.continuation_min_return <= 0.20:
        raise ValueError("合约趋势延续最小收益必须在 0% 到 20% 之间")
    if not 0 <= config.long_min_volume_ratio <= 20:
        raise ValueError("合约做多最低量比必须在 0 到 20 之间")
    if not 0 <= config.long_min_trend_strength <= 1:
        raise ValueError("合约做多趋势强度必须在 0% 到 100% 之间")
    if not 0 <= config.short_min_volume_ratio <= 20:
        raise ValueError("合约做空最低量比必须在 0 到 20 之间")
    if not 0 <= config.short_min_trend_strength <= 1:
        raise ValueError("合约做空趋势强度必须在 0% 到 100% 之间")
    if not 0 <= config.short_continuation_min_return <= 0.20:
        raise ValueError("合约做空延续收益必须在 0% 到 20% 之间")
    if config.higher_timeframe_min_bars < 24:
        raise ValueError("合约高周期确认最少K线必须不少于 24 根")
    if not -1 <= config.higher_timeframe_min_trend_return <= 1:
        raise ValueError("合约高周期最低趋势收益必须在 -100% 到 100% 之间")
    if not 0 <= config.mean_reversion_zscore <= 10:
        raise ValueError("合约均值回归 Z 值必须在 0 到 10 之间")
    if not 50 <= config.mean_reversion_rsi_overbought <= 95:
        raise ValueError("合约均值回归超买 RSI 必须在 50 到 95 之间")
    if not 5 <= config.mean_reversion_rsi_oversold <= 50:
        raise ValueError("合约均值回归超卖 RSI 必须在 5 到 50 之间")
    if not 0 < config.mean_reversion_position_scale <= 1:
        raise ValueError("合约均值回归仓位系数必须在 0% 到 100% 之间")
    if config.weak_momentum_exit_hours < 0:
        raise ValueError("合约弱动量退出小时数不能小于 0")
    return config


def _check(name: str, current, target: str, passed: bool) -> dict:
    return {"name": name, "current": current, "target": target, "passed": bool(passed)}


def _rsi_and_bollinger(close: pd.Series) -> tuple[float, float]:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, math.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if not math.isfinite(rsi):
        last_gain = float(gain.iloc[-1]) if math.isfinite(float(gain.iloc[-1])) else 0.0
        last_loss = float(loss.iloc[-1]) if math.isfinite(float(loss.iloc[-1])) else 0.0
        if last_loss == 0 and last_gain > 0:
            rsi = 100.0
        elif last_gain == 0 and last_loss > 0:
            rsi = 0.0
        else:
            rsi = 50.0
    window = min(20, len(close))
    middle = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = float((middle + 2 * std).iloc[-1])
    lower = float((middle - 2 * std).iloc[-1])
    last = float(close.iloc[-1])
    bollinger_position = (last - lower) / (upper - lower) if upper > lower else 0.5
    return rsi, bollinger_position


def _higher_timeframe_confirmation(
    bars: pd.DataFrame | None,
    direction: str,
    config: CryptoSwapConfig,
) -> tuple[bool, str, dict, list[dict]]:
    if not config.higher_timeframe_enabled:
        return True, "合约高周期确认未启用", {}, []
    if bars is None or bars.empty:
        return (
            False,
            "合约高周期确认失败：缺少高周期K线",
            {"higher_timeframe_confirmed": False},
            [_check("高周期K线", 0, f"≥ {config.higher_timeframe_min_bars}", False)],
        )
    frame = bars[bars["confirm"].astype(bool)].copy() if "confirm" in bars else bars.copy()
    if len(frame) < config.higher_timeframe_min_bars:
        return (
            False,
            (
                "合约高周期确认失败：样本不足 "
                f"{len(frame)}/{config.higher_timeframe_min_bars}"
            ),
            {"higher_timeframe_confirmed": False, "higher_timeframe_bars": len(frame)},
            [
                _check(
                    "高周期K线",
                    len(frame),
                    f"≥ {config.higher_timeframe_min_bars}",
                    False,
                )
            ],
        )
    close = frame["close"].astype(float)
    ema_fast = float(close.ewm(span=12, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=48, adjust=False).mean().iloc[-1])
    ema_trend = float(close.ewm(span=min(120, len(close)), adjust=False).mean().iloc[-1])
    lookback = min(config.higher_timeframe_min_bars, len(close) - 1)
    trend_return = float(close.iloc[-1] / close.iloc[-lookback] - 1)
    if direction == "LONG":
        passed = (
            ema_fast > ema_slow >= ema_trend
            and trend_return >= config.higher_timeframe_min_trend_return
        )
        target = (
            "做多：EMA快 > EMA慢 ≥ 趋势均线；"
            f"趋势≥{config.higher_timeframe_min_trend_return:.2%}"
        )
    else:
        passed = (
            ema_fast < ema_slow <= ema_trend
            and trend_return <= -config.higher_timeframe_min_trend_return
        )
        target = (
            "做空：EMA快 < EMA慢 ≤ 趋势均线；"
            f"趋势≤-{config.higher_timeframe_min_trend_return:.2%}"
        )
    metrics = {
        "higher_timeframe_confirmed": passed,
        "higher_timeframe_bar": config.higher_timeframe_bar,
        "higher_timeframe_trend_return": trend_return,
        "higher_timeframe_ema_fast": ema_fast,
        "higher_timeframe_ema_slow": ema_slow,
        "higher_timeframe_ema_trend": ema_trend,
    }
    checks = [
        _check(
            "高周期方向确认",
            (
                f"{config.higher_timeframe_bar} 趋势 {trend_return:+.2%}；"
                f"EMA快/慢 {ema_fast / ema_slow - 1:+.2%}"
            ),
            target,
            passed,
        )
    ]
    reason = "合约高周期确认通过" if passed else f"合约高周期确认失败：{direction} 大周期不一致"
    return passed, reason, metrics, checks


def _directional_exit_config(
    config: CryptoSwapConfig,
    position_contracts: float,
) -> tuple[float, float, float, float]:
    if position_contracts > 0:
        return (
            config.long_hard_stop_loss,
            config.long_profit_protection_start,
            config.long_profit_giveback,
            config.long_max_hold_hours,
        )
    return (
        config.short_hard_stop_loss,
        config.short_profit_protection_start,
        config.short_profit_giveback,
        config.short_max_hold_hours,
    )


def decide_crypto_swap(
    bars: pd.DataFrame,
    position_contracts: float,
    average_price: float,
    config: CryptoSwapConfig | None = None,
    peak_pnl: float = 0.0,
    partial_stage: int = 0,
    held_hours: float = 0.0,
    contraction_count: int = 0,
    funding_rate: float = 0.0,
    liquidation_buffer: float | None = None,
    higher_timeframe_bars: pd.DataFrame | None = None,
) -> CryptoSwapDecision:
    config = config or CryptoSwapConfig()
    frame = bars[bars["confirm"].astype(bool)].copy() if "confirm" in bars else bars.copy()
    required = max(config.slow_window + 2, config.breakout_window + 2, 50)
    if len(frame) < required:
        return CryptoSwapDecision("HOLD", f"样本不足：{len(frame)}/{required}")
    close = frame["close"].astype(float)
    signal = build_crypto_signal(
        frame,
        CryptoSignalProfile(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            breakout_window=config.breakout_window,
            volatility_expansion=config.volatility_expansion,
            zscore_target=config.zscore_target,
            min_volume_ratio=0.0,
            regime_filter_enabled=config.regime_filter_enabled,
            trend_window=config.trend_window,
            min_trend_strength=config.min_trend_strength,
            max_chase_atr=config.max_chase_atr,
            confirmation_bars=config.confirmation_bars,
            breakout_enabled=config.trend_breakout_enabled,
        ),
    )
    last = float(close.iloc[-1])
    rsi, bollinger_position = _rsi_and_bollinger(close)
    metrics = {
        **signal.metrics,
        "last": last,
        "funding_rate": funding_rate,
        "liquidation_buffer": liquidation_buffer,
        "rsi": rsi,
        "bollinger_position": bollinger_position,
    }
    checks = signal.checks
    raw_long_signal = signal.active and signal.direction == "LONG"
    long_volume_ok = metrics.get("volume_ratio", 0.0) >= config.long_min_volume_ratio
    long_trend_ok = metrics.get("trend_return", 0.0) >= config.long_min_trend_strength
    long_signal = (
        config.trend_breakout_enabled
        and raw_long_signal and long_volume_ok and long_trend_ok
    )
    raw_short_signal = signal.active and signal.direction == "SHORT"
    short_volume_ok = metrics.get("volume_ratio", 0.0) >= config.short_min_volume_ratio
    short_trend_ok = metrics.get("trend_return", 0.0) <= -config.short_min_trend_strength
    short_signal = (
        config.trend_breakout_enabled
        and raw_short_signal and short_volume_ok and short_trend_ok
    )
    mean_reversion_volume_ok = (
        metrics.get("volume_ratio", 0.0) >= config.mean_reversion_min_volume_ratio
    )
    mean_reversion_long = (
        config.mean_reversion_enabled
        and metrics.get("zscore", 0.0) <= -config.mean_reversion_zscore
        and rsi <= config.mean_reversion_rsi_oversold
        and bollinger_position <= config.mean_reversion_bollinger_lower
        and mean_reversion_volume_ok
    )
    mean_reversion_short = (
        config.mean_reversion_enabled
        and metrics.get("zscore", 0.0) >= config.mean_reversion_zscore
        and rsi >= config.mean_reversion_rsi_overbought
        and bollinger_position >= config.mean_reversion_bollinger_upper
        and mean_reversion_volume_ok
    )
    continuation_window = min(config.continuation_bars + 1, len(close))
    recent_return = (
        float(close.iloc[-1] / close.iloc[-continuation_window] - 1)
        if continuation_window >= 2 else 0.0
    )
    continuation_long_ok = recent_return >= config.continuation_min_return
    continuation_short_ok = recent_return <= -config.short_continuation_min_return
    metrics.update({
        "continuation_return": recent_return,
        "continuation_long_ok": continuation_long_ok,
        "continuation_short_ok": continuation_short_ok,
        "raw_long_signal": raw_long_signal,
        "long_volume_ok": long_volume_ok,
        "long_trend_ok": long_trend_ok,
        "long_min_volume_ratio": config.long_min_volume_ratio,
        "long_min_trend_strength": config.long_min_trend_strength,
        "raw_short_signal": raw_short_signal,
        "short_volume_ok": short_volume_ok,
        "short_trend_ok": short_trend_ok,
        "short_min_volume_ratio": config.short_min_volume_ratio,
        "short_min_trend_strength": config.short_min_trend_strength,
        "short_continuation_min_return": config.short_continuation_min_return,
        "mean_reversion_long": mean_reversion_long,
        "mean_reversion_short": mean_reversion_short,
        "mean_reversion_volume_ok": mean_reversion_volume_ok,
        "entry_model": "无",
    })
    checks.append({
        "name": "趋势延续确认",
        "current": f"{recent_return:+.2%}",
        "target": (
            f"做多≥{config.continuation_min_return:.2%}；"
            f"做空≤-{config.short_continuation_min_return:.2%}"
        ),
        "passed": (
            not long_signal and not short_signal
        ) or (long_signal and continuation_long_ok) or (
            short_signal and continuation_short_ok
        ),
    })
    checks.append({
        "name": "做多独立过滤",
        "current": (
            f"量比 {metrics.get('volume_ratio', 0.0):.2f}；"
            f"趋势 {metrics.get('trend_return', 0.0):+.2%}"
        ),
        "target": (
            f"量比≥{config.long_min_volume_ratio:.2f}；"
            f"趋势≥{config.long_min_trend_strength:.2%}"
        ),
        "passed": (not raw_long_signal) or (long_volume_ok and long_trend_ok),
    })
    checks.append({
        "name": "做空独立过滤",
        "current": (
            f"量比 {metrics.get('volume_ratio', 0.0):.2f}；"
            f"趋势 {metrics.get('trend_return', 0.0):+.2%}"
        ),
        "target": (
            f"量比≥{config.short_min_volume_ratio:.2f}；"
            f"趋势≤-{config.short_min_trend_strength:.2%}"
        ),
        "passed": (not raw_short_signal) or (short_volume_ok and short_trend_ok),
    })
    checks.append({
        "name": "均值回归模型",
        "current": (
            f"Z {metrics.get('zscore', 0.0):+.2f}；"
            f"RSI {rsi:.1f}；布林 {bollinger_position:.0%}；"
            f"量比 {metrics.get('volume_ratio', 0.0):.2f}"
        ),
        "target": (
            f"多：Z≤-{config.mean_reversion_zscore:.2f}/RSI≤"
            f"{config.mean_reversion_rsi_oversold:.0f}/布林≤"
            f"{config.mean_reversion_bollinger_lower:.0%}；"
            f"空：Z≥{config.mean_reversion_zscore:.2f}/RSI≥"
            f"{config.mean_reversion_rsi_overbought:.0f}/布林≥"
            f"{config.mean_reversion_bollinger_upper:.0%}"
        ),
        "passed": mean_reversion_long or mean_reversion_short
        or not config.mean_reversion_enabled,
    })
    if position_contracts:
        pnl = (
            last / average_price - 1
            if position_contracts > 0 else average_price / last - 1
        ) if average_price > 0 else 0
        metrics["pnl_ratio"] = pnl
        hard_stop, protection_start, giveback, max_hold_hours = _directional_exit_config(
            config, position_contracts,
        )
        if pnl <= -hard_stop:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"合约{'做多' if position_contracts > 0 else '做空'}硬止损触发：{pnl:.2%}",
                metrics=metrics,
                checks=checks,
            )
        if (
            liquidation_buffer is not None
            and liquidation_buffer <= config.liquidation_buffer_exit
        ):
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                (
                    f"强平缓冲过低：当前 {liquidation_buffer:.2%} ≤ "
                    f"{config.liquidation_buffer_exit:.2%}"
                ),
                metrics=metrics,
                checks=checks,
            )
        emergency_reverse = (
            position_contracts > 0 and short_signal and pnl <= -config.emergency_reverse_loss
        ) or (
            position_contracts < 0 and long_signal and pnl <= -config.emergency_reverse_loss
        )
        if emergency_reverse:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"紧急反向信号退出：亏损 {pnl:.2%} 且反向强信号成立",
                metrics=metrics,
                checks=checks,
            )
        if held_hours < config.min_hold_hours and pnl > -hard_stop:
            return CryptoSwapDecision(
                "HOLD", f"未达到最短持有 {config.min_hold_hours:.1f} 小时，继续管理",
                metrics=metrics, checks=checks,
            )
        if (
            config.weak_momentum_exit_enabled
            and held_hours >= config.weak_momentum_exit_hours
            and peak_pnl < config.weak_momentum_exit_min_peak_pnl
            and pnl <= config.weak_momentum_exit_max_pnl
        ):
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                (
                    f"合约弱动量时间退出：持有 {held_hours:.1f} 小时，"
                    f"峰值浮盈 {peak_pnl:.2%} < "
                    f"{config.weak_momentum_exit_min_peak_pnl:.2%}，"
                    f"当前 {pnl:.2%} ≤ {config.weak_momentum_exit_max_pnl:.2%}"
                ),
                metrics={
                    **metrics,
                    "weak_momentum_exit": True,
                    "peak_pnl_ratio": peak_pnl,
                },
                checks=[
                    *checks,
                    _check(
                        "弱动量退出",
                        f"持有 {held_hours:.1f}h；峰值 {peak_pnl:.2%}；当前 {pnl:.2%}",
                        (
                            f"持有≥{config.weak_momentum_exit_hours:.1f}h；"
                            f"峰值<{config.weak_momentum_exit_min_peak_pnl:.2%}；"
                            f"当前≤{config.weak_momentum_exit_max_pnl:.2%}"
                        ),
                        True,
                    ),
                ],
            )
        if partial_stage < 1 and pnl >= config.partial_profit_tier1:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"合约第一档分批止盈：当前盈利 {pnl:.2%}",
                metrics=metrics, checks=checks,
                quantity_fraction=config.partial_profit_tier1_fraction,
            )
        if partial_stage < 2 and pnl >= config.partial_profit_tier2:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"合约第二档分批止盈：当前盈利 {pnl:.2%}",
                metrics=metrics, checks=checks,
                quantity_fraction=config.partial_profit_tier2_fraction,
            )
        protection_line = peak_pnl * (1 - giveback)
        if peak_pnl >= protection_start and pnl <= protection_line:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"合约盈利保护触发：峰值 {peak_pnl:.2%}，当前 {pnl:.2%}",
                metrics=metrics, checks=checks,
            )
        if held_hours >= max_hold_hours:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"达到合约{'做多' if position_contracts > 0 else '做空'}最长持有 {max_hold_hours:.0f} 小时",
                metrics=metrics, checks=checks,
            )
        funding_against_position = (
            funding_rate > config.funding_exit_rate if position_contracts > 0
            else funding_rate < -config.funding_exit_rate
        )
        if funding_against_position:
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"资金费率恶化退出：当前 {funding_rate:.4%}",
                metrics=metrics, checks=checks,
            )
        if (
            metrics["volatility_expansion"] <= config.contraction_ratio
            and contraction_count >= config.contraction_confirmations
        ):
            return CryptoSwapDecision(
                "CLOSE_LONG" if position_contracts > 0 else "CLOSE_SHORT",
                f"波动收缩连续确认 {contraction_count} 次，退出合约持仓",
                metrics=metrics, checks=checks,
            )
        if position_contracts > 0 and short_signal:
            return CryptoSwapDecision("CLOSE_LONG", "反向做空信号出现，先平多仓", metrics=metrics, checks=checks)
        if position_contracts < 0 and long_signal:
            return CryptoSwapDecision("CLOSE_SHORT", "反向做多信号出现，先平空仓", metrics=metrics, checks=checks)
        return CryptoSwapDecision("HOLD", "持仓条件仍有效，继续管理", metrics=metrics, checks=checks)
    target_notional = config.margin_budget * config.leverage
    if not config.entry_enabled:
        return CryptoSwapDecision(
            "HOLD",
            "合约新开仓已关闭：长样本回测显示当前合约突破模型为负贡献，仅保留已有持仓管理",
            metrics=metrics,
            checks=checks,
        )
    long_entry = long_signal or mean_reversion_long
    short_entry = short_signal or mean_reversion_short
    entry_direction = "LONG" if long_entry else "SHORT" if short_entry else "NONE"
    entry_model = (
        "趋势突破" if long_signal or short_signal
        else "均值回归" if mean_reversion_long or mean_reversion_short
        else "无"
    )
    if entry_direction != "NONE":
        htf_passed, htf_reason, htf_metrics, htf_checks = _higher_timeframe_confirmation(
            higher_timeframe_bars, entry_direction, config,
        )
        metrics = {**metrics, **htf_metrics, "entry_model": entry_model}
        checks = [*checks, *htf_checks]
        if not htf_passed and entry_model == "趋势突破":
            return CryptoSwapDecision(
                "HOLD",
                htf_reason,
                metrics=metrics,
                checks=checks,
            )
    if long_signal and not continuation_long_ok:
        return CryptoSwapDecision(
            "HOLD",
            (
                f"做多信号已出现，但趋势延续不足：近 "
                f"{config.continuation_bars} 根 {recent_return:+.2%}"
            ),
            metrics=metrics,
            checks=checks,
        )
    if config.trend_breakout_enabled and raw_long_signal and not long_signal:
        return CryptoSwapDecision(
            "HOLD",
            "做多信号已出现，但独立做多过滤未通过",
            metrics=metrics,
            checks=checks,
        )
    if short_signal and not continuation_short_ok:
        return CryptoSwapDecision(
            "HOLD",
            (
                f"做空信号已出现，但趋势延续不足：近 "
                f"{config.continuation_bars} 根 {recent_return:+.2%}"
            ),
            metrics=metrics,
            checks=checks,
        )
    if config.trend_breakout_enabled and raw_short_signal and not short_signal:
        return CryptoSwapDecision(
            "HOLD",
            "做空信号已出现，但独立做空过滤未通过",
            metrics=metrics,
            checks=checks,
        )
    if long_signal:
        return CryptoSwapDecision(
            "OPEN_LONG",
            "趋势突破做多：1H 突破、独立做多过滤和高周期确认通过",
            target_notional,
            metrics,
            checks,
        )
    if short_signal:
        return CryptoSwapDecision(
            "OPEN_SHORT",
            "趋势突破做空：1H 跌破、独立做空过滤和高周期确认通过",
            target_notional,
            metrics,
            checks,
        )
    if mean_reversion_long:
        return CryptoSwapDecision(
            "OPEN_LONG",
            "均值回归做多：极端超卖、布林下沿和量能确认",
            target_notional * config.mean_reversion_position_scale,
            metrics,
            checks,
        )
    if mean_reversion_short:
        return CryptoSwapDecision(
            "OPEN_SHORT",
            "均值回归做空：极端超买、布林上沿和量能确认",
            target_notional * config.mean_reversion_position_scale,
            metrics,
            checks,
        )
    return CryptoSwapDecision("HOLD", "永续合约入场条件未全部满足", metrics=metrics, checks=checks)
