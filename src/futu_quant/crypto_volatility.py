from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import os

import pandas as pd

from .crypto_signal import CryptoSignalProfile, build_crypto_signal


@dataclass(frozen=True)
class CryptoVolatilityConfig:
    fast_window: int = 12
    slow_window: int = 72
    breakout_window: int = 24
    volatility_expansion: float = 1.35
    zscore_target: float = 2.0
    min_volume_ratio: float = 1.10
    budget: float = 1_000.0
    max_order_notional: float = 500.0
    target_volatility: float = 0.80
    hard_stop_loss: float = 0.08
    profit_protection_start: float = 0.10
    profit_giveback: float = 0.30
    minimum_locked_profit: float = 0.04
    tactical_profit_protection_start: float = 0.015
    tactical_profit_giveback: float = 0.55
    tactical_minimum_locked_profit: float = 0.002
    weak_momentum_exit_enabled: bool = True
    weak_momentum_exit_hours: float = 12.0
    weak_momentum_exit_max_pnl: float = 0.0
    weak_momentum_exit_min_peak_pnl: float = 0.008
    partial_profit_tier1: float = 0.025
    partial_profit_tier1_fraction: float = 0.50
    partial_profit_tier2: float = 0.050
    partial_profit_tier2_fraction: float = 0.25
    max_hold_hours: float = 120
    contraction_ratio: float = 0.75
    contraction_confirmations: int = 3
    regime_filter_enabled: bool = True
    trend_window: int = 120
    min_trend_strength: float = 0.005
    max_chase_atr: float = 8.0
    confirmation_bars: int = 2
    breakout_enabled: bool = False
    pullback_enabled: bool = False
    pullback_depth: float = 0.03
    pullback_max_rsi: float = 60.0
    pullback_max_bollinger: float = 0.70
    pullback_min_trend_return: float = 0.05
    pullback_min_last_return: float = 0.006
    trend_participation_enabled: bool = True
    trend_participation_min_24h_return: float = 0.030
    trend_participation_min_72h_return: float = 0.045
    trend_participation_max_rsi: float = 75.0
    trend_participation_max_bollinger: float = 1.10
    trend_participation_min_volume_ratio: float = 0.70
    trend_participation_position_scale: float = 0.45
    higher_timeframe_enabled: bool = True
    higher_timeframe_bar: str = "4H"
    higher_timeframe_min_bars: int = 80
    higher_timeframe_min_trend_return: float = 0.0


@dataclass(frozen=True)
class CryptoVolatilityDecision:
    action: str
    reason: str
    target_notional: float = 0.0
    signal_strength: str = "观察"
    metrics: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    quantity_fraction: float = 0.0


