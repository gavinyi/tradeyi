from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
import json
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import (
    load_gamma_campaigns, load_volatility_campaigns,
    save_gamma_campaigns, save_volatility_campaigns,
)
from .option_lifecycle import parse_option_code
from .store import TradingStore


NY = ZoneInfo("America/New_York")
OPEN_INTENT_STATUSES = {
    "CREATED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART",
    "CANCEL_REQUESTED", "SHADOW", "SHADOW_PART",
}


@dataclass(frozen=True)
class CleanupResult:
    config_key: str
    strategy_id: str
    scanned: int
    removed: int
    skipped: int
    state_deleted: int
    details: list[dict]


def _identity(campaign: dict) -> str:
    return str(campaign.get("campaign_id") or campaign.get("code") or "")


def campaign_option_codes(campaign: dict) -> tuple[str, ...]:
    """Return all option contracts owned by one saved campaign."""
    codes: list[str] = []
    primary = str(campaign.get("code") or "").upper()
    if parse_option_code(primary):
        codes.append(primary)
    for leg in campaign.get("legs") or []:
        code = str(leg.get("code") or leg.get("symbol") or "").upper()
        if parse_option_code(code):
            codes.append(code)
    return tuple(dict.fromkeys(codes))


def campaign_expired(campaign: dict, as_of: date | None = None) -> tuple[bool, str]:
    """A campaign is expired only when every recognized option leg is expired."""
    today = as_of or datetime.now(NY).date()
    contracts = [parse_option_code(code) for code in campaign_option_codes(campaign)]
    contracts = [contract for contract in contracts if contract is not None]
    if not contracts:
        return False, "未识别到期权合约代码"
    live = [contract for contract in contracts if contract.expiration >= today]
    if live:
        nearest = min(contract.expiration for contract in live)
        return False, f"仍有未到期合约，最近到期日 {nearest.isoformat()}"
    latest = max(contract.expiration for contract in contracts)
    return True, f"全部合约已过期，最近到期日 {latest.isoformat()}"


def _broker_positions_by_code(broker) -> dict[str, float]:
    positions = broker.positions(refresh=True)
    if positions is None or positions.empty or "code" not in positions.columns:
        return {}
    qty_column = "qty" if "qty" in positions.columns else "quantity" if "quantity" in positions.columns else None
    if not qty_column:
        return {}
    grouped: dict[str, float] = {}
    for _, row in positions.iterrows():
        code = str(row.get("code", "")).upper()
        grouped[code] = grouped.get(code, 0.0) + float(row.get(qty_column, 0) or 0)
    return grouped


def _has_broker_position(positions_by_code: dict[str, float], codes: Iterable[str]) -> tuple[bool, str]:
    active = {
        code: qty for code in codes
        if abs(qty := float(positions_by_code.get(str(code).upper(), 0.0))) > 1e-9
    }
    if active:
        return True, "富途仍有持仓：" + "，".join(f"{code} {qty:g}" for code, qty in active.items())
    return False, "富途无持仓"


def _open_broker_order_symbols(broker) -> set[str]:
    try:
        orders = broker.orders(historical=False)
    except TypeError:
        orders = broker.orders()
    if orders is None or orders.empty or "code" not in orders.columns:
        return set()
    if "order_status" not in orders.columns:
        return {str(row.get("code", "")).upper() for _, row in orders.iterrows()}
    return {
        str(row.get("code", "")).upper()
        for _, row in orders.iterrows()
        if broker.is_cancellable_status(row.get("order_status"))
    }


def _has_shadow_position(store: TradingStore, strategy_id: str, codes: Iterable[str]) -> tuple[bool, str]:
    active = {}
    for code in codes:
        quantity, _ = store.shadow_position(strategy_id, str(code).upper())
        if abs(float(quantity)) > 1e-9:
            active[str(code).upper()] = float(quantity)
    if active:
        return True, "Shadow 仍有持仓：" + "，".join(f"{code} {qty:g}" for code, qty in active.items())
    return False, "Shadow 无持仓"


def _has_open_intent(store: TradingStore, strategy_ids: Iterable[str], codes: Iterable[str]) -> tuple[bool, str]:
    strategy_set = set(strategy_ids)
    code_set = {str(code).upper() for code in codes}
    active = [
        row for row in store.intents(limit=5000)
        if row.get("strategy_id") in strategy_set
        and str(row.get("symbol", "")).upper() in code_set
        and str(row.get("status", "")).upper() in OPEN_INTENT_STATUSES
    ]
    if active:
        return True, f"仍有未完成订单意图 {len(active)} 条"
    return False, "无未完成订单意图"


