from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import uuid

import pandas as pd

from .alerts import AlertManager
from .combo_analytics import capacity_assessment, combo_market_snapshot
from .option_lifecycle import parse_option_code


FINAL_FILLED = {"FILLED", "SHADOW_FILLED"}
WORKING = {"CREATED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "SHADOW"}
FAILED = {"FAILED", "REJECTED", "CANCELLED", "CANCELLED_ALL", "CANCELLED_PART", "UNKNOWN", "APPROVAL_REQUIRED"}
OPEN_PARTIAL = {"FILLED_PART", "PARTIAL_FILLED", "OPEN_PARTIAL"}


def _finite_positive(value, *, integer: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("组合数量/价格不能是布尔值")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("组合数量和价格必须是有限正数")
    if integer and not number.is_integer():
        raise ValueError("组合腿数量必须是整数")
    return number


def _protected_relation(long_contract, short_contract) -> bool:
    return (
        long_contract.option_type == short_contract.option_type
        and long_contract.expiration >= short_contract.expiration
        and (
            (short_contract.option_type in {"C", "CALL"} and long_contract.strike > short_contract.strike)
            or (short_contract.option_type in {"P", "PUT"} and long_contract.strike < short_contract.strike)
        )
    )


def validate_combo(combo: dict, legs: list[dict]) -> tuple[float, float]:
    if not 2 <= len(legs) <= 4:
        raise ValueError("期权组合必须包含 2 至 4 条腿")
    underlying = str(combo.get("underlying", "")).upper()
    if not underlying.startswith("US."):
        raise ValueError("组合正股代码必须使用 US.* 格式")
    symbols = [str(leg.get("symbol", "")).upper() for leg in legs]
    if len(set(symbols)) != len(symbols):
        raise ValueError("组合不得包含重复期权合约腿")
    parsed = []
    for leg in legs:
        contract = parse_option_code(str(leg.get("symbol", "")).upper())
        if contract is None or contract.underlying != underlying:
            raise ValueError("每条腿都必须是同一正股的标准富途期权代码")
        if leg.get("side") not in {"BUY", "SELL"}:
            raise ValueError("组合腿方向只能是 BUY 或 SELL")
        _finite_positive(leg.get("quantity", 0), integer=True)
        _finite_positive(leg.get("limit_price", 0))
        parsed.append((leg, contract))
    # Every opening short must be capped by a filled-first long option with the
    # same expiry/type and sufficient quantity. This deliberately excludes naked legs.
    for short, contract in parsed:
        if short["side"] != "SELL":
            continue
        covered = sum(
            int(long["quantity"])
            for long, long_contract in parsed
            if long["side"] == "BUY"
            and _protected_relation(long_contract, contract)
        )
        if covered < int(short["quantity"]):
            raise ValueError(f"卖出腿 {short['symbol']} 没有足量、同到期日的价差保护腿")
    debit = sum(
        (1 if leg["side"] == "BUY" else -1)
        * int(leg["quantity"]) * float(leg["limit_price"])
        for leg in legs
    )
    max_debit = float(combo.get("max_net_debit", 0) or 0)
    min_credit = float(combo.get("min_net_credit", 0) or 0)
    if debit > 0 and (max_debit <= 0 or debit > max_debit + 1e-9):
        raise ValueError(f"组合预计净借记 ${debit:.2f}，超过允许上限 ${max_debit:.2f}")
    credit = max(0.0, -debit)
    if credit > 0 and credit + 1e-9 < min_credit:
        raise ValueError(f"组合预计净贷记 ${credit:.2f}，低于要求 ${min_credit:.2f}")
    return debit, credit


def create_combo(store, combo: dict, legs: list[dict]) -> str:
    normalized = dict(combo)
    normalized["combo_id"] = normalized.get("combo_id") or uuid.uuid4().hex[:12]
    normalized["underlying"] = str(normalized["underlying"]).upper()
    normalized_legs = [{**leg, "symbol": str(leg["symbol"]).upper()} for leg in legs]
    validate_combo(normalized, normalized_legs)
    return store.create_option_combo(normalized, normalized_legs)


def find_open_combo_for_campaign(
    store,
    *,
    strategy_id: str,
    underlying: str,
    structure: str = "",
    leg_symbols: set[str] | None = None,
) -> dict | None:
    """Recover an already-open automatic combo when campaign identity drifted."""
    leg_symbols = {str(symbol).upper() for symbol in (leg_symbols or set()) if symbol}
    candidates = []
    for combo in store.option_combos(limit=500):
        if combo.get("strategy_id") != strategy_id:
            continue
        if combo.get("status") not in {"OPEN", "EXECUTING", "CLOSING", "ROLLBACK_PENDING"}:
            continue
        if str(combo.get("underlying", "")).upper() != str(underlying).upper():
            continue
        if structure and combo.get("structure") and combo.get("structure") != structure:
            continue
        combo_legs = {
            str(leg.get("symbol", "")).upper()
            for leg in store.option_combo_legs(combo["combo_id"])
        }
        overlap = len(leg_symbols & combo_legs) if leg_symbols else 0
        candidates.append((overlap, combo))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]["created_at"]), reverse=True)[0][1]


