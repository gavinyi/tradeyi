from __future__ import annotations

from math import sqrt

import pandas as pd

from .gamma_risk import market_number


STRUCTURE_LABELS = {
    "LONG_CALL": "买入认购", "LONG_PUT": "买入认沽",
    "LONG_STRADDLE": "买入跨式", "LONG_STRANGLE": "买入宽跨式",
    "CALL_DEBIT_SPREAD": "认购借方价差", "PUT_DEBIT_SPREAD": "认沽借方价差",
}


def earnings_profile(candles: pd.DataFrame) -> dict[str, float]:
    """Build a no-lookahead proxy profile from the available daily history."""
    ordered = candles.sort_values("time_key").copy() if "time_key" in candles else candles.copy()
    close = ordered["close"].astype(float)
    open_ = ordered.get("open", close).astype(float)
    gaps = (open_ / close.shift(1) - 1).dropna().tail(252)
    returns = close.pct_change().dropna().tail(252)
    absolute = gaps.abs()
    return {
        "history_samples": float(len(absolute)),
        "median_gap": float(absolute.median()) if len(absolute) else 0.0,
        "p75_gap": float(absolute.quantile(0.75)) if len(absolute) else 0.0,
        "p90_gap": float(absolute.quantile(0.90)) if len(absolute) else 0.0,
        "up_gap_ratio": float((gaps > 0).mean()) if len(gaps) else 0.5,
        "realized_volatility": float(returns.std() * sqrt(252)) if len(returns) >= 10 else 0.0,
        "momentum_5d": float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0.0,
        "trend_20d": float(close.iloc[-1] / close.tail(20).mean() - 1) if len(close) >= 20 else 0.0,
    }


def opportunity_score(
    profile: dict[str, float], implied_move: float, spread_pct: float,
    open_interest: float, direction_score: float, confirmed: bool,
) -> dict[str, object]:
    """Explainable 0-100 score; no score can override calendar validation or risk controls."""
    historical = float(profile.get("p75_gap", 0))
    edge = historical - max(0.0, float(implied_move))
    history_points = min(15.0, float(profile.get("history_samples", 0)) / 252 * 15)
    edge_points = max(0.0, min(35.0, 17.5 + edge * 700))
    liquidity_points = max(0.0, 20.0 - max(0.0, float(spread_pct) - 3) * 1.5)
    liquidity_points += min(10.0, max(0.0, float(open_interest)) / 1000 * 10)
    direction_points = min(10.0, abs(float(direction_score)) / 3 * 10)
    confirmation_points = 10.0 if confirmed else 0.0
    score = min(100.0, history_points + edge_points + liquidity_points + direction_points + confirmation_points)
    reasons = [
        f"历史75分位跳空 {historical:.2%}", f"期权隐含波动 {implied_move:.2%}",
        f"历史相对隐含优势 {edge:+.2%}", f"买卖价差 {spread_pct:.2f}%",
        f"未平仓量 {int(open_interest)}",
    ]
    minimum = 60.0
    return {
        "opportunity_score": score, "minimum_score": minimum,
        "expected_move_edge": edge, "trade_allowed": bool(confirmed and score >= minimum and edge > 0),
        "score_reason": "；".join(reasons),
    }


