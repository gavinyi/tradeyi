from futu_quant.crypto_dashboard import format_okx_fills, format_okx_swap_positions


def test_swap_positions_are_chinese_and_exclude_empty_positions():
    frame = format_okx_swap_positions([
        {
            "instId": "BTC-USDT-SWAP", "pos": "-2", "avgPx": "100",
            "markPx": "90", "liqPx": "130", "lever": "2", "margin": "50",
            "upl": "20", "uplRatio": ".2", "mgnMode": "isolated",
            "notionalUsd": "-180", "uTime": "1720000000000",
        },
        {"instId": "ETH-USDT-SWAP", "pos": "0"},
    ])
    assert len(frame) == 1
    assert frame.iloc[0]["方向"] == "空仓"
    assert frame.iloc[0]["保证金模式"] == "逐仓"
    assert frame.iloc[0]["持仓张数"] == 2
    assert frame.iloc[0]["距强平比例"] > 0


def test_swap_fills_are_translated_and_time_is_china_timezone():
    frame = format_okx_fills([{
        "instId": "BTC-USDT-SWAP", "side": "sell", "posSide": "net",
        "fillSz": "2", "fillPx": "100", "fee": "-0.1",
        "feeCcy": "USDT", "ts": "1720000000000",
    }])
    assert frame.iloc[0]["方向"] == "卖出"
    assert frame.iloc[0]["持仓方向"] == "净持仓"
    assert str(frame.iloc[0]["成交时间"].tz) == "Asia/Shanghai"