def _cleanup_one_group(
    store: TradingStore,
    broker,
    campaigns: list[dict],
    config_key: str,
    strategy_id: str,
    shadow_strategy_ids: tuple[str, ...],
    positions_by_code: dict[str, float],
    open_broker_symbols: set[str],
    as_of: date | None = None,
) -> tuple[list[dict], CleanupResult]:
    kept: list[dict] = []
    details: list[dict] = []
    state_keys: list[str] = []
    removed = skipped = 0
    for campaign in campaigns:
        identity = _identity(campaign)
        codes = campaign_option_codes(campaign)
        expired, expiry_reason = campaign_expired(campaign, as_of)
        reasons = [expiry_reason]
        if not expired:
            kept.append(campaign)
            continue
        broker_has_position, reason = _has_broker_position(positions_by_code, codes)
        reasons.append(reason)
        shadow_blocks: list[str] = []
        shadow_has_position = False
        for owner in shadow_strategy_ids:
            has_shadow, shadow_reason = _has_shadow_position(store, owner, codes)
            shadow_has_position = shadow_has_position or has_shadow
            if has_shadow:
                shadow_blocks.append(f"{owner}：{shadow_reason}")
        if shadow_blocks:
            reasons.extend(shadow_blocks)
        else:
            reasons.append("Shadow 无持仓")
        open_broker_codes = sorted(set(codes).intersection(open_broker_symbols))
        if open_broker_codes:
            reasons.append("富途仍有待成交订单：" + "，".join(open_broker_codes))
        has_intent, intent_reason = _has_open_intent(store, shadow_strategy_ids, codes)
        reasons.append(intent_reason)
        if broker_has_position or shadow_has_position or open_broker_codes or has_intent:
            skipped += 1
            kept.append(campaign)
            details.append({
                "campaign_id": identity,
                "code": campaign.get("code"),
                "codes": list(codes),
                "action": "SKIPPED",
                "reason": "；".join(reasons),
            })
            continue
        removed += 1
        for owner in shadow_strategy_ids:
            state_keys.extend(f"{owner}:{code}" for code in codes)
        details.append({
            "campaign_id": identity,
            "code": campaign.get("code"),
            "codes": list(codes),
            "action": "REMOVED",
            "reason": "；".join(reasons),
        })
    state_deleted = store.delete_campaign_states(tuple(state_keys))
    if removed:
        store.audit(
            "EXPIRED_CAMPAIGN_AUTO_CLEANED",
            {
                "config_key": config_key,
                "strategy_id": strategy_id,
                "removed": removed,
                "state_deleted": state_deleted,
                "details": [item for item in details if item["action"] == "REMOVED"],
            },
            strategy_id,
        )
    result = CleanupResult(
        config_key=config_key,
        strategy_id=strategy_id,
        scanned=len(campaigns),
        removed=removed,
        skipped=skipped,
        state_deleted=state_deleted,
        details=details,
    )
    return kept, result


def cleanup_expired_campaigns(
    store: TradingStore | None = None,
    broker=None,
    as_of: date | None = None,
) -> dict:
    """Safely remove expired option campaigns that no longer own risk.

    If broker data cannot be read, the function raises instead of deleting
    anything. Expired contracts may still have assignment/settlement residue, so
    broker holdings and open orders are hard safety checks.
    """
    if broker is None:
        raise ValueError("cleanup_expired_campaigns requires a broker for safety checks")
    store = store or TradingStore()
    positions_by_code = _broker_positions_by_code(broker)
    open_broker_symbols = _open_broker_order_symbols(broker)
    gamma = load_gamma_campaigns()
    volatility = load_volatility_campaigns()
    gamma_kept, gamma_result = _cleanup_one_group(
        store, broker, gamma, "GAMMA_CAMPAIGNS", "earnings_gamma",
        ("earnings_gamma", "earnings_gamma_manual"),
        positions_by_code, open_broker_symbols, as_of,
    )
    volatility_kept, volatility_result = _cleanup_one_group(
        store, broker, volatility, "VOLATILITY_CAMPAIGNS", "volatility_long_gamma",
        ("volatility_long_gamma",),
        positions_by_code, open_broker_symbols, as_of,
    )
    if len(gamma_kept) != len(gamma):
        save_gamma_campaigns(gamma_kept)
    if len(volatility_kept) != len(volatility):
        save_volatility_campaigns(volatility_kept)
    result = {
        "groups": [asdict(gamma_result), asdict(volatility_result)],
        "removed_total": gamma_result.removed + volatility_result.removed,
        "skipped_total": gamma_result.skipped + volatility_result.skipped,
    }
    store.set_setting("last_campaign_cleanup_result", json.dumps(result, ensure_ascii=False, default=str))
    return result
