from futu_quant.volatility_structures import automatic_combo_id


def test_automatic_combo_id_is_stable_per_campaign_cycle():
    campaign = {
        "campaign_id": "US.TEST-wave", "code": "US.TEST260821C100000",
        "event_date": "2026-08-01", "structure": "CALL_DEBIT_SPREAD",
    }

    first = automatic_combo_id("volatility_long_gamma", campaign, 0)
    repeat = automatic_combo_id("volatility_long_gamma", campaign, 0)
    next_cycle = automatic_combo_id("volatility_long_gamma", campaign, 1)

    assert first == repeat
    assert first != next_cycle
    assert first.startswith("auto-")
