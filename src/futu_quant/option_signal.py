from __future__ import annotations

import os

import pandas as pd


def directional_metrics(owner, candles: pd.DataFrame) -> dict[str, float]:
    last = float(owner.get("last_price", 0) or 0)
    previous = float(owner.get("prev_close_price", 0) or 0)
    closes = candles["close"].astype(float).dropna() if not candles.empty else pd.Series(dtype=float)
    five_day_base = float(closes.iloc[-6]) if len(closes) >= 6 else previous
    average_20 = float(closes.tail(20).mean()) if len(closes) >= 5 else previous
    return {
        "intraday_return": last / previous - 1 if previous > 0 else 0.0,
        "momentum_5d": last / five_day_base - 1 if five_day_base > 0 else 0.0,
        "trend_20d": last / average_20 - 1 if average_20 > 0 else 0.0,
    }


def automatic_option_direction(
    owner, candles: pd.DataFrame, trigger_mode: str, minimum_move: float | None = None,
) -> tuple[str | None, str, dict[str, float]]:
    metrics = directional_metrics(owner, candles)
    mode = str(trigger_mode).upper()
    if minimum_move is None:
        minimum_move = float(os.getenv(
            "VOLATILITY_MIN_UNDERLYING_MOVE" if mode == "VOLATILITY" else "EARNINGS_MIN_UNDERLYING_MOVE",
            "0.020" if mode == "VOLATILITY" else "0.003",
        ))
    intraday = metrics["intraday_return"]
    if abs(intraday) < minimum_move:
        return None, f"当日涨跌幅 {intraday:.2%} 未达自动方向门槛 {minimum_move:.2%}", metrics
    signs = [
        1 if metrics[name] > 0 else -1 if metrics[name] < 0 else 0
        for name in ("intraday_return", "momentum_5d", "trend_20d")
    ]
    score = sum(signs)
    metrics["direction_score"] = float(score)
    if score >= 2:
        return "CALL", f"看涨确认：日内 {intraday:.2%}、5日 {metrics['momentum_5d']:.2%}、20日趋势 {metrics['trend_20d']:.2%}", metrics
    if score <= -2:
        return "PUT", f"看跌确认：日内 {intraday:.2%}、5日 {metrics['momentum_5d']:.2%}、20日趋势 {metrics['trend_20d']:.2%}", metrics
    return None, f"方向信号分歧：日内 {intraday:.2%}、5日 {metrics['momentum_5d']:.2%}、20日趋势 {metrics['trend_20d']:.2%}", metrics


def configured_direction_allowed(
    owner, candles: pd.DataFrame, option_type: str, trigger_mode: str,
    minimum_move: float | None = None,
) -> tuple[bool, str, dict[str, float]]:
    direction, reason, metrics = automatic_option_direction(
        owner, candles, trigger_mode, minimum_move,
    )
    expected = str(option_type).upper()
    if direction is None:
        return False, reason, metrics
    if direction != expected:
        return False, f"自动方向为 {direction}，与已配置 {expected} 不一致；{reason}", metrics
    return True, reason, metrics


def configured_volatility_direction_allowed(
    owner, candles: pd.DataFrame, option_type: str, regime: dict,
) -> tuple[bool, str, dict[str, float]]:
    """Use the same regime as the radar, with an intraday override for shock moves."""
    metrics = directional_metrics(owner, candles)
    intraday = metrics["intraday_return"]
    score = int(regime.get("direction_score", 0))
    metrics["direction_score"] = float(score)
    strong_override = bool(
        regime.get("strong_signal_override")
        and regime.get("trigger_confirmed")
        and regime.get("volume_confirmed")
    )
    if strong_override:
        direction = "CALL" if intraday > 0 else "PUT"
        if bool(regime.get("direction_confirmed")):
            reason = f"强波动方向确认：日内 {intraday:+.2%}，且方向分 {score:+d}"
        else:
            reason = (
                f"强波动方向覆盖：日内 {intraday:+.2%}，但5日/20日方向分歧，"
                "本轮按低置信度处理"
            )
    elif regime.get("active"):
        direction = "CALL" if score >= 2 else "PUT" if score <= -2 else None
        reason = f"雷达动态状态与方向分共同确认：方向分 {score:+d}"
    else:
        return False, f"动态波动信号未激活：{regime.get('reason', '')}", metrics
    expected = str(option_type).upper()
    if direction != expected:
        return False, f"当前自动方向为 {direction}，与合约方向 {expected} 不一致；{reason}", metrics
    return True, reason, metrics
