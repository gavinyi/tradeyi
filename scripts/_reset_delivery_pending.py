from __future__ import annotations

from futu_quant.scheduled_tasks import IM_DELIVERY_PENDING_KEY
from futu_quant.store import TradingStore


def main() -> int:
    store = TradingStore()
    value, _ = store.get_runtime_config(IM_DELIVERY_PENDING_KEY) or (None, 0)
    record = dict(value) if isinstance(value, dict) else {}
    if record.get("status") not in {"PENDING", "DELIVERED"}:
        raise SystemExit("missing pending/delivered review record")
    record["status"] = "PENDING"
    record.pop("delivered_at", None)
    record.pop("channel", None)
    store.set_runtime_config(IM_DELIVERY_PENDING_KEY, record, "manual_reset_for_timer_test")
    print(f"reset session_date={record.get('session_date')} status={record.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
