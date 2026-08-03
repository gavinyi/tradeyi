from __future__ import annotations

import pandas as pd

from futu_quant.ui import china_time_columns, chinese_columns


def test_utc_database_timestamp_is_displayed_in_china_time():
    frame = pd.DataFrame({"created_at": ["2026-07-15T15:17:20+00:00"]})

    result = china_time_columns(frame)

    assert str(result.loc[0, "created_at"]) == "2026-07-15 23:17:20"
    assert "创建时间（中国）" in chinese_columns(frame).columns


def test_volatility_decision_time_is_converted_before_chinese_label():
    frame = pd.DataFrame({"created_at": ["2026-07-31T17:55:23.216009+00:00"]})

    result = china_time_columns(frame).rename(
        columns={"created_at": "时间（中国）"}
    )
    values = pd.to_datetime(result["时间（中国）"], errors="coerce")
    result["时间（中国）"] = values.dt.strftime("%Y-%m-%d %H:%M:%S")

    assert str(result.loc[0, "时间（中国）"]) == "2026-08-01 01:55:23"
    assert "T" not in str(result.loc[0, "时间（中国）"])
    assert "+00:00" not in str(result.loc[0, "时间（中国）"])


def test_china_time_conversion_is_idempotent_for_display_frames():
    frame = pd.DataFrame({"open_time": ["2026-07-23T14:03:35+00:00"]})

    once = china_time_columns(frame)
    twice = chinese_columns(once)

    assert str(once.loc[0, "open_time"]) == "2026-07-23 22:03:35"
    assert str(twice.loc[0, "开仓时间（中国）"]) == "2026-07-23 22:03:35"


def test_eastern_market_timestamp_is_displayed_in_china_time():
    frame = pd.DataFrame({"time_key": ["2026-07-15 09:30:00"]})

    result = china_time_columns(frame)

    assert str(result.loc[0, "time_key"]) == "2026-07-15 21:30:00"
