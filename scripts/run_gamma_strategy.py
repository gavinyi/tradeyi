from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import time as clock
from zoneinfo import ZoneInfo

import pandas as pd

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_gamma_campaigns
from futu_quant.execution import ExecutionManager
from futu_quant.combo_analytics import combo_market_snapshot
from futu_quant.combo_execution import ComboExecutionManager, create_combo
from futu_quant.gamma_automation import decide_gamma_action
from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues
from futu_quant.gamma_risk import (
    contract_analysis, gamma_add_allowed, iv_entry_allowed, portfolio_greeks,
    realized_volatility, normalize_iv, market_number,
)
from futu_quant.option_signal import (
    configured_direction_allowed, configured_volatility_direction_allowed,
)
from futu_quant.volatility_optimizer import (
    decide_volatility_action, sync_volatility_position_cycle,
    volatility_partial_exit_plan,
    volatility_cooldown_hours, volatility_exit_reason,
    volatility_cooldown_bypass_allowed, volatility_profit_protection_profile,
    volatility_direction_size_factor, volatility_entry_confirmation,
    volatility_entry_quality_allowed, volatility_regime, volatility_threshold_profile,
)
from futu_quant.volatility_structures import automatic_combo_id
from futu_quant.data_quality import (
    option_book_status, option_quote_freshness, quote_freshness,
)
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.store import TradingStore


NY = ZoneInfo("America/New_York")
MARKET_CALENDAR = USMarketCalendar()
LEGACY_STATE_FILE = Path("data/gamma_state.json")


def combo_position_mark(store, broker, combo_id: str) -> tuple[int, float, float]:
    combo = store.option_combo(combo_id)
    if not combo or combo.get("status") != "OPEN":
        return 0, 0.0, 0.0
    legs = store.option_combo_legs(combo_id)
    if not legs:
        return 0, 0.0, 0.0
    quotes = broker.snapshots([leg["symbol"] for leg in legs])
    detail, package = combo_market_snapshot(legs, quotes)
    if detail.empty:
        return 1, 0.0, 0.0
    entry_debit = sum(
        (1 if leg["side"] == "BUY" else -1)
        * float(leg.get("filled_quantity", 0) or 0)
        * float(leg.get("average_fill_price", 0) or 0)
        for leg in legs
    )
    current_value = float(package.get("executable_net_debit", 0) or 0)
    return 1, max(0.01, entry_debit), current_value


def refreshed_trade_quotes(
    broker: FutuPaperBroker, code: str, underlying: str, now: datetime,
    maximum_spread_pct: float, retries: int = 2, wait_seconds: float = 1.5,
) -> tuple[pd.Series, pd.Series, dict]:
    attempts: list[dict] = []
    last_option = None
    last_owner = None
    for attempt in range(retries + 1):
        quote = broker.snapshots([code, underlying])
        option = quote[quote["code"] == code].iloc[0]
        owner = quote[quote["code"] == underlying].iloc[0]
        option_fresh, option_reason, option_age, option_degraded = option_quote_freshness(
            option, now, fresh_seconds=180, maximum_seconds=900,
            maximum_spread_pct=maximum_spread_pct,
        )
        owner_fresh, owner_reason, owner_age = quote_freshness(owner, now)
        book_ok, book_reason, spread_pct = option_book_status(option, maximum_spread_pct)
        attempts.append({
            "attempt": attempt + 1,
            "option_fresh": option_fresh,
            "option_reason": option_reason,
            "option_age_seconds": option_age,
            "option_degraded": option_degraded,
            "underlying_fresh": owner_fresh,
            "underlying_reason": owner_reason,
            "underlying_age_seconds": owner_age,
            "book_ok": book_ok,
            "book_reason": book_reason,
            "spread_pct": spread_pct,
        })
        last_option, last_owner = option, owner
        if option_fresh and owner_fresh and book_ok:
            break
        if attempt < retries:
            clock.sleep(wait_seconds)
    latest = attempts[-1]
    latest["attempts"] = attempts
    return last_option, last_owner, latest


