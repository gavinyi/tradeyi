from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any

import pandas as pd

from .risk import RiskEngine
from .alerts import AlertManager
from .store import TradingStore
from .entry_gate import (
    EntrySignals,
    audit_payload as gate_audit_payload,
    resolve_entry_block_reasons,
)
from .approval import (
    approval_enforcement_enabled, approval_runtime_manifest, runtime_strategy_fingerprint,
    strategy_fingerprint, strategy_requires_approval,
)
from .fee_model import estimate_fees
from .real_trading import check_real_trade, resolve_real_account_limits
from .risk_limits import RiskLimitsConfigError, resolve_risk_limits
from .strategy_pause import is_strategy_entries_paused
from .execution_mode import strategy_execution_mode_override

logger = logging.getLogger("futu_quant.execution")

#: 我方发过撤单的审计事件名（`cancel_stale_orders` 写 `STALE_ORDER_CANCELLED`；
#: 另外两个是历史/手工撤单路径，一并认，避免证据漏认）。
CANCEL_AUDIT_EVENTS = frozenset({
    "STALE_ORDER_CANCELLED", "CANCEL_REQUESTED", "ORDER_CANCEL_REQUESTED",
})


def resolve_cancel_aware_status(
    raw_status: str,
    *,
    filled_quantity: float,
    submitted: bool,
    cancel_requested: bool,
) -> str:
    """券商 `FAILED` 的**按证据改判**（2026-10-27 缺陷修复，纯函数便于守卫）。

    缺陷：券商回报 `FAILED` 时本地无法区分「我方主动撤单」与「券商拒绝」——
    `audit_events` / `order_state_events` 里查无 CANCEL 实据，故改判逻辑
    以「本地是否发出过撤单请求」为唯一证据来源，不依赖任何未证实的时序归因。

    改判规则（**只有满足全部条件才改判**，其余一律保持 FAILED）：

    - `submitted and cancel_requested and filled == 0` → `CANCELLED`（主动撤单）；
    - `submitted and cancel_requested and filled > 0` → `CANCELLED_PART`（部分成交后撤单）；
    - 未提交（券商直接拒绝/交易前检查失败）→ 保持 `FAILED`；
    - 非 `FAILED` 的券商状态一律原样返回（本函数只管 FAILED 这一种歧义）。
    """
    status = str(raw_status or "").strip().upper()
    if status != "FAILED":
        return status
    try:
        filled = float(filled_quantity or 0)
    except (TypeError, ValueError):
        filled = 0.0
    if not submitted or not cancel_requested:
        return "FAILED"
    return "CANCELLED_PART" if filled > 0 else "CANCELLED"




@dataclass(frozen=True)
class ExecutionResult:
    intent_id: int
    status: str
    message: str
    broker_order_id: str | None = None
    broker_response: pd.DataFrame | None = None


