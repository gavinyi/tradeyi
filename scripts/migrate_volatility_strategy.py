from __future__ import annotations

import json
import os

from futu_quant.config import (
    load_gamma_campaigns, load_volatility_campaigns, save_gamma_campaigns,
    save_runtime_settings, save_volatility_campaigns, save_volatility_symbols,
)
from futu_quant.store import TradingStore


def migrate() -> dict:
    gamma = load_gamma_campaigns()
    existing = load_volatility_campaigns()
    moved = [item for item in gamma if item.get("trigger_mode") == "VOLATILITY"]
    earnings = [item for item in gamma if item.get("trigger_mode", "EARNINGS") == "EARNINGS"]
    by_code = {item["code"]: {**item, "trigger_mode": "VOLATILITY"} for item in existing}
    by_code.update({item["code"]: {**item, "trigger_mode": "VOLATILITY"} for item in moved})
    volatility = list(by_code.values())
    save_gamma_campaigns(earnings)
    save_volatility_campaigns(volatility)
    symbols = tuple(dict.fromkeys(item["underlying"] for item in volatility))
    if symbols:
        save_volatility_symbols(symbols)

    budgets = json.loads(os.getenv("STRATEGY_BUDGETS", "{}"))
    budgets.setdefault("volatility_long_gamma", budgets.get("earnings_gamma", 0.1))
    configured = [value.strip() for value in os.getenv(
        "SERVICE_STRATEGIES", "Core Satellite,Earnings Gamma"
    ).split(",") if value.strip()]
    if "Volatility Long Gamma" not in configured:
        configured.append("Volatility Long Gamma")
    save_runtime_settings({
        "STRATEGY_BUDGETS": json.dumps(budgets, separators=(",", ":")),
        "SERVICE_STRATEGIES": ",".join(configured),
    })

    codes = [item["code"] for item in moved]
    store = TradingStore()
    if codes:
        placeholders = ",".join("?" for _ in codes)
        with store.connection() as connection:
            connection.execute(
                f"UPDATE order_intents SET strategy_id='volatility_long_gamma' "
                f"WHERE strategy_id='earnings_gamma' AND symbol IN ({placeholders})", codes,
            )
            connection.execute(
                f"UPDATE decision_logs SET strategy_id='volatility_long_gamma' "
                f"WHERE strategy_id='earnings_gamma' AND symbol IN ({placeholders}) "
                "AND payload_json LIKE '%\"trigger_mode\":\"VOLATILITY\"%'", codes,
            )
            connection.execute(
                f"UPDATE decision_snapshots SET strategy_id='volatility_long_gamma' "
                f"WHERE strategy_id='earnings_gamma' AND symbol IN ({placeholders})", codes,
            )
        old_mode = store.get_setting("strategy_execution_mode:earnings_gamma", "")
        if old_mode:
            store.set_setting("strategy_execution_mode:volatility_long_gamma", old_mode)
        store.set_setting("strategy_enabled:Volatility Long Gamma", "true")
        store.audit("VOLATILITY_STRATEGY_MIGRATED", {"codes": codes}, "volatility_long_gamma", actor="cli:migrate_volatility_strategy")
    return {"moved": codes, "earnings_campaigns": len(earnings), "volatility_campaigns": len(volatility)}


if __name__ == "__main__":
    print(migrate())