def load_state() -> dict:
    store = TradingStore()
    state = store.campaign_states()
    if not state and LEGACY_STATE_FILE.exists():
        try:
            legacy = json.loads(LEGACY_STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            legacy = {}
        if isinstance(legacy, dict):
            store.save_campaign_states(legacy)
            state = legacy
    return state


def save_state(state: dict) -> None:
    TradingStore().save_campaign_states(state)


def run_campaign(
    broker: FutuPaperBroker, executor: ExecutionManager, campaign: dict, state: dict,
    strategy_id: str = "earnings_gamma", positions_snapshot=None, orders_snapshot=None,
) -> None:
    code = campaign["code"]
    legs = campaign.get("legs") or [{"code": code, "side": "BUY", "ratio": 1}]
    issues = diagnose_campaign(campaign, executor.store.earnings_events())
    hard_issues = hard_campaign_issues(campaign, issues)
    if hard_issues:
        reason = "；".join(hard_issues)
        executor.store.decision(strategy_id, code, "BLOCKED", reason)
        print(f"{code}: BLOCKED {reason}")
        return
    now = datetime.now(NY)
    state_key = f"{strategy_id}:{code}"
    if state_key not in state and strategy_id == "volatility_long_gamma" and code in state:
        state[state_key] = state.pop(code)
    campaign_state = state.setdefault(state_key, {"peak": 0.0})
    trigger_mode = campaign.get("trigger_mode", "EARNINGS")
    if issues:
        executor.store.audit("GAMMA_VALIDATION_WARNING", {
            "code": code, "trigger_mode": trigger_mode, "warnings": issues,
        }, strategy_id)
    if trigger_mode == "VOLATILITY":
        signal_started_at = campaign_state.get("signal_started_at")
        event_time = (
            datetime.fromisoformat(signal_started_at) if signal_started_at else now
        ) + timedelta(
            hours=int(campaign.get("entry_hours", 6))
        )
    else:
        event_clock = time(8, 0) if campaign["event_session"] == "盘前" else time(16, 5)
        event_time = datetime.combine(
            datetime.fromisoformat(campaign["event_date"]).date(), event_clock, tzinfo=NY
        )
    positions = positions_snapshot if positions_snapshot is not None else broker.positions()
    position = positions[positions["code"] == code] if not positions.empty else positions
    quantity = int(position.iloc[0]["qty"]) if not position.empty else 0
    cost = float(position.iloc[0]["cost_price"]) if not position.empty else 0.0
    global_mode = executor.store.get_setting("execution_mode", "PAPER")
    strategy_mode = executor.store.get_setting(f"strategy_execution_mode:{strategy_id}", "")
    campaign_mode = str(campaign.get("execution_mode_override") or "").strip()
    execution_mode = (campaign_mode or strategy_mode or global_mode).upper()
    combo_last = 0.0
    combo_id = automatic_combo_id(
        strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
    ) if len(legs) > 1 else ""
    combo = executor.store.option_combo(combo_id) if combo_id else None
    if combo and combo.get("status") in {"EXECUTING", "CLOSING", "ROLLBACK_PENDING"}:
        ComboExecutionManager(broker, executor.store, executor).advance(combo_id)
        combo = executor.store.option_combo(combo_id)
        executor.store.decision_if_changed(
            strategy_id, code, "HOLD",
            f"组合订单正在执行：{combo.get('status', 'UNKNOWN')}",
            {"combo_id": combo_id, "structure": campaign.get("structure")},
            repeat_after_seconds=300,
        )
        return
    if combo and combo.get("status") == "OPEN":
        quantity, cost, combo_last = combo_position_mark(
            executor.store, broker, combo_id
        )
    if execution_mode == "SHADOW":
        shadow_quantity, shadow_cost = executor.store.shadow_position(strategy_id, code)
        quantity, cost = int(shadow_quantity), float(shadow_cost)
    if trigger_mode == "VOLATILITY" and quantity <= 0:
        pending_exit_reason = str(campaign_state.get("pending_exit_reason", ""))
        cycle_transition = sync_volatility_position_cycle(
            campaign_state, quantity=0, cost=0, last=0, current_iv=0,
            current_efficiency=0, now=now,
        )
        if cycle_transition == "CLOSED":
            reason = pending_exit_reason or "波动率仓位已完成退出"
            campaign_state["entry_cycle"] = int(campaign_state.get("entry_cycle", 0)) + 1
            campaign_state["last_exit_at"] = now.isoformat()
            state[f"volatility_cooldown:{campaign['underlying']}"] = now.isoformat()
            state[f"volatility_cooldown_reason:{campaign['underlying']}"] = reason
            state[f"volatility_cooldown_hours:{campaign['underlying']}"] = (
                volatility_cooldown_hours(
                    reason, float(campaign.get("cooldown_hours", 24)),
                    {
                        "take_profit": float(campaign.get("cooldown_take_profit_hours", 4)),
                        "profit_protection": float(campaign.get("cooldown_profit_protection_hours", 8)),
                        "stop_loss": float(campaign.get("cooldown_stop_loss_hours", 24)),
                        "expiry": float(campaign.get("cooldown_expiry_hours", 0)),
                        "decay": float(campaign.get("cooldown_decay_hours", 8)),
                        "contraction": float(campaign.get("cooldown_contraction_hours", 6)),
                    },
                )
            )
    campaign_prefix = f"{campaign.get('event_date', '')}:{campaign.get('campaign_id', code)}:"
    if trigger_mode == "VOLATILITY":
        # A volatility campaign is reusable: a completed exit starts a cooldown,
        # whereas an earnings campaign represents a one-off event.
        campaign_state.pop("closed", None)
    elif quantity <= 0 and executor.store.strategy_has_completed_exit(
        strategy_id, code, campaign_prefix,
    ):
        campaign_state["closed"] = True
    if campaign_state.get("closed"):
        executor.store.decision(
            strategy_id, code, "CLOSED", "该策略活动已完成退出，不再重新开仓",
            {"position_quantity": 0, "planned_order_quantity": 0,
             "position_source": "SHADOW_LEDGER" if execution_mode == "SHADOW" else "FUTU_ACCOUNT",
             "trigger_mode": trigger_mode, "validation_warnings": issues},
        )
        print(f"{code}: CLOSED (campaign exit completed)")
        return
    orders = orders_snapshot if orders_snapshot is not None else broker.orders()
    pending = (
        orders[(orders["code"] == code) & orders["order_status"].map(broker.is_cancellable_status)]
        if not orders.empty else orders
    )
    if not pending.empty:
        print(f"{code}: HOLD pending order {pending.iloc[0]['order_id']}")
        return

    market_session = MARKET_CALENDAR.session(now)
    if not market_session.regular_open(now):
        reason = (
            "当前期权市场未开盘，旧期权报价仅用于参考；"
            "等待美股常规交易时段后重新校验并执行"
        )
        executor.store.decision_if_changed(
            strategy_id, code, "HOLD", reason, {
                "market_session": "OPTION_CLOSED",
                "option_quote_time": "",
            }, repeat_after_seconds=3600,
        )
        print(f"{code}: HOLD ({reason})")
        return
    max_quote_spread_pct = float(campaign.get(
        "max_spread_pct", 12.0 if trigger_mode == "VOLATILITY" else 20.0
    ))
    option, owner, quote_check = refreshed_trade_quotes(
        broker, code, campaign["underlying"], now, max_quote_spread_pct,
    )
    if not quote_check["option_fresh"] or not quote_check["underlying_fresh"] or not quote_check["book_ok"]:
        reason = (
            "行情/盘口检查未通过："
            f"正股 {quote_check['underlying_reason']}；"
            f"期权 {quote_check['option_reason']}；"
            f"{quote_check['book_reason']}；已重试 {len(quote_check['attempts'])} 次"
        )
        decision = "HOLD" if quantity > 0 else "BLOCKED"
        executor.store.decision(strategy_id, code, decision, reason, {
            "market_data_status": "WAITING_EXIT_LIQUIDITY" if quantity > 0 else "PRE_TRADE_BLOCKED",
            "underlying_quote_age_seconds": quote_check["underlying_age_seconds"],
            "option_quote_age_seconds": quote_check["option_age_seconds"],
            "book_ok": quote_check["book_ok"],
            "book_reason": quote_check["book_reason"],
            "spread_pct": quote_check["spread_pct"],
            "quote_attempts": quote_check["attempts"],
            "position_quantity": quantity,
            "planned_order_quantity": 0,
            "trigger_mode": trigger_mode,
        })
        executor.store.audit(
            "OPTION_MARKET_DATA_WAITING" if quantity > 0 else "OPTION_MARKET_DATA_BLOCKED",
            {
                "symbol": code, "underlying": campaign["underlying"],
                "strategy_id": strategy_id, "reason": reason,
                "attempts": quote_check["attempts"],
                "position_quantity": quantity,
            },
            strategy_id,
        )
        print(f"{code}: {decision} ({reason})")
        return
    if quote_check["option_degraded"]:
        executor.store.audit("OPTION_QUOTE_DEGRADED_ACCEPTED", {
            "strategy_id": strategy_id, "symbol": code,
            "option_quote_age_seconds": quote_check["option_age_seconds"],
            "reason": quote_check["option_reason"],
        }, strategy_id)
    last = float(option["last_price"] or 0)
    bid, ask = float(option["bid_price"] or 0), float(option["ask_price"] or 0)
    gamma_now = abs(market_number(option.get("option_gamma", 0)))
    theta_now = abs(market_number(option.get("option_theta", 0)))
    current_efficiency = gamma_now / theta_now if theta_now > 0 else 0.0
    current_iv = normalize_iv(option.get("option_implied_volatility", 0))
    if combo and combo.get("status") == "OPEN" and combo_last > 0:
        last = combo_last
    if trigger_mode == "VOLATILITY":
        sync_volatility_position_cycle(
            campaign_state, quantity=quantity, cost=cost, last=last,
            current_iv=current_iv, current_efficiency=current_efficiency, now=now,
        )
    else:
        campaign_state["peak"] = max(float(campaign_state.get("peak", 0)), last)
    pnl_ratio = last / cost - 1 if quantity > 0 and cost > 0 else 0.0
    peak = float(campaign_state.get("peak", 0))
    drawdown = 1 - last / peak if peak > 0 else 0.0
    peak_pnl_ratio = (
        peak / cost - 1
        if quantity > 0 and cost > 0 else 0.0
    )
    profit_giveback = (
        max(0.0, (peak_pnl_ratio - pnl_ratio) / peak_pnl_ratio)
        if peak_pnl_ratio > 0 else 0.0
    )
    underlying_bars = broker.candles(campaign["underlying"], count=90)
    regime = None
    if trigger_mode == "VOLATILITY":
        adaptive = volatility_threshold_profile(underlying_bars)
        effective_z = min(
            float(campaign.get("entry_zscore", 2.0)), adaptive["entry_zscore"]
        )
        effective_move = min(
            float(campaign.get("minimum_underlying_move", .02)),
            adaptive["minimum_underlying_move"],
        )
        regime = volatility_regime(
            owner, underlying_bars, effective_z, effective_move,
        )
        regime["adaptive_profile"] = adaptive
        was_regime_active = bool(campaign_state.get("regime_was_active", False))
        if regime["active"]:
            if not was_regime_active or not campaign_state.get("signal_started_at"):
                campaign_state["signal_started_at"] = now.isoformat()
            campaign_state["signal_active"] = True
        elif float(regime["zscore_20d"]) < 1.2:
            campaign_state["signal_active"] = False
            campaign_state.pop("signal_started_at", None)
        campaign_state["regime_was_active"] = bool(regime["active"])
        signal_started_at = campaign_state.get("signal_started_at")
        event_time = (
            datetime.fromisoformat(signal_started_at) if signal_started_at else now
        ) + timedelta(hours=int(campaign.get("entry_hours", 6)))
    nondirectional = campaign.get("structure") in {"LONG_STRADDLE", "LONG_STRANGLE"}
    if nondirectional:
        direction_ok, direction_reason, direction_metrics = True, "非方向性结构，无需强制判断涨跌方向", {}
    elif trigger_mode == "VOLATILITY":
        direction_ok, direction_reason, direction_metrics = configured_volatility_direction_allowed(
            owner, underlying_bars, campaign["option_type"], regime,
        )
    else:
        direction_ok, direction_reason, direction_metrics = configured_direction_allowed(
            owner, underlying_bars, campaign["option_type"], trigger_mode,
            float(campaign.get("minimum_underlying_move", 0.020 if trigger_mode == "VOLATILITY" else 0.003)),
        )
    if trigger_mode == "VOLATILITY":
        direction_ok = bool(direction_ok and campaign_state.get("signal_active", False))
        direction_reason = f"{direction_reason}；动态状态：{regime['reason']}"
        if quantity <= 0 and campaign.get("optimizer_version") != 2:
            reason = "旧版波动率活动尚未完成动态状态和合约质量评分，请重新生成候选"
            executor.store.decision(strategy_id, code, "HOLD", reason, {"regime": regime})
            print(f"{code}: HOLD ({reason})")
            return
        if quantity <= 0:
            entry_confirmed, confirmation_reason = volatility_entry_confirmation(
                now=now,
                signal_started_at=campaign_state.get("signal_started_at"),
                regime=regime,
                confirmation_minutes=float(
                    campaign.get("entry_confirmation_minutes", 5)
                ),
            )
            direction_reason = f"{direction_reason}；{confirmation_reason}"
            if not entry_confirmed:
                executor.store.decision_if_changed(
                    strategy_id,
                    code,
                    "HOLD",
                    confirmation_reason,
                    {
                        "position_quantity": quantity,
                        "planned_order_quantity": 0,
                        "position_source": (
                            "SHADOW_LEDGER" if execution_mode == "SHADOW"
                            else "FUTU_ACCOUNT"
                        ),
                        "pnl_ratio": pnl_ratio,
                        "drawdown": drawdown,
                        "trigger_mode": trigger_mode,
                        "validation_warnings": issues,
                        "direction_reason": direction_reason,
                        "direction_metrics": direction_metrics,
                        "volatility_regime": regime,
                        "signal_started_at": campaign_state.get("signal_started_at"),
                    },
                    repeat_after_seconds=300,
                )
                print(f"{code}: HOLD ({confirmation_reason})")
                return
        cooldown_key = f"volatility_cooldown:{campaign['underlying']}"
        last_exit = state.get(cooldown_key)
        if quantity <= 0 and last_exit:
            cooldown_duration = float(state.get(
                f"volatility_cooldown_hours:{campaign['underlying']}",
                campaign.get("cooldown_hours", 24),
            ))
            remaining = cooldown_duration - (
                now - datetime.fromisoformat(str(last_exit))
            ).total_seconds() / 3600
            if remaining > 0:
                cooldown_reason = str(state.get(
                    f"volatility_cooldown_reason:{campaign['underlying']}", "未记录退出原因",
                ))
                bypass_allowed, bypass_reason = volatility_cooldown_bypass_allowed(
                    cooldown_reason, regime,
                    enabled=bool(campaign.get("cooldown_bypass_enabled", True)),
                    allow_stop_loss=bool(campaign.get("cooldown_bypass_stop_loss", False)),
                    min_zscore=float(campaign.get("cooldown_bypass_min_zscore", 3.0)),
                    min_intraday_move=float(campaign.get("cooldown_bypass_min_intraday_move", 0.035)),
                    min_atr_ratio=float(campaign.get("cooldown_bypass_min_atr_ratio", 2.0)),
                    min_volume_ratio=float(campaign.get("cooldown_bypass_min_volume_ratio", 1.5)),
                )
                if bypass_allowed:
                    for key in (
                        cooldown_key,
                        f"volatility_cooldown_hours:{campaign['underlying']}",
                        f"volatility_cooldown_reason:{campaign['underlying']}",
                    ):
                        state.pop(key, None)
                    executor.store.decision(strategy_id, code, "COOLDOWN_BYPASS", bypass_reason, {
                        "regime": regime, "cooldown_reason": cooldown_reason,
                        "remaining_cooldown_hours": remaining,
                    })
                    print(f"{code}: COOLDOWN_BYPASS ({bypass_reason})")
                else:
                    reason = f"同一标的退出冷却中，剩余约 {remaining:.1f} 小时；{bypass_reason}"
                    executor.store.decision(strategy_id, code, "HOLD", reason, {
                        "regime": regime, "cooldown_reason": cooldown_reason,
                        "remaining_cooldown_hours": remaining,
                    })
                    print(f"{code}: HOLD ({reason})")
                    return
    if quantity <= 0 and trigger_mode == "EARNINGS" and (
        campaign.get("optimizer_version") != 1 or not campaign.get("trade_allowed", False)
    ):
        reason = (
            "旧版财报活动尚未完成机会评分，请重新生成候选"
            if campaign.get("optimizer_version") != 1
            else "财报机会评分未达到交易门槛，自动放弃本次交易"
        )
        executor.store.decision(strategy_id, code, "HOLD", reason, {
            "opportunity_score": campaign.get("opportunity_score", 0),
            "minimum_score": campaign.get("minimum_score", 60),
            "expected_move_edge": campaign.get("expected_move_edge", 0),
            "score_reason": campaign.get("score_reason", ""),
            "optimizer_version": campaign.get("optimizer_version", 0),
        })
        print(f"{code}: HOLD ({reason})")
        return
    per_stage = int(campaign["contracts_per_stage"])
    size_factor = 1.0
    size_reason = ""
    effective_max_stages = int(campaign["max_stages"])
    if trigger_mode == "VOLATILITY":
        size_factor, size_reason = volatility_direction_size_factor(regime)
        if size_factor < 1:
            per_stage = max(1, int(per_stage * size_factor))
            effective_max_stages = min(effective_max_stages, 1)
    stages_filled = quantity // max(1, per_stage)
    if trigger_mode == "VOLATILITY":
        decay_reason = None
        if quantity > 0:
            if regime.get("contraction") and not regime.get("active", False):
                campaign_state["contraction_confirmations"] = int(
                    campaign_state.get("contraction_confirmations", 0)
                ) + 1
            else:
                campaign_state["contraction_confirmations"] = 0
            regime["contraction_confirmations"] = campaign_state[
                "contraction_confirmations"
            ]
            decay_reason = volatility_exit_reason(
                regime, current_iv,
                float(campaign_state.get(
                    "entry_iv_actual", campaign.get("entry_iv", 0)
                )),
                current_efficiency,
                float(campaign_state.get(
                    "entry_gamma_theta_efficiency_actual",
                    campaign.get("entry_gamma_theta_efficiency", 0),
                )),
                float(campaign.get("iv_decline_exit_ratio", .20)),
                float(campaign.get("efficiency_decline_exit_ratio", .40)),
                int(campaign.get("contraction_confirmations_required", 3)),
                pnl_ratio=pnl_ratio,
                directional_confirmation=direction_ok,
            )
        opened_at = datetime.fromisoformat(str(
            campaign_state.get("position_opened_at", now.isoformat())
        ))
        partial_exit_quantity, partial_exit_tier, partial_exit_reason = (
            volatility_partial_exit_plan(
                quantity=quantity,
                pnl_ratio=pnl_ratio,
                campaign_state=campaign_state,
                enabled=bool(campaign.get("partial_profit_enabled", True)),
                tier1_profit=float(campaign.get("partial_profit_tier1", .30)),
                tier1_fraction=float(campaign.get("partial_profit_tier1_fraction", .50)),
                tier2_profit=float(campaign.get("partial_profit_tier2", .60)),
                tier2_fraction=float(campaign.get("partial_profit_tier2_fraction", .25)),
            )
        )
        if partial_exit_quantity > 0 and partial_exit_reason:
            action, reason = "SELL", partial_exit_reason
        else:
            protection_profile = volatility_profit_protection_profile(campaign, regime)
            action, reason = decide_volatility_action(
                now=now, opened_at=opened_at, quantity=quantity,
                stages_filled=stages_filled, max_stages=effective_max_stages,
                pnl_ratio=pnl_ratio, peak_pnl_ratio=peak_pnl_ratio,
                drawdown_from_peak=drawdown, directional_confirmation=direction_ok,
                signal_active=bool(campaign_state.get("signal_active", False)),
                profit_target=float(campaign["profit_target"]),
                stop_loss=float(campaign["stop_loss"]),
                trailing_stop=float(campaign["trailing_stop"]),
                max_hold_hours=float(campaign.get("max_hold_hours", 24)),
                decay_reason=decay_reason,
                minimum_hold_minutes=float(campaign.get("minimum_hold_minutes", 90)),
                profit_protection_start=float(
                    protection_profile["profit_protection_start"]
                ),
                profit_protection_drawdown=float(
                    protection_profile["profit_protection_giveback"]
                ),
                profit_giveback_from_peak=profit_giveback,
                full_profit_target_enabled=not (
                    bool(campaign.get("partial_profit_enabled", True)) and quantity > 1
                ),
                minimum_locked_profit=float(
                    protection_profile["minimum_locked_profit"]
                ),
            )
    else:
        partial_exit_quantity = 0
        partial_exit_tier = None
        action, reason = decide_gamma_action(
            now, event_time, quantity, stages_filled, int(campaign["max_stages"]),
            int(campaign["entry_hours"]), pnl_ratio, drawdown, direction_ok,
            float(campaign["profit_target"]), float(campaign["stop_loss"]),
            float(campaign["trailing_stop"]),
            float(campaign.get("max_hold_hours", 8)),
        )
    if trigger_mode == "VOLATILITY" and quantity > 0:
        expiry = datetime.fromisoformat(str(campaign.get("strike_time", now.date()))).date()
        if now.date() > expiry or (now.date() == expiry and now.hour >= 15):
            action, reason = "SELL", "合约到期日临近收盘，强制退出以避免自动行权"
    print(f"{code}: {action} ({reason}) qty={quantity} pnl={pnl_ratio:.2%}")
    planned_order_quantity = (
        per_stage if action == "BUY"
        else (partial_exit_quantity or quantity) if action == "SELL"
        else 0
    )
    decision_payload = {
         "position_quantity": quantity, "planned_order_quantity": planned_order_quantity,
         "position_source": "SHADOW_LEDGER" if execution_mode == "SHADOW" else "FUTU_ACCOUNT",
         "pnl_ratio": pnl_ratio, "drawdown": drawdown, "trigger_mode": trigger_mode,
         "profit_giveback": profit_giveback,
         "validation_warnings": issues, "direction_reason": direction_reason,
         "direction_metrics": direction_metrics,
         "volatility_regime": regime,
         "size_factor": size_factor,
         "size_reason": size_reason,
         "structure": campaign.get("structure", "LONG_OPTION"),
         "opportunity_score": campaign.get("opportunity_score"),
         "expected_move_edge": campaign.get("expected_move_edge"),
    }
    if trigger_mode == "VOLATILITY":
        decision_payload.update({
            "entry_tier": campaign.get("entry_tier", campaign.get("adaptive_risk_tier")),
            "base_premium_budget": campaign.get("base_premium_budget"),
            "effective_premium_budget": campaign.get(
                "effective_premium_budget", campaign.get("max_premium_per_stage")
            ),
            "signal_started_at": campaign_state.get("signal_started_at"),
            "entry_window_end": event_time.isoformat(),
            "position_opened_at": campaign_state.get("position_opened_at"),
            "peak_pnl_ratio": peak_pnl_ratio,
            "profit_protection_profile": (
                volatility_profit_protection_profile(campaign, regime)
                if regime is not None else {}
            ),
            "partial_exit_tier": partial_exit_tier,
            "entry_iv_actual": campaign_state.get("entry_iv_actual"),
            "entry_gamma_theta_efficiency_actual": campaign_state.get(
                "entry_gamma_theta_efficiency_actual"
            ),
        })
    else:
        decision_payload.update({
            "entry_start": (
                event_time - timedelta(hours=int(campaign["entry_hours"]))
            ).isoformat(),
            "event_time": event_time.isoformat(),
        })
    if action == "HOLD":
        executor.store.decision_if_changed(
            strategy_id, code, action, reason, decision_payload, repeat_after_seconds=900,
        )
    else:
        executor.store.decision_if_changed(
            strategy_id, code, action, reason, decision_payload, repeat_after_seconds=900,
        )
    if action == "BUY":
        def record_pretrade_block(check: str, block_reason: str, details: dict) -> None:
            payload = {**decision_payload, "pre_trade_check": check, **details}
            executor.store.decision(
                strategy_id, code, "BLOCKED", block_reason, payload,
            )
            executor.store.audit(
                "PRE_TRADE_BLOCKED", {
                    "symbol": code, "underlying": campaign["underlying"],
                    "check": check, "reason": block_reason, **details,
                }, strategy_id,
            )

        multiplier = float(option["option_contract_multiplier"])
        premium = float(campaign.get("structure_net_debit", ask)) * multiplier * per_stage
        if ask <= 0 or premium > float(campaign["max_premium_per_stage"]):
            block_reason = (
                f"交易前权利金检查未通过：本批权利金 ${premium:,.2f}，"
                f"活动预算 ${float(campaign['max_premium_per_stage']):,.2f}"
            )
            record_pretrade_block("PREMIUM_BUDGET", block_reason, {
                "premium": premium,
                "premium_budget": float(campaign["max_premium_per_stage"]),
                "ask_price": ask,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        option_positions = positions[positions["code"].astype(str).str.contains(r"\d{6}[CP]\d+$", regex=True)] if not positions.empty else positions
        option_codes = option_positions["code"].tolist() if not option_positions.empty else []
        option_quotes = broker.snapshots(option_codes) if option_codes else option_positions
        greeks = portfolio_greeks(option_positions, option_quotes)
        allowed, greek_reason = gamma_add_allowed(greeks, option, per_stage)
        if not allowed:
            block_reason = f"交易前 Greeks 风控未通过：{greek_reason}"
            record_pretrade_block("GREEKS_RISK", block_reason, {
                "greeks_reason": greek_reason,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        analysis = contract_analysis(
            option, float(owner["last_price"]),
            realized_volatility(underlying_bars),
            executor.store,
        )
        if trigger_mode == "VOLATILITY":
            quality_allowed, quality_reason, quality_checks = volatility_entry_quality_allowed(
                campaign,
                default_min_score=float(campaign.get("minimum_entry_quality_score", 55)),
                default_min_expected_return=float(
                    campaign.get("minimum_model_expected_return", .02)
                ),
            )
            if not quality_allowed:
                record_pretrade_block("ENTRY_QUALITY", quality_reason, {
                    "entry_quality_checks": quality_checks,
                })
                print(f"{code}: BLOCKED ({quality_reason})")
                return
        allowed, iv_reason = iv_entry_allowed(analysis, trigger_mode)
        if not allowed:
            block_reason = f"交易前 IV 风控未通过：{iv_reason}"
            record_pretrade_block("IV_RISK", block_reason, {
                "iv_reason": iv_reason, "analysis": analysis,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        if len(legs) > 1 and execution_mode != "SHADOW":
            active_combo_id = automatic_combo_id(
                strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
            )
            existing_combo = executor.store.option_combo(active_combo_id)
            if existing_combo and existing_combo.get("status") == "OPEN":
                executor.store.decision_if_changed(
                    strategy_id, code, "HOLD", "定义风险组合已经完成开仓",
                    {"combo_id": active_combo_id, "structure": campaign.get("structure")},
                    repeat_after_seconds=300,
                )
                return
            quotes = broker.snapshots([leg["code"] for leg in legs]).set_index("code")
            combo_legs = []
            for leg in legs:
                quote = quotes.loc[leg["code"]]
                side = str(leg["side"]).upper()
                combo_legs.append({
                    "role": str(leg.get("role") or (
                        "RISK" if side == "SELL" else "PROTECTIVE"
                    )),
                    "symbol": str(leg["code"]),
                    "side": side,
                    "quantity": per_stage * int(leg.get("ratio", 1)),
                    "limit_price": float(
                        quote["ask_price"] if side == "BUY" else quote["bid_price"]
                    ),
                })
            manager = ComboExecutionManager(broker, executor.store, executor)
            if existing_combo is None:
                create_combo(executor.store, {
                    "combo_id": active_combo_id,
                    "strategy_id": strategy_id,
                    "name": f"自动波动率表达 {campaign.get('underlying')}",
                    "underlying": campaign["underlying"],
                    "structure": campaign.get("structure", "DEFINED_RISK"),
                    "max_net_debit": float(campaign["max_premium_per_stage"]) / 100,
                    "min_net_credit": 0,
                    "assembly_timeout_seconds": 300,
                    "naked_timeout_seconds": 15,
                    "auto_rollback": True,
                }, combo_legs)
            manager.start(active_combo_id)
            executor.store.audit("VOLATILITY_COMBO_SUBMITTED", {
                "combo_id": active_combo_id, "symbol": code,
                "underlying": campaign["underlying"],
                "structure": campaign.get("structure"), "legs": combo_legs,
            }, strategy_id)
            executor.store.decision(
                strategy_id, code, "BUY",
                f"已提交定义风险组合：{campaign.get('structure_label', campaign.get('structure'))}",
                {**decision_payload, "combo_id": active_combo_id, "legs": combo_legs},
            )
            return
        leg_quotes = broker.snapshots([leg["code"] for leg in legs]).set_index("code")
        cycle_suffix = (
            f":cycle{int(campaign_state.get('entry_cycle', 0))}"
            if trigger_mode == "VOLATILITY" else ""
        )
        for leg_number, leg in enumerate(legs, 1):
            leg_quote = leg_quotes.loc[leg["code"]]
            side = str(leg["side"]).upper()
            leg_price = float(leg_quote["ask_price"] if side == "BUY" else leg_quote["bid_price"])
            result = executor.submit_limit(
                strategy_id,
                f"{campaign['event_date']}:{campaign.get('campaign_id', code)}"
                f"{cycle_suffix}:OPEN:stage{stages_filled + 1}:leg{leg_number}",
                leg["code"], side, per_stage * int(leg.get("ratio", 1)), leg_price,
                {"multiplier": float(leg_quote.get("option_contract_multiplier", multiplier)),
                 "reason": reason, "underlying": campaign["underlying"],
                 "structure": campaign.get("structure"), "combo_primary": code,
                 "entry_tier": campaign.get("entry_tier", campaign.get("adaptive_risk_tier")),
                 "base_premium_budget": campaign.get("base_premium_budget"),
                 "effective_premium_budget": campaign.get(
                     "effective_premium_budget", campaign.get("max_premium_per_stage")
                 )},
            )
            print(result)
    elif action == "SELL" and quantity > 0:
        if bid <= 0:
            reason = f"等待可卖出盘口：买一 {bid:.2f} 不可用，下一轮继续刷新尝试退出"
            executor.store.decision(strategy_id, code, "HOLD", reason, {
                **decision_payload,
                "market_data_status": "WAITING_EXIT_LIQUIDITY",
                "book_reason": reason,
                "planned_order_quantity": int(planned_order_quantity or quantity),
            })
            executor.store.audit("OPTION_EXIT_WAITING_FOR_BID", {
                "symbol": code, "strategy_id": strategy_id, "reason": reason,
                "position_quantity": quantity,
                "planned_order_quantity": int(planned_order_quantity or quantity),
            }, strategy_id)
            print(f"{code}: HOLD ({reason})")
            return
        if len(legs) > 1 and execution_mode != "SHADOW":
            active_combo_id = automatic_combo_id(
                strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
            )
            active_combo = executor.store.option_combo(active_combo_id)
            if not active_combo:
                executor.store.decision(
                    strategy_id, code, "BLOCKED",
                    "组合平仓失败：未找到对应的自动组合账本",
                    {**decision_payload, "combo_id": active_combo_id},
                )
                return
            ComboExecutionManager(
                broker, executor.store, executor
            ).request_close(active_combo_id)
            campaign_state["pending_exit_reason"] = reason
            executor.store.audit("VOLATILITY_COMBO_CLOSE_REQUESTED", {
                "combo_id": active_combo_id, "symbol": code, "reason": reason,
            }, strategy_id)
            return
        exit_quantity = int(planned_order_quantity or quantity)
        leg_quotes = broker.snapshots([leg["code"] for leg in legs]).set_index("code")
        cycle_suffix = (
            f":cycle{int(campaign_state.get('entry_cycle', 0))}"
            if trigger_mode == "VOLATILITY" else ""
        )
        for leg_number, leg in enumerate(legs, 1):
            opening_side = str(leg["side"]).upper()
            closing_side = "SELL" if opening_side == "BUY" else "BUY"
            leg_quote = leg_quotes.loc[leg["code"]]
            leg_price = float(leg_quote["bid_price"] if closing_side == "SELL" else leg_quote["ask_price"])
            result = executor.submit_limit(
                strategy_id,
                f"{campaign['event_date']}:{campaign.get('campaign_id', code)}"
                f"{cycle_suffix}:CLOSE:{reason}:leg{leg_number}",
                leg["code"], closing_side, exit_quantity * int(leg.get("ratio", 1)), leg_price,
                {"multiplier": float(leg_quote.get("option_contract_multiplier", 100)),
                 "reason": reason, "urgent": True, "structure": campaign.get("structure"),
                 "combo_primary": code, "partial_exit_tier": partial_exit_tier,
                 "planned_order_quantity": exit_quantity},
            )
            print(result)
            if trigger_mode == "VOLATILITY" and result.status in {
                "SHADOW", "SHADOW_PART", "SHADOW_FILLED", "SUBMITTED",
                "FILLED_PART", "FILLED",
            }:
                if partial_exit_tier:
                    tiers = list(campaign_state.get("partial_exit_tiers") or [])
                    if partial_exit_tier not in tiers:
                        tiers.append(partial_exit_tier)
                    campaign_state["partial_exit_tiers"] = tiers
                else:
                    # Do not start cooldown when an exit is merely submitted. The next
                    # observed position transition from positive to zero confirms it.
                    campaign_state["pending_exit_reason"] = reason


def run_once(reconcile: bool = True) -> None:
    settings = Settings.load()
    if not settings.enable_paper_orders:
        raise RuntimeError("ENABLE_PAPER_ORDERS=false; automatic paper execution is disabled")
    broker = FutuPaperBroker(settings)
    executor = ExecutionManager(broker)
    if reconcile:
        executor.reconcile()
    positions = broker.positions()
    orders = broker.orders()
    state = load_state()
    campaigns = load_gamma_campaigns()
    active_keys = {f"earnings_gamma:{item['code']}" for item in campaigns}
    state = {
        key: value for key, value in state.items()
        if not key.startswith("earnings_gamma:") or key in active_keys
    }
    executor.store.prune_campaign_states("earnings_gamma:", active_keys)
    for campaign in campaigns:
        if campaign.get("enabled", True) and campaign.get("trigger_mode", "EARNINGS") == "EARNINGS":
            try:
                run_campaign(
                    broker, executor, campaign, state,
                    positions_snapshot=positions, orders_snapshot=orders,
                )
            except (BrokerError, ValueError, KeyError) as exc:
                print(f"{campaign.get('code')}: ERROR {exc}")
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run automatic Futu paper Gamma strategies")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    while True:
        run_once()
        if not args.loop:
            break
        clock.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