def crypto_volatility_config_from_env() -> CryptoVolatilityConfig:
    config = CryptoVolatilityConfig(
        fast_window=int(os.getenv("CRYPTO_VOL_FAST_WINDOW", "12")),
        slow_window=int(os.getenv("CRYPTO_VOL_SLOW_WINDOW", "72")),
        breakout_window=int(os.getenv("CRYPTO_VOL_BREAKOUT_WINDOW", "24")),
        volatility_expansion=float(os.getenv("CRYPTO_VOL_EXPANSION", "1.35")),
        zscore_target=float(os.getenv("CRYPTO_VOL_ZSCORE", "2.0")),
        min_volume_ratio=float(os.getenv("CRYPTO_VOL_MIN_VOLUME_RATIO", "1.10")),
        budget=float(os.getenv("CRYPTO_VOLATILITY_BUDGET", "1000")),
        max_order_notional=float(os.getenv("CRYPTO_MAX_ORDER_NOTIONAL", "500")),
        target_volatility=float(os.getenv("CRYPTO_VOL_TARGET_VOLATILITY", "0.80")),
        hard_stop_loss=float(os.getenv("CRYPTO_VOL_HARD_STOP", "0.08")),
        profit_protection_start=float(
            os.getenv("CRYPTO_VOL_PROFIT_PROTECTION_START", "0.10")
        ),
        profit_giveback=float(os.getenv("CRYPTO_VOL_PROFIT_GIVEBACK", "0.30")),
        minimum_locked_profit=float(
            os.getenv("CRYPTO_VOL_MINIMUM_LOCKED_PROFIT", "0.04")
        ),
        tactical_profit_protection_start=float(
            os.getenv("CRYPTO_VOL_TACTICAL_PROFIT_PROTECTION_START", "0.015")
        ),
        tactical_profit_giveback=float(
            os.getenv("CRYPTO_VOL_TACTICAL_PROFIT_GIVEBACK", "0.55")
        ),
        tactical_minimum_locked_profit=float(
            os.getenv("CRYPTO_VOL_TACTICAL_MINIMUM_LOCKED_PROFIT", "0.002")
        ),
        weak_momentum_exit_enabled=os.getenv(
            "CRYPTO_WEAK_MOMENTUM_EXIT_ENABLED", "true"
        ).lower() == "true",
        weak_momentum_exit_hours=float(
            os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_HOURS", "12")
        ),
        weak_momentum_exit_max_pnl=float(
            os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_MAX_PNL", "0")
        ),
        weak_momentum_exit_min_peak_pnl=float(
            os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL", "0.008")
        ),
        partial_profit_tier1=float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER1", "0.025")),
        partial_profit_tier1_fraction=float(
            os.getenv("CRYPTO_PARTIAL_PROFIT_TIER1_FRACTION", "0.50")
        ),
        partial_profit_tier2=float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER2", "0.050")),
        partial_profit_tier2_fraction=float(
            os.getenv("CRYPTO_PARTIAL_PROFIT_TIER2_FRACTION", "0.25")
        ),
        max_hold_hours=float(os.getenv("CRYPTO_MAX_HOLD_HOURS", "120")),
        contraction_ratio=float(os.getenv("CRYPTO_CONTRACTION_RATIO", "0.75")),
        contraction_confirmations=int(os.getenv("CRYPTO_CONTRACTION_CONFIRMATIONS", "3")),
        regime_filter_enabled=os.getenv("CRYPTO_REGIME_FILTER_ENABLED", "true").lower() == "true",
        trend_window=int(os.getenv("CRYPTO_TREND_WINDOW", "120")),
        min_trend_strength=float(os.getenv("CRYPTO_MIN_TREND_STRENGTH", "0.005")),
        max_chase_atr=float(os.getenv("CRYPTO_MAX_CHASE_ATR", "8.0")),
        confirmation_bars=int(os.getenv("CRYPTO_CONFIRMATION_BARS", "2")),
        breakout_enabled=os.getenv("CRYPTO_BREAKOUT_ENABLED", "false").lower() == "true",
        pullback_enabled=os.getenv("CRYPTO_PULLBACK_ENABLED", "false").lower() == "true",
        pullback_depth=float(os.getenv("CRYPTO_PULLBACK_DEPTH", "0.03")),
        pullback_max_rsi=float(os.getenv("CRYPTO_PULLBACK_MAX_RSI", "60")),
        pullback_max_bollinger=float(os.getenv("CRYPTO_PULLBACK_MAX_BOLLINGER", "0.70")),
        pullback_min_trend_return=float(
            os.getenv("CRYPTO_PULLBACK_MIN_TREND_RETURN", "0.05")
        ),
        pullback_min_last_return=float(
            os.getenv("CRYPTO_PULLBACK_MIN_LAST_RETURN", "0.006")
        ),
        trend_participation_enabled=os.getenv(
            "CRYPTO_TREND_PARTICIPATION_ENABLED", "true"
        ).lower() == "true",
        trend_participation_min_24h_return=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_24H_RETURN", "0.030")
        ),
        trend_participation_min_72h_return=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_72H_RETURN", "0.045")
        ),
        trend_participation_max_rsi=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_MAX_RSI", "75")
        ),
        trend_participation_max_bollinger=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_MAX_BOLLINGER", "1.10")
        ),
        trend_participation_min_volume_ratio=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_VOLUME_RATIO", "0.70")
        ),
        trend_participation_position_scale=float(
            os.getenv("CRYPTO_TREND_PARTICIPATION_POSITION_SCALE", "0.45")
        ),
        higher_timeframe_enabled=os.getenv(
            "CRYPTO_HIGHER_TIMEFRAME_ENABLED", "true"
        ).lower() == "true",
        higher_timeframe_bar=os.getenv("CRYPTO_HIGHER_TIMEFRAME_BAR", "4H"),
        higher_timeframe_min_bars=int(
            os.getenv("CRYPTO_HIGHER_TIMEFRAME_MIN_BARS", "80")
        ),
        higher_timeframe_min_trend_return=float(
            os.getenv("CRYPTO_HIGHER_TIMEFRAME_MIN_TREND_RETURN", "0")
        ),
    )
    if not 6 <= config.fast_window < config.slow_window <= 500:
        raise ValueError("币策略快窗口必须不少于 6，并且小于慢窗口（最大 500）")
    if not 12 <= config.breakout_window <= 300:
        raise ValueError("币策略突破窗口必须在 12 到 300 之间")
    if not 1.0 <= config.volatility_expansion <= 10:
        raise ValueError("币策略波动扩张倍数必须在 1.0 到 10.0 之间")
    if not 0 <= config.zscore_target <= 10:
        raise ValueError("币策略波动 Z 值必须在 0 到 10 之间")
    if not 0 <= config.min_volume_ratio <= 20:
        raise ValueError("币策略最低量比必须在 0 到 20 之间")
    if not 0 < config.target_volatility <= 10 or not 0 < config.hard_stop_loss <= .50:
        raise ValueError("目标波动率或硬止损参数超出有效范围")
    if not 0 < config.profit_protection_start <= 2:
        raise ValueError("盈利保护启动比例必须在 0% 到 200% 之间")
    if not 0 < config.profit_giveback < 1:
        raise ValueError("盈利回吐比例必须在 0% 到 100% 之间")
    if not 0 <= config.minimum_locked_profit < config.profit_protection_start:
        raise ValueError("最低锁定盈利必须小于盈利保护启动比例")
    if not 0 < config.tactical_profit_protection_start <= config.profit_protection_start:
        raise ValueError("早期盈利保护启动比例必须大于 0 且不高于普通盈利保护")
    if not 0 < config.tactical_profit_giveback < 1:
        raise ValueError("早期盈利回吐比例必须在 0% 到 100% 之间")
    if not 0 <= config.tactical_minimum_locked_profit < config.tactical_profit_protection_start:
        raise ValueError("早期最低锁定盈利必须小于早期盈利保护启动比例")
    if config.weak_momentum_exit_hours < 0:
        raise ValueError("弱动量退出持有小时数不能小于 0")
    if not -0.50 <= config.weak_momentum_exit_max_pnl <= 0.50:
        raise ValueError("弱动量退出当前收益阈值必须在 -50% 到 50% 之间")
    if not 0 <= config.weak_momentum_exit_min_peak_pnl <= 0.50:
        raise ValueError("弱动量退出最低峰值浮盈必须在 0% 到 50% 之间")
    if not 0 < config.partial_profit_tier1 < config.partial_profit_tier2:
        raise ValueError("分批止盈第二档必须高于第一档")
    if not all(0 < value <= 1 for value in (
        config.partial_profit_tier1_fraction, config.partial_profit_tier2_fraction,
    )):
        raise ValueError("分批止盈比例必须在 0% 到 100% 之间")
    if config.max_hold_hours <= 0 or not 0 < config.contraction_ratio <= 1:
        raise ValueError("最长持有或波动收缩参数无效")
    if not 24 <= config.trend_window <= 1000:
        raise ValueError("趋势窗口必须在 24 到 1000 之间")
    if not 0 <= config.min_trend_strength <= 1:
        raise ValueError("趋势强度门槛必须在 0% 到 100% 之间")
    if not 1 <= config.max_chase_atr <= 50:
        raise ValueError("反追价 ATR 倍数必须在 1 到 50 之间")
    if not 1 <= config.confirmation_bars <= 6:
        raise ValueError("突破确认根数必须在 1 到 6 之间")
    if not 0 <= config.pullback_depth <= .30:
        raise ValueError("回调深度必须在 0% 到 30% 之间")
    if not 30 <= config.pullback_max_rsi <= 90:
        raise ValueError("回调 RSI 上限必须在 30 到 90 之间")
    if not 0.1 <= config.pullback_max_bollinger <= 1:
        raise ValueError("回调布林位置上限必须在 10% 到 100% 之间")
    if not 0 <= config.pullback_min_trend_return <= 1:
        raise ValueError("回调最低趋势收益必须在 0% 到 100% 之间")
    if not 0 <= config.pullback_min_last_return <= .20:
        raise ValueError("回调单K反弹确认必须在 0% 到 20% 之间")
    if not 0 <= config.trend_participation_min_24h_return <= 1:
        raise ValueError("趋势参与 24h 动量必须在 0% 到 100% 之间")
    if not 0 <= config.trend_participation_min_72h_return <= 1:
        raise ValueError("趋势参与 72h 动量必须在 0% 到 100% 之间")
    if not 30 <= config.trend_participation_max_rsi <= 95:
        raise ValueError("趋势参与 RSI 上限必须在 30 到 95 之间")
    if not 0.1 <= config.trend_participation_max_bollinger <= 1.5:
        raise ValueError("趋势参与布林位置上限必须在 10% 到 150% 之间")
    if not 0 <= config.trend_participation_min_volume_ratio <= 20:
        raise ValueError("趋势参与最低量比必须在 0 到 20 之间")
    if not 0 < config.trend_participation_position_scale <= 1:
        raise ValueError("趋势参与仓位系数必须在 0% 到 100% 之间")
    if config.higher_timeframe_min_bars < 24:
        raise ValueError("高周期确认最少K线必须不少于 24 根")
    if not -1 <= config.higher_timeframe_min_trend_return <= 1:
        raise ValueError("高周期最低趋势收益必须在 -100% 到 100% 之间")
    return config


