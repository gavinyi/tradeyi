from __future__ import annotations

import pandas as pd


def numeric_sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())


def account_pnl_metrics(positions: pd.DataFrame) -> dict[str, float]:
    """Aggregate broker position P&L without relying on Futu's N/A account totals."""
    return {
        "today_position_pnl": numeric_sum(positions, "today_pl_val"),
        "total_position_pnl": numeric_sum(positions, "pl_val"),
    }
