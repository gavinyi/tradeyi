from __future__ import annotations

import math

from futu_quant.execution_quality import execution_quality_report


class _Store:
    def intents(self, limit: int):
        assert limit == 20_000
        return [
            {
                "id": 1, "strategy_id": "volatility_long_gamma",
                "symbol": "US.TEST", "side": "BUY", "status": "FILLED",
                "metadata_json": "{}", "average_fill_price": 2.0,
                "filled_quantity": 1, "reprice_count": 0,
                "created_at": "2026-07-31T17:55:00+00:00",
                "updated_at": "2026-07-31T17:55:10+00:00",
            },
            {
                "id": 2, "strategy_id": "volatility_long_gamma",
                "symbol": "US.TEST2", "side": "BUY", "status": "FILLED",
                "metadata_json": '{"reference_price": 10}',
                "average_fill_price": 10.5, "filled_quantity": 1,
                "reprice_count": 1,
                "created_at": "2026-07-31T17:56:00+00:00",
                "updated_at": "2026-07-31T17:56:20+00:00",
            },
        ]


def test_missing_reference_price_does_not_break_execution_quality_report():
    summary, comparison = execution_quality_report(_Store())

    assert not summary.empty
    assert math.isclose(float(summary.iloc[0]["平均不利滑点"]), 0.05)
    assert int(summary.iloc[0]["交易意图数"]) == 2
    assert not comparison.empty


def test_invalid_metadata_is_treated_as_missing_reference_price():
    store = _Store()
    rows = store.intents(20_000)
    rows[0]["metadata_json"] = "not-json"
    store.intents = lambda limit: rows

    summary, _ = execution_quality_report(store)

    assert not summary.empty
