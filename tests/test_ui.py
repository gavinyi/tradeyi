import pandas as pd

from futu_quant.ui import chinese_columns


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