class ExecutionManager:
    def __init__(
        self,
        broker,
        store: TradingStore | None = None,
        alerts: AlertManager | None = None,
        tenant: Any | None = None,
        tenant_registry: dict[str, Any] | None = None,
    ):
        self.broker = broker
        self.store = store or TradingStore()
        self.risk = RiskEngine(self.store, broker=self.broker)
        self.alerts = alerts or AlertManager(self.store)
        self.tenant_registry = tenant_registry

        from .tenant import resolve_tenant
        broker_acc = getattr(self.broker, "account_id", None)
        broker_settings = getattr(self.broker, "settings", None)
        raw_env = getattr(self.broker, "trading_env", None)
        if raw_env is None or not isinstance(raw_env, (str, bytes)):
            raw_env = getattr(broker_settings, "trading_env", "SIMULATE")
        broker_env = str(raw_env).split(".")[-1].upper()

        self.tenant = resolve_tenant(
            tenant,
            account_id=broker_acc,
            env=broker_env,
            registry=self.tenant_registry,
            strict=bool(tenant),
        )
        if tenant and self.tenant:
            if broker_env != self.tenant.trading_env:
                raise ValueError(
                    f"租户环境冲突：租户 '{self.tenant.tenant_id}' 声明环境为 {self.tenant.trading_env}，"
                    f"但券商实际环境为 {broker_env}"
                )
            if broker_acc and str(broker_acc).strip() != str(self.tenant.account_id).strip():
                raise ValueError(
                    f"租户账号冲突：租户 '{self.tenant.tenant_id}' 绑定账号为 {self.tenant.account_id}，"
                    f"但券商实际账号为 {broker_acc}"
                )

    def submit_limit(
        self, strategy_id: str, intent_key: str, symbol: str, side: str,
        quantity: int, price: float, metadata: dict[str, Any] | None = None,
        execution_mode_override: str | None = None,
    ) -> ExecutionResult:
        metadata = dict(metadata or {})
        # Bind every intent to the concrete broker account / firm before any
        # idempotency, risk, or REAL-gate decision. REAL tenants must never
        # inherit authorization or daily budget from another REAL account.
        broker_account_id = getattr(self.broker, "account_id", None)
        if broker_account_id is not None:
            metadata.setdefault("account_id", str(broker_account_id))
        broker_firm = getattr(self.broker, "security_firm", None)
        broker_firm_name = getattr(broker_firm, "name", str(broker_firm or ""))
        if broker_firm_name:
            metadata.setdefault("security_firm", str(broker_firm_name).split(".")[-1].upper())
        # 铁律：实盘账户开启/关闭生命周期检查：已关闭统一管理的账户，严禁向其提交任何订单！
        if broker_account_id:
            broker_settings_obj = getattr(self.broker, "settings", None)
            if broker_settings_obj is None:
                from .config import Settings
                broker_settings_obj = Settings.load()
            limits = resolve_real_account_limits(
                self.store, broker_settings_obj, broker_account_id,
            )
            if not limits.enabled:
                reason = f"账户管理安全铁律：实盘账户 {broker_account_id} 已关闭统一管理，禁止向其提交任何订单"
                self.store.audit("DISABLED_ACCOUNT_ORDER_REJECTED", {"account_id": str(broker_account_id), "strategy_id": strategy_id}, actor="service:execution")
                return ExecutionResult(0, "REJECTED", reason)
        global_mode = self.store.get_setting("execution_mode", os.getenv("EXECUTION_MODE", "PAPER"))
        tenant_id = (
            getattr(self.tenant, "tenant_id", "") if self.tenant else ""
        ) or getattr(self.broker, "tenant_id", "") or str(metadata.get("tenant_id") or "")
        if tenant_id:
            metadata.setdefault("tenant_id", tenant_id)
        tenant_mode_override = strategy_execution_mode_override(self.store, tenant_id, strategy_id)
        override_mode = tenant_mode_override
        # 红区 fail-open 收口（Reviewer）：非法覆盖值**绝不能**参与后续提升。
        # 实盘白名单提升的判据是 `override_mode != "SHADOW"`，所以 ``INHERIT``/拼错的
        # ``SHAODW``/手工改库的垃圾值都会被当成「不是 SHADOW」→ 提升成 REAL → 下真金单。
        # 配置错误必须拒单 + 可见，不得静默兜底。
        _allowed_modes = {"SHADOW", "PAPER", "REAL"}
        _invalid_override = next(
            (
                str(value).upper()
                for value in (execution_mode_override, override_mode)
                if value and str(value).strip().upper() not in _allowed_modes
            ),
            "",
        )
        if _invalid_override:
            reason = (
                f"执行模式配置非法：{_invalid_override!r}（只允许 SHADOW/PAPER/REAL）—— 已拒单"
            )
            self.store.audit(
                "INVALID_EXECUTION_MODE_OVERRIDE",
                {
                    "tenant_id": tenant_id,
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "invalid_value": _invalid_override,
                },
                strategy_id, actor="service:execution",
            )
            self.alerts.send("ERROR", "INVALID_EXECUTION_MODE_OVERRIDE", f"{strategy_id}: {reason}")
            return ExecutionResult(0, "REJECTED", reason)
        broker_settings = getattr(self.broker, "settings", None)
        raw_env = getattr(self.broker, "trading_env", None)
        if raw_env is None or not isinstance(raw_env, (str, bytes)):
            raw_env = getattr(broker_settings, "trading_env", "SIMULATE")
        broker_env = str(raw_env).split(".")[-1].upper()
        if broker_env == "SIMULATE":
            # 伴生模拟沙盒券商隔离：
            # 1. 显式 SHADOW 保持 SHADOW（不碰券商）；
            # 2. 显式 REAL 保持 REAL（禁止静默降级为模拟盘，随后在门禁处安全拒绝）；
            # 3. 其它情况（默认或伴生沙盒）走 PAPER。
            resolved = str(execution_mode_override or override_mode or global_mode or "PAPER").upper()
            if resolved in {"SHADOW", "REAL"}:
                mode = resolved
            else:
                mode = "PAPER"
        elif broker_env == "REAL":
            # 智能匹配实盘白名单授权：实盘环境下白名单策略自动放行 REAL 执行模式
            if strategy_id in getattr(broker_settings, "real_allowed_strategies", ()):
                if not execution_mode_override and override_mode != "SHADOW":
                    override_mode = "REAL"
            mode = (execution_mode_override or override_mode or global_mode).upper()
        else:
            mode = (execution_mode_override or override_mode or global_mode).upper()
        actual_env = getattr(self.broker, "trd_env", None)
        if mode not in {"SHADOW", "PAPER", "REAL"}:
            raise ValueError(f"无效执行模式：{mode}")
        if mode == "PAPER" and (broker_env != "SIMULATE" or
                                (actual_env is not None and str(actual_env).upper().split(".")[-1] != "SIMULATE")):
            reason = "PAPER 只能使用模拟券商环境，禁止向 REAL 券商提交"
            self.store.audit("EXECUTION_ENV_REJECTED", {"mode": mode, "broker_env": broker_env}, strategy_id, actor="service:execution")
            return ExecutionResult(0, "REJECTED", reason)
        if mode == "REAL" and (broker_settings is None or broker_env != "REAL" or
                               (actual_env is not None and str(actual_env).upper().split(".")[-1] != "REAL")):
            reason = "REAL 必须连接 REAL 券商环境"
            self.store.audit("EXECUTION_ENV_REJECTED", {"mode": mode, "broker_env": broker_env}, strategy_id, actor="service:execution")
            return ExecutionResult(0, "REJECTED", reason)
        # 铁律：实盘环境严格保护 NVDA 至少保留 5 股长线核心底仓；
        # 同时若非 core_satellite 策略 (如期权策略)，绝对禁止卖出任何正股！
        if mode == "REAL" and side == "SELL":
            from .option_lifecycle import parse_option_code
            is_opt = parse_option_code(symbol) is not None
            if not is_opt:
                broker_acc = str(getattr(self.broker, "account_id", "") or "").strip()
                if broker_acc and broker_acc != "281756481476169109":
                    reason = f"实盘账户隔离铁律：正股卖单仅允许在富途香港底仓专户 (281756481476169109) 执行，账户 {broker_acc} 禁止提交正股卖单 ({symbol})"
                    self.store.audit("REAL_STOCK_SELL_UNAUTHORIZED", {"symbol": symbol, "account_id": broker_acc, "strategy_id": strategy_id}, actor="service:execution")
                    self.alerts.send("CRITICAL", "REAL_STOCK_SELL_UNAUTHORIZED", reason)
                    return ExecutionResult(0, "REJECTED", reason)
                if strategy_id != "core_satellite":
                    reason = f"实盘底仓安全铁律：策略 {strategy_id} 无权向实盘提交正股卖单 ({symbol})"
                    self.store.audit("REAL_STOCK_SELL_UNAUTHORIZED", {"symbol": symbol, "strategy_id": strategy_id}, actor="service:execution")
                    self.alerts.send("CRITICAL", "REAL_STOCK_SELL_UNAUTHORIZED", reason)
                    return ExecutionResult(0, "REJECTED", reason)
                if symbol == "US.NVDA":
                    pos_df = self.broker.positions()
                    nvda_rows = pos_df[pos_df["code"] == "US.NVDA"] if not pos_df.empty else pos_df
                    current_nvda_qty = float(nvda_rows.iloc[0]["qty"]) if not nvda_rows.empty else 0.0
                    if current_nvda_qty - float(quantity) < 5.0 - 1e-6:
                        reason = f"实盘底仓物理保护铁律：NVDA 卖单数量 {quantity} 导致剩余数量低于最低保护底线 5 股 (当前在仓 {current_nvda_qty} 股)"
                        self.store.audit("REAL_PROTECTED_STOCK_SELL_FORBIDDEN", {"symbol": symbol, "strategy_id": strategy_id, "quantity": quantity, "retained_floor": 5.0}, actor="service:execution")
                        self.alerts.send("CRITICAL", "REAL_PROTECTED_STOCK_SELL_FORBIDDEN", reason)
                        return ExecutionResult(0, "REJECTED", reason)
        # --- Reduce-Only / 暂停新开仓门禁（L2 终极闸门）---
        # 铁律：拦截严格仅作用于 BUY 开仓/增仓；SELL 退出/平仓必须 100% 绝对放行
        if side.upper() == "BUY":
            effective_tenant = tenant_id or (getattr(self.tenant, "tenant_id", "") if self.tenant else "")
            if is_strategy_entries_paused(self.store, strategy_id, effective_tenant):
                reason = (
                    f"策略「{strategy_id}」在租户「{effective_tenant or 'default'}」已开启「暂停新开仓」："
                    f"坚决不建新仓，已有持仓继续由策略执行止盈、止损与退出管理"
                )
                self.store.audit("STRATEGY_NEW_ENTRY_BLOCKED", {
                    "strategy_id": strategy_id,
                    "tenant_id": effective_tenant,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                }, strategy_id=strategy_id, actor="service:execution")
                self.store.decision(strategy_id, symbol, "BLOCKED", reason, tenant_id=effective_tenant)
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
             actor="service:execution")
            self.store.decision(strategy_id, symbol, "BLOCKED", reason, tenant_id=tenant_id)
            return ExecutionResult(0, "APPROVAL_REQUIRED", reason)
        metadata.setdefault("reference_price", float(price))
        price = self._initial_smart_price(symbol, side, float(price), metadata)
        acc_tag = f":{broker_account_id}" if broker_account_id and str(broker_account_id) not in intent_key else ""
        intent, created = self.store.create_intent(
            strategy_id, f"{intent_key}{acc_tag}:{mode}", symbol, side, int(quantity), float(price), metadata,
            tenant_id=tenant_id,
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
                 actor="service:execution")
                self.alerts.send(
                    "ERROR", "REAL_TRADE_REJECTED",
                    f"{strategy_id} {symbol}: {real_decision.reason}",
                )
                return ExecutionResult(intent["id"], "REJECTED", real_decision.reason)
        # P0-7 风控门：**按目标 broker 账户**解析一次生效限额，再交给风控引擎。
        # 账户级 ``risk`` 段配置不完整 → fail-closed 拒绝开仓 + ERROR 审计，
        # 绝不回落 env 静默补齐。
        try:
            risk_limits = resolve_risk_limits(self.store, self.broker)
        except RiskLimitsConfigError as exc:
            self.store.update_intent(intent["id"], "REJECTED", error=str(exc))
            self.store.audit(
                "RISK_LIMITS_CONFIG_INCOMPLETE",
                {"intent_id": intent["id"], "reason": str(exc), "account_id": broker_account_id},
                strategy_id,
             actor="service:execution")
            self.alerts.send(
                "ERROR", "RISK_LIMITS_CONFIG_INCOMPLETE",
                f"{strategy_id} {symbol}: {exc}",
            )
            return ExecutionResult(intent["id"], "REJECTED", str(exc))
        try:
            decision = self.risk.check(
                self.broker, symbol, side, int(quantity), float(price), metadata.get("multiplier"),
                metadata.get("underlying"), strategy_id, mode,
                bool(metadata.get("protected_short", False)),
                metadata,
                limits=risk_limits,
            )
        except Exception as exc:
            self.store.update_intent(intent["id"], "FAILED", error=f"交易前检查异常：{exc}")
            self.store.audit(
                "PRE_TRADE_CHECK_FAILED", {"intent_id": intent["id"], "error": str(exc)},
                strategy_id,
             actor="service:execution")
            raise
        # 开仓门禁聚合（G4/D3.1 + Chief 2026-10-25 裁定）：
        # BUY 只读一次聚合函数（参数 signals + decision），
        # `RiskEngine.blocked_by` 与熔断原因**一次写 audit**（不再分两个事件）；
        # 熔断维度未启用（如 SIMULATE）只记录、不拦，异常绝不上冒到下单路径。
        if side == "BUY":
            signals = EntrySignals(
                breaker_reasons=tuple(getattr(decision, "breaker_reasons", ()) or ()),
                breaker_unsupported=str(getattr(decision, "breaker_unsupported", "") or ""),
            )
            entry_reasons = resolve_entry_block_reasons(signals, decision)
            # P0 防线：**是否拒绝只看 decision.allowed**，聚合原因只用于 audit。
            # 绝不能改成「reasons 为空即放行」——RiskEngine 32 个拒绝路径里历史只有 7 个
            # 带 blocked_by，其余 25 条会被静默放行（Reviewer 2026-10-25 抓到的 fail-open）。
            if not decision.allowed or entry_reasons:
                detail = (
                    decision.reason if not decision.allowed else entry_reasons[0].detail
                )
                self.store.update_intent(intent["id"], "REJECTED", error=detail)
                self.store.audit(
                    "RISK_REJECTED",
                    {"intent_id": intent["id"], "reason": detail,
                     "blocked_by": [reason.code for reason in entry_reasons],
                     "block_details": [reason.as_audit() for reason in entry_reasons]},
                    strategy_id,
                 actor="service:execution")
                self.alerts.send("WARNING", "RISK_REJECTED", f"{strategy_id} {symbol}: {detail}")
                return ExecutionResult(intent["id"], "REJECTED", detail)
            breaker_would_block = bool(getattr(decision, "breaker_would_block", False))
            if breaker_would_block or signals.breaker_unsupported:
                self.store.audit(
                    "ENTRY_GATE_SHADOW",
                    gate_audit_payload(
                        entry_reasons,
                        breaker_unsupported=signals.breaker_unsupported,
                    ),
                    strategy_id,
                 actor="service:execution")
        elif not decision.allowed:
            # 非 BUY（SELL）：保持原有拒绝路径与原因（熔断/聚合不参与退出）
            self.store.update_intent(intent["id"], "REJECTED", error=decision.reason)
            self.store.audit(
                "RISK_REJECTED",
                {
                    "intent_id": intent["id"],
                    "reason": decision.reason,
                    "blocked_by": list(decision.blocked_by),
                },
                strategy_id,
             actor="service:execution")
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
             actor="service:execution")
            self.store.decision(strategy_id, symbol, side, "Shadow 模式：风控通过但未提交券商", tenant_id=tenant_id)
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
                is_net_err = isinstance(exc, (TimeoutError, ConnectionError)) or any(k in str(exc).lower() for k in ["网络", "timeout", "timed out", "connect", "broken pipe", "connection", "disconnected"])
                # 针对紧急平仓止盈单，若遭遇瞬时网络断开/超时，进行最多 3 次退避重试，确保保单送达
                if is_urgent_exit and is_net_err and attempt < max_attempts - 1:
                    import time
                    time.sleep(1.0)
                    continue
                # 券商业务级明确拒绝（例如中国内地监管限制、不可交易、权限不足等），记录为 REJECTED，绝不标记为 UNKNOWN 虚假占用日额度
                if not is_net_err:
                    self.store.update_intent(intent["id"], "REJECTED", error=str(exc))
                    self.store.audit("ORDER_SUBMISSION_REJECTED", {"intent_id": intent["id"], "error": str(exc)}, strategy_id, actor="service:execution")
                    return ExecutionResult(intent["id"], "REJECTED", f"券商明确拒绝订单：{exc}")
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
         actor="service:execution")
        order_label = "实盘订单" if mode == "REAL" else "模拟订单"
        self.store.decision(strategy_id, symbol, side, f"风控通过并提交{order_label}", tenant_id=tenant_id)
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
        except Exception as exc:
            # The pre-submission SUBMITTING marker is deliberately non-retryable.
            # 但不能静默：UNKNOWN 标记没落库，必须能从日志追。
            logger.error(
                "标记 UNKNOWN 失败（intent_id=%s, event=%s）：%s", intent.get("id"), event, exc,
            )
        try:
            self.store.audit(event, {"intent_id": intent["id"], "broker_order_id": order_id,
                                     "reason": reason}, intent["strategy_id"], actor="service:execution")
            self.alerts.send("ERROR", event, f"{intent['strategy_id']} {intent['symbol']}: {reason}")
        except Exception as exc:
            # 审计/告警本身失败时没有别的落库通道，只能升级到 ERROR 日志（禁止 pass）。
            logger.error(
                "审计/告警写入失败（event=%s, intent_id=%s, strategy=%s）：%s",
                event, intent.get("id"), intent.get("strategy_id"), exc,
            )

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

    def _cancel_evidence(self, broker_order_id: str) -> tuple[bool, bool]:
        """本地是否「已提交」且「我方发过撤单」——用于区分主动撤单与券商拒绝。

        券商回报 `FAILED` 时，本地无法区分「我方主动撤单」与「券商拒绝」——
        库内 `audit_events` / `order_state_events` 均无 CANCEL 实据，故以本地
        是否发出过撤单请求为准（不依赖任何未证实的时序归因）。
        只看券商状态无法区分「主动撤单」与「券商拒绝」，必须结合本地证据：

        - **已提交**：`order_intents` 里存在该 `broker_order_id` 的行（券商受理过）；
        - **我方发过撤单**：该 intent 状态为 `CANCEL_REQUESTED`（`cancel_stale_orders`
          在发撤单请求前写入），或审计里有该 intent 的撤单事件（兜底：别的推送路径
          先把状态推到终态时，intent.status 已不可信）。

        返回 `(submitted, cancel_requested)`。读库失败一律按「无证据」处理（fail-safe：
        宁可保留 FAILED，也不把券商拒绝误记成主动撤单）。
        """
        order_id = str(broker_order_id or "").strip()
        if not order_id:
            return False, False
        try:
            intent = self.store.intent_by_broker_order(order_id)
        except Exception as exc:  # noqa: BLE001 - 证据读取失败不得打断对账
            logger.error("撤单证据读取失败（按无证据处理）：%s", exc)
            return False, False
        if not intent:
            return False, False
        if str(intent.get("status") or "").upper() == "CANCEL_REQUESTED":
            return True, True
        intent_id = intent.get("id")
        try:
            events = self.store.audit_events(limit=500)
        except Exception as exc:  # noqa: BLE001 - 同上，审计读不到就按无撤单证据
            logger.error("撤单审计读取失败（按无证据处理）：%s", exc)
            return True, False
        for event in events:
            if str(event.get("event_type") or "") not in CANCEL_AUDIT_EVENTS:
                continue
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except (TypeError, ValueError):
                continue
            if intent_id is not None and str(payload.get("intent_id")) == str(intent_id):
                return True, True
        return True, False

    def resolve_status_with_cancel_evidence(
        self, broker_order_id: str, raw_status: str, filled_quantity: float,
    ) -> str:
        """把券商 `FAILED` 按本地证据改判（纯逻辑见 `resolve_cancel_aware_status`）。"""
        submitted, cancel_requested = self._cancel_evidence(broker_order_id)
        return resolve_cancel_aware_status(
            raw_status,
            filled_quantity=filled_quantity,
            submitted=submitted,
            cancel_requested=cancel_requested,
        )

    def reconcile(self) -> int:
        frames = [self.broker.orders()]
        try:
            frames.append(self.broker.orders(historical=True))
        except Exception as exc:
            # 历史委托拉不到 → 本轮对账可能「假 MATCHED」，必须可见（禁止 pass）。
            logger.error("历史委托拉取失败，本轮对账可能不完整：%s", exc)
            try:
                now = datetime.now(timezone.utc)
                last = self.store.get_setting("reconcile_history_orders_error_at")
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() if last else None
                if elapsed is None or elapsed >= 600:
                    self.store.set_setting(
                        "reconcile_history_orders_error_at", now.isoformat(),
                    )
                    self.store.audit(
                        "RECONCILIATION_HISTORY_ORDERS_FAILED",
                        {"reason": str(exc)},
                        "order_reconciliation",
                        actor="service:execution",
                    )
            except Exception as audit_exc:  # 审计通道也失败时至少留日志
                logger.error("写「历史委托拉取失败」审计出错：%s", audit_exc)
        updated = 0
        seen_order_ids: set[str] = set()
        for frame in frames:
            if frame.empty or not {"order_id", "order_status"}.issubset(frame.columns):
                continue
            for _, order in frame.iterrows():
                seen_order_ids.add(str(order["order_id"]))
                status = self.normalize_status(order["order_status"])
                filled = float(order.get("dealt_qty", 0) or 0)
                if status == "FAILED":
                    # 券商把我方撤单成功也回报成 FAILED：只有「已提交 + 我方发过撤单」
                    # 才改判为主动撤单；券商拒绝仍保持 FAILED（2026-10-27 修复）。
                    # 证据查询只在 FAILED 这一支做，正常订单零额外开销。
                    status = self.resolve_status_with_cancel_evidence(
                        str(order["order_id"]), status, filled,
                    )
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
        broker_env = str(getattr(self.broker, "trading_env", "SIMULATE")).upper()
        for intent in self.store.intents(limit=2000):
            if intent["status"] not in {
                "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "CANCEL_REQUESTED",
            }:
                continue
            # 严格双轨对账环境过滤：REAL 执行器只对账 REAL 订单，SIMULATE 执行器只对账 PAPER 订单，杜绝串轨误判！
            intent_mode = str(intent.get("intent_key", "")).rsplit(":", 1)[-1].upper()
            expected_env = "REAL" if intent_mode == "REAL" else "SIMULATE"
            if expected_env != broker_env:
                continue
            if self.tenant:
                intent_tenant = str(intent.get("tenant_id") or "")
                if intent_tenant and intent_tenant != self.tenant.tenant_id:
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
        if self.tenant:
            intent_tenant = str(intent.get("tenant_id") or "")
            if intent_tenant and intent_tenant != self.tenant.tenant_id:
                return False
        mode = str(intent.get("intent_key", "")).rsplit(":", 1)[-1].upper()
        expected = {"PAPER": "SIMULATE", "REAL": "REAL"}.get(mode)
        broker_settings = getattr(self.broker, "settings", None)
        actual = str(getattr(self.broker, "trading_env", getattr(broker_settings, "trading_env", "SIMULATE"))).upper()
        # Legacy intents without mode suffix may only be managed in simulation.
        if expected is None:
            expected = "SIMULATE"
        if expected != actual:
            # 静默过滤跨环境订单：当前执行器（如 REAL）不应干预对方环境（如 SIMULATE）的订单，直接返回 False，不误报警
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

                expected_env = "REAL" if str(intent.get("intent_key", "")).rsplit(":", 1)[-1].upper() == "REAL" else "SIMULATE"
                try:
                    self.broker.modify_limit_order(intent["broker_order_id"], int(intent["quantity"]), target, expected_env=expected_env)
                except TypeError:
                    self.broker.modify_limit_order(intent["broker_order_id"], int(intent["quantity"]), target)
                self.store.record_reprice(intent["id"], target)
                self.store.audit("ORDER_REPRICED", {"intent_id": intent["id"], "price": target, "urgent": is_urgent}, actor="service:execution")
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
                expected_env = "REAL" if str(intent.get("intent_key", "")).rsplit(":", 1)[-1].upper() == "REAL" else "SIMULATE"
                try:
                    self.broker.cancel_order(intent["broker_order_id"], expected_env=expected_env)
                except TypeError:
                    self.broker.cancel_order(intent["broker_order_id"])
                self.store.update_intent(intent["id"], "CANCEL_REQUESTED")
                self.store.audit("STALE_ORDER_CANCELLED", {"intent_id": intent["id"], "age": age, "urgent": is_urgent}, actor="service:execution")
                cancelled += 1
            except Exception as exc:
                self.alerts.send("ERROR", "CANCEL_FAILED", f"订单 {intent['broker_order_id']} 超时撤单失败: {exc}")
        return cancelled