class ComboExecutionManager:
    def __init__(self, broker, store, executor, positions_snapshot: pd.DataFrame | None = None):
        self.broker = broker
        self.store = store
        self.executor = executor
        # A service cycle may inspect many failed/orphaned combos. Reuse the
        # single broker position snapshot for every exposure check instead of
        # issuing one slow OpenD query per combo.
        self._positions_snapshot_cache = positions_snapshot
        self.alerts = AlertManager(store)

    def start(self, combo_id: str) -> dict:
        combo = self._required_combo(combo_id)
        legs = self.store.option_combo_legs(combo_id)
        validate_combo(combo, legs)
        if combo["status"] not in {"DRAFT", "FAILED"}:
            return combo
        if combo["status"] == "FAILED":
            if not hasattr(self.broker, "positions"):
                if any(float(leg.get("filled_quantity", 0) or 0) > 0 for leg in legs):
                    raise ValueError("组合已有成交腿，禁止直接重试；请先完成回滚或人工风险处置")
                self._prepare_retry(combo, legs)
            else:
                try:
                    exposed = self._combo_has_broker_exposure(combo_id)
                except RuntimeError:
                    if any(float(leg.get("filled_quantity", 0) or 0) > 0 for leg in legs):
                        raise ValueError("无法确认券商暴露，且组合存在本地成交腿，禁止重试")
                    raise
                if exposed:
                    self.store.update_option_combo(combo_id, "ROLLBACK_PENDING", "失败组合仍有券商暴露，禁止重新开仓并转入回滚")
                    self.store.option_combo_event(combo_id, "FAILED_RETRY_BLOCKED_EXPOSURE", "ROLLBACK_PENDING", "失败组合存在实际持仓，禁止盲目重试")
                    self.advance(combo_id)
                    return self._required_combo(combo_id)
                self._prepare_retry(combo, legs)
            combo = self._required_combo(combo_id)
            legs = self.store.option_combo_legs(combo_id)
        if combo.get("requires_parent_closed") and combo.get("parent_combo_id"):
            parent = self.store.option_combo(combo["parent_combo_id"])
            if not parent or parent["status"] != "CLOSED":
                raise ValueError("旧组合尚未完全平仓，滚仓/调整草稿禁止启动")
        quotes = self.broker.snapshots([leg["symbol"] for leg in legs])
        detail, package = combo_market_snapshot(legs, quotes)
        capacity = capacity_assessment(detail, requested_contracts=1)
        if not capacity["allowed"]:
            raise ValueError(f"组合容量检查未通过：{capacity['reason']}")
        for _, row in detail.iterrows():
            if float(row["mid"]) > 0:
                self.store.update_option_combo_leg(int(row["id"]), limit_price=round(float(row["mid"]), 2))
        legs = self.store.option_combo_legs(combo_id)
        validate_combo(combo, legs)
        self.store.update_option_combo(combo_id, "EXECUTING")
        self.store.option_combo_event(
            combo_id, "EXECUTION_STARTED", "EXECUTING", "从组合中间价开始受控分腿开仓",
            {**package, **capacity},
        )
        self.advance(combo_id)
        return self._required_combo(combo_id)

    def _prepare_retry(self, combo: dict, legs: list[dict]) -> None:
        if any(float(leg.get("filled_quantity", 0) or 0) > 0 for leg in legs):
            raise ValueError("组合已有成交腿，禁止直接重试；请先完成回滚或人工风险处置")
        for leg in legs:
            for intent_id in (leg.get("intent_id"), leg.get("close_intent_id")):
                intent = self.store.intent(int(intent_id)) if intent_id else None
                if intent and intent.get("broker_order_id") and str(intent.get("status")) in WORKING | OPEN_PARTIAL:
                    try:
                        self.broker.cancel_order(intent["broker_order_id"])
                    except Exception as exc:
                        raise RuntimeError(f"重试前撤销旧券商订单失败：{intent['broker_order_id']}：{exc}") from exc
        for leg in legs:
            self.store.update_option_combo_leg(
                leg["id"], status="PLANNED", intent_id=None, close_intent_id=None,
                filled_quantity=0, average_fill_price=0, error="", close_attempts=0,
            )
        self.store.update_option_combo(combo["combo_id"], "DRAFT")
        self.store.option_combo_event(
            combo["combo_id"], "RETRY_PREPARED", "DRAFT",
            "已清理上一轮未成交失败状态，准备新的幂等执行代次",
        )

    def request_close(self, combo_id: str) -> dict:
        combo = self._required_combo(combo_id)
        if combo["status"] != "OPEN":
            raise ValueError("只有已完整开仓的组合可以发起组合平仓")
        self.store.update_option_combo(combo_id, "CLOSING")
        self.store.option_combo_event(combo_id, "CLOSE_REQUESTED", "CLOSING", "开始组合平仓：先回补短腿")
        self.advance(combo_id)
        return self._required_combo(combo_id)

    def monitor_all(
        self,
        active_combo_ids: set[str] | None = None,
        *,
        close_orphan_open: bool = False,
        allow_open_execution: bool = True,
    ) -> list[dict]:
        results = []
        active_combo_ids = active_combo_ids or set()
        for combo in self.store.option_combos(limit=500):
            if combo["status"] in {"EXECUTING", "CLOSING", "ROLLBACK_PENDING", "FAILED"}:
                legs = self.store.option_combo_legs(combo["combo_id"])
                if combo["status"] == "EXECUTING" and not allow_open_execution:
                    self._cancel_working(legs)
                    continue
                if combo["status"] == "FAILED" and not self._combo_has_broker_exposure(combo["combo_id"]):
                    continue
                if combo["status"] == "FAILED":
                    self.store.update_option_combo(combo["combo_id"], "ROLLBACK_PENDING", "失败组合仍有券商暴露，自动恢复残腿平仓")
                    self.store.option_combo_event(combo["combo_id"], "FAILED_WITH_EXPOSURE_RECOVERED", "ROLLBACK_PENDING", "失败状态仍有残腿，恢复回滚平仓")
                results.append(self.advance(combo["combo_id"]))
            elif (
                close_orphan_open
                and combo["status"] == "OPEN"
                and combo.get("strategy_id") == "volatility_long_gamma"
                and combo["combo_id"] not in active_combo_ids
                and self._combo_has_broker_exposure(combo["combo_id"])
            ):
                self.store.option_combo_event(
                    combo["combo_id"], "ORPHAN_OPEN_COMBO_CLOSE_REQUESTED", "CLOSING",
                    "OPEN 组合已脱离当前波动率活动接管，自动进入组合级平仓",
                    {"active_combo_ids": sorted(active_combo_ids)},
                )
                results.append(self.request_close(combo["combo_id"]))
        return results

    def advance(self, combo_id: str) -> dict:
        combo = self._required_combo(combo_id)
        legs = self.store.option_combo_legs(combo_id)
        self._refresh_legs(legs)
        legs = self.store.option_combo_legs(combo_id)
        if combo["status"] == "EXECUTING":
            self._advance_open(combo, legs)
        elif combo["status"] in {"CLOSING", "ROLLBACK_PENDING"}:
            self._advance_close(combo, legs)
        return self._required_combo(combo_id)

    def _advance_open(self, combo: dict, legs: list[dict]) -> None:
        combo_id = combo["combo_id"]
        if any(leg["status"] == "FAILED" for leg in legs):
            if combo["auto_rollback"] and any(leg["status"] == "FILLED" for leg in legs):
                self.store.update_option_combo(combo_id, "ROLLBACK_PENDING", "部分腿失败，自动回滚")
                self.store.option_combo_event(combo_id, "ROLLBACK_STARTED", "ROLLBACK_PENDING", "部分腿失败，开始反向平仓")
                self._advance_close(self._required_combo(combo_id), legs)
            else:
                self.store.update_option_combo(combo_id, "FAILED", "组合腿提交失败")
            return
        if all(leg["status"] == "FILLED" for leg in legs):
            self.store.update_option_combo(combo_id, "OPEN")
            self.store.option_combo_event(combo_id, "OPENED", "OPEN", "全部组合腿已成交")
            return
        self._reprice_working_combo(combo, legs)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(combo["updated_at"])).total_seconds()
        if age > int(combo["assembly_timeout_seconds"]):
            self._cancel_working(legs)
            target = "ROLLBACK_PENDING" if any(leg["status"] == "FILLED" for leg in legs) else "FAILED"
            self.store.update_option_combo(combo_id, target, "组合装配超时")
            self.store.option_combo_event(combo_id, "ASSEMBLY_TIMEOUT", target, "组合未在允许时间内完成")
            return
        for leg in legs:
            if leg["status"] != "PLANNED":
                continue
            protected_short = leg["side"] == "SELL"
            if protected_short and not self._short_is_protected(leg, legs):
                continue
            self._submit_leg(combo, leg, closing=False, protected_short=protected_short)
            return

    def _advance_close(self, combo: dict, legs: list[dict]) -> None:
        filled = [leg for leg in legs if leg["filled_quantity"] > 0]
        if not filled:
            if combo["status"] == "ROLLBACK_PENDING":
                try:
                    if self._combo_has_broker_exposure(combo["combo_id"]):
                        self.store.option_combo_event(
                            combo["combo_id"], "ROLLBACK_EXPOSURE_UNCONFIRMED", "ROLLBACK_PENDING",
                            "本地成交记录为空但券商仍有暴露，继续等待风险接管",
                        )
                        return
                except RuntimeError:
                    self.store.option_combo_event(
                        combo["combo_id"], "ROLLBACK_BROKER_QUERY_UNKNOWN", "ROLLBACK_PENDING",
                        "无法确认券商持仓，拒绝将回滚标记为失败结束",
                    )
                    return
                self.store.update_option_combo(combo["combo_id"], "FAILED", "回滚无本地成交且券商确认无暴露")
                return
            self.store.update_option_combo(combo["combo_id"], "CLOSED")
            return
        self._emergency_naked_check(combo, legs)
        self._reprice_working_combo(combo, legs)
        # A close order can be submitted by the emergency naked-short path.
        # Do not submit a duplicate combo close while that intent is working.
        for active_leg in filled:
            close_id = active_leg.get("close_intent_id")
            if not close_id:
                continue
            close_intent = self.store.intent(int(close_id))
            if close_intent and str(close_intent.get("status")) in WORKING | OPEN_PARTIAL | {"CANCEL_REQUESTED"}:
                return
        failed_closes = [leg for leg in filled if leg["status"] == "CLOSE_FAILED"]
        if failed_closes:
            leg = failed_closes[0]
            attempts = int(leg.get("close_attempts", 0))
            message = f"{leg['symbol']} 组合平仓未成交，继续风险回滚"
            if attempts >= 3:
                self.store.update_option_combo(combo["combo_id"], "ROLLBACK_PENDING", message)
                self.alerts.send(
                    "ERROR", "COMBO_CLOSE_FAILED", message,
                    incident_key=f"COMBO_CLOSE_FAILED:{combo['combo_id']}:{leg['id']}",
                )
            self.store.update_option_combo_leg(
                leg["id"], status="FILLED", close_intent_id=None, error="",
            )
            return
        short_legs = [leg for leg in filled if leg["side"] == "SELL"]
        short_open = [leg for leg in short_legs if leg["status"] != "CLOSED"]
        candidates = short_open or [leg for leg in filled if leg["side"] == "BUY" and leg["status"] != "CLOSED"]
        if not candidates:
            final = "FAILED" if combo["status"] == "ROLLBACK_PENDING" else "CLOSED"
            self.store.update_option_combo(combo["combo_id"], final)
            self.store.option_combo_event(combo["combo_id"], "CLOSED", final, "组合风险已全部解除")
            return
        leg = candidates[0]
        if not leg.get("close_intent_id"):
            self._submit_leg(combo, leg, closing=True, protected_short=False)

    def _submit_leg(self, combo: dict, leg: dict, closing: bool, protected_short: bool) -> None:
        side = ("BUY" if leg["side"] == "SELL" else "SELL") if closing else leg["side"]
        quantity = int(leg["filled_quantity"] if closing else leg["quantity"])
        if closing:
            quantity = max(0, quantity - int(leg.get("close_filled_quantity", 0) or 0))
            quantity, quantity_reason = self._closing_quantity(leg, side, quantity)
            if quantity <= 0:
                self.store.update_option_combo_leg(
                    leg["id"], status="CLOSED", close_intent_id=None,
                    close_filled_quantity=float(leg.get("filled_quantity", 0) or 0),
                    remaining_quantity=0,
                    error=quantity_reason,
                )
                self.store.option_combo_event(
                    combo["combo_id"], "LEG_ALREADY_FLAT", "CLOSED",
                    f"{leg['symbol']} 已无需要平仓的持仓：{quantity_reason}",
                    {"leg_id": leg["id"], "closing_side": side},
                )
                return
            if quantity_reason:
                self.store.option_combo_event(
                    combo["combo_id"], "LEG_CLOSE_QUANTITY_ADJUSTED", "WARNING",
                    f"{leg['symbol']} 平仓数量调整为 {quantity}：{quantity_reason}",
                    {"leg_id": leg["id"], "closing_side": side, "quantity": quantity},
                )
        price = self._marketable_price(leg["symbol"], side, float(leg["limit_price"])) if closing else float(leg["limit_price"])
        attempts = sum(
            event["event_type"] == "EXECUTION_STARTED"
            for event in self.store.option_combo_events(combo["combo_id"], limit=1000)
        )
        intent_key = (
            f"combo:{combo['combo_id']}:{'close' if closing else 'open'}:"
            f"{leg['id']}:attempt:{max(1, attempts)}"
            + (f":close:{int(leg.get('close_attempts', 0)) + 1}" if closing else "")
        )
        try:
            result = self.executor.submit_limit(
                combo["strategy_id"], intent_key, leg["symbol"], side, quantity, price,
                {"underlying": combo["underlying"], "multiplier": 100, "combo_id": combo["combo_id"],
                 "combo_leg_id": leg["id"], "protected_short": protected_short,
                 "reduce_only": closing, "urgent": closing, "smart_execution": closing,
                  "close_filled_before": float(leg.get("close_filled_quantity", 0) or 0) if closing else 0},
            )
        except Exception as exc:
            saved = next(
                (item for item in self.store.intents(limit=200)
                 if str(item["intent_key"]).startswith(intent_key + ":")), None,
            )
            field = "close_intent_id" if closing else "intent_id"
            self.store.update_option_combo_leg(
                leg["id"], **{field: saved["id"] if saved else None,
                               "status": "CLOSE_FAILED" if closing else "FAILED", "error": str(exc)},
            )
            self.store.option_combo_event(
                combo["combo_id"], "LEG_SUBMIT_FAILED", "FAILED", f"{leg['symbol']}：{exc}",
            )
            return
        field = "close_intent_id" if closing else "intent_id"
        status = "CLOSE_WORKING" if closing else ("WORKING" if result.status in WORKING else "FAILED")
        # Approval/risk rejections happen before an order intent exists and use
        # intent_id=0. Persisting 0 into a foreign-key column caused the unified
        # service to fail its whole combo-monitor task.
        values = {
            field: result.intent_id if result.intent_id > 0 else None,
            "status": status,
            "error": result.message,
        }
        if closing:
            values["close_attempts"] = int(leg.get("close_attempts", 0)) + 1
        self.store.update_option_combo_leg(leg["id"], **values)
        self.store.option_combo_event(
            combo["combo_id"], "LEG_CLOSE_SUBMITTED" if closing else "LEG_OPEN_SUBMITTED",
            result.status, f"{leg['symbol']} {side} {quantity}：{result.message}",
            {"leg_id": leg["id"], "intent_id": result.intent_id or None},
        )

    def _positions_snapshot(self) -> pd.DataFrame | None:
        if self._positions_snapshot_cache is not None:
            return self._positions_snapshot_cache
        if not hasattr(self.broker, "positions"):
            return None
        try:
            positions = self.broker.positions(refresh=True)
        except TypeError:
            try:
                positions = self.broker.positions()
            except Exception as exc:
                raise RuntimeError(f"券商持仓查询失败：{exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"券商持仓查询失败：{exc}") from exc
        if positions is None:
            raise RuntimeError("券商持仓查询返回空值")
        self._positions_snapshot_cache = positions if isinstance(positions, pd.DataFrame) else pd.DataFrame(positions)
        return self._positions_snapshot_cache

    def _combo_has_broker_exposure(self, combo_id: str) -> bool:
        if not hasattr(self.broker, "positions"):
            # Test/replay adapters without broker positions cannot report live
            # exposure; production Futu brokers always implement this method.
            return False
        positions = self._positions_snapshot()
        if positions is None:
            raise RuntimeError("券商持仓查询不可用，拒绝将组合暴露判定为零")
        if "code" not in positions.columns:
            # A valid empty broker response may omit columns in lightweight PAPER
            # adapters; only treat non-empty malformed data as unknown.
            if not positions.empty:
                raise RuntimeError("券商持仓响应缺少 code，拒绝将组合暴露判定为零")
            return False
        if positions.empty:
            return False
        leg_codes = {
            str(leg.get("symbol", "")).upper()
            for leg in self.store.option_combo_legs(combo_id)
        }
        if not leg_codes:
            return False
        matched = positions[positions["code"].astype(str).str.upper().isin(leg_codes)]
        for _, row in matched.iterrows():
            quantity = self._position_number(
                row, ("qty", "quantity", "position", "position_qty"),
            )
            if quantity is None:
                raise RuntimeError("券商持仓数量未知，拒绝判定组合无暴露")
            if abs(quantity) > 1e-9:
                return True
        return False

    @staticmethod
    def _position_number(row: pd.Series, columns: tuple[str, ...]) -> float | None:
        for column in columns:
            if column not in row:
                continue
            try:
                value = float(row.get(column) or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return None

    def _closing_quantity(self, leg: dict, closing_side: str, requested: int) -> tuple[int, str]:
        """Cap a closing order to the broker-visible exposure.

        Automatic combos can be partially closed manually in Futu, or one leg can
        be flattened by a previous recovery attempt. Submitting the original
        ratio again creates repeated "持仓不足" failures. If broker positions are
        unavailable we keep the existing deterministic behavior; if they are
        available, the broker ledger becomes the source of truth for reduce-only
        combo closes.
        """
        requested = max(0, int(requested))
        if not hasattr(self.broker, "positions"):
            return requested, ""
        # Never reuse a service-cycle snapshot after an intervening close fill.
        self._positions_snapshot_cache = None
        positions = self._positions_snapshot()
        if positions is None:
            raise RuntimeError("券商持仓不可用，拒绝计算减仓数量")
        if "code" not in positions.columns:
            raise RuntimeError("券商持仓响应缺少 code，拒绝计算减仓数量")
        symbol = str(leg.get("symbol", "")).upper()
        matched = positions[positions["code"].astype(str).str.upper() == symbol]
        if matched.empty:
            return 0, "富途当前无该合约持仓，视为已经平掉"

        quantities = [
            self._position_number(row, ("qty", "quantity", "position", "position_qty"))
            for _, row in matched.iterrows()
        ]
        if any(value is None for value in quantities):
            raise RuntimeError("券商持仓数量未知，拒绝计算减仓数量")
        qty = sum(quantities)
        sellable_values = [
            self._position_number(row, ("can_sell_qty", "sellable_qty", "avail_qty", "available_qty"))
            for _, row in matched.iterrows()
        ]
        sellable = sum(value for value in sellable_values if value is not None)
        if closing_side == "SELL":
            exposure = max(qty, 0.0)
            if sellable_values and sellable <= 0:
                exposure = 0.0
            elif sellable_values:
                exposure = min(exposure if exposure > 0 else sellable, sellable)
            if exposure <= 0:
                return 0, f"富途当前多头数量 {qty:g}，没有可卖出的多头腿"
        else:
            exposure = abs(min(qty, 0.0))
            if exposure <= 0:
                return 0, f"富途当前数量 {qty:g}，没有需要买入回补的空头腿"

        adjusted = min(requested, int(math.floor(exposure + 1e-9)))
        if adjusted < requested:
            return adjusted, f"请求 {requested} 张，但富途可平仓暴露只有 {exposure:g} 张"
        return adjusted, ""

    def _marketable_price(self, symbol: str, side: str, fallback: float) -> float:
        try:
            quote = self.broker.snapshots([symbol])
            if not quote.empty:
                column = "ask_price" if side == "BUY" else "bid_price"
                value = float(quote.iloc[0].get(column, 0) or 0)
                if value > 0:
                    return value
        except Exception:
            pass
        return fallback

    def _reprice_working_combo(self, combo: dict, legs: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        for leg in legs:
            if leg["status"] not in {"WORKING", "CLOSE_WORKING", "OPEN_PARTIAL"}:
                continue
            closing = leg["status"] == "CLOSE_WORKING"
            intent_id = leg.get("close_intent_id" if closing else "intent_id")
            if not intent_id:
                continue
            intent = self.store.intent(int(intent_id))
            if (not intent or str(intent.get("status")) not in WORKING | OPEN_PARTIAL
                    or not intent.get("broker_order_id") or int(intent.get("reprice_count", 0)) >= 4):
                continue
            side = ("SELL" if leg["side"] == "BUY" else "BUY") if closing else leg["side"]
            if intent.get("side") != side:
                continue
            anchor = intent.get("last_reprice_at") or intent["created_at"]
            if (now - datetime.fromisoformat(anchor)).total_seconds() < 30:
                continue
            try:
                quote = self.broker.snapshots([leg["symbol"]]).iloc[0]
                current = float(intent["limit_price"])
                market = float(quote.get("ask_price" if side == "BUY" else "bid_price", 0) or 0)
                if not math.isfinite(market) or market <= 0:
                    continue
                if (side == "BUY" and market <= current) or (side == "SELL" and market >= current):
                    continue
                step = max(0.01, round(abs(market - current) / 4, 2))
                target = min(market, current + step) if side == "BUY" else max(market, current - step)
                projected = sum(
                    (1 if item["side"] == "BUY" else -1) * int(item["quantity"])
                    * (target if item["id"] == leg["id"] else float(item["limit_price"]))
                    for item in legs
                )
                max_debit = float(combo["max_net_debit"] or 0)
                min_credit = float(combo["min_net_credit"] or 0)
                if not closing and ((projected > 0 and (max_debit <= 0 or projected > max_debit)) or (
                    projected < 0 and -projected < min_credit
                )):
                    continue
                self.broker.modify_limit_order(intent["broker_order_id"], int(intent["quantity"]), target)
                self.store.record_reprice(intent["id"], target)
                if not closing:
                    self.store.update_option_combo_leg(leg["id"], limit_price=target)
                self.store.option_combo_event(
                    combo["combo_id"], "PACKAGE_REPRICED", "EXECUTING",
                    f"{leg['symbol']} 限价调整为 ${target:.2f}，组合净价仍在边界内",
                    {"projected_net_debit": projected},
                )
            except Exception as exc:
                self.store.option_combo_event(
                    combo["combo_id"], "PACKAGE_REPRICE_FAILED", "WARNING", str(exc),
                )

    def _refresh_legs(self, legs: list[dict]) -> None:
        for leg in legs:
            intent_id = leg.get("close_intent_id") or leg.get("intent_id")
            if not intent_id:
                continue
            intent = self.store.intent(int(intent_id))
            if not intent:
                continue
            status = str(intent["status"])
            if leg.get("close_intent_id"):
                before = float(json.loads(intent.get("metadata_json") or "{}").get("close_filled_before") or 0)
                close_filled = min(
                    float(leg.get("filled_quantity", 0) or 0),
                    max(float(leg.get("close_filled_quantity", 0) or 0),
                        before + float(intent.get("filled_quantity") or 0)),
                )
                if status in FINAL_FILLED or status in WORKING or status in OPEN_PARTIAL:
                    self.store.update_option_combo_leg(
                        leg["id"], close_filled_quantity=close_filled,
                        remaining_quantity=max(float(leg.get("filled_quantity", 0) or 0) - close_filled, 0),
                        status=("CLOSED" if close_filled >= float(leg.get("filled_quantity", 0) or 0)
                                else "CLOSE_FAILED" if status in FINAL_FILLED else "CLOSE_WORKING"),
                    )
                elif status in FAILED:
                    self.store.update_option_combo_leg(
                        leg["id"], status="CLOSE_FAILED", close_filled_quantity=close_filled,
                        remaining_quantity=max(float(leg.get("filled_quantity", 0) or 0) - close_filled, 0),
                        error=intent.get("error") or status,
                    )
            elif status in FINAL_FILLED or status in WORKING or status in OPEN_PARTIAL:
                filled = min(float(leg.get("quantity", 0) or 0), float(intent.get("filled_quantity") or 0))
                self.store.update_option_combo_leg(
                    leg["id"], status="FILLED" if status in FINAL_FILLED and filled >= float(leg.get("quantity", 0) or 0) else "OPEN_PARTIAL",
                    filled_quantity=max(float(leg.get("filled_quantity", 0) or 0), filled),
                    remaining_quantity=max(float(leg.get("quantity", 0) or 0) - filled, 0),
                    average_fill_price=float(intent.get("average_fill_price") or leg["limit_price"]),
                )
            elif status in FAILED:
                self.store.update_option_combo_leg(leg["id"], status="FAILED", error=intent.get("error") or status)

    def _short_is_protected(self, short: dict, legs: list[dict]) -> bool:
        short_contract = parse_option_code(short["symbol"])
        return sum(
            int(leg["filled_quantity"])
            for leg in legs
            if leg["side"] == "BUY" and leg["status"] == "FILLED"
            and (contract := parse_option_code(leg["symbol"])) is not None
            and _protected_relation(contract, short_contract)
        ) >= int(short["quantity"])

    def _emergency_naked_check(self, combo: dict, legs: list[dict]) -> None:
        for leg in legs:
            if leg["side"] == "SELL" and float(leg.get("filled_quantity", 0) or 0) > 0 and not self._short_is_protected(leg, legs):
                self.alerts.send(
                    "ERROR", "COMBO_NAKED_EXPOSURE",
                    f"组合 {combo['combo_id']} 的 {leg['symbol']} 保护缺失，"
                    f"必须在 {combo['naked_timeout_seconds']} 秒保护时限内优先回补短腿",
                    incident_key=f"COMBO_NAKED_EXPOSURE:{combo['combo_id']}:{leg['id']}",
                )

    def _cancel_working(self, legs: list[dict]) -> None:
        for leg in legs:
            if leg["status"] not in {"WORKING", "CLOSE_WORKING", "OPEN_PARTIAL"}:
                continue
            intent_id = leg.get("close_intent_id" if leg["status"] == "CLOSE_WORKING" else "intent_id")
            if not intent_id:
                continue
            intent = self.store.intent(int(intent_id))
            if intent and str(intent.get("status")) in WORKING | OPEN_PARTIAL and intent.get("broker_order_id"):
                try:
                    self.broker.cancel_order(intent["broker_order_id"])
                    self.store.update_intent(intent["id"], "CANCEL_REQUESTED")
                except Exception as exc:
                    self.store.update_option_combo_leg(leg["id"], error=f"超时撤单失败：{exc}")

    def _required_combo(self, combo_id: str) -> dict:
        combo = self.store.option_combo(combo_id)
        if combo is None:
            raise ValueError("找不到期权组合")
        return combo
