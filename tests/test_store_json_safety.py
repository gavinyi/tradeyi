from __future__ import annotations

import json

from futu_quant.store import TradingStore


def test_decision_payload_cuts_circular_references(tmp_path):
    store = TradingStore(tmp_path / "trading.db")
    payload = {"code": "US.KO"}
    payload["diagnostics"] = payload

    store.decision("volatility_long_gamma", "US.KO", "HOLD", "复核", payload)

    saved = json.loads(store.decisions(limit=1)[0]["payload_json"])
    assert saved["code"] == "US.KO"
    assert saved["diagnostics"] == "<循环引用>"

