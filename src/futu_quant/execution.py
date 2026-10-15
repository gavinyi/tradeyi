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
from .approval import (
    approval_enforcement_enabled, approval_runtime_manifest, runtime_strategy_fingerprint,
    strategy_fingerprint, strategy_requires_approval,
)
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
        execution_mode_override: str | None = None,
    ) -> ExecutionResult:
        metadata = metadata or {}
        global_mode = self.store.get_setting("execution_mode", os.getenv("EXECUTION_MODE", "PAPER"))
        override_mode = self.store.get_setting(f"strategy_execution_mode:{strategy_id}", "")
        mode = (execution_mode_override or override_mode or global_mode).upper()
        broker_settings = getattr(self.broker, "settings", None)
        broker_env = str(getattr(broker_settings, "trading_env", "SIMULATE")).upper()
        actual_env = getattr(self.broker, "trd_env", None)
        if mode not in {"SHADOW", "PAPER", "REAL"}:
            raise ValueError(f"无效执行模式：{mode}")
        if mode == "PAPER" and (broker_env != "SIMULATE" or
                                (actual_env is not None and str(actual_env).upper().split(".")[-1] != "SIMULATE")):
            reason = "PAPER 只能使用模拟券商环境，禁止向 REAL 券商提交"
            self.store.audit("EXECUTION_ENV_REJECTED", {"mode": mode, "broker_env": broker_env}, strategy_id)
            return ExecutionResult(0, "REJECTED", reason)
        if mode == "REAL" and (broker_settings is None or broker_env != "REAL" or
                               (actual_env is not None and str(actual_env).upper().split(".")[-1] != "REAL")):
            reason = "REAL 必须连接 REAL 券商环境"
            self.store.audit("EXECUTION_ENV_REJECTED", {"mode": mode, "broker_env": broker_env}, strategy_id)
            return ExecutionResult(0, "REJECTED", reason)
        fingerprint = runtime_strategy_fingerprint(strategy_id, self.store)
        current_source_fingerprint = strategy_fingerprint(strategy_id, self.store)
        require_approval = approval_enforcement_enabled(self.store)
        if (mode in {"PAPER", "REAL"} and side == "BUY" and require_approval
                and strategy_requires_approval(strategy_id) and not self.store.strategy_approved(
            strategy_id, fingerprint
        )):
            source_drifted = current_source_fingerprint != fingerprint
            reason = (
                "交易服务运行版本与当前代码不一致，请重启统一交易服务后重新审批"
                if source_drifted else
                "策略尚未完成当前运行版本审批，只允许 Shadow 运行"
            )
            self.store.audit(
                "APPROVAL_REQUIRED", {
                    "strategy": strategy_id,
                    "version_hash": fingerprint,
                    "current_source_hash": current_source_fingerprint,
                    "source_drifted": source_drifted,
                    "runtime_manifest": approval_runtime_manifest(self.store),
                },
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
                metadata,
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
        # Persist the irreversible boundary before invoking the broker. FAILED is retryable
        # in TradingStore when no order id exists; neither transport errors nor local write
        # failures after this boundary prove that the broker rejected the order.
        self.store.update_intent(intent["id"], "SUBMITTING")
        is_urgent_exit = bool(metadata.get("urgent") or metadata.get("reduce_only") or side == "SELL")
        max_attempts = 3 if is_urgent_exit else 1
        response = None
        for attempt in range(max_attempts):
            try:
                kwargs = {"real_trade_context": (self.store, strategy_id, metadata, intent["id"])} if mode == "REAL" else {}
                response = self.broker.place_limit_order(symbol, side, int(quantity), float(price), **kwargs)
                break
            except Exception as exc:
                if mode == "REAL" and "实盘下单门禁拒绝" in str(exc):
                    self.store.update_intent(intent["id"], "REJECTED", error=str(exc))
                    return ExecutionResult(intent["id"], "REJECTED", str(exc))
                is_net_err = any(k in str(exc).lower() for k in ["网络", "timeout", "connect", "broken pipe", "connection", "disconnected"])
                # 针对紧急平仓止盈单，若遭遇瞬时网络断开/超时，进行最多 3 次退避重试，确保保单送达
                if is_urgent_exit and is_net_err and attempt < max_attempts - 1:
                    import time
                    time.sleep(1.0)
                    continue
                self._mark_unknown(intent, f"券商下单结果不确定：{exc}", "ORDER_SUBMISSION_UNKNOWN")
                raise
        try:
            order_id = str(response.iloc[0]["order_id"]) if not response.empty and "order_id" in response else None
        except Exception:
            order_id = None
        if not order_id or order_id.lower() in {"none", "nan"}:
            self._mark_unknown(intent, "券商下单返回缺少有效订单号", "ORDER_SUBMISSION_UNKNOWN")
            return ExecutionResult(intent["id"], "UNKNOWN", "券商下单结果不确定，请人工核查")
        try:
            # Push callbacks can advance an order before the synchronous response arrives.
            current = self.store.intent(intent["id"])
            if current and current["status"] in {"SUBMITTING", "CREATED"}:
                self.store.update_intent(intent["id"], "SUBMITTED", broker_order_id=order_id)
            elif current and current.get("broker_order_id") != order_id:
                raise RuntimeError("券商回报订单号与本地状态不一致")
        except Exception as exc:
            self._mark_unknown(intent, f"券商已接受订单 {order_id}，本地落库失败：{exc}",
                               "ORDER_PERSIST_UNKNOWN", order_id)
            return ExecutionResult(intent["id"], "UNKNOWN", "券商已接受订单，本地状态待人工核查", order_id, response)
        self.store.audit(
            "ORDER_SUBMITTED",
            {"intent_id": intent["id"], "broker_order_id": order_id, "notional": decision.notional},
            strategy_id,
        )
        order_label = "实盘订单" if mode == "REAL" else "模拟订单"
        self.store.decision(strategy_id, symbol, side, f"风控通过并提交{order_label}")
        final = self.store.intent(intent["id"])
        status = final["status"] if final else "SUBMITTED"
        return ExecutionResult(intent["id"], status, f"{order_label}已提交", order_id, response)

    def _mark_unknown(self, intent: dict, reason: str, event: str,
                      order_id: str | None = None) -> None:
        try:
            current = self.store.intent(intent["id"])
            if current and current["status"] in {"CREATED", "SUBMITTING", "SUBMITTED"}:
                self.store.update_intent(intent["id"], "UNKNOWN", broker_order_id=order_id,
                                         error=reason)
        except Exception:
            # The pre-submission SUBMITTING marker is deliberately non-retryable.
            pass
        try:
            self.store.audit(event, {"intent_id": intent["id"], "broker_order_id": order_id,
                                     "reason": reason}, intent["strategy_id"])
            self.alerts.send("ERROR", event, f"{intent['strategy_id']} {intent['symbol']}: {reason}")
        except Exception:
            pass

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

    def _order_environment_ok(self, intent: dict) -> bool:
        mode = str(intent.get("intent_key", "")).rsplit(":", 1)[-1].upper()
        expected = {"PAPER": "SIMULATE", "REAL": "REAL"}.get(mode)
        actual = str(getattr(getattr(self.broker, "settings", None), "trading_env", "SIMULATE")).upper()
        # Legacy intents without mode suffix may only be managed in simulation.
        if expected is None:
            expected = "SIMULATE"
        if expected != actual:
            self.alerts.send("ERROR", "ORDER_ENV_MISMATCH",
                             f"订单 {intent.get('broker_order_id')} 环境 {mode} 与券商 {actual} 不一致，禁止改单撤单")
            return False
        return True

    def reprice_open_orders(self) -> int:
        normal_interval = max(15, int(os.getenv("EXEC_REPRICE_INTERVAL_SECONDS", "60")))
        urgent_interval = max(10, int(os.getenv("EXEC_URGENT_REPRICE_INTERVAL_SECONDS", "15")))
        normal_max_attempts = max(0, int(os.getenv("EXEC_MAX_REPRICE_ATTEMPTS", "3")))
        urgent_max_attempts = max(5, int(os.getenv("EXEC_URGENT_MAX_REPRICE_ATTEMPTS", "12")))
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
            is_urgent = bool(metadata.get("urgent"))
            max_attempts = urgent_max_attempts if is_urgent else normal_max_attempts
            interval = urgent_interval if is_urgent else normal_interval
            if int(intent.get("reprice_count", 0)) >= max_attempts:
                continue
            anchor = intent.get("last_reprice_at") or intent["created_at"]
            if (now - datetime.fromisoformat(anchor)).total_seconds() < interval:
                continue
            remaining = int(intent["quantity"] - float(intent.get("filled_quantity", 0)))
            if remaining <= 0 or not intent.get("broker_order_id") or not self._order_environment_ok(intent):
                continue
            try:
                quote = self.broker.snapshots([intent["symbol"]]).iloc[0]
                bid, ask = float(quote.get("bid_price", 0) or 0), float(quote.get("ask_price", 0) or 0)
                if bid <= 0 or ask <= 0:
                    continue
                current = float(intent["limit_price"])
                reference = float(metadata.get("reference_price", current))
                slip = float(os.getenv(
                    "EXEC_URGENT_EXIT_MAX_SLIPPAGE_PCT"
                    if is_urgent else
                    "EXEC_MAX_SLIPPAGE_PCT",
                    "0.12" if is_urgent else "0.01",
                ))

                if is_urgent and intent["side"] == "SELL":
                    # 紧急平仓/锁利卖出: 积极主动吃单，紧贴买一价下方 1% 确保对手方撮合成交
                    target = round(min(current, bid * 0.99), 2)
                    target = max(target, round(reference * (1 - slip), 2))
                elif is_urgent and intent["side"] == "BUY":
                    # 紧急买回平仓: 紧贴卖一价上方 1% 主动吃单
                    target = round(max(current, ask * 1.01), 2)
                    target = min(target, round(reference * (1 + slip), 2))
                else:
                    target = min(ask, current * (1 + step)) if intent["side"] == "BUY" else max(bid, current * (1 - step))
                    if intent["side"] == "BUY":
                        target = min(target, reference * (1 + slip))
                    else:
                        target = max(target, reference * (1 - slip))

                target = round(max(0.01, target), 2)
                if abs(target - current) < 1e-4:
                    continue

                self.broker.modify_limit_order(intent["broker_order_id"], int(intent["quantity"]), target)
                self.store.record_reprice(intent["id"], target)
                self.store.audit("ORDER_REPRICED", {"intent_id": intent["id"], "price": target, "urgent": is_urgent})
                repriced += 1
            except Exception as exc:
                self.alerts.send("WARNING", "REPRICE_FAILED", f"订单 {intent['broker_order_id']} 追价失败: {exc}")
        return repriced

    def cancel_stale_orders(self) -> int:
        normal_timeout = max(60, int(os.getenv("ORDER_TIMEOUT_SECONDS", "900")))
        urgent_timeout = max(30, int(os.getenv("EXEC_URGENT_ORDER_TIMEOUT_SECONDS", "60")))
        cancelled = 0
        now = datetime.now(timezone.utc)
        for intent in self.store.intents(limit=1000):
            if intent["status"] not in {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}:
                continue
            metadata = json.loads(intent["metadata_json"])
            is_urgent = bool(metadata.get("urgent"))
            timeout = urgent_timeout if is_urgent else normal_timeout
            age = (now - datetime.fromisoformat(intent["created_at"])).total_seconds()
            if age < timeout or not intent.get("broker_order_id") or not self._order_environment_ok(intent):
                continue
            try:
                self.broker.cancel_order(intent["broker_order_id"])
                self.store.update_intent(intent["id"], "CANCEL_REQUESTED")
                self.store.audit("STALE_ORDER_CANCELLED", {"intent_id": intent["id"], "age": age, "urgent": is_urgent})
                cancelled += 1
            except Exception as exc:
                self.alerts.send("ERROR", "CANCEL_FAILED", f"订单 {intent['broker_order_id']} 超时撤单失败: {exc}")
        return cancelled