def _check(name: str, current, target: str, passed: bool) -> dict:
    return {"name": name, "current": current, "target": target, "passed": bool(passed)}


def higher_timeframe_confirmation(
    bars: pd.DataFrame | None,
    config: CryptoVolatilityConfig,
) -> tuple[bool, str, dict, list[dict]]:
    if not config.higher_timeframe_enabled:
        return True, "高周期确认未启用", {}, []
    if bars is None or bars.empty:
        return (
            False,
            "高周期确认失败：缺少高周期K线",
            {"higher_timeframe_confirmed": False},
            [_check("高周期K线", 0, f"≥ {config.higher_timeframe_min_bars}", False)],
        )
    frame = bars[bars.get("confirm", True).astype(bool)].copy() if "confirm" in bars else bars.copy()
    if len(frame) < config.higher_timeframe_min_bars:
        return (
            False,
            f"高周期确认失败：样本不足 {len(frame)}/{config.higher_timeframe_min_bars}",
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
    passed = (
        ema_fast > ema_slow >= ema_trend
        and trend_return >= config.higher_timeframe_min_trend_return
    )
    metrics = {
        "higher_timeframe_confirmed": passed,
        "higher_timeframe_bar": config.higher_timeframe_bar,
        "higher_timeframe_bars": len(frame),
        "higher_timeframe_trend_return": trend_return,
        "higher_timeframe_ema_fast": ema_fast,
        "higher_timeframe_ema_slow": ema_slow,
        "higher_timeframe_ema_trend": ema_trend,
    }
    checks = [
        _check(
            "高周期方向",
            (
                f"{config.higher_timeframe_bar} 趋势 {trend_return:+.2%}；"
                f"EMA快/慢 {ema_fast / ema_slow - 1:+.2%}"
            ),
            (
                f"EMA快 > EMA慢 ≥ 趋势均线；"
                f"趋势≥{config.higher_timeframe_min_trend_return:.2%}"
            ),
            passed,
        )
    ]
    reason = "高周期趋势确认通过" if passed else "高周期确认失败：大周期趋势未向上"
    return passed, reason, metrics, checks


def decide_crypto_volatility(
    bars: pd.DataFrame,
    position_size: Decimal = Decimal("0"),
    average_price: Decimal | None = None,
    peak_price: Decimal | None = None,
    partial_stage: int = 0,
    held_hours: float = 0,
    contraction_count: int = 0,
    config: CryptoVolatilityConfig | None = None,
    higher_timeframe_bars: pd.DataFrame | None = None,
) -> CryptoVolatilityDecision:
    config = config or CryptoVolatilityConfig()
    confirmed = bars[bars.get("confirm", True).astype(bool)].copy() if "confirm" in bars else bars.copy()
    required = max(config.slow_window + 2, config.breakout_window + 2, 50)
    if len(confirmed) < required:
        return CryptoVolatilityDecision(
            "HOLD", f"样本不足：当前 {len(confirmed)} 根，至少需要 {required} 根",
            checks=[_check("完整K线数量", len(confirmed), f"≥ {required}", False)],
        )
    close = confirmed["close"].astype(float)
    last = float(close.iloc[-1])
    if position_size > 0 and average_price and average_price > 0:
        pnl = last / float(average_price) - 1
        if pnl <= -config.hard_stop_loss:
            return CryptoVolatilityDecision(
                "SELL_ALL", f"硬止损触发：当前 {pnl:.2%} ≤ -{config.hard_stop_loss:.2%}",
                metrics={"pnl_ratio": pnl},
                checks=[_check("持仓收益率", f"{pnl:.2%}", f"> -{config.hard_stop_loss:.2%}", False)],
            )
        peak_pnl = (
            float(peak_price) / float(average_price) - 1
            if peak_price is not None and peak_price > 0 else pnl
        )
        locked_profit = max(
            config.minimum_locked_profit,
            peak_pnl * (1 - config.profit_giveback),
        )
        if peak_pnl >= config.profit_protection_start and pnl <= locked_profit:
            return CryptoVolatilityDecision(
                "SELL_ALL",
                (
                    f"盈利保护触发：峰值 {peak_pnl:.2%}，当前 {pnl:.2%}，"
                    f"保护线 {locked_profit:.2%}"
                ),
                metrics={
                    "pnl_ratio": pnl, "peak_pnl_ratio": peak_pnl,
                    "locked_profit_ratio": locked_profit,
                },
                checks=[
                    _check(
                        "峰值盈利", f"{peak_pnl:.2%}",
                        f"≥ {config.profit_protection_start:.2%}", True,
                    ),
                    _check("当前盈利", f"{pnl:.2%}", f"> {locked_profit:.2%}", False),
                ],
            )
        tactical_locked_profit = max(
            config.tactical_minimum_locked_profit,
            peak_pnl * (1 - config.tactical_profit_giveback),
        )
        if (
            peak_pnl >= config.tactical_profit_protection_start
            and pnl <= tactical_locked_profit
        ):
            return CryptoVolatilityDecision(
                "SELL_ALL",
                (
                    f"早期浮盈保护触发：峰值 {peak_pnl:.2%}，当前 {pnl:.2%}，"
                    f"保护线 {tactical_locked_profit:.2%}"
                ),
                metrics={
                    "pnl_ratio": pnl, "peak_pnl_ratio": peak_pnl,
                    "locked_profit_ratio": tactical_locked_profit,
                },
                checks=[
                    _check(
                        "早期峰值盈利", f"{peak_pnl:.2%}",
                        f"≥ {config.tactical_profit_protection_start:.2%}", True,
                    ),
                    _check(
                        "当前盈利", f"{pnl:.2%}",
                        f"> {tactical_locked_profit:.2%}", False,
                    ),
                ],
            )
        if (
            config.weak_momentum_exit_enabled
            and held_hours >= config.weak_momentum_exit_hours
            and peak_pnl < config.weak_momentum_exit_min_peak_pnl
            and pnl <= config.weak_momentum_exit_max_pnl
        ):
            return CryptoVolatilityDecision(
                "SELL_ALL",
                (
                    f"弱动量时间退出：持有 {held_hours:.1f} 小时，"
                    f"峰值浮盈 {peak_pnl:.2%} < "
                    f"{config.weak_momentum_exit_min_peak_pnl:.2%}，"
                    f"当前 {pnl:.2%} ≤ {config.weak_momentum_exit_max_pnl:.2%}"
                ),
                metrics={
                    "pnl_ratio": pnl,
                    "peak_pnl_ratio": peak_pnl,
                    "weak_momentum_exit": True,
                },
                checks=[
                    _check(
                        "持有时间",
                        f"{held_hours:.1f} 小时",
                        f"≥ {config.weak_momentum_exit_hours:.1f} 小时",
                        True,
                    ),
                    _check(
                        "峰值浮盈",
                        f"{peak_pnl:.2%}",
                        f"≥ {config.weak_momentum_exit_min_peak_pnl:.2%}",
                        False,
                    ),
                    _check(
                        "当前收益",
                        f"{pnl:.2%}",
                        f"> {config.weak_momentum_exit_max_pnl:.2%}",
                        False,
                    ),
                ],
            )
    returns = close.pct_change().dropna()
    signal = build_crypto_signal(
        confirmed,
        CryptoSignalProfile(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            breakout_window=config.breakout_window,
            volatility_expansion=config.volatility_expansion,
            zscore_target=config.zscore_target,
            min_volume_ratio=config.min_volume_ratio,
            regime_filter_enabled=config.regime_filter_enabled,
            trend_window=config.trend_window,
            min_trend_strength=config.min_trend_strength,
            max_chase_atr=config.max_chase_atr,
            confirmation_bars=config.confirmation_bars,
            breakout_enabled=config.breakout_enabled,
            pullback_enabled=config.pullback_enabled,
            pullback_depth=config.pullback_depth,
            pullback_max_rsi=config.pullback_max_rsi,
            pullback_max_bollinger=config.pullback_max_bollinger,
            pullback_min_trend_return=config.pullback_min_trend_return,
            pullback_min_last_return=config.pullback_min_last_return,
            trend_participation_enabled=config.trend_participation_enabled,
            trend_participation_min_24h_return=config.trend_participation_min_24h_return,
            trend_participation_min_72h_return=config.trend_participation_min_72h_return,
            trend_participation_max_rsi=config.trend_participation_max_rsi,
            trend_participation_max_bollinger=config.trend_participation_max_bollinger,
            trend_participation_min_volume_ratio=config.trend_participation_min_volume_ratio,
        ),
    )
    checks = signal.checks
    metrics = signal.metrics
    if position_size > 0:
        pnl = last / float(average_price) - 1 if average_price else 0.0
        if partial_stage < 1 and pnl >= config.partial_profit_tier1:
            return CryptoVolatilityDecision(
                "SELL_PARTIAL", f"第一档分批止盈：当前盈利 {pnl:.2%}",
                metrics={**metrics, "pnl_ratio": pnl}, checks=checks,
                quantity_fraction=config.partial_profit_tier1_fraction,
            )
        if partial_stage < 2 and pnl >= config.partial_profit_tier2:
            return CryptoVolatilityDecision(
                "SELL_PARTIAL", f"第二档分批止盈：当前盈利 {pnl:.2%}",
                metrics={**metrics, "pnl_ratio": pnl}, checks=checks,
                quantity_fraction=config.partial_profit_tier2_fraction,
            )
        if held_hours >= config.max_hold_hours:
            return CryptoVolatilityDecision(
                "SELL_ALL", f"达到最长持有 {config.max_hold_hours:.0f} 小时",
                metrics=metrics, checks=checks,
            )
        if signal.direction == "SHORT" and float(returns.iloc[-1]) < 0:
            return CryptoVolatilityDecision(
                "SELL_ALL", "趋势反转确认：共用信号层转为空头方向",
                metrics=metrics, checks=checks,
            )
        if (
            metrics["volatility_expansion"] <= config.contraction_ratio
            and contraction_count >= config.contraction_confirmations
        ):
            return CryptoVolatilityDecision(
                "SELL_ALL",
                f"波动收缩连续确认 {contraction_count} 次，退出持仓",
                metrics=metrics, checks=checks,
            )
        return CryptoVolatilityDecision("HOLD", "持仓继续观察", metrics=metrics, checks=checks)
    if not signal.active or signal.direction != "LONG":
        return CryptoVolatilityDecision("HOLD", signal.reason, metrics=metrics, checks=checks)
    htf_passed, htf_reason, htf_metrics, htf_checks = higher_timeframe_confirmation(
        higher_timeframe_bars, config,
    )
    metrics = {**metrics, **htf_metrics}
    checks = [*checks, *htf_checks]
    if not htf_passed:
        return CryptoVolatilityDecision(
            "HOLD",
            htf_reason,
            metrics=metrics,
            checks=checks,
        )
    strength = signal.strength
    coefficient = 1.0 if strength == "强信号" else .7
    if metrics.get("trend_participation_ok"):
        coefficient *= config.trend_participation_position_scale
    volatility_scale = min(1.0, config.target_volatility / metrics["rv_fast"]) if metrics["rv_fast"] > 0 else 0.0
    notional = min(config.max_order_notional, config.budget * coefficient * volatility_scale)
    return CryptoVolatilityDecision(
        "BUY", signal.reason,
        target_notional=max(0.0, notional), signal_strength=strength,
        metrics=metrics, checks=checks,
    )
