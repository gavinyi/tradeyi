from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Any

import pandas as pd

from .risk import RiskEngine
from .alerts import AlertManager
from .store import TradingStore
from .approval import strategy_fingerprint, strategy_requires_approval
from .fee_model import estimate_fees
from .real_trading import check_real_trade


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: int
    status: str
    message: str
    broker_order_id: str | None = None
    broker_response: pd.DataFrame | None = None


class ExecutionManager:
    def __init__(self, broker, store: TradingStore | None = None):
        self.broker = broker
        self.store = store or TradingStore()
        self.risk = RiskEngine(self.store)
        self.alerts = AlertManager(self.store)

    def submit_limit(
        self, strategy_id: str, intent_key: str, symbol: str, side: str,
        quantity: int, price: float, metadata: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        metadata = metadata or {}
        global_mode = self.store.get_setting("execution_mode", os.getenv("EXECUTION_MODE", "PAPER"))
        override_mode = self.store.get_setting(f"strategy_execution_mode:{strategy_id}", "")
        mode = (override_mode or global_mode).upper()
        fingerprint = strategy_fingerprint(strategy_id)
        require_approval = os.getenv("REQUIRE_STRATEGY_APPROVAL", "true").lower() == "true"
        if (mode in {"PAPER", "REAL"} and side == "BUY" and require_approval
                and strategy_requires_approval(strategy_id) and not self.store.strategy_approved(
            strategy_id, fingerprint
        )):
            reason = "策略尚未完成当前版本审批，只允许 Shadow 运行"
            self.store.audit(
                "APPROVAL_REQUIRED", {"strategy": strategy_id, "version_hash": fingerprint},
                strategy_id,
            )
            self.store.decision(strategy_id, symbol, "BLOCKED", reason)
            return ExecutionResult(0, "APPROVAL_REQUIRED", reason)
        metadata.setdefault("reference_price", float(price))
        price = self._initial_smart_price(symbol, side, float(price), metadata)
        intent, created = self.store.create_intent(
            strategy_id, f"{intent_key}:{mode}", symbol, side, int(quantity), float(price), metadata
        )
        if not created:
            return ExecutionResult(
                intent["id"], intent["status"], "重复交易意图已拦截",
                intent.get("broker_order_id"),
            )
        if mode == "REAL":
            real_decision = check_real_trade(
                self.store, self.broker.settings, strategy_id, symbol, side,
                int(quantity), float(price), metadata,
            )
            if not real_decision.allowed:
                self.store.update_intent(intent["id"], "REJECTED", error=real_decision.reason)
                self.store.audit(
                    "REAL_TRADE_REJECTED",
                    {"intent_id": intent["id"], "reason": real_decision.reason,
                     "details": real_decision.details},
                    strategy_id,
                )
                self.alerts.send(
                    "ERROR", "REAL_TRADE_REJECTED",
                    f"{strategy_id} {symbol}: {real_decision.reason}",
                )
                return ExecutionResult(intent["id"], "REJECTED", real_decision.reason)
        try:
            decision = self.risk.check(
                self.broker, symbol, side, int(quantity), float(price), metadata.get("multiplier"),
                metadata.get("underlying"), strategy_id, mode,
                bool(metadata.get("protected_short", False)),
            )
        except Exception as exc:
            self.store.update_intent(intent["id"], "FAILED", error=f"交易前检查异常：{exc}")
            self.store.audit(
                "PRE_TRADE_CHECK_FAILED", {"intent_id": intent["id"], "error": str(exc)},
                strategy_id,
            )
            raise
        if not decision.allowed:
            self.store.update_intent(intent["id"], "REJECTED", error=decision.reason)
            self.store.audit("RISK_REJECTED", {"intent_id": intent["id"], "reason": decision.reason}, strategy_id)
            self.alerts.send("WARNING", "RISK_REJECTED", f"{strategy_id} {symbol}: {decision.reason}")
            return ExecutionResult(intent["id"], "REJECTED", decision.reason)
        self.store.decision_snapshot(strategy_id, symbol, self._snapshot_context({
            "version_hash": fingerprint, "execution_mode": mode, "side": side,
            "quantity": quantity, "reference_price": metadata["reference_price"],
            "smart_limit_price": price, "risk_result": decision.reason,
            "metadata": metadata,
        }, symbol, metadata.get("underlying")))
        if mode == "SHADOW":
            self.store.update_intent(intent["id"], "SHADOW")
            self.store.audit(
                "SHADOW_ORDER", {"intent_id": intent["id"], "price": price, "quantity": quantity},
                strategy_id,
            )
            self.store.decision(strategy_id, symbol, side, "Shadow 模式：风控通过但未提交券商")
            return ExecutionResult(intent["id"], "SHADOW", "Shadow 模式：未提交富途")
        try:
            response = self.broker.place_limit_order(symbol, side, int(quantity), float(price))
            order_id = str(response.iloc[0]["order_id"]) if not response.empty and "order_id" in response else None
            self.store.update_intent(intent["id"], "SUBMITTED", broker_order_id=order_id)
            self.store.audit(
                "ORDER_SUBMITTED",
                {"intent_id": intent["id"], "broker_order_id": order_id, "notional": decision.notional},
                strategy_id,
            )
            order_label = "实盘订单" if mode == "REAL" else "模拟订单"
            self.store.decision(strategy_id, symbol, side, f"风控通过并提交{order_label}")
            return ExecutionResult(intent["id"], "SUBMITTED", f"{order_label}已提交", order_id, response)
        except Exception as exc:
            self.store.update_intent(intent["id"], "FAILED", error=str(exc))
            self.store.audit("ORDER_FAILED", {"intent_id": intent["id"], "error": str(exc)}, strategy_id)
            self.alerts.send("ERROR", "ORDER_FAILED", f"{strategy_id} {symbol}: {exc}")
            raise

    def _snapshot_context(
        self, base: dict[str, Any], symbol: str, underlying: str | None,
    ) -> dict[str, Any]:
        def records(frame: pd.DataFrame) -> list[dict]:
            return json.loads(frame.to_json(orient="records", date_format="iso"))

        snapshot = dict(base)
        snapshot["risk_and_execution_parameters"] = {
            key: value for key, value in os.environ.items()
            if key.startswith(("RISK_", "EXEC_", "STRATEGY_", "CORE_", "GAMMA_"))
        }
        try:
            snapshot["quote"] = records(self.broker.snapshots([symbol]))
        except Exception as exc:
            snapshot["quote_error"] = str(exc)
        try:
            snapshot["account"] = records(self.broker.account_info())
            positions = self.broker.positions()
            snapshot["positions"] = records(positions[positions["code"] == symbol])
        except Exception as exc:
            snapshot["account_or_position_error"] = str(exc)
        try:
            snapshot["recent_bars"] = records(self.broker.candles(underlying or symbol, count=5))
        except Exception as exc:
            snapshot["bars_error"] = str(exc)
        return snapshot

    @staticmethod
    def normalize_status(value: object) -> str:
        status = str(value).upper().split(".")[-1].replace("ORDERSTATUS_", "")
        return {
            "FILLED_ALL": "FILLED", "CANCELLED_ALL": "CANCELLED",
            "CANCELLED_PART": "CANCELLED_PART", "FAILED": "FAILED",
        }.get(status, status)

    def reconcile(self) -> int:
        frames = [self.broker.orders()]
        try:
            frames.append(self.broker.orders(historical=True))
        except Exception:
            pass
        updated = 0
        seen_order_ids: set[str] = set()
        for frame in frames:
            if frame.empty or not {"order_id", "order_status"}.issubset(frame.columns):
                continue
            for _, order in frame.iterrows():
                seen_order_ids.add(str(order["order_id"]))
                status = self.normalize_status(order["order_status"])
                filled = float(order.get("dealt_qty", 0) or 0)
                average = float(order.get("dealt_avg_price", 0) or 0)
                fees = float(order.get("commission", 0) or 0)
                if filled > 0 and fees <= 0:
                    fees = estimate_fees(
                        str(order.get("code", "")),
                        str(order.get("trd_side", "BUY")).upper().split(".")[-1],
                        filled, average,
                    )
                self.store.update_by_broker_order(
                    str(order["order_id"]), status, filled, average, fees
                )
                updated += 1
        unknown_after = max(60, int(os.getenv("ORDER_UNKNOWN_AFTER_SECONDS", "300")))
        now = datetime.now(timezone.utc)
        for intent in self.store.intents(limit=2000):
            if intent["status"] not in {
                "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "CANCEL_REQUESTED",
            }:
                continue
            broker_order_id = str(intent.get("broker_order_id") or "")
            if not broker_order_id or broker_order_id in seen_order_ids:
                continue
            anchor = intent.get("last_broker_update") or intent["updated_at"]
            if (now - datetime.fromisoformat(anchor)).total_seconds() < unknown_after:
                continue
            self.store.update_intent(
                intent["id"], "UNKNOWN", error="券商当前及历史订单中均未找到该订单",
                source="RECONCILIATION",
            )
            self.alerts.send(
                "ERROR", "ORDER_STATE_UNKNOWN",
                f"订单 {broker_order_id} 在券商订单列表中消失，已暂停自动处置并等待人工复核",
                incident_key=f"order-state-unknown:{broker_order_id}",
            )
        self.cancel_stale_orders()
        self.reprice_open_orders()
        return updated

    def _initial_smart_price(
        self, symbol: str, side: str, reference: float, metadata: dict[str, Any]
    ) -> float:
        if metadata.get("smart_execution", True) is False:
            return reference
        try:
            quote = self.broker.snapshots([symbol]).iloc[0]
            bid, ask = float(quote.get("bid_price", 0) or 0), float(quote.get("ask_price", 0) or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return reference
            mid = (bid + ask) / 2
            max_spread = float(os.getenv("EXEC_MAX_SPREAD_PCT", "0.20"))
            if mid <= 0 or (ask - bid) / mid > max_spread:
                return reference
            if metadata.get("urgent"):
                return round(ask if side == "BUY" else bid, 2)
            slip = float(os.getenv("EXEC_MAX_SLIPPAGE_PCT", "0.01"))
            bounded = min(mid, reference * (1 + slip)) if side == "BUY" else max(mid, reference * (1 - slip))
            return round(bounded, 2)
        except Exception:
            return reference

    def reprice_open_orders(self) -> int:
        interval = max(15, int(os.getenv("EXEC_REPRICE_INTERVAL_SECONDS", "60")))
        max_attempts = max(0, int(os.getenv("EXEC_MAX_REPRICE_ATTEMPTS", "3")))
        step = max(0.0001, float(os.getenv("EXEC_REPRICE_STEP_PCT", "0.0025")))
        now = datetime.now(timezone.utc)
        repriced = 0
        for intent in self.store.intents(limit=1000):
            if intent["status"] not in {"SUBMITTED", "FILLED_PART"}:
                continue
            metadata = json.loads(intent["metadata_json"])
            if metadata.get("combo_id"):
                # Package-level repricing owns combo legs so an independent leg
                # cannot silently violate the package net-price boundary.
                continue
            if int(intent.get("reprice_count", 0)) >= max_attempts:
                continue
            anchor = intent.get("last_reprice_at") or intent["created_at"]
            if (now - datetime.fromisoformat(anchor)).total_seconds() < interval:
                continue
            remaining = int(intent["quantity"] - float(intent.get("filled_quantity", 0)))
            if remaining <= 0 or not intent.get("broker_order_id"):
                continue
            try:
                quote = self.broker.snapshots([intent["symbol"]]).iloc[0]
                bid, ask = float(quote.get("bid_price", 0) or 0), float(quote.get("ask_price", 0) or 0)
                if bid <= 0 or ask <= 0:
                    continue
                current = float(intent["limit_price"])
                target = min(ask, current * (1 + step)) if intent["side"] == "BUY" else max(bid, current * (1 - step))
                reference = float(metadata.get("reference_price", current))
                slip = float(os.getenv("EXEC_MAX_SLIPPAGE_PCT", "0.01"))
                if intent["side"] == "BUY":
                    target = min(target, reference * (1 + slip))
                else:
                    target = max(target, reference * (1 - slip))
                self.broker.modify_limit_order(intent["broker_order_id"], int(intent["quantity"]), target)
                self.store.record_reprice(intent["id"], round(target, 2))
                self.store.audit("ORDER_REPRICED", {"intent_id": intent["id"], "price": target})
                repriced += 1
            except Exception as exc:
                self.alerts.send("WARNING", "REPRICE_FAILED", f"订单 {intent['broker_order_id']} 追价失败: {exc}")
        return repriced

    def cancel_stale_orders(self) -> int:
        timeout = max(60, int(os.getenv("ORDER_TIMEOUT_SECONDS", "900")))
        cancelled = 0
        now = datetime.now(timezone.utc)
        for intent in self.store.intents(limit=1000):
            if intent["status"] not in {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}:
                continue
            age = (now - datetime.fromisoformat(intent["created_at"])).total_seconds()
            if age < timeout or not intent.get("broker_order_id"):
                continue
            try:
                self.broker.cancel_order(intent["broker_order_id"])
                self.store.update_intent(intent["id"], "CANCEL_REQUESTED")
                self.store.audit("STALE_ORDER_CANCELLED", {"intent_id": intent["id"], "age": age})
                cancelled += 1
            except Exception as exc:
                self.alerts.send("ERROR", "CANCEL_FAILED", f"订单 {intent['broker_order_id']} 超时撤单失败: {exc}")
        return cancelled
