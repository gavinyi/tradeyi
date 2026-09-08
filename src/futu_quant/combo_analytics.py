from __future__ import annotations

from datetime import date, datetime, timezone
from math import inf
from zoneinfo import ZoneInfo

import pandas as pd

from .option_lifecycle import parse_option_code


def combo_market_snapshot(legs: list[dict], quotes: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    frame = pd.DataFrame(legs).copy()
    if frame.empty:
        return frame, {}
    quote_map = quotes.set_index("code").to_dict("index") if not quotes.empty and "code" in quotes else {}
    rows = []
    for _, leg in frame.iterrows():
        quote = quote_map.get(str(leg["symbol"]), {})
        quote_time = pd.to_datetime(quote.get("update_time"), errors="coerce")
        if not pd.isna(quote_time):
            if quote_time.tzinfo is None:
                quote_time = quote_time.tz_localize(ZoneInfo("America/New_York"))
            quote_age = max(0.0, (datetime.now(timezone.utc) - quote_time.tz_convert("UTC").to_pydatetime()).total_seconds())
        else:
            quote_age = inf
        bid = float(quote.get("bid_price", 0) or 0)
        ask = float(quote.get("ask_price", 0) or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0
        spread = (ask - bid) / mid if mid > 0 else inf
        sign = 1 if leg["side"] == "BUY" else -1
        quantity = int(leg["quantity"])
        rows.append({
            **leg.to_dict(), "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread,
            "executable_open_price": ask if sign > 0 else bid,
            "mid_net_contribution": sign * quantity * mid,
            "executable_net_contribution": sign * quantity * (ask if sign > 0 else bid),
            "delta_exposure": sign * quantity * 100 * float(quote.get("option_delta", 0) or 0),
            "gamma_exposure": sign * quantity * 100 * float(quote.get("option_gamma", 0) or 0),
            "theta_exposure": sign * quantity * 100 * float(quote.get("option_theta", 0) or 0),
            "vega_exposure": sign * quantity * 100 * float(quote.get("option_vega", 0) or 0),
            "volume": float(quote.get("option_volume", quote.get("volume", 0)) or 0),
            "open_interest": float(quote.get("option_open_interest", 0) or 0),
            "quote_age_seconds": quote_age,
        })
    detail = pd.DataFrame(rows)
    mid_net = float(detail["mid_net_contribution"].sum())
    executable = float(detail["executable_net_contribution"].sum())
    return detail, {
        "mid_net_debit": mid_net,
        "executable_net_debit": executable,
        "package_spread": max(0.0, executable - mid_net),
        "net_delta": float(detail["delta_exposure"].sum()),
        "net_gamma": float(detail["gamma_exposure"].sum()),
        "net_theta": float(detail["theta_exposure"].sum()),
        "net_vega": float(detail["vega_exposure"].sum()),
    }


def capacity_assessment(
    detail: pd.DataFrame, requested_contracts: int = 1, max_spread_pct: float = 0.15,
    volume_participation: float = 0.05, oi_participation: float = 0.02,
) -> dict:
    if detail.empty:
        return {"allowed": False, "capacity": 0, "reason": "没有组合行情"}
    if (detail["mid"] <= 0).any():
        return {"allowed": False, "capacity": 0, "reason": "至少一条腿没有有效双边报价"}
    worst_spread = float(detail["spread_pct"].max())
    volume_capacity = min(
        int(float(row["volume"]) * volume_participation / max(1, int(row["quantity"])))
        for _, row in detail.iterrows()
    )
    oi_capacity = min(
        int(float(row["open_interest"]) * oi_participation / max(1, int(row["quantity"])))
        for _, row in detail.iterrows()
    )
    capacity = max(0, min(volume_capacity, oi_capacity))
    reasons = []
    if worst_spread > max_spread_pct:
        reasons.append(f"最差腿价差 {worst_spread:.1%} 超过 {max_spread_pct:.1%}")
    if requested_contracts > capacity:
        reasons.append(f"请求 {requested_contracts} 组超过估算容量 {capacity} 组")
    return {
        "allowed": not reasons, "capacity": capacity, "worst_spread_pct": worst_spread,
        "volume_capacity": volume_capacity, "oi_capacity": oi_capacity,
        "reason": "；".join(reasons) if reasons else "流动性与容量检查通过",
    }


def expiry_payoff(legs: list[dict], spot_min: float, spot_max: float, points: int = 101) -> pd.DataFrame:
    prices = pd.Series([spot_min + (spot_max - spot_min) * i / (points - 1) for i in range(points)])
    payoff = pd.Series(0.0, index=prices.index)
    entry_cashflow = 0.0
    for leg in legs:
        contract = parse_option_code(str(leg["symbol"]))
        if contract is None:
            continue
        sign = 1 if leg["side"] == "BUY" else -1
        qty = int(leg["quantity"])
        premium = float(leg.get("average_fill_price") or leg.get("limit_price", 0))
        intrinsic = (
            (prices - contract.strike).clip(lower=0)
            if contract.option_type == "CALL" else (contract.strike - prices).clip(lower=0)
        )
        payoff += sign * qty * intrinsic * 100
        entry_cashflow -= sign * qty * premium * 100
    payoff += entry_cashflow
    return pd.DataFrame({"正股到期价格": prices, "组合到期盈亏": payoff})


def build_template(chain: pd.DataFrame, structure: str, underlying: str, spot: float) -> list[dict]:
    required = {"code", "option_type", "strike_price"}
    if chain.empty or not required.issubset(chain.columns):
        raise ValueError("期权链缺少代码、类型或行权价")
    frame = chain.copy()
    frame["strike_price"] = pd.to_numeric(frame["strike_price"], errors="coerce")
    frame = frame.dropna(subset=["strike_price"]).sort_values("strike_price")

    def rows(kind: str) -> pd.DataFrame:
        selected = frame[frame["option_type"].astype(str).str.upper().str.contains(kind)].copy()
        selected["distance"] = (selected["strike_price"] - spot).abs()
        return selected.sort_values("strike_price")

    calls, puts = rows("CALL"), rows("PUT")
    if len(calls) < 3 or len(puts) < 3:
        raise ValueError("当前到期日至少需要 3 个 Call 和 3 个 Put 行权价")
    call_atm = int((calls["strike_price"] - spot).abs().argmin())
    put_atm = int((puts["strike_price"] - spot).abs().argmin())

    def leg(row, side: str, quantity: int = 1, role: str = "PROTECTIVE") -> dict:
        bid = float(row.get("bid_price", 0) or 0)
        ask = float(row.get("ask_price", 0) or 0)
        return {"role": role, "symbol": str(row["code"]), "side": side, "quantity": quantity,
                "limit_price": ask if side == "BUY" else bid}

    if structure == "VERTICAL_CALL":
        base = min(call_atm, len(calls) - 2)
        return [leg(calls.iloc[base + 1], "BUY"), leg(calls.iloc[base], "SELL", role="RISK")]
    if structure == "VERTICAL_PUT":
        base = max(put_atm, 1)
        return [leg(puts.iloc[base - 1], "BUY"), leg(puts.iloc[base], "SELL", role="RISK")]
    if structure == "BUTTERFLY":
        center = min(max(call_atm, 1), len(calls) - 2)
        return [leg(calls.iloc[center - 1], "BUY"), leg(calls.iloc[center], "SELL", 2, "RISK"),
                leg(calls.iloc[center + 1], "BUY")]
    if structure == "IRON_CONDOR":
        pc = min(max(put_atm, 1), len(puts) - 1)
        cc = min(max(call_atm, 0), len(calls) - 2)
        return [leg(puts.iloc[pc - 1], "BUY"), leg(puts.iloc[pc], "SELL", role="RISK"),
                leg(calls.iloc[cc], "SELL", role="RISK"), leg(calls.iloc[cc + 1], "BUY")]
    if structure == "STRADDLE":
        return [leg(calls.iloc[call_atm], "BUY"), leg(puts.iloc[put_atm], "BUY")]
    if structure == "STRANGLE":
        return [leg(calls.iloc[min(call_atm + 1, len(calls) - 1)], "BUY"),
                leg(puts.iloc[max(put_atm - 1, 0)], "BUY")]
    if structure == "CALENDAR":
        dated = calls.copy()
        dated["expiration"] = dated["code"].map(
            lambda value: parse_option_code(str(value)).expiration if parse_option_code(str(value)) else None
        )
        expiries = sorted(value for value in dated["expiration"].dropna().unique())
        if len(expiries) < 2:
            raise ValueError("日历价差需要近月和远月两条期权链")
        strike = float(dated.iloc[(dated["strike_price"] - spot).abs().argmin()]["strike_price"])
        near = dated[(dated["expiration"] == expiries[0]) & (dated["strike_price"] == strike)]
        far = dated[(dated["expiration"] == expiries[-1]) & (dated["strike_price"] == strike)]
        if near.empty or far.empty:
            raise ValueError("近月与远月没有共同的近平值行权价")
        return [leg(far.iloc[0], "BUY"), leg(near.iloc[0], "SELL", role="RISK")]
    raise ValueError("暂不支持该结构模板")


def rank_vertical_candidates(chain: pd.DataFrame, spot: float, top_n: int = 30) -> pd.DataFrame:
    candidates = []
    for kind in ("CALL", "PUT"):
        frame = chain[chain["option_type"].astype(str).str.upper().str.contains(kind)].copy()
        frame["strike_price"] = pd.to_numeric(frame["strike_price"], errors="coerce")
        frame = frame.dropna(subset=["strike_price"]).sort_values("strike_price")
        for index in range(len(frame) - 1):
            low, high = frame.iloc[index], frame.iloc[index + 1]
            short, protective = (low, high) if kind == "CALL" else (high, low)
            credit = float(short.get("bid_price", 0) or 0) - float(protective.get("ask_price", 0) or 0)
            width = float(high["strike_price"] - low["strike_price"])
            max_loss = max(0.01, width - credit)
            spreads = []
            for row in (short, protective):
                bid, ask = float(row.get("bid_price", 0) or 0), float(row.get("ask_price", 0) or 0)
                mid = (bid + ask) / 2
                spreads.append((ask - bid) / mid if mid > 0 else 9)
            oi = min(float(short.get("option_open_interest", 0) or 0), float(protective.get("option_open_interest", 0) or 0))
            distance = abs(float(short["strike_price"]) - spot) / max(spot, 0.01)
            score = credit / max_loss * 40 + min(distance, 0.2) * 100 + min(oi / 1000, 1) * 20 - max(spreads) * 100
            candidates.append({
                "结构": f"{kind} 信用价差", "保护腿": protective["code"], "短腿": short["code"],
                "净贷记": credit, "价差宽度": width, "最大亏损": max_loss,
                "风险收益比": credit / max_loss, "最差腿价差": max(spreads),
                "最小未平仓量": oi, "综合评分": score,
            })
    return pd.DataFrame(candidates).sort_values("综合评分", ascending=False).head(top_n).reset_index(drop=True)


def run_combo_backtest(
    histories: dict[str, pd.DataFrame], legs: list[dict], initial_cash: float = 100_000,
    fee_per_contract: float = 0.65, take_profit: float | None = None,
    stop_loss: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    merged = None
    for index, leg in enumerate(legs):
        frame = histories.get(leg["symbol"], pd.DataFrame()).copy()
        if frame.empty or not {"time_key", "bid", "ask"}.issubset(frame.columns):
            raise ValueError(f"{leg['symbol']} 没有可用的历史 Bid/Ask")
        frame["time_key"] = pd.to_datetime(frame["time_key"], errors="coerce", utc=True)
        frame = frame.dropna(subset=["time_key"]).sort_values("time_key")
        frame = frame[frame.get("quality_status", "GOOD") == "GOOD"] if "quality_status" in frame else frame
        columns = ["time_key", "bid", "ask"] + (["underlying_price"] if "underlying_price" in frame else [])
        renamed = {"bid": f"bid_{index}", "ask": f"ask_{index}"}
        if "underlying_price" in frame:
            renamed["underlying_price"] = f"spot_{index}"
        frame = frame[columns].rename(columns=renamed)
        merged = frame if merged is None else pd.merge_asof(
            merged.sort_values("time_key"), frame.sort_values("time_key"), on="time_key",
            direction="nearest", tolerance=pd.Timedelta("90s"),
        )
    merged = merged.dropna()
    if len(merged) < 5:
        raise ValueError(f"组合腿对齐后只有 {len(merged)} 条样本，至少需要 5 条")
    entry_net = 0.0
    fees = 0.0
    for index, leg in enumerate(legs):
        sign = 1 if leg["side"] == "BUY" else -1
        qty = int(leg["quantity"])
        entry = float(merged.iloc[0][f"ask_{index}" if sign > 0 else f"bid_{index}"])
        entry_net += sign * qty * entry
        fees += qty * fee_per_contract
    cash = initial_cash - entry_net * 100 - fees
    rows = []
    exit_reason = "历史结束"
    entry_risk = max(abs(entry_net) * 100, 1.0)
    for position, row in merged.iterrows():
        liquidation = 0.0
        for index, leg in enumerate(legs):
            sign = 1 if leg["side"] == "BUY" else -1
            qty = int(leg["quantity"])
            contract = parse_option_code(leg["symbol"])
            expired = contract is not None and row["time_key"].date() >= contract.expiration
            spot = float(row.get(f"spot_{index}", 0) or 0)
            if expired and spot > 0:
                mark = max(0.0, spot - contract.strike) if contract.option_type == "CALL" else max(0.0, contract.strike - spot)
            else:
                mark = float(row[f"bid_{index}" if sign > 0 else f"ask_{index}"])
            liquidation += sign * qty * mark
        pnl = (liquidation - entry_net) * 100 - fees
        rows.append({"时间": row["time_key"], "账户权益": cash + liquidation * 100, "组合平仓净价": liquidation, "组合盈亏": pnl})
        if take_profit is not None and pnl >= entry_risk * take_profit:
            exit_reason = "达到组合止盈"
            break
        if stop_loss is not None and pnl <= -entry_risk * stop_loss:
            exit_reason = "达到组合止损"
            break
    equity = pd.DataFrame(rows)
    final_liquidation = float(equity.iloc[-1]["组合平仓净价"])
    exit_fees = sum(int(leg["quantity"]) for leg in legs) * fee_per_contract
    final_equity = cash + final_liquidation * 100 - exit_fees
    curve = equity["账户权益"]
    drawdown = curve / curve.cummax() - 1
    metrics = {
        "初始资金": initial_cash, "最终权益": final_equity,
        "总收益率": final_equity / initial_cash - 1, "最大回撤": float(drawdown.min()),
        "样本数": len(equity), "开仓净价": entry_net, "平仓净价": final_liquidation,
        "手续费": fees + exit_fees, "退出原因": exit_reason,
    }
    trades = pd.DataFrame([{"时间": merged.iloc[0]["time_key"], "动作": "组合开仓", "净价": entry_net},
                           {"时间": equity.iloc[-1]["时间"], "动作": "组合平仓", "净价": final_liquidation, "原因": exit_reason}])
    return equity, trades, metrics


def combo_execution_quality(store) -> pd.DataFrame:
    rows = []
    for combo in store.option_combos(limit=1000):
        legs = store.option_combo_legs(combo["combo_id"])
        planned = sum((1 if leg["side"] == "BUY" else -1) * leg["quantity"] * leg["limit_price"] for leg in legs)
        actual = sum((1 if leg["side"] == "BUY" else -1) * leg["filled_quantity"] * leg["average_fill_price"] for leg in legs)
        started = datetime.fromisoformat(combo["created_at"])
        updated = datetime.fromisoformat(combo["updated_at"])
        rows.append({
            "组合编号": combo["combo_id"], "名称": combo["name"], "状态": combo["status"],
            "计划净借记": planned, "实际成交净借记": actual,
            "组合不利滑点": actual - planned, "生命周期秒数": (updated - started).total_seconds(),
            "平仓重试次数": sum(int(leg.get("close_attempts", 0)) for leg in legs),
            "成交腿数": sum(leg["filled_quantity"] > 0 for leg in legs), "总腿数": len(legs),
        })
    return pd.DataFrame(rows)


def roll_symbol(symbol: str, new_expiration: date) -> str:
    contract = parse_option_code(symbol)
    if contract is None:
        raise ValueError(f"无法识别合约 {symbol}")
    kind = "C" if contract.option_type == "CALL" else "P"
    strike = int(round(contract.strike * 1000))
    return f"{contract.underlying}{new_expiration:%y%m%d}{kind}{strike}"