def build_structure_candidates(
    candidates: pd.DataFrame, spot: float, profile: dict[str, float],
    direction: str | None, confirmed: bool,
) -> pd.DataFrame:
    """Create defined-risk earnings structures from a screened, single-expiry chain."""
    if candidates.empty:
        return pd.DataFrame()
    frame = candidates.copy()
    frame["distance"] = (frame["strike_price"].astype(float) - spot).abs()
    calls = frame[frame["option_type"].astype(str).str.upper() == "CALL"].sort_values("distance")
    puts = frame[frame["option_type"].astype(str).str.upper() == "PUT"].sort_values("distance")
    structures: list[dict] = []

    def add(
        kind: str, legs: list[dict], debit: float, spread: float, oi: float,
        implied_move: float | None = None,
    ) -> None:
        implied = debit / spot if implied_move is None and spot > 0 else float(implied_move or 0)
        score = opportunity_score(profile, implied, spread, oi, 3 if direction else 0, confirmed)
        structures.append({
            "structure": kind, "structure_label": STRUCTURE_LABELS[kind], "legs": legs,
            "net_debit": debit, "implied_move": implied, "max_loss": debit * 100,
            "spread_pct": spread, "open_interest": oi, **score,
        })

    if not calls.empty:
        call = calls.iloc[0]
        debit = market_number(call.ask_price)
        breakeven_move = max(0.0, (market_number(call.strike_price) + debit - spot) / spot)
        add("LONG_CALL", [{"code": call.code, "side": "BUY", "ratio": 1}],
            debit, market_number(call.spread_pct, 100), market_number(call.option_open_interest), breakeven_move)
    if not puts.empty:
        put = puts.iloc[0]
        debit = market_number(put.ask_price)
        breakeven_move = max(0.0, (spot - (market_number(put.strike_price) - debit)) / spot)
        add("LONG_PUT", [{"code": put.code, "side": "BUY", "ratio": 1}],
            debit, market_number(put.spread_pct, 100), market_number(put.option_open_interest), breakeven_move)
    if not calls.empty and not puts.empty:
        call, put = calls.iloc[0], puts.iloc[0]
        debit = market_number(call.ask_price) + market_number(put.ask_price)
        add("LONG_STRADDLE", [
            {"code": call.code, "side": "BUY", "ratio": 1},
            {"code": put.code, "side": "BUY", "ratio": 1},
        ], debit, max(market_number(call.spread_pct, 100), market_number(put.spread_pct, 100)),
            min(market_number(call.option_open_interest), market_number(put.option_open_interest)))
        otm_call = calls[calls["strike_price"].astype(float) > spot]
        otm_put = puts[puts["strike_price"].astype(float) < spot]
        if not otm_call.empty and not otm_put.empty:
            call, put = otm_call.iloc[0], otm_put.iloc[0]
            debit = market_number(call.ask_price) + market_number(put.ask_price)
            add("LONG_STRANGLE", [
                {"code": call.code, "side": "BUY", "ratio": 1},
                {"code": put.code, "side": "BUY", "ratio": 1},
            ], debit, max(market_number(call.spread_pct, 100), market_number(put.spread_pct, 100)),
                min(market_number(call.option_open_interest), market_number(put.option_open_interest)))
    chosen = str(direction or "").upper()
    directional = calls if chosen == "CALL" else puts if chosen == "PUT" else pd.DataFrame()
    if len(directional) >= 2:
        long = directional.iloc[0]
        further = directional[
            directional["strike_price"].astype(float) > float(long.strike_price)
            if chosen == "CALL" else directional["strike_price"].astype(float) < float(long.strike_price)
        ]
        if not further.empty:
            short = further.iloc[0]
            debit = max(0.01, market_number(long.ask_price) - market_number(short.bid_price))
            breakeven = (
                market_number(long.strike_price) + debit
                if chosen == "CALL" else market_number(long.strike_price) - debit
            )
            breakeven_move = abs(breakeven - spot) / spot if spot > 0 else 0.0
            add(f"{chosen}_DEBIT_SPREAD", [
                {"code": long.code, "side": "BUY", "ratio": 1},
                {"code": short.code, "side": "SELL", "ratio": 1},
            ], debit, max(market_number(long.spread_pct, 100), market_number(short.spread_pct, 100)),
                min(market_number(long.option_open_interest), market_number(short.option_open_interest)),
                breakeven_move)
    result = pd.DataFrame(structures)
    if result.empty:
        return result
    # Directional structures are only eligible when they agree with the signal.
    if chosen == "CALL":
        result.loc[result.structure == "LONG_PUT", "trade_allowed"] = False
    elif chosen == "PUT":
        result.loc[result.structure == "LONG_CALL", "trade_allowed"] = False
    return result.sort_values(
        ["trade_allowed", "opportunity_score", "expected_move_edge"], ascending=[False, False, False]
    ).reset_index(drop=True)


def recommend_timing(score: float, expected_move_edge: float, event_session: str) -> dict[str, object]:
    """Prefer short theta exposure unless the measured edge is unusually strong."""
    strong = float(score) >= 75 and float(expected_move_edge) >= 0.02
    entry_hours = 24 if strong else 6
    max_hold_hours = 8 if event_session == "盘前" else 18
    return {
        "entry_hours": entry_hours, "max_hold_hours": max_hold_hours,
        "timing_reason": (
            "波动优势较强，允许提前一个交易日建立仓位"
            if strong else "波动优势一般，缩短财报前持有时间以控制 Theta 损耗"
        ) + ("；盘前财报最迟当日收盘退出" if event_session == "盘前" else "；盘后财报最迟次日常规时段退出"),
    }
