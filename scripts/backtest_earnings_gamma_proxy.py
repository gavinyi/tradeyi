from __future__ import annotations

import pandas as pd

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings


# Official NVIDIA after-hours earnings release dates.
EVENT_DATES = [
    "2024-02-21", "2024-05-22", "2024-08-28", "2024-11-20",
    "2025-02-26", "2025-05-28", "2025-08-27", "2025-11-19",
    "2026-02-25", "2026-05-20",
]
PREMIUM_COSTS = [0.03, 0.05, 0.07, 0.10]


broker = FutuPaperBroker(Settings.load())
bars = broker.candles_range("US.NVDA", "2024-02-01", "2026-06-01").copy()
bars["date"] = pd.to_datetime(bars["time_key"]).dt.strftime("%Y-%m-%d")
bars = bars.reset_index(drop=True)
rows = []
for event_date in EVENT_DATES:
    matches = bars.index[bars["date"] == event_date]
    if len(matches) != 1 or matches[0] + 1 >= len(bars):
        continue
    index = int(matches[0])
    close = float(bars.loc[index, "close"])
    next_open = float(bars.loc[index + 1, "open"])
    gap = next_open / close - 1
    row = {
        "财报日期": event_date,
        "财报日收盘": close,
        "次日开盘": next_open,
        "跳空幅度": gap,
        "绝对跳空": abs(gap),
    }
    for cost in PREMIUM_COSTS:
        # Conservative expiry-intrinsic proxy: ignores any remaining extrinsic value.
        row[f"成本{cost:.0%}收益"] = (abs(gap) - cost) / cost
    rows.append(row)

events = pd.DataFrame(rows)
print(events.to_string(index=False, formatters={"跳空幅度": "{:.2%}".format, "绝对跳空": "{:.2%}".format}))
summary = []
for cost in PREMIUM_COSTS:
    returns = events[f"成本{cost:.0%}收益"]
    summary.append(
        {
            "跨式权利金/股价": cost,
            "事件数": len(returns),
            "盈利次数": int((returns > 0).sum()),
            "胜率": float((returns > 0).mean()),
            "平均单次收益": float(returns.mean()),
            "中位数收益": float(returns.median()),
            "累计复合收益": float((1 + returns).prod() - 1),
        }
    )
print("\n敏感性汇总")
print(pd.DataFrame(summary).to_string(index=False))
