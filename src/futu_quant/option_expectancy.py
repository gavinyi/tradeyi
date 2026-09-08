from __future__ import annotations

from math import sqrt
from statistics import NormalDist

import pandas as pd

from .gamma_risk import market_number, normalize_iv


MODEL_VERSION = "SHORT_HORIZON_GREEKS_V1"


def iv_history_percentile(
    current_iv: float,
    history: pd.DataFrame | list[dict],
    *,
    minimum_samples: int = 30,
) -> dict[str, object]:
    """Return a point-in-time IV percentile without pretending missing data is 50%."""
    history = history if isinstance(history, pd.DataFrame) else pd.DataFrame(history)
    current = normalize_iv(current_iv)
    if history.empty or "iv" not in history:
        return {
            "percentile": None, "sample_count": 0,
            "status": "INSUFFICIENT", "reason": "没有历史 IV 样本",
        }
    values = pd.to_numeric(history["iv"], errors="coerce").dropna().map(normalize_iv)
    values = values[(values > 0) & (values < 5)]
    if len(values) < minimum_samples or current <= 0:
        return {
            "percentile": None, "sample_count": int(len(values)),
            "status": "INSUFFICIENT",
            "reason": f"历史 IV 样本 {len(values)} 条，至少需要 {minimum_samples} 条",
        }
    percentile = float((values <= current).mean())
    return {
        "percentile": percentile, "sample_count": int(len(values)),
        "status": "GOOD", "reason": f"按 {len(values)} 条同正股历史 IV 计算",
    }


def add_iv_history_percentiles(
    candidates: pd.DataFrame,
    history: pd.DataFrame | list[dict],
    *,
    minimum_samples: int = 30,
) -> pd.DataFrame:
    """Attach a real history percentile, with an explicit chain proxy fallback."""
    history = history if isinstance(history, pd.DataFrame) else pd.DataFrame(history)
    result = candidates.copy()
    if result.empty:
        result["iv_history_percentile"] = pd.Series(dtype=float)
        result["iv_percentile_status"] = pd.Series(dtype=str)
        result["iv_history_samples"] = pd.Series(dtype=int)
        return result
    historical = pd.to_numeric(history.get("iv"), errors="coerce").dropna().map(
        normalize_iv
    ) if not history.empty and "iv" in history else pd.Series(dtype=float)
    historical = historical[(historical > 0) & (historical < 5)]
    current = pd.to_numeric(
        result.get("option_implied_volatility"), errors="coerce"
    ).fillna(0).map(normalize_iv)
    if len(historical) >= minimum_samples:
        result["iv_history_percentile"] = current.map(
            lambda value: float((historical <= value).mean()) if value > 0 else 1.0
        )
        result["iv_percentile_status"] = "HISTORICAL"
        result["iv_history_samples"] = int(len(historical))
    else:
        # A chain percentile is not mislabeled as historical. It is only a
        # conservative same-snapshot proxy until the warehouse has enough data.
        result["iv_history_percentile"] = current.rank(pct=True, method="average")
        result["iv_percentile_status"] = "CHAIN_PROXY"
        result["iv_history_samples"] = int(len(historical))
    return result


def empirical_forward_returns(
    history: pd.DataFrame | list[dict],
    holding_hours: float,
    *,
    tolerance_minutes: float = 30,
) -> list[float]:
    """Build executable ask-to-bid forward returns at the strategy horizon."""
    history = history if isinstance(history, pd.DataFrame) else pd.DataFrame(history)
    if history.empty or not {"time_key", "ask", "bid"}.issubset(history.columns):
        return []
    frame = history.copy()
    frame["time_key"] = pd.to_datetime(frame["time_key"], errors="coerce", utc=True)
    frame["ask"] = pd.to_numeric(frame["ask"], errors="coerce")
    frame["bid"] = pd.to_numeric(frame["bid"], errors="coerce")
    frame = frame.dropna(subset=["time_key", "ask", "bid"]).sort_values("time_key")
    frame = frame[(frame["ask"] > 0) & (frame["bid"] > 0)]
    if len(frame) < 2:
        return []
    future = frame[["time_key", "bid"]].rename(
        columns={"time_key": "future_time", "bid": "future_bid"}
    )
    targets = frame[["time_key", "ask"]].copy()
    targets["target_time"] = targets["time_key"] + pd.to_timedelta(holding_hours, unit="h")
    aligned = pd.merge_asof(
        targets.sort_values("target_time"),
        future.sort_values("future_time"),
        left_on="target_time", right_on="future_time", direction="nearest",
        tolerance=pd.Timedelta(minutes=tolerance_minutes),
    ).dropna(subset=["future_bid"])
    if aligned.empty:
        return []
    return (aligned["future_bid"] / aligned["ask"] - 1).astype(float).tolist()


