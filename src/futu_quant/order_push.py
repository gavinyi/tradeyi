from __future__ import annotations

import os
import json

from futu import RET_OK, TradeDealHandlerBase, TradeOrderHandlerBase

from .fee_model import estimate_fees


class OrderPushHandler(TradeOrderHandlerBase):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def on_recv_rsp(self, rsp_pb):
        code, data = super().on_recv_rsp(rsp_pb)
        if code != RET_OK:
            self.store.operational_event("order_push", "ORDER_PUSH_ERROR", "ERROR", {"error": str(data)})
            return code, data
        for _, row in data.iterrows():
            if str(row.get("trd_env", "")).upper().split(".")[-1] != "SIMULATE":
                continue
            status = str(row.get("order_status", "")).upper().split(".")[-1].replace("ORDERSTATUS_", "")
            filled = float(row.get("dealt_qty", 0) or 0)
            average = float(row.get("dealt_avg_price", 0) or 0)
            fees = estimate_fees(str(row.get("code", "")), str(row.get("trd_side", "BUY")), filled, average) if filled else 0
            self.store.update_by_broker_order(
                str(row["order_id"]), status, filled, average, fees, source="BROKER_PUSH",
            )
            self.store.operational_event("order_push", "ORDER_UPDATE", "HEALTHY", row.to_dict())
        self.store.heartbeat("order_push", "HEALTHY", "模拟盘订单推送已接收", os.getpid())
        return code, data


class DealPushHandler(TradeDealHandlerBase):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def on_recv_rsp(self, rsp_pb):
        code, data = super().on_recv_rsp(rsp_pb)
        if code == RET_OK:
            for _, row in data.iterrows():
                if str(row.get("trd_env", "")).upper().split(".")[-1] == "SIMULATE":
                    payload = row.to_dict()
                    order_id = str(row.get("order_id", ""))
                    intent = self.store.intent_by_broker_order(order_id)
                    if intent:
                        qty = float(row.get("dealt_qty", row.get("qty", 0)) or 0)
                        price = float(row.get("dealt_avg_price", row.get("price", 0)) or 0)
                        deal_id = row.get("deal_id", row.get("dealId"))
                        if qty > 0 and price > 0:
                            self.store.record_fill(
                                intent["id"], qty, price,
                                float(row.get("commission", 0) or 0),
                                source="BROKER_DEAL", broker_deal_id=str(deal_id) if deal_id else None,
                                reference_price=json.loads(intent["metadata_json"] or "{}").get("reference_price"),
                                multiplier=float(json.loads(intent["metadata_json"] or "{}").get("multiplier", 1)),
                            )
                    self.store.operational_event("order_push", "DEAL_UPDATE", "HEALTHY", payload)
        return code, data


def open_order_push(broker, store):
    context = broker.new_trade_context()
    context.set_handler(OrderPushHandler(store))
    context.set_handler(DealPushHandler(store))
    store.heartbeat("order_push", "HEALTHY", "推送通道已连接，轮询作为兜底", os.getpid())
    store.set_setting("order_push_connected", "true")
    store.operational_event("order_push", "CONNECTED", "HEALTHY")
    return context
