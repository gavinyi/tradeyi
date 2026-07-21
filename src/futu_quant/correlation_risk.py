from __future__ import annotations

import json
import os

import pandas as pd


def update_correlation_clusters(store, broker, symbols: tuple[str, ...]) -> dict[str, list[str]]:
    closes = {}
    for symbol in symbols:
        try:
            frame = broker.candles(symbol, count=90)
            closes[symbol] = frame.set_index("time_key")["close"].astype(float)
        except Exception:
            continue
    if len(closes) < 2:
        clusters = {symbol: [symbol] for symbol in closes}
    else:
        correlation = pd.DataFrame(closes).pct_change().corr()
        threshold = float(os.getenv("RISK_CORRELATION_THRESHOLD", "0.80"))
        clusters = {symbol: sorted(correlation.index[correlation[symbol] >= threshold].tolist())
                    for symbol in correlation.columns}
    store.set_setting("correlation_clusters", json.dumps(clusters, separators=(",", ":")))
    store.operational_event("portfolio_risk", "CORRELATION_UPDATED", "HEALTHY", clusters)
    return clusters