def short_horizon_option_expectancy(
    row: pd.Series | dict,
    spot: float,
    realized_volatility: float,
    regime: dict[str, object] | None = None,
    *,
    holding_hours: float = 4.0,
    profit_target: float = 0.50,
    stop_loss: float = 0.35,
    history: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Estimate executable option return over the actual 90m-24h holding window.

    The deterministic scenario grid reprices the option with Delta, Gamma, Vega
    and Theta. Entry is paid at Ask and exit is marked at an estimated Bid. If
    enough point-in-time history exists, empirical ask-to-bid returns are blended
    into the scenario estimate.
    """
    item = dict(row)
    spot = max(0.01, float(spot))
    rv = max(0.01, float(realized_volatility or 0.30))
    hours = max(1.5, min(24.0, float(holding_hours or 4.0)))
    ask = market_number(item.get("ask_price"), market_number(item.get("ask")))
    bid = market_number(item.get("bid_price"), market_number(item.get("bid")))
    mid = (bid + ask) / 2 if bid > 0 and ask >= bid else ask
    if ask <= 0 or mid <= 0:
        return {
            "expected_return": -1.0, "profit_probability": 0.0,
            "scenario_expected_return": -1.0, "empirical_expected_return": None,
            "empirical_samples": 0, "holding_hours": hours,
            "model_version": MODEL_VERSION, "reason": "期权 Ask/Bid 无效",
        }

    option_type = str(item.get("option_type", "CALL")).upper()
    delta = market_number(item.get("option_delta", item.get("delta")))
    if option_type == "PUT" and delta > 0:
        delta = -delta
    gamma = abs(market_number(item.get("option_gamma", item.get("gamma"))))
    theta = market_number(item.get("option_theta", item.get("theta")))
    vega = market_number(item.get("option_vega", item.get("vega")))
    iv = normalize_iv(item.get("option_implied_volatility", item.get("iv", 0)))
    regime = regime or {}
    intraday = float(regime.get("intraday_return", 0) or 0)
    direction = 1.0 if option_type == "CALL" else -1.0
    signal_direction = 1.0 if intraday > 0 else -1.0 if intraday < 0 else direction
    aligned = signal_direction == direction

    year_fraction = hours / (252.0 * 6.5)
    sigma = rv * sqrt(year_fraction)
    signal_strength = min(1.25, max(0.0, abs(intraday) / max(sigma, 0.005)))
    drift = direction * sigma * (0.20 + 0.35 * signal_strength) if aligned else 0.0
    if not bool(regime.get("active")):
        drift *= 0.35
    iv_mean_reversion = (rv - iv) * min(0.35, hours / 48.0)
    shock_persistence = abs(intraday) * (0.12 if bool(regime.get("active")) else 0.0)
    iv_change = iv_mean_reversion + shock_persistence

    normal = NormalDist()
    returns: list[float] = []
    exit_bids: list[float] = []
    for index in range(61):
        probability = (index + 0.5) / 61
        underlying_return = drift + sigma * normal.inv_cdf(probability)
        price_move = spot * underlying_return
        option_change = (
            delta * price_move
            + 0.5 * gamma * price_move * price_move
            + vega * iv_change * 100
            + theta * (hours / 24.0)
        )
        theoretical_exit = max(0.0, mid + option_change)
        half_spread = max(0.0, ask - bid) / 2
        executable_exit = max(0.0, theoretical_exit - half_spread)
        exit_bids.append(executable_exit)
        raw_return = executable_exit / ask - 1
        returns.append(max(-float(stop_loss), min(float(profit_target), raw_return)))
    scenario_expected = float(sum(returns) / len(returns))
    scenario_probability = float(sum(value > 0 for value in returns) / len(returns))

    empirical = empirical_forward_returns(history, hours) if history is not None else []
    empirical_expected = float(pd.Series(empirical).mean()) if empirical else None
    if len(empirical) >= 20:
        weight = min(0.70, len(empirical) / 100)
        expected = scenario_expected * (1 - weight) + float(empirical_expected) * weight
        empirical_probability = float((pd.Series(empirical) > 0).mean())
        profit_probability = scenario_probability * (1 - weight) + empirical_probability * weight
        source = "情景重估与历史逐笔混合"
    else:
        expected = scenario_expected
        profit_probability = scenario_probability
        source = "短周期 Greeks 情景重估"
    return {
        "expected_return": float(expected),
        "profit_probability": float(profit_probability),
        "scenario_expected_return": scenario_expected,
        "empirical_expected_return": empirical_expected,
        "empirical_samples": len(empirical),
        "holding_hours": hours,
        "model_version": MODEL_VERSION,
        "expected_exit_bid": float(sum(exit_bids) / len(exit_bids)),
        "expected_exit_ask": float(sum(exit_bids) / len(exit_bids)) + max(0.0, ask - bid),
        "reason": f"{source}；买入 Ask {ask:.2f}，退出按 Bid/价差计费",
    }
