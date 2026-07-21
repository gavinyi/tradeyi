from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.earnings_gamma import screen_gamma_contracts
from futu_quant.earnings_optimizer import build_structure_candidates, earnings_profile, opportunity_score
from futu_quant.option_ranking import rank_long_option_candidates
from futu_quant.option_signal import automatic_option_direction


EVENTS = [
    "2024-01-24", "2024-04-17", "2024-07-18", "2024-10-16",
    "2025-01-22", "2025-04-16", "2025-07-17", "2025-10-15",
    "2026-01-22", "2026-04-16", "2026-07-16",
]


def structure_return(structure: pd.Series, contract_rows: pd.DataFrame, exit_ratio: float, spot: float) -> float:
    by_code = contract_rows.set_index("code")
    value = 0.0
    for leg in structure["legs"]:
        row = by_code.loc[leg["code"]]
        strike_ratio = float(row["strike_price"]) / spot
        intrinsic = (
            max(exit_ratio - strike_ratio, 0.0)
            if str(row["option_type"]).upper() == "CALL"
            else max(strike_ratio - exit_ratio, 0.0)
        )
        value += intrinsic * (1 if leg["side"] == "BUY" else -1) * int(leg.get("ratio", 1))
    debit_ratio = float(structure["net_debit"]) / spot
    return value / debit_ratio - 1 if debit_ratio > 0 else -1.0


def main() -> None:
    broker = FutuPaperBroker(Settings.load())
    symbol = "US.ABT"
    event_date = date(2026, 7, 16)
    expirations = broker.option_expirations(symbol).copy()
    valid = expirations[
        (pd.to_datetime(expirations["strike_time"]).dt.date >= event_date)
        & (pd.to_datetime(expirations["strike_time"]).dt.date <= event_date + timedelta(days=7))
    ]
    expiration = str(valid.sort_values("strike_time").iloc[0]["strike_time"])
    chain = broker.option_chain(symbol, expiration)
    spot = float(broker.snapshots([symbol]).iloc[0]["last_price"])
    quotes = broker.snapshots(chain["code"].tolist())
    screened = pd.concat([
        screen_gamma_contracts(chain, quotes, spot, kind, 100, 20, 10)
        for kind in ("CALL", "PUT")
    ], ignore_index=True)
    ranked = rank_long_option_candidates(screened, spot)
    bars = broker.candles_range(symbol, "2023-10-01", "2026-07-16").sort_values("time_key").reset_index(drop=True)
    profile = earnings_profile(bars)
    live_direction, live_reason, _ = automatic_option_direction(
        broker.snapshots([symbol]).iloc[0], broker.candles(symbol, count=90), "EARNINGS", .003,
    )
    structures = build_structure_candidates(ranked, spot, profile, live_direction, True)
    selected = structures.iloc[0]

    bars["date"] = pd.to_datetime(bars["time_key"]).dt.strftime("%Y-%m-%d")
    rows = []
    for event in EVENTS:
        matches = bars.index[bars["date"] == event]
        if len(matches) != 1 or int(matches[0]) + 1 >= len(bars):
            continue
        idx = int(matches[0])
        close = float(bars.loc[idx, "close"])
        next_open = float(bars.loc[idx + 1, "open"])
        event_return = next_open / close - 1
        direction, _, _ = automatic_option_direction(
            {"last_price": close, "prev_close_price": float(bars.loc[idx - 1, "close"])},
            bars.loc[:idx, ["close"]], "EARNINGS", .003,
        )
        rows.append({
            "财报日期": event, "正股隔夜涨跌": event_return, "自动方向": direction or "不交易",
            "结构代理收益": structure_return(selected, ranked, next_open / close, spot),
        })
    results = pd.DataFrame(rows)
    results["机会评分"] = 0.0
    results["允许交易"] = False
    directional = selected["structure"] not in {"LONG_STRADDLE", "LONG_STRANGLE"}
    expected_direction = "CALL" if "CALL" in selected["structure"] else "PUT" if "PUT" in selected["structure"] else None
    for idx in results.index:
        history = results.loc[:idx - 1, "正股隔夜涨跌"].abs()
        if len(history) < 4:
            continue
        scored = opportunity_score(
            {"p75_gap": float(history.quantile(.75)), "history_samples": 252},
            float(selected["implied_move"]), float(selected["spread_pct"]),
            float(selected["open_interest"]), 3 if results.loc[idx, "自动方向"] != "不交易" else 0, True,
        )
        direction_ok = not directional or results.loc[idx, "自动方向"] == expected_direction
        results.loc[idx, "机会评分"] = scored["opportunity_score"]
        results.loc[idx, "允许交易"] = bool(scored["trade_allowed"] and direction_ok)

    traded = results.loc[results["允许交易"], "结构代理收益"].clip(-.4, .5)
    curve = (1 + traded).cumprod()
    max_drawdown = float((curve / curve.cummax() - 1).min()) if len(curve) else 0.0
    print("LIVE_SELECTION")
    print({
        "symbol": symbol, "spot": spot, "expiration": expiration,
        "direction": live_direction, "direction_reason": live_reason,
        "qualified_contracts": len(ranked), "structure": selected["structure_label"],
        "score": float(selected["opportunity_score"]), "implied_move": float(selected["implied_move"]),
        "historical_p75_gap": profile["p75_gap"], "trade_allowed": bool(selected["trade_allowed"]),
    })
    print("\nWALK_FORWARD")
    print(results.to_string(index=False))
    print("\nSUMMARY")
    print({
        "events": len(results), "trades": len(traded),
        "win_rate": float((traded > 0).mean()) if len(traded) else 0.0,
        "average_return": float(traded.mean()) if len(traded) else 0.0,
        "median_return": float(traded.median()) if len(traded) else 0.0,
        "max_drawdown": max_drawdown,
        "compound_return": float(curve.iloc[-1] - 1) if len(curve) else 0.0,
    })
    print("\nLIMITATION")
    print("Current-chain normalized intrinsic proxy; historical IV, spreads and intraday barriers unavailable.")


if __name__ == "__main__":
    main()
