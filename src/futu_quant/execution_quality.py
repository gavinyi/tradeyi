from __future__ import annotations

import json

import pandas as pd


def _reference_price(value: object) -> float | None:
    """Read an optional reference price without breaking the whole report."""
    try:
        payload = json.loads(value or "{}")
        price = pd.to_numeric(payload.get("reference_price"), errors="coerce")
        return float(price) if pd.notna(price) and float(price) > 0 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def execution_quality_report(store) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.DataFrame(store.intents(limit=20_000))
    if frame.empty:
        return pd.DataFrame(), pd.DataFrame()
    frame["reference_price"] = pd.to_numeric(
        frame["metadata_json"].map(_reference_price), errors="coerce"
    )
    frame["average_fill_price"] = pd.to_numeric(
        frame["average_fill_price"], errors="coerce"
    )
    frame["filled_quantity"] = pd.to_numeric(
        frame["filled_quantity"], errors="coerce"
    ).fillna(0)
    frame["reprice_count"] = pd.to_numeric(
        frame["reprice_count"], errors="coerce"
    ).fillna(0)
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
    frame["updated_at"] = pd.to_datetime(frame["updated_at"], utc=True, errors="coerce")
    frame["wait_seconds"] = (frame["updated_at"] - frame["created_at"]).dt.total_seconds()
    direction = frame["side"].map({"BUY": 1, "SELL": -1}).fillna(0)
    denominator = frame["reference_price"].where(frame["reference_price"] > 0)
    frame["slippage"] = (
        (frame["average_fill_price"] - frame["reference_price"])
        / denominator * direction
    )
    frame["filled"] = frame["filled_quantity"] > 0
    summary = frame.groupby("strategy_id", as_index=False).agg(
        交易意图数=("id", "count"), 已成交订单=("filled", "sum"),
        平均等待秒数=("wait_seconds", "mean"), 平均不利滑点=("slippage", "mean"),
        平均追价次数=("reprice_count", "mean"),
    )
    summary["成交率"] = summary["已成交订单"] / summary["交易意图数"]

    shadow = frame[frame["status"].astype(str).str.startswith("SHADOW")].groupby(
        ["strategy_id", "symbol", "side"], as_index=False
    )["average_fill_price"].mean().rename(columns={"average_fill_price": "Shadow虚拟成交价"})
    paper = frame[frame["filled"]].groupby(
        ["strategy_id", "symbol", "side"], as_index=False
    )["average_fill_price"].mean().rename(columns={"average_fill_price": "模拟成交均价"})
    comparison = shadow.merge(paper, on=["strategy_id", "symbol", "side"], how="outer")
    if not comparison.empty:
        shadow_denominator = comparison["Shadow虚拟成交价"].where(
            comparison["Shadow虚拟成交价"] > 0
        )
        comparison["价格偏差"] = (
            comparison["模拟成交均价"] / shadow_denominator - 1
        )
    return summary.rename(columns={"strategy_id": "策略"}), comparison.rename(columns={
        "strategy_id": "策略", "symbol": "代码", "side": "方向",
    })
