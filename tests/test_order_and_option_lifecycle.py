from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from futu_quant.option_lifecycle import (
    assess_option_positions, execute_disposition_plans, parse_option_code,
)
from futu_quant.store import TradingStore


def test_option_code_parser_supports_standard_futu_symbol():
    contract = parse_option_code("US.AAPL260720C332500")
    assert contract is not None
    assert contract.underlying == "US.AAPL"
    assert contract.expiration == date(2026, 7, 20)
    assert contract.option_type == "CALL"
    assert contract.strike == 332.5


def test_option_code_parser_normalizes_berkshire_underlying_for_snapshots():
    contract = parse_option_code("US.BRKB260828C530000")

    assert contract is not None
    assert contract.code == "US.BRKB260828C530000"
    assert contract.underlying == "US.BRK.B"


def test_long_itm_call_detects_exercise_cash_shortfall():
    positions = pd.DataFrame([
        {"code": "US.AAPL260720C300000", "qty": 2},
    ])
    quotes = pd.DataFrame([{"code": "US.AAPL", "last_price": 330.0}])
    rows = assess_option_positions(positions, quotes, cash=10_000, as_of=date(2026, 7, 19))
    assert len(rows) == 1
    assert rows[0]["intrinsic_value"] == 30
    assert rows[0]["projected_cash_need"] == 60_000
    assert rows[0]["severity"] == "CRITICAL"
    assert "资金缺口" in rows[0]["reason"]


def test_covered_short_call_has_no_share_shortfall():
    positions = pd.DataFrame([
        {"code": "US.AAPL", "qty": 100},
        {"code": "US.AAPL260720C300000", "qty": -1},
    ])
    quotes = pd.DataFrame([{"code": "US.AAPL", "last_price": 330.0}])
    rows = assess_option_positions(positions, quotes, cash=0, as_of=date(2026, 7, 18))
    assert rows[0]["severity"] == "WARNING"
    assert "缺口" not in rows[0]["reason"]


def test_order_state_history_records_each_transition(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    intent, created = store.create_intent("core", "once", "US.AAPL", "BUY", 1, 200)
    assert created
    store.update_intent(intent["id"], "SUBMITTED", broker_order_id="123")
    store.update_by_broker_order("123", "FILLED_PART", 0.5, 199.5, 0.1)
    store.update_by_broker_order("123", "FILLED", 1, 199.75, 0.2, source="BROKER_PUSH")
    events = list(reversed(store.order_state_events()))
    assert [row["new_status"] for row in events] == [
        "CREATED", "SUBMITTED", "FILLED_PART", "FILLED",
    ]
    assert events[-1]["source"] == "BROKER_PUSH"


def test_auto_close_requires_explicit_plan_and_strategy_ownership(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    code = "US.AAPL260720C300000"
    store.record_option_lifecycle([{
        "code": code, "underlying": "US.AAPL", "option_type": "CALL",
        "expiration": "2026-07-20", "strike": 300, "quantity": 1,
        "underlying_price": 330, "intrinsic_value": 30, "days_to_expiry": 0,
        "severity": "CRITICAL", "action": "立即复核", "reason": "到期",
        "projected_cash_need": 30_000, "projected_share_need": 100,
    }])
    store.save_option_disposition(code, "AUTO_CLOSE", 0, 60, True, "test")
    store.set_virtual_allocation("gamma", code, 1, 10)

    class Broker:
        @staticmethod
        def snapshots(symbols):
            return pd.DataFrame([{"code": symbols[0], "bid_price": 31.0}])

    class Executor:
        calls = []

        def submit_limit(self, *args):
            self.calls.append(args)
            return SimpleNamespace(status="SUBMITTED", message="模拟订单已提交")

    class Session:
        @staticmethod
        def pre_close(now, minutes):
            return minutes == 60

    executor = Executor()
    results = execute_disposition_plans(
        Broker(), store, executor, Session(),
        datetime(2026, 7, 20, 15, 30, tzinfo=ZoneInfo("America/New_York")),
    )
    assert results[0]["status"] == "SUBMITTED"
    assert executor.calls[0][0] == "gamma"
    assert executor.calls[0][3:6] == ("SELL", 1, 31.0)


def test_auto_close_is_blocked_without_strategy_ownership(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    code = "US.AAPL260720C300000"
    store.record_option_lifecycle([{
        "code": code, "underlying": "US.AAPL", "option_type": "CALL",
        "expiration": "2026-07-20", "strike": 300, "quantity": 1,
        "underlying_price": 330, "intrinsic_value": 30, "days_to_expiry": 0,
        "severity": "CRITICAL", "action": "立即复核", "reason": "到期",
        "projected_cash_need": 30_000, "projected_share_need": 100,
    }])
    store.save_option_disposition(code, "AUTO_CLOSE", 0, 60, True, "test")

    class Session:
        @staticmethod
        def pre_close(now, minutes):
            return True

    results = execute_disposition_plans(
        object(), store, object(), Session(),
        datetime(2026, 7, 20, 15, 30, tzinfo=ZoneInfo("America/New_York")),
    )
    assert results[0]["status"] == "BLOCKED"
    assert "持仓归属" in results[0]["message"]
