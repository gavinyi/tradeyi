from __future__ import annotations

from dataclasses import dataclass
import math
import os

import pandas as pd

from .crypto_swap import CryptoSwapConfig, CryptoSwapDecision


MAJOR_BASES = {"BTC", "ETH"}


@dataclass(frozen=True)
class CryptoSwapManagedFuturesConfig:
    engine: str = "MANAGED_FUTURES"
    alt_short_enabled: bool = True
    allow_major_short: bool = False
    excluded_bases: tuple[str, ...] = ("BTC", "ETH")
    score_threshold: float = 65.0
    relative_weakness_threshold: float = -0.015
    min_volume_ratio: float = 0.80
    max_negative_funding_for_short: float = -0.0010
    initial_margin_fraction: float = 0.35
    recovery_add_loss_steps: tuple[float, ...] = (0.03, 0.06, 0.10)
    recovery_add_margin_multipliers: tuple[float, ...] = (1.0, 1.2, 1.5)
    max_recovery_layers: int = 3
    recovery_take_profit: float = 0.008
    max_position_loss: float = 0.12
    trend_invalidation_score: float = 45.0
    benchmark_symbol: str = "BTC-USDT-SWAP"


def managed_futures_config_from_env() -> CryptoSwapManagedFuturesConfig:
    return CryptoSwapManagedFuturesConfig(
        engine=os.getenv("CRYPTO_SWAP_ENGINE", "MANAGED_FUTURES").upper(),
        alt_short_enabled=os.getenv("CRYPTO_SWAP_ALT_SHORT_ENABLED", "true").lower()
        == "true",
        allow_major_short=os.getenv("CRYPTO_SWAP_ALLOW_MAJOR_SHORT", "false").lower()
        == "true",
        excluded_bases=tuple(
            item.strip().upper()
            for item in os.getenv("CRYPTO_SWAP_ALT_SHORT_EXCLUDED_BASES", "BTC,ETH").split(",")
            if item.strip()
        )
        or ("BTC", "ETH"),
        score_threshold=float(os.getenv("CRYPTO_SWAP_MF_SCORE_THRESHOLD", "65")),
        relative_weakness_threshold=float(
            os.getenv("CRYPTO_SWAP_MF_RELATIVE_WEAKNESS", "-0.015")
        ),
        min_volume_ratio=float(os.getenv("CRYPTO_SWAP_MF_MIN_VOLUME_RATIO", "0.80")),
        max_negative_funding_for_short=float(
            os.getenv("CRYPTO_SWAP_MF_MAX_NEGATIVE_FUNDING_SHORT", "-0.001")
        ),
        initial_margin_fraction=float(
            os.getenv("CRYPTO_SWAP_ALT_SHORT_INITIAL_MARGIN_FRACTION", "0.35")
        ),
        recovery_add_loss_steps=_float_tuple(
            os.getenv("CRYPTO_SWAP_ALT_SHORT_ADD_LOSS_STEPS", "0.03,0.06,0.10")
        ),
        recovery_add_margin_multipliers=_float_tuple(
            os.getenv("CRYPTO_SWAP_ALT_SHORT_ADD_MARGIN_MULTIPLIERS", "1.0,1.2,1.5")
        ),
        max_recovery_layers=int(os.getenv("CRYPTO_SWAP_ALT_SHORT_MAX_LAYERS", "3")),
        recovery_take_profit=float(
            os.getenv("CRYPTO_SWAP_ALT_SHORT_RECOVERY_TAKE_PROFIT", "0.008")
        ),
        max_position_loss=float(os.getenv("CRYPTO_SWAP_ALT_SHORT_MAX_POSITION_LOSS", "0.12")),
        trend_invalidation_score=float(
            os.getenv("CRYPTO_SWAP_MF_TREND_INVALIDATION_SCORE", "45")
        ),
        benchmark_symbol=os.getenv("CRYPTO_SWAP_MF_BENCHMARK_SYMBOL", "BTC-USDT-SWAP").upper(),
    )


def _float_tuple(text: str) -> tuple[float, ...]:
    values = []
    for item in str(text).replace(";", ",").split(","):
        if item.strip():
            values.append(float(item.strip()))
    return tuple(values)


def _base(symbol: str) -> str:
    return str(symbol).upper().split("-")[0]


