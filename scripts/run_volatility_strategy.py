from __future__ import annotations

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_volatility_campaigns, upsert_campaign, delete_campaign
from futu_quant.execution import ExecutionManager
from futu_quant.combo_execution import find_open_combo_for_campaign
from futu_quant.volatility_roll import roll_campaign, should_replace_campaign
from futu_quant.volatility_optimizer import volatility_entry_quality_allowed
from futu_quant.volatility_orphans import (
    manage_volatility_orphans,
    protected_volatility_state_keys,
)
from futu_quant.option_lifecycle import parse_option_code
from datetime import date, datetime, timezone
import os
from pathlib import Path
import pandas as pd
if __package__:
    from scripts.run_gamma_strategy import load_state, run_campaign, save_state
else:
    from run_gamma_strategy import load_state, run_campaign, save_state

STOP_FILE = Path("data/trading-service.stop")
RESTART_FILE = Path("data/trading-service.restart")


def stop_requested() -> bool:
    return STOP_FILE.exists() or RESTART_FILE.exists()


def close_stale_expired_combos(store, positions: pd.DataFrame, today: date) -> int:
    """Close local combo records that cannot map to a live broker position anymore.

    This is a bookkeeping cleanup only. It deliberately requires all combo legs
    to be expired and absent from the broker account, so it does not hide a real
    position that still needs to be exited.
    """
    position_codes = set()
    if positions is not None and not positions.empty and "code" in positions.columns:
        for _, row in positions.iterrows():
            try:
                qty = float(row.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if abs(qty) > 1e-9:
                position_codes.add(str(row.get("code", "")).upper())

    cleaned = 0
    for combo in store.option_combos(limit=500):
        if combo.get("strategy_id") != "volatility_long_gamma" or combo.get("status") != "OPEN":
            continue
        legs = store.option_combo_legs(combo["combo_id"])
        contracts = [parse_option_code(str(leg.get("symbol", "")).upper()) for leg in legs]
        if not contracts or any(contract is None for contract in contracts):
            continue
        leg_codes = {str(leg.get("symbol", "")).upper() for leg in legs}
        if any(code in position_codes for code in leg_codes):
            continue
        if max(contract.expiration for contract in contracts if contract is not None) >= today:
            continue
        store.update_option_combo(combo["combo_id"], "CLOSED", "过期组合在富途已无持仓，自动完成本地收尾")
        store.option_combo_event(
            combo["combo_id"], "STALE_EXPIRED_COMBO_CLOSED", "CLOSED",
            "过期组合在富途已无持仓，已自动完成本地账本收尾",
            {"leg_codes": sorted(leg_codes)},
        )
        cleaned += 1
    return cleaned


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
    stale_cleaned = close_stale_expired_combos(executor.store, positions, date.today())
    if stale_cleaned:
        executor.store.audit(
            "VOLATILITY_STALE_EXPIRED_COMBOS_CLOSED",
            {"count": stale_cleaned},
            "volatility_long_gamma",
        )
    state = load_state()
    campaigns = load_volatility_campaigns()
    # Clean malformed contracts before they reach the decision loop.  A bad
    # code otherwise produces hundreds of repetitive BLOCKED decisions and
    # obscures the actionable diagnostics for valid campaigns.
    valid_campaigns = []
    for item in campaigns:
        code = str(item.get("code", ""))
        parsed_code = parse_option_code(code)
        if parsed_code is None:
            valid_legs = [
                str(leg.get("code", "")).upper()
                for leg in (item.get("legs") or [])
                if parse_option_code(str(leg.get("code", "")).upper()) is not None
            ]
            if valid_legs:
                normalized = dict(item)
                normalized["code"] = next(
                    (
                        str(leg.get("code", "")).upper()
                        for leg in (item.get("legs") or [])
                        if str(leg.get("side", "")).upper() == "BUY"
                        and parse_option_code(str(leg.get("code", "")).upper()) is not None
                    ),
                    valid_legs[0],
                )
                parsed = parse_option_code(normalized["code"])
                normalized["strike_time"] = parsed.expiration.isoformat() if parsed else ""
                executor.store.audit("VOLATILITY_CAMPAIGN_CODE_RECOVERED", {
                    "old_code": code, "new_code": normalized["code"],
                    "underlying": normalized.get("underlying"),
                    "structure": normalized.get("structure"),
                    "valid_legs": valid_legs,
                }, "volatility_long_gamma")
                valid_campaigns.append(normalized)
                continue
            executor.store.decision_if_changed(
                "volatility_long_gamma", code or None, "BLOCKED",
                "候选池已过滤：期权代码格式无效或无法解析到期日",
                {"code": code, "underlying": item.get("underlying", "")},
                repeat_after_seconds=3600,
            )
            continue
        normalized = dict(item)
        normalized["code"] = code.upper()
        strike_time = pd.to_datetime(normalized.get("strike_time"), errors="coerce")
        if pd.isna(strike_time):
            normalized["strike_time"] = parsed_code.expiration.isoformat()
            executor.store.audit("VOLATILITY_CAMPAIGN_EXPIRY_RECOVERED", {
                "code": normalized["code"],
                "old_strike_time": str(item.get("strike_time")),
                "new_strike_time": normalized["strike_time"],
            }, "volatility_long_gamma")
        valid_campaigns.append(normalized)
    campaigns = valid_campaigns
    orphan_results = manage_volatility_orphans(
        broker=broker,
        executor=executor,
        campaigns=campaigns,
        positions=positions,
        orders=orders,
        now=datetime.now(timezone.utc),
    )
    if orphan_results:
        print(f"VOLATILITY ORPHANS HANDLED: {len(orphan_results)}")
        adopted_campaigns = [
            item.get("campaign") for item in orphan_results
            if item.get("action") == "ADOPTED" and isinstance(item.get("campaign"), dict)
        ]
        existing_codes = {str(item.get("code", "")).upper() for item in campaigns}
        for adopted in adopted_campaigns:
            if str(adopted.get("code", "")).upper() not in existing_codes:
                campaigns.append(adopted)
                existing_codes.add(str(adopted.get("code", "")).upper())
        state = load_state()
    protected_keys = protected_volatility_state_keys(
        executor.store, positions, orders, broker,
    )
    active_keys = {
        f"volatility_long_gamma:{item['code']}" for item in campaigns
    } | protected_keys
    state = {
        key: value for key, value in state.items()
        if not key.startswith("volatility_long_gamma:") or key in active_keys
    }
    executor.store.prune_campaign_states("volatility_long_gamma:", active_keys)
    rolled_pairs = []
    prepared = []
    now = datetime.now(timezone.utc)
    maintenance_interval = max(
        60, int(os.getenv("VOLATILITY_EXPRESSION_REVIEW_SECONDS", "300"))
    )
    for campaign in campaigns:
        if stop_requested():
            executor.store.audit(
                "VOLATILITY_LOOP_INTERRUPTED_FOR_SERVICE_CONTROL",
                {"reason": "检测到服务停止或重启请求，提前结束本轮波动率策略循环"},
                "volatility_long_gamma",
            )
            break
        current_code = str(campaign.get("code", ""))
        underlying = str(campaign.get("underlying", ""))
        leg_symbols = {
            str(leg.get("code", "")).upper()
            for leg in (campaign.get("legs") or [])
            if parse_option_code(str(leg.get("code", "")).upper()) is not None
        }
        open_combo = find_open_combo_for_campaign(
            executor.store,
            strategy_id="volatility_long_gamma",
            underlying=underlying,
            structure=str(campaign.get("structure", "")),
            leg_symbols=leg_symbols,
        ) if len(leg_symbols) > 1 else None
        broker_position = (
            positions[positions["code"] == current_code] if not positions.empty
            else positions
        )
        broker_quantity = int(broker_position.iloc[0]["qty"]) if not broker_position.empty else 0
        shadow_quantity, _ = executor.store.shadow_position(
            "volatility_long_gamma", current_code,
        )
        quantity = max(broker_quantity, int(shadow_quantity), 1 if open_combo else 0)
        pending = (
            orders[
                (orders["code"] == current_code)
                & orders["order_status"].map(broker.is_cancellable_status)
            ]
            if not orders.empty and current_code else pd.DataFrame()
        )
        maintenance_key = f"volatility_expression_review:{underlying}"
        last_review = state.get(maintenance_key)
        due_for_review = True
        if last_review:
            try:
                due_for_review = (
                    now - datetime.fromisoformat(str(last_review))
                ).total_seconds() >= maintenance_interval
            except ValueError:
                due_for_review = True
        expiry = pd.to_datetime(campaign.get("strike_time"), errors="coerce")
        needs_expiry_roll = pd.isna(expiry) or expiry.date() <= date.today()
        quality_allowed, quality_reason, quality_checks = volatility_entry_quality_allowed(
            campaign,
            default_min_score=float(campaign.get("minimum_entry_quality_score", 54)),
            default_min_expected_return=float(
                campaign.get("minimum_model_expected_return", .02)
            ),
        )
        needs_quality_replacement = bool(
            campaign.get("optimizer_version") == 2
            and quantity <= 0
            and pending.empty
            and not quality_allowed
        )
        should_review_expression = (
            campaign.get("enabled", True)
            and quantity <= 0
            and pending.empty
            and (due_for_review or needs_quality_replacement)
        )
        if needs_expiry_roll or should_review_expression:
            rolled, reason = roll_campaign(broker, campaign, executor.store)
            replace, replace_reason = (
                (
                    parse_option_code(str(rolled.get("code", ""))) is not None
                    and rolled.get("code") != campaign.get("code"),
                    reason,
                ) if needs_expiry_roll
                else should_replace_campaign(campaign, rolled)
            )
            if needs_quality_replacement:
                executor.store.audit("VOLATILITY_QUALITY_REPLACEMENT_REQUESTED", {
                    "old_code": campaign.get("code"),
                    "underlying": underlying,
                    "quality_reason": quality_reason,
                    "quality_checks": quality_checks,
                    "roll_reason": reason,
                }, "volatility_long_gamma")
            state[maintenance_key] = now.isoformat()
            if replace and rolled.get("code") != campaign.get("code"):
                rolled_pairs.append((
                    campaign.get("campaign_id", campaign.get("code")),
                    campaign,
                    rolled,
                ))
                executor.store.audit("VOLATILITY_CONTRACT_ROLLED", {
                    "old_code": campaign.get("code"), "new_code": rolled.get("code"),
                    "reason": reason, "replace_reason": replace_reason,
                    "maintenance_type": (
                        "EXPIRY_ROLL" if needs_expiry_roll else
                        "QUALITY_REPLACEMENT" if needs_quality_replacement else
                        "BEST_EXPRESSION_REVIEW"
                    ),
                }, "volatility_long_gamma")
                executor.store.decision(
                    "volatility_long_gamma", rolled.get("code"), "HOLD",
                    f"已维护为当前最佳期权表达：{replace_reason}",
                    {"old_code": campaign.get("code"), "new_code": rolled.get("code"),
                     "roll_reason": reason,
                     "diagnostics": rolled.get("_roll_diagnostics", {})},
                )
            else:
                executor.store.decision_if_changed(
                    "volatility_long_gamma", campaign.get("code"), "HOLD",
                    f"期权表达复核未替换：{replace_reason if should_review_expression else reason}",
                    {"roll_reason": reason, "diagnostics": rolled.get("_roll_diagnostics", {})},
                    repeat_after_seconds=900,
                )
            campaign = rolled
        prepared.append(campaign)
    for old_identity, old_campaign, rolled in rolled_pairs:
        old_code = str(old_campaign.get("code") or "").upper()
        old_has_broker_position = False
        old_has_pending_order = False
        if old_code:
            if not positions.empty and "code" in positions.columns:
                old_rows = positions[positions["code"].astype(str).str.upper() == old_code]
                if not old_rows.empty:
                    old_has_broker_position = (
                        pd.to_numeric(old_rows.get("qty"), errors="coerce").fillna(0).abs().sum() > 0
                    )
            if not orders.empty and {"code", "order_status"}.issubset(orders.columns):
                old_order_rows = orders[orders["code"].astype(str).str.upper() == old_code]
                old_has_pending_order = bool(
                    not old_order_rows.empty
                    and old_order_rows["order_status"].map(broker.is_cancellable_status).any()
                )
        if old_has_broker_position or old_has_pending_order:
            retained = {
                **old_campaign,
                "exit_only": True,
                "replacement_code": rolled.get("code"),
                "replacement_reason": "旧合约仍有富途风险，保留为退出维护配置",
            }
            upsert_campaign("VOLATILITY_CAMPAIGNS", retained)
            executor.store.audit("VOLATILITY_OLD_CAMPAIGN_RETAINED_FOR_EXIT", {
                "old_code": old_code,
                "new_code": rolled.get("code"),
                "has_broker_position": old_has_broker_position,
                "has_pending_order": old_has_pending_order,
            }, "volatility_long_gamma")
        else:
            delete_campaign("VOLATILITY_CAMPAIGNS", old_identity)
        upsert_campaign("VOLATILITY_CAMPAIGNS", rolled)
    for campaign in prepared:
        if stop_requested():
            executor.store.audit(
                "VOLATILITY_EXECUTION_INTERRUPTED_FOR_SERVICE_CONTROL",
                {"reason": "检测到服务停止或重启请求，提前结束本轮波动率交易执行"},
                "volatility_long_gamma",
            )
            break
        if not campaign.get("enabled", True):
            continue
        campaign = {**campaign, "trigger_mode": "VOLATILITY"}
        try:
            run_campaign(
                broker, executor, campaign, state, "volatility_long_gamma",
                positions_snapshot=positions, orders_snapshot=orders,
            )
        except (BrokerError, ValueError, KeyError) as exc:
            print(f"{campaign.get('code')}: ERROR {exc}")
    save_state(state)


if __name__ == "__main__":
    run_once()
