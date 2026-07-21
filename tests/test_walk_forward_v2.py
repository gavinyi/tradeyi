from __future__ import annotations

import pandas as pd

from futu_quant.walk_forward import chronological_splits, walk_forward_validate


def bars(count: int) -> pd.DataFrame:
    close = pd.Series([100 + index * 0.1 + (index % 17) * 0.03 for index in range(count)])
    return pd.DataFrame(
        {
            "time_key": pd.bdate_range("2024-01-01", periods=count).astype(str),
            "open": close,
            "close": close,
        }
    )


def test_splits_are_ordered_and_embargoed():
    split = chronological_splits(500, 250, 60, 60, 5)[0]

    assert split.train.stop + 5 == split.validation.start
    assert split.validation.stop + 5 == split.test.start
    assert split.train.start == 0


def test_splits_reject_impossible_sizes():
    try:
        chronological_splits(100, 80, 20, 20, 5)
    except ValueError as exc:
        assert "不足" in str(exc)
    else:
        raise AssertionError("impossible chronological split must be rejected")


def test_stress_uses_one_one_point_five_and_two_times_slippage():
    result = walk_forward_validate(bars(400), "SMA Cross", 10_000, 10, 1, 2)

    assert [row["滑点倍数"] for row in result["sensitivity"]] == [1.0, 1.5, 2.0]
    assert result["evidence"]["ordered"] is True
    assert result["evidence"]["bootstrap_seed"] == 42