def _confirmed(bars: pd.DataFrame | None) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    if "confirm" in frame:
        frame = frame[frame["confirm"].astype(bool)].copy()
    return frame.sort_values("timestamp").reset_index(drop=True)


def _return(close: pd.Series, lookback: int) -> float:
    if len(close) <= lookback:
        return 0.0
    start = float(close.iloc[-lookback])
    end = float(close.iloc[-1])
    return end / start - 1 if start > 0 else 0.0


def _volume_ratio(frame: pd.DataFrame, window: int = 48) -> float:
    if "volume" not in frame or len(frame) < 3:
        return 0.0
    volume = frame["volume"].astype(float)
    baseline = float(volume.iloc[-window - 1 : -1].mean()) if len(volume) > window else float(volume.iloc[:-1].mean())
    return float(volume.iloc[-1]) / baseline if baseline > 0 else 0.0


def _realized_vol(close: pd.Series, window: int = 72) -> float:
    returns = close.pct_change().dropna()
    if returns.empty:
        return 0.0
    return float(returns.tail(window).std(ddof=0) * math.sqrt(24 * 365))


def _ema_alignment_score(close: pd.Series, direction: str) -> tuple[float, dict]:
    if len(close) < 80:
        return 0.0, {"ema_alignment": False}
    ema_fast = float(close.ewm(span=24, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=72, adjust=False).mean().iloc[-1])
    ema_trend = float(close.ewm(span=min(180, len(close)), adjust=False).mean().iloc[-1])
    if direction == "SHORT":
        passed = ema_fast < ema_slow <= ema_trend
    else:
        passed = ema_fast > ema_slow >= ema_trend
    return (20.0 if passed else 0.0), {
        "ema_alignment": passed,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_trend": ema_trend,
    }


def _benchmark_relative_return(
    frame: pd.DataFrame,
    benchmark_bars: pd.DataFrame | None,
    lookback: int = 72,
) -> float:
    own = _return(frame["close"].astype(float), lookback)
    benchmark = _confirmed(benchmark_bars)
    if benchmark.empty:
        return own
    bench_close = benchmark["close"].astype(float)
    return own - _return(bench_close, min(lookback, len(bench_close) - 1))


def _short_signal_score(
    frame: pd.DataFrame,
    htf: pd.DataFrame,
    daily: pd.DataFrame,
    benchmark_bars: pd.DataFrame | None,
    funding_rate: float,
    mf_config: CryptoSwapManagedFuturesConfig,
) -> tuple[float, dict, list[dict]]:
    close = frame["close"].astype(float)
    returns = {
        "ret_24h": _return(close, 24),
        "ret_72h": _return(close, 72),
        "ret_120h": _return(close, 120),
    }
    relative = _benchmark_relative_return(frame, benchmark_bars, 72)
    volume_ratio = _volume_ratio(frame)
    vol = _realized_vol(close)
    htf_score, htf_metrics = _ema_alignment_score(htf["close"].astype(float), "SHORT") if not htf.empty else (0.0, {"ema_alignment": False})
    daily_score, daily_metrics = _ema_alignment_score(daily["close"].astype(float), "SHORT") if not daily.empty else (0.0, {"ema_alignment": False})
    momentum_score = 0.0
    momentum_score += 15.0 if returns["ret_24h"] < 0 else 0.0
    momentum_score += 15.0 if returns["ret_72h"] < -0.01 else 0.0
    momentum_score += 10.0 if returns["ret_120h"] < -0.015 else 0.0
    relative_score = 20.0 if relative <= mf_config.relative_weakness_threshold else 0.0
    volume_score = 10.0 if volume_ratio >= mf_config.min_volume_ratio else 0.0
    funding_score = 10.0 if funding_rate >= mf_config.max_negative_funding_for_short else 0.0
    vol_penalty = -15.0 if vol > 2.5 else 0.0
    score = max(0.0, min(100.0, momentum_score + relative_score + volume_score + funding_score + htf_score + daily_score + vol_penalty))
    metrics = {
        **returns,
        "relative_weakness": relative,
        "volume_ratio": volume_ratio,
        "realized_volatility": vol,
        "funding_rate": funding_rate,
        "managed_short_score": score,
        "htf_ema_alignment": htf_metrics.get("ema_alignment", False),
        "daily_ema_alignment": daily_metrics.get("ema_alignment", False),
        "entry_model": "山寨币空头恢复",
        "strategy_engine": "crypto_swap_managed_futures",
    }
    checks = [
        {
            "name": "山寨币相对弱势",
            "current": f"{relative:+.2%}",
            "target": f"≤ {mf_config.relative_weakness_threshold:.2%}",
            "passed": relative <= mf_config.relative_weakness_threshold,
        },
        {
            "name": "成交量可交易",
            "current": f"{volume_ratio:.2f}",
            "target": f"≥ {mf_config.min_volume_ratio:.2f}",
            "passed": volume_ratio >= mf_config.min_volume_ratio,
        },
        {
            "name": "资金费率不过度不利",
            "current": f"{funding_rate:.4%}",
            "target": f"≥ {mf_config.max_negative_funding_for_short:.4%}",
            "passed": funding_rate >= mf_config.max_negative_funding_for_short,
        },
        {
            "name": "4H/高周期空头排列",
            "current": "通过" if htf_metrics.get("ema_alignment") else "未通过",
            "target": "EMA 快 < 慢 ≤ 趋势",
            "passed": bool(htf_metrics.get("ema_alignment")),
        },
        {
            "name": "日线空头排列",
            "current": "通过" if daily_metrics.get("ema_alignment") else "未通过",
            "target": "EMA 快 < 慢 ≤ 趋势",
            "passed": bool(daily_metrics.get("ema_alignment")),
        },
        {
            "name": "综合评分",
            "current": f"{score:.1f}",
            "target": f"≥ {mf_config.score_threshold:.1f}",
            "passed": score >= mf_config.score_threshold,
        },
    ]
    return score, metrics, checks


