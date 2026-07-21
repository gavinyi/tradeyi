import pandas as pd

from futu_quant.earnings_gamma import screen_gamma_contracts


def test_gamma_screen_prefers_liquid_atm_contract():
    chain = pd.DataFrame(
        {
            "code": ["US.TEST260717C100000", "US.TEST260717C120000"],
            "option_type": ["CALL", "CALL"], "strike_price": [100.0, 120.0],
            "suspension": [False, False],
        }
    )
    quotes = pd.DataFrame(
        {
            "code": chain["code"], "bid_price": [4.9, 1.0], "ask_price": [5.1, 2.0],
            "option_open_interest": [1000, 10],
        }
    )
    result = screen_gamma_contracts(chain, quotes, 101.0, "CALL")
    assert result["code"].tolist() == ["US.TEST260717C100000"]
