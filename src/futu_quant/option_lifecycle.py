from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

import pandas as pd


OPTION_CODE = re.compile(
    r"^US\.(?P<underlying>[A-Z][A-Z0-9.]*)"
    r"(?P<expiration>\d{6})(?P<option_type>[CP])(?P<strike>\d+)$"
)
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OptionContract:
    code: str
    underlying: str
    expiration: date
    option_type: str
    strike: float


def parse_option_code(code: str) -> OptionContract | None:
    match = OPTION_CODE.fullmatch(str(code).upper())
    if not match:
        return None
    return OptionContract(
        code=str(code).upper(),
        underlying=f"US.{match.group('underlying')}",
        expiration=datetime.strptime(match.group("expiration"), "%y%m%d").date(),
        option_type="CALL" if match.group("option_type") == "C" else "PUT",
        strike=int(match.group("strike")) / 1000,
    )


def assess_option_positions(
    positions: pd.DataFrame,
    underlying_quotes: pd.DataFrame,
    cash: float,
    as_of: date | None = None,
) -> list[dict]:
    """Project exercise/assignment obligations for every option position."""
    today = as_of or datetime.now(NEW_YORK).date()
    quote_map = {
        str(row.get("code")): float(row.get("last_price", row.get("price", 0)) or 0)
        for _, row in underlying_quotes.iterrows()
    }
    stock_qty = {
        str(row.get("code")): float(row.get("qty", 0) or 0)
        for _, row in positions.iterrows() if parse_option_code(str(row.get("code", ""))) is None
    }
    rows: list[dict] = []
    for _, position in positions.iterrows():
        contract = parse_option_code(str(position.get("code", "")))
        if contract is None:
            continue
        quantity = float(position.get("qty", 0) or 0)
        if quantity == 0:
            continue
        spot = quote_map.get(contract.underlying, 0.0)
        intrinsic = (
            max(0.0, spot - contract.strike)
            if contract.option_type == "CALL"
            else max(0.0, contract.strike - spot)
        )
        dte = (contract.expiration - today).days
        itm = intrinsic > 0
        projected_cash = 0.0
        projected_shares = 0.0
        obligations: list[str] = []
        if itm and quantity > 0 and contract.option_type == "CALL":
            projected_cash = contract.strike * 100 * quantity
            projected_shares = 100 * quantity
            if projected_cash > cash:
                obligations.append(f"预计行权资金缺口 ${projected_cash - cash:,.2f}")
        elif itm and quantity > 0 and contract.option_type == "PUT":
            projected_shares = -100 * quantity
            available = stock_qty.get(contract.underlying, 0.0)
            if available < 100 * quantity:
                obligations.append(f"预计交割正股缺口 {100 * quantity - available:,.0f} 股")
        elif itm and quantity < 0 and contract.option_type == "CALL":
            projected_shares = 100 * quantity
            available = stock_qty.get(contract.underlying, 0.0)
            if available < abs(100 * quantity):
                obligations.append(f"裸 Call 指派缺口 {abs(100 * quantity) - available:,.0f} 股")
        elif itm and quantity < 0 and contract.option_type == "PUT":
            projected_cash = contract.strike * 100 * abs(quantity)
            projected_shares = 100 * abs(quantity)
            if projected_cash > cash:
                obligations.append(f"短 Put 指派资金缺口 ${projected_cash - cash:,.2f}")

        if dte < 0:
            severity, action = "CRITICAL", "核对到期结算"
        elif dte == 0:
            severity, action = "CRITICAL", "到期日前决定平仓或接受行权/指派"
        elif dte <= 1 and (itm or obligations):
            severity, action = "HIGH", "立即复核并准备平仓"
        elif dte <= 3:
            severity, action = "WARNING", "制定到期处置计划"
        elif dte <= 7:
            severity, action = "INFO", "关注到期风险"
        else:
            severity, action = "NORMAL", "继续监测"
        state = "价内" if itm else "价外或平值"
        reason = f"剩余 {dte} 天，{state}，每份内在价值 ${intrinsic:.2f}"
        if obligations:
            reason += "；" + "；".join(obligations)
            severity = "CRITICAL" if dte <= 1 else "HIGH"
        row = asdict(contract)
        row.update({
            "expiration": contract.expiration.isoformat(), "quantity": quantity,
            "underlying_price": spot, "intrinsic_value": intrinsic,
            "days_to_expiry": dte, "severity": severity, "action": action,
            "reason": reason, "projected_cash_need": projected_cash,
            "projected_share_need": projected_shares,
        })
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "INFO": 3, "NORMAL": 4}[row["severity"]],
            row["days_to_expiry"], row["code"],
        ),
    )


