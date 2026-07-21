import pandas as pd
import pytest

from futu_quant.market_information import (
    combined_watchlist,
    normalize_market_snapshots,
    parse_recent_sec_filings,
    rank_option_market,
    summarize_market_breadth,
)


class SettingsFixture:
    watchlist = ("US.AAPL", "US.NVDA")
    core_satellite_symbols = ("US.SPY", "US.NVDA")
    gamma_symbols = ("US.NVDA",)
    volatility_symbols = ("US.AMD", "US.NVDA")


def test_combined_watchlist_deduplicates_and_preserves_strategy_membership():
    result = combined_watchlist(SettingsFixture(), held_symbols=("US.MSFT", "US.AAPL"))

    assert result["代码"].tolist() == ["US.AAPL", "US.AMD", "US.MSFT", "US.NVDA", "US.SPY"]
    nvda = result.set_index("代码").loc["US.NVDA"]
    assert nvda["来源"] == "普通策略、Core Satellite、财报 Gamma、波动率期权"
    assert result.set_index("代码").loc["US.AAPL", "当前持仓"] == "是"


def test_normalize_market_snapshots_calculates_change_spread_and_freshness():
    raw = pd.DataFrame([
        {
            "code": "US.AAPL", "name": "Apple", "last_price": 110.0,
            "prev_close_price": 100.0, "open_price": 101.0,
            "high_price": 112.0, "low_price": 99.0,
            "bid_price": 109.9, "ask_price": 110.1,
            "volume": 2_000_000, "turnover": 220_000_000,
            "update_time": "2026-07-20 09:31:00",
        }
    ])

    result = normalize_market_snapshots(raw)

    assert result.loc[0, "涨跌幅"] == pytest.approx(0.10)
    assert round(result.loc[0, "买卖价差"], 6) == round(0.2 / 110.0, 6)
    assert result.loc[0, "日内振幅"] == 0.13
    assert result.loc[0, "行情状态"] in {"新鲜", "延迟", "时间异常"}


def test_market_breadth_ignores_flat_or_missing_quotes():
    frame = pd.DataFrame({"涨跌幅": [0.03, -0.02, 0.0, float("nan")]})
    breadth = summarize_market_breadth(frame)
    assert breadth["上涨数"] == 1
    assert breadth["下跌数"] == 1
    assert breadth["平盘数"] == 1
    assert breadth["上涨占比"] == 0.5
    assert breadth["平均涨跌幅"] == pytest.approx(1 / 300)


def test_parse_recent_sec_filings_builds_official_document_links():
    payload = {
        "cik": "0000320193",
        "filings": {"recent": {
            "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
            "filingDate": ["2026-07-19", "2026-07-18"],
            "reportDate": ["2026-06-30", "2026-07-17"],
            "acceptanceDateTime": ["20260719120000", "20260718120000"],
            "form": ["10-Q", "4"],
            "primaryDocument": ["aapl-20260630.htm", "ownership.xml"],
            "primaryDocDescription": ["Quarterly report", "Ownership report"],
        }},
    }

    rows = parse_recent_sec_filings(payload, forms={"10-Q", "8-K"}, limit=10)

    assert len(rows) == 1
    assert rows[0]["表格类型"] == "10-Q"
    assert rows[0]["公告链接"].endswith("/000032019326000001/aapl-20260630.htm")


def test_option_market_ranking_prefers_liquid_tight_contracts():
    frame = pd.DataFrame([
        {"code": "WIDE", "strike_price": 100, "bid_price": 1, "ask_price": 2,
         "option_open_interest": 10, "volume": 1},
        {"code": "TIGHT", "strike_price": 101, "bid_price": 1.45, "ask_price": 1.55,
         "option_open_interest": 5000, "volume": 400},
    ])
    result = rank_option_market(frame, spot=100)
    assert result.iloc[0]["code"] == "TIGHT"
    assert result.iloc[0]["买卖价差"] == pytest.approx(0.1 / 1.5)