def _next_recovery_layer(pnl: float, layer: int, mf_config: CryptoSwapManagedFuturesConfig) -> int | None:
    max_layers = min(mf_config.max_recovery_layers, len(mf_config.recovery_add_loss_steps))
    if layer >= max_layers:
        return None
    threshold = mf_config.recovery_add_loss_steps[layer]
    return layer + 1 if pnl <= -threshold else None


def decide_crypto_swap_managed_futures(
    symbol: str,
    bars: pd.DataFrame,
    position_contracts: float,
    average_price: float,
    base_config: CryptoSwapConfig | None = None,
    mf_config: CryptoSwapManagedFuturesConfig | None = None,
    *,
    peak_pnl: float = 0.0,
    partial_stage: int = 0,
    held_hours: float = 0.0,
    recovery_layer: int = 0,
    funding_rate: float = 0.0,
    liquidation_buffer: float | None = None,
    higher_timeframe_bars: pd.DataFrame | None = None,
    daily_bars: pd.DataFrame | None = None,
    benchmark_bars: pd.DataFrame | None = None,
) -> CryptoSwapDecision:
    base_config = base_config or CryptoSwapConfig()
    mf_config = mf_config or CryptoSwapManagedFuturesConfig()
    frame = _confirmed(bars)
    required = max(base_config.slow_window + 2, 130)
    if len(frame) < required:
        return CryptoSwapDecision("HOLD", f"样本不足：{len(frame)}/{required}")
    htf = _confirmed(higher_timeframe_bars)
    daily = _confirmed(daily_bars)
    score, metrics, checks = _short_signal_score(
        frame, htf, daily, benchmark_bars, funding_rate, mf_config
    )
    last = float(frame["close"].astype(float).iloc[-1])
    metrics.update({
        "last": last,
        "recovery_layer": recovery_layer,
        "liquidation_buffer": liquidation_buffer,
    })
    symbol_base = _base(symbol)
    alt_short_allowed = (
        mf_config.alt_short_enabled
        and (mf_config.allow_major_short or symbol_base not in set(mf_config.excluded_bases) | MAJOR_BASES)
    )
    checks.append({
        "name": "山寨币做空范围",
        "current": symbol_base,
        "target": f"不在排除列表 {','.join(mf_config.excluded_bases)}",
        "passed": alt_short_allowed,
    })
    short_signal = alt_short_allowed and score >= mf_config.score_threshold
    if position_contracts:
        pnl = (
            last / average_price - 1
            if position_contracts > 0 else average_price / last - 1
        ) if average_price > 0 else 0.0
        metrics["pnl_ratio"] = pnl
        if position_contracts > 0:
            return CryptoSwapDecision(
                "CLOSE_LONG",
                "新合约主策略只管理山寨币空头：检测到多仓，先退出",
                metrics=metrics,
                checks=checks,
            )
        if pnl <= -mf_config.max_position_loss:
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                f"山寨币空头恢复硬止损：当前亏损 {pnl:.2%}",
                metrics=metrics,
                checks=checks,
            )
        if (
            liquidation_buffer is not None
            and liquidation_buffer <= base_config.liquidation_buffer_exit
        ):
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                (
                    f"强平缓冲过低：当前 {liquidation_buffer:.2%} ≤ "
                    f"{base_config.liquidation_buffer_exit:.2%}"
                ),
                metrics=metrics,
                checks=checks,
            )
        if pnl >= mf_config.recovery_take_profit:
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                f"山寨币空头恢复转正止盈：当前盈利 {pnl:.2%}",
                metrics=metrics,
                checks=checks,
            )
        if partial_stage < 1 and pnl >= base_config.partial_profit_tier1:
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                f"合约第一档分批止盈：当前盈利 {pnl:.2%}",
                metrics=metrics,
                checks=checks,
                quantity_fraction=base_config.partial_profit_tier1_fraction,
            )
        if peak_pnl >= base_config.short_profit_protection_start:
            protection_line = peak_pnl * (1 - base_config.short_profit_giveback)
            if pnl <= protection_line:
                return CryptoSwapDecision(
                    "CLOSE_SHORT",
                    f"合约盈利保护触发：峰值 {peak_pnl:.2%}，当前 {pnl:.2%}",
                    metrics=metrics,
                    checks=checks,
                )
        if funding_rate < -base_config.funding_exit_rate:
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                f"空头资金费率恶化退出：当前 {funding_rate:.4%}",
                metrics=metrics,
                checks=checks,
            )
        if score < mf_config.trend_invalidation_score and held_hours >= base_config.min_hold_hours:
            return CryptoSwapDecision(
                "CLOSE_SHORT",
                f"空头趋势质量失效：评分 {score:.1f} < {mf_config.trend_invalidation_score:.1f}",
                metrics=metrics,
                checks=checks,
            )
        next_layer = _next_recovery_layer(pnl, recovery_layer, mf_config)
        if next_layer is not None:
            if short_signal:
                multiplier = mf_config.recovery_add_margin_multipliers[min(
                    next_layer - 1, len(mf_config.recovery_add_margin_multipliers) - 1
                )]
                return CryptoSwapDecision(
                    "OPEN_SHORT",
                    (
                        f"山寨币空头恢复第 {next_layer} 层补仓：亏损 {pnl:.2%}，"
                        f"弱势评分 {score:.1f} 仍通过"
                    ),
                    target_notional=base_config.margin_budget
                    * base_config.leverage
                    * mf_config.initial_margin_fraction
                    * multiplier,
                    metrics={**metrics, "next_recovery_layer": next_layer},
                    checks=checks,
                )
            return CryptoSwapDecision(
                "HOLD",
                (
                    f"达到第 {recovery_layer + 1} 层补仓亏损区间，但弱势条件未通过，"
                    "不机械补仓"
                ),
                metrics=metrics,
                checks=checks,
            )
        return CryptoSwapDecision(
            "HOLD",
            "山寨币空头持仓继续恢复管理",
            metrics=metrics,
            checks=checks,
        )
    if not base_config.entry_enabled:
        return CryptoSwapDecision(
            "HOLD",
            "合约新开仓已关闭：新主策略只做风险受限验证，不新增仓位",
            metrics=metrics,
            checks=checks,
        )
    if not alt_short_allowed:
        return CryptoSwapDecision(
            "HOLD",
            f"{symbol_base} 在山寨币空头排除范围内，禁止开空",
            metrics=metrics,
            checks=checks,
        )
    if not short_signal:
        return CryptoSwapDecision(
            "HOLD",
            f"山寨币空头弱势评分不足：{score:.1f}/{mf_config.score_threshold:.1f}",
            metrics=metrics,
            checks=checks,
        )
    return CryptoSwapDecision(
        "OPEN_SHORT",
        f"山寨币空头恢复首仓：弱势评分 {score:.1f} 通过，小批量开空",
        target_notional=base_config.margin_budget
        * base_config.leverage
        * mf_config.initial_margin_fraction,
        metrics=metrics,
        checks=checks,
    )


__all__ = [
    "CryptoSwapManagedFuturesConfig",
    "decide_crypto_swap_managed_futures",
    "managed_futures_config_from_env",
]
