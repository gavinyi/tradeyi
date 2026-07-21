from __future__ import annotations

import pandas as pd

from futu_quant.ui import china_time_columns, chinese_columns


def test_utc_database_timestamp_is_displayed_in_china_time():
    frame = pd.DataFrame({"created_at": ["2026-07-15T15:17:20+00:00"]})

    result = china_time_columns(frame)

    assert str(result.loc[0, "created_at"]) == "2026-07-15 23:17:20"
    assert "创建时间（中国）" in chinese_columns(frame).columns


def test_eastern_market_timestamp_is_displayed_in_china_time():
    frame = pd.DataFrame({"time_key": ["2026-07-15 09:30:00"]})

    result = china_time_columns(frame)

    assert str(result.loc[0, "time_key"]) == "2026-07-15 21:30:00"
