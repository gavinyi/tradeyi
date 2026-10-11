from __future__ import annotations

from math import sqrt
import os

import pandas as pd


def market_number(value, default: float = 0.0) -> float:
    """Normalize Futu numeric fields, including the literal N/A placeholder."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(number) else number


def normalize_iv(value: float) -> float:
    value = market_number(value)
    return value / 100 if value > 3 else value


def realized_volatility(candles: pd.DataFrame, window: int = 60) -> float:
    if candles.empty or "close" not in candles.columns:
        return 0.0
    returns = candles["close"].astype(float).pct_change().dropna().tail(window)
    if len(returns) >= 5:
        std = returns.std()
        if pd.notna(std) and std > 0:
            return float(std * sqrt(252))
    return 0.0


def contract_analysis(
    row, spot: float, realized_vol: float, store=None,
    peer_contracts: pd.DataFrame | None = None,
) -> dict:
    iv = normalize_iv(row.get("option_implied_volatility", 0))
    dte = max(1, int(row.get("option_expiry_date_distance", 1) or 1))
    symbol = str(row.get("code", ""))
    history = store.metric_history(symbol, "iv", 500) if store and symbol else []
    sample_values = history
    sample_source = "同一合约历史"
    sample_confidence = "高" if len(history) >= 60 else "中"
    if len(history) < 20 and peer_contracts is not None and not peer_contracts.empty:
        peers = peer_contracts.copy()
        option_type = str(row.get("option_type", "")).upper()
        current_delta = abs(market_number(row.get("option_delta")))
        strike = market_number(row.get("strike_price"))
        current_moneyness = strike / spot - 1 if spot > 0 and strike > 0 else 0.0
        peer_dte = pd.to_numeric(
            peers.get("option_expiry_date_distance"), errors="coerce"
        )
        peer_delta = pd.to_numeric(
            peers.get("option_delta"), errors="coerce"
        ).abs()
        peer_strike = pd.to_numeric(peers.get("strike_price"), errors="coerce")
        peer_moneyness = peer_strike / spot - 1 if spot > 0 else peer_strike * 0
        peer_iv = peers.get(
            "option_implied_volatility", pd.Series(index=peers.index, dtype=float)
        ).map(normalize_iv)
        same_type = peers.get("option_type", "").astype(str).str.upper().eq(option_type)
        tiers = (
            ("严格同类合约横截面", 7, .15, .05, "中"),
            ("扩展同类合约横截面", 14, .25, .10, "低"),
            ("同方向近月合约横截面", 21, 1.00, .15, "低"),
        )
        for source, dte_range, delta_range, moneyness_range, confidence in tiers:
            comparable = peers[
                same_type
                & peer_dte.sub(dte).abs().le(dte_range)
                & peer_delta.sub(current_delta).abs().le(delta_range)
                & peer_moneyness.sub(current_moneyness).abs().le(moneyness_range)
                & peer_iv.gt(0)
            ]
            peer_values = comparable["option_implied_volatility"].map(normalize_iv).tolist()
            if len(peer_values) >= 10:
                sample_values = peer_values
                sample_source = source
                sample_confidence = confidence if len(peer_values) < 30 else "中"
                break
    percentile_ready = len(sample_values) >= 10 and (
        len(history) >= 20 or "横截面" in sample_source
    )
    percentile = (
        sum(value <= iv for value in sample_values) / len(sample_values)
        if percentile_ready else 0.5
    )
    if store and symbol and iv > 0:
        store.record_metric(symbol, "iv", iv)
    return {
        "隐含波动率": iv, "历史实现波动率": realized_vol,
        "IV/RV": iv / realized_vol if realized_vol > 0 else 0,
        "IV历史百分位": percentile,
        "IV历史样本数": len(sample_values),
        "IV百分位可用": percentile_ready,
        "IV样本来源": sample_source if percentile_ready else "样本不足",
        "IV样本置信度": sample_confidence if percentile_ready else "不足",
        "IV变化": iv - history[-1] if history else 0,
        "预期波动金额": spot * iv * sqrt(dte / 365),
        "预期波动比例": iv * sqrt(dte / 365),
    }


def iv_entry_allowed(analysis: dict, trigger_mode: str = "EARNINGS") -> tuple[bool, str]:
    volatility = str(trigger_mode).upper() == "VOLATILITY"
    max_ratio = float(os.getenv(
        "GAMMA_VOLATILITY_MAX_IV_RV" if volatility else "GAMMA_MAX_IV_RV",
        "2.5" if volatility else "2.5",
    ))
    max_percentile = float(os.getenv(
        "GAMMA_VOLATILITY_MAX_IV_PERCENTILE" if volatility else "GAMMA_MAX_IV_PERCENTILE",
        "0.85" if volatility else "0.95",
    ))
    max_absolute_iv = float(os.getenv(
        "GAMMA_VOLATILITY_MAX_ABSOLUTE_IV" if volatility else "GAMMA_MAX_ABSOLUTE_IV",
        "0.85" if volatility else "1.50",
    ))
    iv = float(analysis.get("隐含波动率") or 0.0)
    if iv > max_absolute_iv:
        return False, f"隐含波动率 {iv:.1%} 超过绝对上限 {max_absolute_iv:.1%}，期权溢价过高易遭剧烈磨损"
    if analysis.get("IV/RV", 0) > max_ratio:
        return False, f"IV/RV {analysis['IV/RV']:.2f} 超过 {max_ratio:.2f}，IV Crush 风险过高"
    if analysis.get("IV历史百分位", 0) > max_percentile:
        return False, f"IV 百分位 {analysis['IV历史百分位']:.1%} 过高"
    return True, f"{'波动行情' if volatility else '财报事件'} IV 风控通过"


def portfolio_greeks(positions: pd.DataFrame, quotes: pd.DataFrame) -> dict:
    if positions.empty or quotes.empty:
        return {"Delta": 0.0, "Gamma": 0.0, "Vega": 0.0, "Theta": 0.0}
    merged = positions[["code", "qty"]].merge(quotes, on="code", how="inner")
    totals = {}
    for greek in ("delta", "gamma", "vega", "theta"):
        column = f"option_{greek}"
        multiplier = merged.get("option_contract_multiplier", pd.Series(100, index=merged.index))
        values = merged.get(column, pd.Series(0, index=merged.index)).fillna(0)
        totals[greek.title()] = float((merged["qty"] * multiplier * values).sum())
    return totals


def gamma_add_allowed(current: dict, option, quantity: int) -> tuple[bool, str]:
    multiplier = market_number(option.get("option_contract_multiplier", 100), 100)
    projected = {
        greek: abs(market_number(current.get(greek, 0)) + quantity * multiplier * market_number(
            option.get(f"option_{greek.lower()}", 0)
        )) for greek in ("Delta", "Gamma", "Vega")
    }
    limits = {
        "Delta": float(os.getenv("GAMMA_MAX_ABS_DELTA", "1000")),
        "Gamma": float(os.getenv("GAMMA_MAX_ABS_GAMMA", "500")),
        "Vega": float(os.getenv("GAMMA_MAX_ABS_VEGA", "50000")),
    }
    for greek, value in projected.items():
        if value > limits[greek]:
            return False, f"组合 {greek} 敞口 {value:.2f} 超过上限 {limits[greek]:.2f}"
    return True, "Greeks 风控通过"
