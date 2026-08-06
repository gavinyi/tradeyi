from __future__ import annotations

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_volatility_campaigns, upsert_campaign, delete_campaign
from futu_quant.execution import ExecutionManager
from futu_quant.volatility_roll import roll_campaign, should_replace_campaign
from futu_quant.volatility_optimizer import volatility_entry_quality_allowed
from futu_quant.option_lifecycle import parse_option_code
from datetime import date, datetime, timezone
import os
import pandas as pd
if __package__:
    from scripts.run_gamma_strategy import load_state, run_campaign, save_state
else:
    from run_gamma_strategy import load_state, run_campaign, save_state


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
    campaigns = load_volatility_campaigns()
    # Clean malformed contracts before they reach the decision loop.  A bad
    # code otherwise produces hundreds of repetitive BLOCKED decisions and
    # obscures the actionable diagnostics for valid campaigns.
    valid_campaigns = []
    for item in campaigns:
        code = str(item.get("code", ""))
        if parse_option_code(code) is None:
            executor.store.decision_if_changed(
                "volatility_long_gamma", code or None, "BLOCKED",
                "候选池已过滤：期权代码格式无效或无法解析到期日",
                {"code": code, "underlying": item.get("underlying", "")},
                repeat_after_seconds=3600,
            )
            continue
        valid_campaigns.append(item)
    campaigns = valid_campaigns
    active_keys = {f"volatility_long_gamma:{item['code']}" for item in campaigns}
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
        current_code = str(campaign.get("code", ""))
        underlying = str(campaign.get("underlying", ""))
        broker_position = (
            positions[positions["code"] == current_code] if not positions.empty
            else positions
        )
        broker_quantity = int(broker_position.iloc[0]["qty"]) if not broker_position.empty else 0
        shadow_quantity, _ = executor.store.shadow_position(
            "volatility_long_gamma", current_code,
        )
        quantity = max(broker_quantity, int(shadow_quantity))
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
                (True, reason) if needs_expiry_roll
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
                rolled_pairs.append((campaign.get("campaign_id", campaign.get("code")), rolled))
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
    for old_identity, rolled in rolled_pairs:
        delete_campaign("VOLATILITY_CAMPAIGNS", old_identity)
        upsert_campaign("VOLATILITY_CAMPAIGNS", rolled)
    for campaign in prepared:
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
