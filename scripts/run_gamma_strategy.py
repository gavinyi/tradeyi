from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import time as clock
from zoneinfo import ZoneInfo

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_gamma_campaigns
from futu_quant.execution import ExecutionManager
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
    volatility_cooldown_hours, volatility_exit_reason, volatility_regime,
    volatility_threshold_profile,
)
from futu_quant.data_quality import option_quote_freshness, quote_freshness
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.store import TradingStore


NY = ZoneInfo("America/New_York")
MARKET_CALENDAR = USMarketCalendar()
LEGACY_STATE_FILE = Path("data/gamma_state.json")


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
    execution_mode = (strategy_mode or global_mode).upper()
    if len(legs) > 1:
        reason = "多腿组合尚未接入原子组合订单，已禁止自动逐腿交易"
        executor.store.decision(strategy_id, code, "BLOCKED", reason, {
            "structure": campaign.get("structure"), "legs": legs,
        })
        print(f"{code}: BLOCKED {reason}")
        return
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
            state[f"volatility_cooldown_hours:{campaign['underlying']}"] = (
                volatility_cooldown_hours(
                    reason, float(campaign.get("cooldown_hours", 24))
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

    quote = broker.snapshots([code, campaign["underlying"]])
    option = quote[quote["code"] == code].iloc[0]
    owner = quote[quote["code"] == campaign["underlying"]].iloc[0]
    market_session = MARKET_CALENDAR.session(now)
    if not market_session.regular_open(now):
        reason = (
            "当前期权市场未开盘，旧期权报价仅用于参考；"
            "等待美股常规交易时段后重新校验并执行"
        )
        executor.store.decision_if_changed(
            strategy_id, code, "HOLD", reason, {
                "market_session": "OPTION_CLOSED",
                "option_quote_time": option.get("update_time", option.get("updated_time", "")),
            }, repeat_after_seconds=3600,
        )
        print(f"{code}: HOLD ({reason})")
        return
    option_fresh, option_fresh_reason, option_age, option_degraded = option_quote_freshness(
        option, now, fresh_seconds=180, maximum_seconds=900,
        maximum_spread_pct=float(campaign.get(
            "max_spread_pct", 12.0 if trigger_mode == "VOLATILITY" else 20.0
        )),
    )
    owner_fresh, owner_fresh_reason, owner_age = quote_freshness(owner, now)
    if not option_fresh or not owner_fresh:
        reason = f"行情数据新鲜度检查未通过：正股 {owner_fresh_reason}；期权 {option_fresh_reason}"
        executor.store.decision(strategy_id, code, "BLOCKED", reason, {
            "underlying_quote_age_seconds": owner_age, "option_quote_age_seconds": option_age,
        })
        print(f"{code}: BLOCKED ({reason})")
        return
    if option_degraded:
        executor.store.audit("OPTION_QUOTE_DEGRADED_ACCEPTED", {
            "strategy_id": strategy_id, "symbol": code,
            "option_quote_age_seconds": option_age,
            "reason": option_fresh_reason,
        }, strategy_id)
    last = float(option["last_price"] or 0)
    bid, ask = float(option["bid_price"] or 0), float(option["ask_price"] or 0)
    gamma_now = abs(market_number(option.get("option_gamma", 0)))
    theta_now = abs(market_number(option.get("option_theta", 0)))
    current_efficiency = gamma_now / theta_now if theta_now > 0 else 0.0
    current_iv = normalize_iv(option.get("option_implied_volatility", 0))
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
                reason = f"同一标的退出冷却中，剩余约 {remaining:.1f} 小时"
                executor.store.decision(strategy_id, code, "HOLD", reason, {"regime": regime})
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
    if len(legs) > 1 and execution_mode == "PAPER":
        reason = "多腿结构需要券商原子组合下单；当前富途接口仅支持拆腿，为避免裸腿风险只允许 Shadow 验证"
        executor.store.decision(strategy_id, code, "BLOCKED", reason, {
            "structure": campaign.get("structure"), "legs": legs,
        })
        print(f"{code}: BLOCKED ({reason})")
        return
    per_stage = int(campaign["contracts_per_stage"])
    stages_filled = quantity // per_stage
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
            )
        opened_at = datetime.fromisoformat(str(
            campaign_state.get("position_opened_at", now.isoformat())
        ))
        action, reason = decide_volatility_action(
            now=now, opened_at=opened_at, quantity=quantity,
            stages_filled=stages_filled, max_stages=int(campaign["max_stages"]),
            pnl_ratio=pnl_ratio, peak_pnl_ratio=peak_pnl_ratio,
            drawdown_from_peak=drawdown, directional_confirmation=direction_ok,
            signal_active=bool(campaign_state.get("signal_active", False)),
            profit_target=float(campaign["profit_target"]),
            stop_loss=float(campaign["stop_loss"]),
            trailing_stop=float(campaign["trailing_stop"]),
            max_hold_hours=float(campaign.get("max_hold_hours", 24)),
            decay_reason=decay_reason,
            minimum_hold_minutes=float(campaign.get("minimum_hold_minutes", 30)),
        )
    else:
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
    planned_order_quantity = per_stage if action == "BUY" else quantity if action == "SELL" else 0
    decision_payload = {
         "position_quantity": quantity, "planned_order_quantity": planned_order_quantity,
         "position_source": "SHADOW_LEDGER" if execution_mode == "SHADOW" else "FUTU_ACCOUNT",
         "pnl_ratio": pnl_ratio, "drawdown": drawdown, "trigger_mode": trigger_mode,
         "validation_warnings": issues, "direction_reason": direction_reason,
         "direction_metrics": direction_metrics,
         "volatility_regime": regime,
         "structure": campaign.get("structure", "LONG_OPTION"),
         "opportunity_score": campaign.get("opportunity_score"),
         "expected_move_edge": campaign.get("expected_move_edge"),
    }
    if trigger_mode == "VOLATILITY":
        decision_payload.update({
            "signal_started_at": campaign_state.get("signal_started_at"),
            "entry_window_end": event_time.isoformat(),
            "position_opened_at": campaign_state.get("position_opened_at"),
            "peak_pnl_ratio": peak_pnl_ratio,
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
        executor.store.decision(strategy_id, code, action, reason, decision_payload)
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
        allowed, iv_reason = iv_entry_allowed(analysis, trigger_mode)
        if not allowed:
            block_reason = f"交易前 IV 风控未通过：{iv_reason}"
            record_pretrade_block("IV_RISK", block_reason, {
                "iv_reason": iv_reason, "analysis": analysis,
            })
            print(f"{code}: BLOCKED ({block_reason})")
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
                 "structure": campaign.get("structure"), "combo_primary": code},
            )
            print(result)
    elif action == "SELL" and quantity > 0:
        if bid <= 0:
            print(f"{code}: blocked because bid is unavailable")
            return
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
                leg["code"], closing_side, quantity * int(leg.get("ratio", 1)), leg_price,
                {"multiplier": float(leg_quote.get("option_contract_multiplier", 100)),
                 "reason": reason, "urgent": True, "structure": campaign.get("structure"),
                 "combo_primary": code},
            )
            print(result)
            if trigger_mode == "VOLATILITY" and result.status in {
                "SHADOW", "SHADOW_PART", "SHADOW_FILLED", "SUBMITTED",
                "FILLED_PART", "FILLED",
            }:
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