def monitor_option_lifecycle(broker, store) -> list[dict]:
    positions = broker.positions()
    contracts = [
        parsed for code in positions.get("code", pd.Series(dtype=str)).astype(str)
        if (parsed := parse_option_code(code)) is not None
    ]
    underlyings = sorted({contract.underlying for contract in contracts})
    quotes = broker.snapshots(underlyings) if underlyings else pd.DataFrame()
    account = broker.account_info()
    cash = float(account.iloc[0].get("cash", 0) or 0) if not account.empty else 0.0
    rows = assess_option_positions(positions, quotes, cash)
    store.record_option_lifecycle(rows)
    critical = [row for row in rows if row["severity"] in {"CRITICAL", "HIGH"}]
    store.heartbeat(
        "option_lifecycle", "HEALTHY" if not critical else "WARNING",
        f"监测 {len(rows)} 个期权持仓；高风险 {len(critical)} 个",
    )
    return rows


def execute_disposition_plans(broker, store, executor, session, now: datetime) -> list[dict]:
    """Submit explicitly enabled expiry exits through the normal simulated execution layer."""
    results: list[dict] = []
    lifecycle = {row["code"]: row for row in store.latest_option_lifecycle()}
    virtual = store.virtual_positions()
    for plan in store.option_dispositions():
        if not plan["enabled"] or plan["action"] != "AUTO_CLOSE":
            continue
        row = lifecycle.get(plan["code"])
        if row is None or int(row["days_to_expiry"]) > int(plan["close_days_before"]):
            continue
        if not session.pre_close(now, int(plan["close_window_minutes"])):
            continue
        quantity = float(row["quantity"])
        if quantity <= 0 or not quantity.is_integer():
            results.append({
                "code": plan["code"], "status": "BLOCKED",
                "message": "当前仅支持正整数的期权多头持仓自动平仓",
            })
            continue
        owners = [
            item for item in virtual
            if item["symbol"] == plan["code"] and float(item["quantity"]) >= quantity
        ]
        if not owners:
            results.append({
                "code": plan["code"], "status": "BLOCKED",
                "message": "没有找到数量足够的策略持仓归属，禁止自动平仓",
            })
            continue
        owner = max(owners, key=lambda item: float(item["quantity"]))["strategy_id"]
        quote = broker.snapshots([plan["code"]])
        if quote.empty:
            results.append({"code": plan["code"], "status": "BLOCKED", "message": "没有期权行情"})
            continue
        bid = float(quote.iloc[0].get("bid_price", 0) or 0)
        if bid <= 0:
            results.append({"code": plan["code"], "status": "BLOCKED", "message": "有效买一价为空"})
            continue
        result = executor.submit_limit(
            owner,
            f"expiry-auto-close:{plan['code']}:{row['expiration']}",
            plan["code"], "SELL", int(quantity), bid,
            {
                "underlying": row["underlying"], "multiplier": 100,
                "urgent": True, "reduce_only": True,
                "reason": "管理员预先批准的期权到期自动平仓",
            },
        )
        results.append({"code": plan["code"], "status": result.status, "message": result.message})
        store.audit("OPTION_DISPOSITION_EXECUTED", results[-1], owner)
    return results
