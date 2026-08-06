import pandas as pd

from futu_quant.ui import chinese_columns, readable_payload, translate_common_values


def test_order_columns_and_enum_values_are_localized():
    frame = pd.DataFrame(
        {"order_status": ["OrderStatus.SUBMITTED"], "order_type": ["NORMAL"], "trd_side": ["BUY"]}
    )
    localized = chinese_columns(frame)
    assert localized.iloc[0].to_dict() == {
        "订单状态": "已提交，等待成交", "订单类型": "限价单", "交易方向": "买入"
    }


def test_duplicate_translated_names_are_coalesced():
    frame = pd.DataFrame({
        "underlying": ["US.NVDA", None],
        "stock_owner": ["US.OTHER", "US.AAPL"],
        "code": ["US.NVDA260717C200000", "US.AAPL260717C200000"],
    })

    localized = chinese_columns(frame)

    assert localized.columns.tolist().count("正股代码") == 1
    assert localized["正股代码"].tolist() == ["US.NVDA", "US.AAPL"]


def test_mixed_numeric_option_columns_are_arrow_compatible():
    frame = pd.DataFrame({
        "option_open_interest": [120, "--", 0],
        "volume": [3, "N/A", 1],
    })

    localized = chinese_columns(frame)

    assert localized["未平仓量"].tolist() == ["120", "--", "0"]
    assert localized["成交量"].tolist() == ["3", "N/A", "1"]


def test_option_market_data_events_and_statuses_are_localized():
    frame = pd.DataFrame({
        "event_type": [
            "OPTION_MARKET_DATA_WAITING",
            "OPTION_EXIT_WAITING_FOR_BID",
            "VOLATILITY_EXPIRED_CAMPAIGNS_CLEANED",
        ],
        "status": ["WAITING_EXIT_LIQUIDITY", "PRE_TRADE_BLOCKED", "HEALTHY"],
    })

    localized = translate_common_values(frame)

    assert localized["event_type"].tolist() == [
        "期权行情/盘口等待退出",
        "期权平仓等待买一价",
        "过期/失效波动率策略已清理",
    ]
    assert localized["status"].tolist() == ["等待可退出盘口", "开仓前已阻断", "正常"]


def test_option_market_data_payload_is_readable_in_chinese():
    payload = {
        "market_data_status": "WAITING_EXIT_LIQUIDITY",
        "book_ok": False,
        "book_reason": "盘口不可用：买一 0.00、卖一 1.20",
        "option_quote_age_seconds": 244,
    }

    readable = readable_payload(payload)

    assert "行情/盘口处理状态：等待可退出盘口" in readable
    assert "盘口是否可用：否" in readable
    assert "期权行情延迟（秒）：244" in readable
