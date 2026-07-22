from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import re
import math

from .store import TradingStore
from .portfolio_risk import projected_portfolio_allowed
from .portfolio_allocator import strategy_budget_allowed


OPTION_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$")
NY = ZoneInfo("America/New_York")


def ny_trading_day_start_utc(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(NY)
    return current.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def market_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(number) else number


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 20_000.0
    max_daily_notional: float = 100_000.0
    max_reduction_order_notional: float = 150_000.0
    max_daily_reduction_notional: float = 500_000.0
    max_pending_orders: int = 20
    max_symbol_weight: float = 0.25
    max_gross_exposure: float = 0.85
    max_option_premium: float = 30_000.0
    max_daily_loss: float = 5_000.0
    max_drawdown_pct: float = 0.05

    @classmethod
    def load(cls) -> "RiskLimits":
        return cls(
            max_order_notional=float(os.getenv("RISK_MAX_ORDER_NOTIONAL", "20000")),
            max_daily_notional=float(os.getenv("RISK_MAX_DAILY_NOTIONAL", "100000")),
            max_reduction_order_notional=float(
                os.getenv("RISK_MAX_REDUCTION_ORDER_NOTIONAL", "150000")
            ),
            max_daily_reduction_notional=float(
                os.getenv("RISK_MAX_DAILY_REDUCTION_NOTIONAL", "500000")
            ),
            max_pending_orders=int(os.getenv("RISK_MAX_PENDING_ORDERS", "20")),
            max_symbol_weight=float(os.getenv("RISK_MAX_SYMBOL_WEIGHT", "0.25")),
            max_gross_exposure=float(os.getenv("RISK_MAX_GROSS_EXPOSURE", "0.85")),
            max_option_premium=float(os.getenv("RISK_MAX_OPTION_PREMIUM", "30000")),
            max_daily_loss=float(os.getenv("RISK_MAX_DAILY_LOSS", "5000")),
            max_drawdown_pct=float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0.05")),
        )


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    notional: float


class RiskEngine:
    def __init__(self, store: TradingStore, limits: RiskLimits | None = None):
        self.store = store
        self.limits = limits or RiskLimits.load()

    def check(
        self, broker, symbol: str, side: str, quantity: int, price: float,
        multiplier: float | None = None, underlying: str | None = None,
        strategy_id: str | None = None, execution_mode: str = "PAPER",
        protected_short: bool = False,
    ) -> RiskDecision:
        if not symbol.startswith("US.") or side not in {"BUY", "SELL"}:
            return RiskDecision(False, "仅允许美股 BUY/SELL 指令", 0)
        if quantity <= 0 or price <= 0:
            return RiskDecision(False, "数量和限价必须大于 0", 0)
        if side == "BUY" and self.store.get_setting("connectivity_degraded", "false") == "true":
            return RiskDecision(False, "连接恢复尚未完成对账，暂时禁止新增风险", 0)
        if side == "BUY" and self.store.get_setting("recovery_blocked", "false") == "true":
            return RiskDecision(False, "启动恢复发现三账差异，请先完成持仓归属与对账", 0)
        contract_multiplier = multiplier or (100.0 if OPTION_PATTERN.match(symbol) else 1.0)
        notional = float(quantity) * float(price) * contract_multiplier
        # 急停禁止新增风险，但保留卖出平仓通道。
        if self.store.kill_switch() and side == "BUY":
            return RiskDecision(False, "系统急停已开启，禁止新增仓位", notional)
        # A regular SELL may still open risk (for example a protected option short),
        # so only verified long-position reductions receive the dedicated allowance.
        if (side != "SELL" or protected_short) and notional > self.limits.max_order_notional:
            return RiskDecision(False, f"单笔名义金额超过 ${self.limits.max_order_notional:,.0f}", notional)
        budget_allowed, budget_reason, budget_details = strategy_budget_allowed(
            self.store, broker, strategy_id, symbol, side, quantity, price, contract_multiplier,
        )
        self.store.operational_event(
            "portfolio_allocator", "PRE_TRADE_BUDGET_CHECK",
            "HEALTHY" if budget_allowed else "BLOCKED",
            {"strategy": strategy_id, "symbol": symbol, "reason": budget_reason, **budget_details},
        )
        if not budget_allowed:
            return RiskDecision(False, budget_reason, notional)
        orders = broker.orders()
        pending = 0 if orders.empty else int(orders["order_status"].map(broker.is_cancellable_status).sum())
        if pending >= self.limits.max_pending_orders:
            return RiskDecision(False, f"待成交订单已达到 {self.limits.max_pending_orders} 笔", notional)
        positions = broker.positions() if hasattr(broker, "positions") else None
        info = broker.account_info() if hasattr(broker, "account_info") else None
        risk_reducing_sell = False
        if side == "SELL" and positions is not None and not protected_short:
            owned_rows = positions[positions["code"] == symbol] if not positions.empty else positions
            owned = float(owned_rows.iloc[0].get("qty", 0)) if not owned_rows.empty else 0
            pending_sell = 0.0
            if execution_mode == "SHADOW" and strategy_id:
                owned, _ = self.store.shadow_position(strategy_id, symbol)
            elif not orders.empty and {"code", "trd_side", "qty"}.issubset(orders.columns):
                active = orders[orders["order_status"].map(broker.is_cancellable_status)]
                pending_sell = float(active[(active["code"] == symbol) &
                    (active["trd_side"].astype(str).str.upper().str.contains("SELL"))]["qty"].sum())
            if quantity + pending_sell > owned:
                return RiskDecision(False, f"卖出数量超过可用持仓 {max(0, owned - pending_sell):g}", notional)
            enforce_virtual = (
                execution_mode != "SHADOW"
                and os.getenv("ENFORCE_VIRTUAL_POSITIONS", "false").lower() == "true"
            )
            if strategy_id and enforce_virtual:
                strategy_owned = self.store.strategy_position(strategy_id, symbol)
                if quantity > strategy_owned:
                    return RiskDecision(
                        False, f"{strategy_id} 虚拟持仓只有 {strategy_owned:g}，禁止占用其他策略持仓",
                        notional,
                    )
            risk_reducing_sell = True
            if notional > self.limits.max_reduction_order_notional:
                return RiskDecision(
                    False,
                    f"单笔减仓金额超过 ${self.limits.max_reduction_order_notional:,.0f}",
                    notional,
                )
            if self.store.get_setting(f"symbol_blocked:{symbol}", "false") == "true":
                self.store.audit(
                    "RECONCILIATION_EXIT_ALLOWED",
                    {"symbol": symbol, "quantity": quantity, "broker_owned": owned},
                    strategy_id,
                )
        elif side == "SELL" and protected_short:
            if not OPTION_PATTERN.match(symbol):
                return RiskDecision(False, "组合保护卖出只允许标准美股期权合约", notional)
            self.store.audit(
                "PROTECTED_COMBO_SHORT_ALLOWED",
                {"symbol": symbol, "quantity": quantity, "underlying": underlying},
                strategy_id,
            )
        if side == "SELL" and not risk_reducing_sell and not protected_short:
            if notional > self.limits.max_order_notional:
                return RiskDecision(
                    False, f"无法确认属于减仓，沿用单笔 ${self.limits.max_order_notional:,.0f} 限额",
                    notional,
                )
        day_start = ny_trading_day_start_utc()
        if risk_reducing_sell:
            used = self.store.submitted_notional_since(day_start.isoformat(), side="SELL")
            if used + notional > self.limits.max_daily_reduction_notional:
                return RiskDecision(
                    False,
                    f"当日累计减仓金额将超过 ${self.limits.max_daily_reduction_notional:,.0f}",
                    notional,
                )
        else:
            used = self.store.submitted_notional_since(
                day_start.isoformat(), side=None if protected_short else "BUY",
            )
            if used + notional > self.limits.max_daily_notional:
                return RiskDecision(
                    False, f"当日累计名义金额将超过 ${self.limits.max_daily_notional:,.0f}",
                    notional,
                )
        if side == "BUY" and info is not None and not info.empty:
            os.environ["RISK_CORRELATION_CLUSTERS"] = self.store.get_setting(
                "correlation_clusters", os.getenv("RISK_CORRELATION_CLUSTERS", "{}")
            )
            account = info.iloc[0]
            assets = market_number(account.get("total_assets", 0))
            buying_power = market_number(account.get("power", account.get("cash", 0)))
            daily_pl = market_number(account.get("unrealized_pl", account.get("profit_val", 0)))
            if buying_power > 0 and notional > buying_power:
                return RiskDecision(False, "可用购买力不足", notional)
            if daily_pl <= -self.limits.max_daily_loss:
                self.store.set_setting("kill_switch", "true")
                return RiskDecision(False, f"账户亏损已达到 ${self.limits.max_daily_loss:,.0f} 急停线", notional)
            if assets > 0 and positions is not None:
                shadow_positions = self.store.shadow_open_positions()
                shadow_total = sum(
                    abs(float(item["quantity"]) * float(item["average_cost"])
                        * (100 if OPTION_PATTERN.match(item["symbol"]) else 1))
                    for item in shadow_positions
                )
                if execution_mode == "SHADOW":
                    if shadow_total + notional > assets * self.limits.max_gross_exposure:
                        return RiskDecision(False, "Shadow 组合总敞口将超过账户总风险上限", notional)
                    if OPTION_PATTERN.match(symbol):
                        shadow_option = sum(
                            abs(float(item["quantity"]) * float(item["average_cost"]) * 100)
                            for item in shadow_positions if OPTION_PATTERN.match(item["symbol"])
                        )
                        if shadow_option + notional > self.limits.max_option_premium:
                            return RiskDecision(False, "Shadow 期权权利金总敞口超过上限", notional)
                day_start = ny_trading_day_start_utc()
                snapshots = self.store.equity_since(day_start.isoformat())
                peak = max([float(row["total_assets"]) for row in snapshots] + [assets])
                drawdown = 1 - assets / peak if peak > 0 else 0
                if peak - assets >= self.limits.max_daily_loss:
                    self.store.set_setting("kill_switch", "true")
                    return RiskDecision(False, "账户日内权益损失达到急停线，已自动急停", notional)
                if drawdown >= self.limits.max_drawdown_pct:
                    self.store.set_setting("kill_switch", "true")
                    return RiskDecision(False, f"账户日内回撤达到 {drawdown:.2%}，已自动急停", notional)
                market_value = float(positions.get("market_val", []).sum()) if "market_val" in positions else 0
                symbol_rows = positions[positions["code"] == symbol] if not positions.empty else positions
                symbol_value = float(symbol_rows["market_val"].sum()) if "market_val" in symbol_rows else 0
                if not OPTION_PATTERN.match(symbol) and (symbol_value + notional) / assets > self.limits.max_symbol_weight:
                    return RiskDecision(False, f"单标的仓位将超过 {self.limits.max_symbol_weight:.0%}", notional)
                family = underlying or (symbol if not OPTION_PATTERN.match(symbol) else None)
                if family and not positions.empty and "market_val" in positions:
                    family_value = float(positions[
                        positions["code"].astype(str).map(
                            lambda code: code == family or code.startswith(family + "2")
                        )
                    ]["market_val"].abs().sum())
                    if (family_value + notional) / assets > self.limits.max_symbol_weight:
                        return RiskDecision(False, f"正股与期权合并敞口将超过 {self.limits.max_symbol_weight:.0%}", notional)
                if (market_value + notional) / assets > self.limits.max_gross_exposure:
                    return RiskDecision(False, f"账户总持仓将超过 {self.limits.max_gross_exposure:.0%}", notional)
                if OPTION_PATTERN.match(symbol):
                    option_value = 0.0
                    if not positions.empty and "market_val" in positions:
                        option_value = float(positions[
                            positions["code"].astype(str).map(lambda value: bool(OPTION_PATTERN.match(value)))
                        ]["market_val"].abs().sum())
                    if option_value + notional > self.limits.max_option_premium:
                        return RiskDecision(False, f"期权权利金敞口将超过 ${self.limits.max_option_premium:,.0f}", notional)
            allowed, portfolio_reason, exposure = projected_portfolio_allowed(
                broker, symbol, side, quantity, price, multiplier, underlying,
                self.store, execution_mode,
            )
            self.store.operational_event("portfolio_risk", "PRE_TRADE_CHECK",
                                         "HEALTHY" if allowed else "BLOCKED", exposure)
            if not allowed:
                return RiskDecision(False, portfolio_reason, notional)
        return RiskDecision(True, "风控通过", notional)
