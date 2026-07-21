import pandas as pd

from futu_quant.earnings_calendar import filter_optionable_earnings


class FakeBroker:
    def option_expirations(self, symbol):
        return pd.DataFrame({"strike_time": ["2026-07-17"]})

    def snapshots(self, symbols):
        if symbols == ["US.GOOD"] or symbols == ["US.BAD"]:
            return pd.DataFrame({"code": symbols, "last_price": [100.0]})
        liquid = symbols[0].startswith("US.GOOD")
        return pd.DataFrame({
            "code": symbols, "bid_price": [4.9 if liquid else 0.0] * len(symbols),
            "ask_price": [5.1] * len(symbols),
            "option_open_interest": [500 if liquid else 0] * len(symbols),
        })

    def option_chain(self, symbol, expiration):
        return pd.DataFrame({
            "code": [f"{symbol}C", f"{symbol}P"], "option_type": ["CALL", "PUT"],
            "strike_price": [100.0, 100.0], "suspension": [False, False],
        })


def test_filter_optionable_earnings_removes_illiquid_symbols() -> None:
    events = [
        {"symbol": "US.GOOD", "event_date": "2026-07-16", "session": "盘前"},
        {"symbol": "US.BAD", "event_date": "2026-07-16", "session": "盘前"},
    ]
    accepted, rejected = filter_optionable_earnings(events, FakeBroker())
    assert [item["symbol"] for item in accepted] == ["US.GOOD"]
    assert accepted[0]["qualified_contracts"] == 2
    assert [item["symbol"] for item in rejected] == ["US.BAD"]
    assert "OI≥100" in rejected[0]["filter_reason"]
