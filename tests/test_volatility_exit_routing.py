import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "run_gamma_strategy",
    Path(__file__).resolve().parents[1] / "scripts" / "run_gamma_strategy.py",
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
volatility_exit_close_legs = module.volatility_exit_close_legs


def test_exit_only_multileg_volatility_campaign_closes_primary_leg_only():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1, "role": "PROTECTIVE"},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1, "role": "RISK"},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "exit_only": True,
        "structure": "PUT_DEBIT_SPREAD",
        "legs": legs,
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs,
    )

    assert route == "EXIT_ONLY"
    assert close_legs == [{
        "code": "US.SPCX260814P142000",
        "side": "BUY",
        "ratio": 1,
        "role": "PRIMARY",
    }]


def test_missing_combo_ledger_falls_back_to_primary_leg_exit():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "structure": "PUT_DEBIT_SPREAD",
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs, combo_available=False,
    )

    assert route == "MISSING_COMBO"
    assert len(close_legs) == 1
    assert close_legs[0]["code"] == "US.SPCX260814P142000"


def test_available_combo_keeps_multileg_close_route():
    legs = [
        {"code": "US.SPCX260814P142000", "side": "BUY", "ratio": 1},
        {"code": "US.SPCX260814P137000", "side": "SELL", "ratio": 1},
    ]
    campaign = {
        "code": "US.SPCX260814P142000",
        "trigger_mode": "VOLATILITY",
        "structure": "PUT_DEBIT_SPREAD",
    }

    close_legs, route = volatility_exit_close_legs(
        campaign, "US.SPCX260814P142000", legs, combo_available=True,
    )

    assert route is None
    assert close_legs == legs
