from __future__ import annotations

import pandas as pd

from futu_quant.option_evidence import assess_option_evidence, option_research_buckets


def option_rows(count=2, *, bid=1.0, ask=1.1, quality="GOOD"):
    return pd.DataFrame(
        {
            "code": ["US.AAPL260724C300000"] * count,
            "time_key": pd.date_range("2026-07-17 10:00", periods=count, freq="min"),
            "bid": [bid] * count,
            "ask": [ask] * count,
            "quality_status": [quality] * count,
            "delta": [0.55] * count,
            "volume": [100] * count,
            "open_interest": [500] * count,
        }
    )


def test_insufficient_evidence_explains_gap():
    evidence = assess_option_evidence(option_rows(), min_samples=5, min_days=2)

    assert evidence.status == "INSUFFICIENT_EVIDENCE"
    assert evidence.missing_samples == 3
    assert evidence.missing_days == 1
    assert "还缺 3 条" in evidence.reason


def test_unusable_books_never_count_as_fill_evidence():
    frame = pd.concat(
        [option_rows(1, bid=0, ask=1), option_rows(1, bid=1, ask=0.5)], ignore_index=True
    )

    evidence = assess_option_evidence(frame, min_samples=1)

    assert evidence.usable_samples == 0
    assert evidence.status == "INSUFFICIENT_EVIDENCE"


def test_option_buckets_expose_delta_dte_type_and_liquidity():
    buckets = option_research_buckets(option_rows(3), as_of="2026-07-17")

    assert buckets.loc[0, "期权类型"] == "认购"
    assert buckets.loc[0, "Delta分组"] == "0.40-0.60"
    assert buckets.loc[0, "到期天数组"] == "0-7天"
    assert buckets.loc[0, "流动性分组"] == "高"
