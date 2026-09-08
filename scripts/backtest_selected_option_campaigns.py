from __future__ import annotations

import pandas as pd

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.option_signal import automatic_option_direction
from futu_quant.earnings_optimizer import opportunity_score


AA_EVENTS = [
    "2024-01-17", "2024-04-17", "2024-07-17", "2024-10-16",
    "2025-01-22", "2025-04-16", "2025-07-16", "2025-10-22",
    "2026-01-22", "2026-04-22",
]


def option_intrinsic_return(
    exit_underlying_ratio: float, strike_ratio: float, premium_ratio: float,
) -> float:
    return max(exit_underlying_ratio - strike_ratio, 0.0) / premium_ratio - 1.0


def summarize(name: str, returns: pd.Series) -> dict:
    clipped = returns.clip(lower=-0.40, upper=0.50)
    if clipped.empty:
        return {
            "方案": name, "样本数": 0, "胜率": 0.0, "平均收益": 0.0,
            "中位收益": 0.0, "最大回撤": 0.0, "累计复合收益": 0.0,
        }
    curve = (1 + clipped).cumprod()
    drawdown = curve / curve.cummax() - 1
    return {
        "方案": name, "样本数": len(clipped), "胜率": float((clipped > 0).mean()),
        "平均收益": float(clipped.mean()), "中位收益": float(clipped.median()),
        "最大回撤": float(drawdown.min()), "累计复合收益": float(curve.iloc[-1] - 1),
    }


def confirmed_call(frame: pd.DataFrame, index: int, mode: str, threshold: float) -> bool:
    """Apply the production automatic direction rule to end-of-day proxy data."""
    if index < 20:
        return False
    previous = float(frame.loc[index - 1, "close"])
    current = float(frame.loc[index, "close"])
    owner = {"last_price": current, "prev_close_price": previous}
    direction, _, _ = automatic_option_direction(
        owner, frame.loc[:index, ["close"]], mode, minimum_move=threshold,
    )
    return direction == "CALL"


def main() -> None:
    broker = FutuPaperBroker(Settings.load())

    aa = broker.candles_range("US.AA", "2024-01-01", "2026-07-15").copy()
    aa["date"] = pd.to_datetime(aa["time_key"]).dt.strftime("%Y-%m-%d")
    aa = aa.reset_index(drop=True)
    aa_rows = []
    aa_strike_ratio = 45.0 / 48.58
    aa_premium_ratio = 4.40 / 48.58
    aa_breakeven_move = max(0.0, (45.0 + 4.40 - 48.58) / 48.58)
    for event in AA_EVENTS:
        matches = aa.index[aa["date"] == event]
        if len(matches) != 1 or int(matches[0]) + 1 >= len(aa):
            continue
        idx = int(matches[0])
        close, next_open = float(aa.loc[idx, "close"]), float(aa.loc[idx + 1, "open"])
        underlying_return = next_open / close - 1
        proxy_return = option_intrinsic_return(
            next_open / close, aa_strike_ratio, aa_premium_ratio,
        )
        aa_rows.append({
            "event_date": event, "entry_close": close, "next_open": next_open,
            "underlying_return": underlying_return, "option_proxy_return": proxy_return,
            "自动看涨确认": confirmed_call(aa, idx, "EARNINGS", 0.003),
        })
    aa_events = pd.DataFrame(aa_rows)
    aa_events["机会评分"] = 0.0
    aa_events["历史相对隐含波动优势"] = 0.0
    aa_events["新系统允许交易"] = False
    for row_index in aa_events.index:
        # Walk-forward: the current event is excluded from its own historical profile.
        previous_moves = aa_events.loc[:row_index - 1, "underlying_return"].abs()
        if len(previous_moves) < 4:
            continue
        scored = opportunity_score(
            {"p75_gap": float(previous_moves.quantile(.75)), "history_samples": 252},
            aa_breakeven_move, 3.0, 1000, 3.0 if aa_events.loc[row_index, "自动看涨确认"] else 0.0,
            True,
        )
        aa_events.loc[row_index, "机会评分"] = scored["opportunity_score"]
        aa_events.loc[row_index, "历史相对隐含波动优势"] = scored["expected_move_edge"]
        aa_events.loc[row_index, "新系统允许交易"] = bool(
            scored["trade_allowed"] and aa_events.loc[row_index, "自动看涨确认"]
        )

    aapl = broker.candles_range("US.AAPL", "2024-01-01", "2026-07-15").copy()
    aapl = aapl.sort_values("time_key").reset_index(drop=True)
    aapl["daily_return"] = aapl["close"].pct_change()
    aapl["next_close_ratio"] = aapl["close"].shift(-1) / aapl["close"]
    aapl_strike_ratio = 300.0 / 327.50
    aapl_premium_ratio = 27.90 / 327.50
    summaries = [
        summarize("AA 财报事件（优化前）", aa_events["option_proxy_return"]),
        summarize(
            "AA 财报事件（0.3%门槛+自动看涨确认）",
            aa_events.loc[aa_events["自动看涨确认"], "option_proxy_return"],
        ),
        summarize(
            "AA 单腿CALL新系统（评分+方向走步验证）",
            aa_events.loc[aa_events["新系统允许交易"], "option_proxy_return"],
        ),
    ]
    aapl_samples = {}
    for threshold in (0.0, 0.01, 0.015, 0.02):
        sample = aapl[
            (aapl["daily_return"] >= threshold) & aapl["next_close_ratio"].notna()
        ].copy()
        sample["option_proxy_return"] = sample["next_close_ratio"].map(
            lambda ratio: option_intrinsic_return(
                float(ratio), aapl_strike_ratio, aapl_premium_ratio,
            )
        )
        label = f"AAPL 上涨触发≥{threshold:.1%}"
        summaries.append(summarize(label, sample["option_proxy_return"]))
        if threshold > 0:
            confirmed_indices = [
                index for index in sample.index
                if confirmed_call(aapl, int(index), "VOLATILITY", threshold)
            ]
            summaries.append(summarize(
                f"AAPL ≥{threshold:.1%}+自动看涨确认",
                sample.loc[confirmed_indices, "option_proxy_return"],
            ))
        aapl_samples[label] = sample[["time_key", "daily_return", "next_close_ratio", "option_proxy_return"]]

    pd.set_option("display.max_columns", None)
    print("AA_EVENTS")
    print(aa_events.to_string(index=False))
    print("\nSUMMARY")
    print(pd.DataFrame(summaries).to_string(index=False))
    print("\nASSUMPTIONS")
    print({
        "AA_strike_ratio": aa_strike_ratio, "AA_premium_ratio": aa_premium_ratio,
        "AA_breakeven_move": aa_breakeven_move,
        "AAPL_strike_ratio": aapl_strike_ratio, "AAPL_premium_ratio": aapl_premium_ratio,
        "profit_cap": 0.50, "stop_floor": -0.40,
        "note": "Underlying-only intrinsic proxy; no historical option IV, spread or intraday barrier path.",
    })


if __name__ == "__main__":
    main()
