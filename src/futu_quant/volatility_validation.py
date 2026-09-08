from __future__ import annotations

import pandas as pd

from .option_expectancy import empirical_forward_returns


def volatility_walk_forward(
    histories: dict[str, pd.DataFrame | list[dict]],
    *,
    holding_hours: float = 4.0,
    windows: int = 3,
    minimum_returns: int = 20,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Validate executable ask-to-bid returns in chronological OOS windows."""
    observations: list[dict[str, object]] = []
    for code, history in histories.items():
        values = empirical_forward_returns(history, holding_hours)
        for sequence, value in enumerate(values):
            observations.append({
                "code": code, "sequence": sequence,
                "return": float(value),
            })
    frame = pd.DataFrame(observations)
    if frame.empty or len(frame) < minimum_returns:
        summary = {
            "passed": False, "status": "INSUFFICIENT",
            "sample_count": int(len(frame)), "window_count": 0,
            "holding_hours": float(holding_hours),
            "reason": f"可配对的短周期逐笔收益只有 {len(frame)} 条，至少需要 {minimum_returns} 条",
        }
        return pd.DataFrame(), summary
    frame = frame.sort_values(["sequence", "code"]).reset_index(drop=True)
    frame["window"] = pd.qcut(
        frame.index, q=min(windows, len(frame)), labels=False, duplicates="drop"
    ) + 1
    rows = []
    for window, group in frame.groupby("window"):
        positive = group[group["return"] > 0]["return"].sum()
        negative = -group[group["return"] < 0]["return"].sum()
        rows.append({
            "window": int(window), "sample_count": int(len(group)),
            "mean_return": float(group["return"].mean()),
            "median_return": float(group["return"].median()),
            "win_rate": float((group["return"] > 0).mean()),
            "profit_factor": float(positive / negative) if negative > 0 else float("inf"),
            "worst_return": float(group["return"].min()),
        })
    result = pd.DataFrame(rows)
    stable_windows = int((result["mean_return"] > 0).sum())
    passed = bool(
        len(frame) >= minimum_returns
        and float(frame["return"].mean()) > 0
        and stable_windows >= max(1, len(result) - 1)
        and float((frame["return"] > 0).mean()) >= .45
    )
    summary = {
        "passed": passed,
        "status": "PASSED" if passed else "FAILED",
        "sample_count": int(len(frame)),
        "contract_count": int(frame["code"].nunique()),
        "window_count": int(len(result)),
        "positive_windows": stable_windows,
        "holding_hours": float(holding_hours),
        "mean_return": float(frame["return"].mean()),
        "median_return": float(frame["return"].median()),
        "win_rate": float((frame["return"] > 0).mean()),
        "reason": (
            "短周期逐笔收益和走步稳定性达到验收要求"
            if passed else "短周期净收益、胜率或跨窗口稳定性未达到验收要求"
        ),
    }
    return result, summary
