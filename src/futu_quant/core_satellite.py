from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from futu_quant.config import normalize_us_symbol


STRATEGY_NAME = "Core Satellite"
CORE_MAX_WEIGHTS = {
    "US.SPY": 0.25, "US.VOO": 0.25, "US.IVV": 0.25,
    "US.QQQ": 0.25, "US.SCHD": 0.20,
}
PROFILES = {
    "conservative": {
        "label": "保守", "max_stock_weight": 0.70, "base_factor": 0.60,
        "core_budget": 0.55, "satellite_budget": 0.15,
        "core_weights": {
            "US.SPY": 0.30, "US.VOO": 0.25, "US.IVV": 0.20,
            "US.QQQ": 0.15, "US.SCHD": 0.10,
        },
        "satellite_weight": 0.05,
    },
    "balanced": {
        "label": "均衡", "max_stock_weight": 0.80, "base_factor": 0.80,
        "core_budget": 0.65, "satellite_budget": 0.15,
        "core_weights": {
            "US.SPY": 0.30, "US.VOO": 0.25, "US.IVV": 0.20,
            "US.QQQ": 0.15, "US.SCHD": 0.10,
        },
        "satellite_weight": 0.10,
    },
    "aggressive": {
        "label": "进取", "max_stock_weight": 0.90, "base_factor": 0.90,
        "core_budget": 0.70, "satellite_budget": 0.20,
        "core_weights": {
            "US.SPY": 0.28, "US.VOO": 0.22, "US.IVV": 0.18,
            "US.QQQ": 0.22, "US.SCHD": 0.10,
        },
        "satellite_weight": 0.15,
    },
}


@dataclass(frozen=True)
class RebalanceOrder:
    symbol: str
    side: str
    quantity: int
    price: float
    current_weight: float
    target_weight: float
    reason: str
    desired_quantity: int = 0
    desired_notional: float = 0.0
    order_notional: float = 0.0
    execution_slice: int = 1
    total_slices: int = 1
    throttle_reason: str = ""


def rebalance_due(frame: pd.DataFrame, frequency: str) -> tuple[bool, str]:
    """Decide whether live planning is due; risk-state changes bypass the calendar."""
    if frequency == "daily":
        return True, "daily schedule"
    close = frame["close"].astype(float)
    if len(close) < 201:
        raise ValueError("Core Satellite schedule requires at least 201 daily bars")
    current_risk = close.iloc[-1] >= close.iloc[-200:].mean()
    previous_risk = close.iloc[-2] >= close.iloc[-201:-1].mean()
    if current_risk != previous_risk:
        return True, "SPY risk state changed"
    day = pd.Timestamp(frame["time_key"].iloc[-1])
    if frequency == "weekly":
        return day.weekday() == 4, "weekly Friday schedule"
    if frequency == "monthly":
        return (day + pd.Timedelta(days=7)).month != day.month, "month-end week schedule"
    raise ValueError(f"Unknown rebalance frequency: {frequency}")


def indicators(frame: pd.DataFrame) -> dict[str, float]:
    close = frame["close"].astype(float)
    if len(close) < 200:
        raise ValueError("Core Satellite requires at least 200 daily bars")
    price = float(close.iloc[-1])
    sma20 = float(close.iloc[-20:].mean())
    sma200 = float(close.iloc[-200:].mean())
    high60 = float(close.iloc[-60:].max())
    returns = close.pct_change().dropna()
    realized_vol20 = float(returns.iloc[-20:].std()) if len(returns) >= 20 else 0.0
    momentum20 = price / float(close.iloc[-20]) - 1 if len(close) >= 20 and close.iloc[-20] else 0.0
    momentum60 = price / float(close.iloc[-60]) - 1 if len(close) >= 60 and close.iloc[-60] else 0.0
    return {
        "price": price,
        "sma20": sma20,
        "sma200": sma200,
        "drawdown60": price / high60 - 1,
        "extension200": price / sma200 - 1,
        "realized_vol20": realized_vol20,
        "momentum20": momentum20,
        "momentum60": momentum60,
    }


def relative_strength(
    stats: dict[str, float], benchmark_stats: dict[str, float] | None = None,
) -> tuple[float, float]:
    relative20 = stats.get("momentum20", 0.0)
    relative60 = stats.get("momentum60", 0.0)
    if benchmark_stats:
        relative20 -= benchmark_stats.get("momentum20", 0.0)
        relative60 -= benchmark_stats.get("momentum60", 0.0)
    return relative20, relative60


def satellite_quality_score(
    stats: dict[str, float], benchmark_stats: dict[str, float] | None = None,
) -> tuple[float, list[str]]:
    above_short = stats["price"] >= stats["sma20"]
    relative20, relative60 = relative_strength(stats, benchmark_stats)
    score = 0.0
    reasons: list[str] = []
    if above_short:
        score += 1.0
        reasons.append("站上20日线")
    if stats.get("momentum20", 0.0) > 0:
        score += 1.0
        reasons.append("20日动量为正")
    if stats.get("momentum60", 0.0) > 0:
        score += 1.0
        reasons.append("60日动量为正")
    if relative20 > 0:
        score += 1.25
        reasons.append("20日相对SPY强势")
    if relative60 > 0:
        score += 1.25
        reasons.append("60日相对SPY强势")
    if stats["drawdown60"] > -0.08:
        score += 0.75
        reasons.append("回撤可控")
    if stats["extension200"] >= 0.45:
        reasons.append("偏离200日线过高")
    elif stats["extension200"] >= 0.30:
        reasons.append("偏离200日线较高")
    elif stats["drawdown60"] <= -0.08:
        reasons.append("60日回撤超过8%")
    return score, reasons


def core_quality_score(stats: dict[str, float], risk_on: bool) -> tuple[float, str]:
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    if not risk_on:
        if above_long and above_short:
            return 0.35, "大盘防御期，仅保留趋势仍健康的 Core"
        return 0.20, "大盘防御期，Core 降到防守底仓"
    score = 1.0
    reasons = []
    if above_long:
        score += 1.0
        reasons.append("站上200日线")
    if above_short:
        score += 0.5
        reasons.append("站上20日线")
    if stats["momentum60"] > 0:
        score += 0.5
        reasons.append("60日动量为正")
    if stats["drawdown60"] <= -0.16:
        score += 0.4
        reasons.append("回撤提供分批机会")
    if stats["extension200"] >= 0.40:
        score *= 0.55
        reasons.append("过度偏离200日线")
    elif stats["extension200"] >= 0.25:
        score *= 0.75
        reasons.append("偏离200日线较高")
    return max(0.1, score), "Core 质量评分：" + "、".join(reasons[:4])


def allocation_factor(
    stats: dict[str, float], kind: str, risk_on: bool, base_factor: float = 0.60,
    benchmark_stats: dict[str, float] | None = None,
    satellite_min_score: float = 2.5,
    currently_held: bool = False,
) -> tuple[float, str]:
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    if kind == "satellite":
        if not risk_on:
            return 0.0, "大盘低于200日线，Satellite 暂停开仓"
        if not above_long:
            return 0.0, "个股跌破200日线，Satellite 退出/不新开"
        if not above_short and stats["drawdown60"] <= -0.12:
            return 0.0, "个股跌破20日线且60日回撤超过12%，Satellite 退出"

        relative20, relative60 = relative_strength(stats, benchmark_stats)
        quality_score, reasons = satellite_quality_score(stats, benchmark_stats)

        if relative20 <= 0 and relative60 <= 0:
            if currently_held and above_long:
                return 0.35, f"在仓底仓防洗盘维稳（评分 {quality_score:.1f}，均线上方暂维持观察）"
            return 0.0, f"Satellite 评分 {quality_score:.1f}/6.5，但相对 SPY 强度不足，等待更强入场信号"
        if quality_score <= satellite_min_score:
            if currently_held and above_long:
                return 0.40, f"在仓底仓防震荡保护（评分 {quality_score:.1f}，200日均线上方维持现有底仓，拒绝追涨杀跌）"
            return 0.0, (
                f"Satellite 评分 {quality_score:.1f}/6.5 低于每日机会门槛 "
                f"{satellite_min_score:.1f}，等待更强入场信号"
            )
        if quality_score < 4.0:
            factor = 0.35
        elif quality_score < 5.0:
            factor = 0.60
        else:
            factor = min(1.0, base_factor + 0.20)

        if stats["extension200"] >= 0.45:
            factor = min(factor, 0.35)
            reasons.append("偏离200日线过高，主动降权")
        elif stats["extension200"] >= 0.30:
            factor = min(factor, 0.60)
            reasons.append("偏离200日线较高，限制追高")
        elif stats["drawdown60"] <= -0.08:
            factor = min(factor, 0.50)
            reasons.append("60日回撤超过8%，只保留半仓表达")
        return factor, f"Satellite 评分 {quality_score:.1f}/6.5：" + "、".join(reasons[:4])

    if not risk_on:
        factor = 0.20 if kind == "core" else (0.20 if above_long and above_short else 0.0)
        return factor, "SPY below SMA200; defensive allocation"
    if not above_long:
        factor = 0.25 if above_short else (0.20 if kind == "core" else 0.0)
        return factor, "below SMA200; waiting for right-side confirmation"

    factor, reason = base_factor, "above SMA200; base allocation"
    drawdown = stats["drawdown60"]
    if drawdown <= -0.25:
        factor, reason = 1.00, "60-day drawdown >= 25%; fourth tranche"
    elif drawdown <= -0.16:
        factor, reason = 0.90, "60-day drawdown >= 16%; third tranche"
    elif drawdown <= -0.08:
        factor, reason = 0.75, "60-day drawdown >= 8%; second tranche"

    extension = stats["extension200"]
    if extension >= 0.40:
        factor, reason = min(factor, 0.55), "price >= 40% above SMA200; trim"
    elif extension >= 0.25:
        factor, reason = min(factor, 0.75), "price >= 25% above SMA200; trim"
    return factor, reason


def max_weights(
    symbols: tuple[str, ...], max_stock_weight: float, profile: str = "conservative",
    core_budget: float | None = None, satellite_budget: float | None = None,
) -> dict[str, float]:
    parameters = PROFILES[profile]
    core = [symbol for symbol in symbols if symbol in CORE_MAX_WEIGHTS]
    satellites = [symbol for symbol in symbols if symbol not in CORE_MAX_WEIGHTS]
    core_budget = float(parameters["core_budget"]) if core_budget is None else float(core_budget)
    satellite_budget = (
        float(parameters["satellite_budget"])
        if satellite_budget is None else float(satellite_budget)
    )
    core_cap = min(max_stock_weight, max(0.0, core_budget))
    satellite_cap = max(0.0, min(
        max_stock_weight - core_cap, max(0.0, satellite_budget),
    ))
    weights = {symbol: parameters["core_weights"][symbol] for symbol in core}
    core_total = sum(weights.values())
    if core_total > core_cap and core_total > 0:
        scale = core_cap / core_total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    remaining = max(0.0, min(satellite_cap, max_stock_weight - sum(weights.values())))
    satellite_weight = min(parameters["satellite_weight"], remaining / len(satellites)) if satellites else 0.0
    weights.update({symbol: satellite_weight for symbol in satellites})
    return weights


def ranked_target_weights(
    symbols: tuple[str, ...],
    stats_by_symbol: dict[str, dict[str, float]],
    spy_stats: dict[str, float],
    risk_on: bool,
    max_stock_weight: float,
    profile: str,
    core_budget: float | None,
    satellite_budget: float | None,
    satellite_min_score: float = 2.5,
    held_symbols: set[str] | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    parameters = PROFILES[profile]
    configured_core_budget = (
        float(parameters["core_budget"]) if core_budget is None else float(core_budget)
    )
    configured_satellite_budget = (
        float(parameters["satellite_budget"])
        if satellite_budget is None else float(satellite_budget)
    )
    core_cap = min(max_stock_weight, max(0.0, configured_core_budget))
    satellite_cap = max(0.0, min(
        max_stock_weight - core_cap, max(0.0, configured_satellite_budget),
    ))
    core_symbols = [symbol for symbol in symbols if symbol in CORE_MAX_WEIGHTS]
    satellite_symbols = [symbol for symbol in symbols if symbol not in CORE_MAX_WEIGHTS]
    base_core = {
        symbol: float(parameters["core_weights"].get(symbol, CORE_MAX_WEIGHTS.get(symbol, 0.0)))
        for symbol in core_symbols
    }
    core_total = sum(base_core.values())
    if core_total > 0:
        base_core = {symbol: weight / core_total for symbol, weight in base_core.items()}

    weights: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    reasons: dict[str, str] = {}
    core_scores: dict[str, float] = {}
    for symbol in core_symbols:
        score, score_reason = core_quality_score(stats_by_symbol[symbol], risk_on)
        core_scores[symbol] = max(0.0, base_core.get(symbol, 0.0) * score)
        reasons[symbol] = score_reason
    core_score_total = sum(core_scores.values())
    if core_score_total > 0:
        for symbol, score in core_scores.items():
            weights[symbol] = min(CORE_MAX_WEIGHTS[symbol], core_cap * score / core_score_total)

    satellite_scores: dict[str, float] = {}
    for symbol in satellite_symbols:
        is_held = bool(held_symbols and symbol in held_symbols)
        factor, factor_reason = allocation_factor(
            stats_by_symbol[symbol], "satellite", risk_on,
            parameters["base_factor"], spy_stats, satellite_min_score,
            currently_held=is_held,
        )
        if factor <= 0:
            reasons[symbol] = factor_reason
            continue
        quality_score, _ = satellite_quality_score(stats_by_symbol[symbol], spy_stats)
        satellite_scores[symbol] = max(0.0, quality_score * factor)
        reasons[symbol] = factor_reason
    satellite_score_total = sum(satellite_scores.values())
    per_satellite_cap = float(parameters["satellite_weight"])
    if satellite_score_total > 0:
        remaining = satellite_cap
        for symbol, score in sorted(
            satellite_scores.items(), key=lambda item: item[1], reverse=True
        ):
            if remaining <= 0:
                break
            target = min(per_satellite_cap, satellite_cap * score / satellite_score_total, remaining)
            weights[symbol] = target
            remaining -= target
    return weights, reasons


def trade_participation(
    spy_stats: dict[str, float],
    max_single_rebalance_pct: float = 1.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
) -> tuple[float, str]:
    base = min(1.0, max(0.0, float(max_single_rebalance_pct)))
    realized_vol = float(spy_stats.get("realized_vol20", 0.0))
    if realized_vol >= high_volatility_threshold:
        factor = min(base, max(0.0, float(high_volatility_trade_factor)))
        return factor, (
            f"市场20日波动率 {realized_vol:.2%} 高于阈值 "
            f"{high_volatility_threshold:.2%}，单次调仓降速到 {factor:.0%}"
        )
    return base, f"单次调仓比例 {base:.0%}"


def build_rebalance_plan(
    frames: dict[str, pd.DataFrame],
    positions: pd.DataFrame,
    account_value: float,
    symbols: tuple[str, ...],
    max_stock_weight: float = 0.70,
    min_trade_usd: float = 500.0,
    profile: str = "conservative",
    weight_tolerance: float = 0.0,
    cash_buffer: float = 0.0,
    max_single_rebalance_pct: float = 1.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
    max_order_notional: float = 0.0,
    core_budget: float | None = None,
    satellite_budget: float | None = None,
    satellite_min_score: float = 2.5,
    satellite_only: bool = False,
    max_target_change: float = 0.05,
    cash_yield_enabled: bool = False,
    cash_yield_symbol: str = "US.SGOV",
) -> tuple[list[RebalanceOrder], bool]:
    if account_value <= 0:
        raise ValueError("account value must be positive")
    spy = indicators(frames["US.SPY"])
    risk_on = spy["price"] >= spy["sma200"]
    parameters = PROFILES[profile]
    max_stock_weight = min(max_stock_weight, parameters["max_stock_weight"])
    cash_buffer = min(0.50, max(0.0, float(cash_buffer)))
    max_stock_weight = max(0.0, max_stock_weight - cash_buffer)
    participation, throttle_reason = trade_participation(
        spy, max_single_rebalance_pct, high_volatility_threshold,
        high_volatility_trade_factor,
    )
    position_map = {}
    total_market_value = 0.0
    if not positions.empty:
        for _, row in positions.iterrows():
            code = str(row["code"])
            market_value = float(row.get("market_val", row.get("val", 0.0)) or 0.0)
            # 现金理财标的（如 US.SGOV）属闲置现金类资产，不占用股票敞口总市值；两侧经 normalize_us_symbol 严密匹配
            is_cash_yield = (
                cash_yield_enabled
                and normalize_us_symbol(code) == normalize_us_symbol(cash_yield_symbol)
            )
            if not is_cash_yield:
                total_market_value += max(0.0, market_value)
            position_map[code] = {
                "qty": float(row.get("qty", 0.0)),
                "can_sell_qty": float(row.get("can_sell_qty", row.get("qty", 0.0))),
            }

    stats_by_symbol = {symbol: indicators(frames[symbol]) for symbol in symbols}
    held_symbols = {k for k, v in position_map.items() if v.get("qty", 0) > 0}
    target_weights, allocation_reasons = ranked_target_weights(
        symbols, stats_by_symbol, spy, risk_on, max_stock_weight, profile,
        core_budget, satellite_budget, satellite_min_score,
        held_symbols=held_symbols,
    )
    # Avoid flipping a symbol from zero to a full target in one rebalance.
    # This is a portfolio-level hysteresis guard; the existing participation
    # throttle still controls the order slice sent to the broker.
    max_target_change = min(0.20, max(0.01, float(max_target_change)))
    candidates = []
    for symbol in symbols:
        if satellite_only and symbol in CORE_MAX_WEIGHTS:
            continue
        stats = stats_by_symbol[symbol]
        target_weight = target_weights.get(symbol, 0.0)
        reason = allocation_reasons.get(symbol, "未通过择时/择标的过滤")
        held = position_map.get(symbol, {"qty": 0.0, "can_sell_qty": 0.0})
        current_value = held["qty"] * stats["price"]
        current_weight = current_value / account_value

        # 用户长线底仓物理保护铁律：NVDA 永远保留至少 5 股长线核心仓位，仅超出部分允许参与平衡；GLD 纳入卫星组合由策略自动买卖
        if symbol == "US.NVDA":
            min_nvda_qty = 5.0
            min_nvda_weight = (min_nvda_qty * stats["price"]) / account_value
            if target_weight < min_nvda_weight:
                target_weight = max(target_weight, min_nvda_weight)
                reason = f"存量长线底仓保底保护：NVDA 严格保留至少 {min_nvda_qty:.0f} 股核心仓位"

        # 核心防洗盘保护：如果当前只是每日机会扫描 (satellite_only)，
        # 除非标的发生实质性破位（跌破200日均线或深度回撤），否则非正式调仓日只抓机会、严禁小幅减仓倒手！
        if satellite_only and target_weight < current_weight:
            is_breakdown = (stats["price"] < stats["sma200"]) or (
                stats["price"] < stats["sma20"] and stats["drawdown60"] <= -0.12
            )
            if not is_breakdown:
                target_weight = current_weight
                reason = "Satellite 每日扫描：趋势正常维持底仓，非正式调仓日不执行减仓磨损"

        original_target_weight = target_weight
        target_weight = min(
            max(current_weight - max_target_change, target_weight),
            current_weight + max_target_change,
        )
        if abs(target_weight - original_target_weight) > 1e-9:
            reason = f"组合目标权重变化受限（单次最多{max_target_change:.1%}）；{reason}"
        elif target_weight < current_weight:
            reason = f"组合再平衡减仓；{reason}"

        if symbol == "US.NVDA" and target_weight < min_nvda_weight:
            target_weight = min_nvda_weight

        delta_value = account_value * target_weight - current_value
        candidates.append((symbol, stats, held, current_weight, target_weight, delta_value, reason))

    # 资金安全底线（Fail-closed，宁可少买不可超买）：
    # 排除 SGOV 后，权重敞口预算仅反映目标股票比例上限；买入总预算必须受限于实际可用现金与流动性。
    # 若 SGOV 可卖数量不足，绝不能把不可卖的 SGOV 视作购买力，避免券商拒单或动用保证金。
    stock_exposure_budget = max(0.0, account_value * max_stock_weight - total_market_value)

    # 基础现金估算：总资产减去股票持仓市值及理财持仓市值
    sgov_mv = 0.0
    sgov_sellable_value = 0.0
    if cash_yield_enabled:
        norm_yield = normalize_us_symbol(cash_yield_symbol)
        for code, h in position_map.items():
            if normalize_us_symbol(code) == norm_yield:
                yield_p = frames[cash_yield_symbol]["close"].iloc[-1] if cash_yield_symbol in frames else 100.0
                sgov_mv = h["qty"] * yield_p
                sgov_sellable_value = h["can_sell_qty"] * yield_p
                break

    estimated_cash = max(0.0, account_value - total_market_value - sgov_mv)
    # 股票调仓所能借助的最大流动性 = 当前现金 + 实际可变现的 SGOV（扣除必须预留的 cash_buffer）
    target_cash_buffer = account_value * cash_buffer
    max_available_liquidity = max(0.0, estimated_cash + sgov_sellable_value - target_cash_buffer)

    buy_budget = min(stock_exposure_budget, max_available_liquidity)
    requested_buys = sum(max(0.0, item[5]) for item in candidates)
    buy_scale = min(1.0, buy_budget / requested_buys) if requested_buys and buy_budget > 0 else 0.0

    orders = []
    for symbol, stats, held, current_weight, target_weight, delta_value, reason in candidates:
        if abs(target_weight - current_weight) < weight_tolerance:
            continue
        if delta_value > 0:
            delta_value *= buy_scale
        if abs(delta_value) < min_trade_usd:
            continue
        desired_quantity = int(abs(delta_value) // stats["price"])
        if desired_quantity < 1:
            continue
        quantity = max(1, int(desired_quantity * participation))
        side = "BUY" if delta_value > 0 else "SELL"
        if side == "SELL":
            quantity = min(quantity, int(held["can_sell_qty"]))
            if symbol == "US.NVDA":
                max_sell_nvda = max(0, int(held["qty"] - 5.0))
                quantity = min(quantity, max_sell_nvda)
        desired_notional = desired_quantity * stats["price"]
        if max_order_notional > 0 and quantity * stats["price"] > max_order_notional:
            quantity = max(1, int(max_order_notional // stats["price"]))
        if quantity < 1:
            continue
        total_slices = max(1, int((desired_notional + max_order_notional - 0.01) // max_order_notional)) if max_order_notional > 0 else 1
        orders.append(
            RebalanceOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=stats["price"],
                current_weight=current_weight,
                target_weight=target_weight,
                reason=reason,
                desired_quantity=desired_quantity,
                desired_notional=desired_notional,
                order_notional=quantity * stats["price"],
                execution_slice=1,
                total_slices=total_slices,
                throttle_reason=throttle_reason,
            )
        )

    # 现金理财池 (SGOV 超短国债 ETF 增益机制)：若启用且有行情，在闲置现金充裕时自动配置国债理财吃 4.8% 利息
    if cash_yield_enabled and cash_yield_symbol in frames:
        yield_stats = indicators(frames[cash_yield_symbol])
        yield_held = position_map.get(cash_yield_symbol, {"qty": 0.0, "can_sell_qty": 0.0})
        yield_mv = yield_held["qty"] * yield_stats["price"]
        # 计算所有股票指令执行后的净现金变动
        net_buy_outflow = sum(
            o.quantity * o.price for o in orders if o.side == "BUY"
        ) - sum(
            o.quantity * o.price for o in orders if o.side == "SELL"
        )
        # 预估调仓后的可用现金
        current_cash = max(0.0, account_value - total_market_value)
        post_cash = current_cash - net_buy_outflow
        target_cash_buffer = account_value * cash_buffer

        if post_cash < target_cash_buffer and yield_held["can_sell_qty"] > 0:
            # 战时抽调：现金不足以维持预留缓冲时，优先卖出 SGOV 赎回现金
            shortage = target_cash_buffer - post_cash
            redeem_qty = min(
                int(yield_held["can_sell_qty"]),
                max(1, int(shortage // yield_stats["price"] + 1)),
            )
            if redeem_qty > 0 and redeem_qty * yield_stats["price"] >= min_trade_usd:
                orders.append(
                    RebalanceOrder(
                        symbol=cash_yield_symbol,
                        side="SELL",
                        quantity=redeem_qty,
                        price=yield_stats["price"],
                        current_weight=yield_mv / account_value,
                        target_weight=max(0.0, (yield_mv - redeem_qty * yield_stats["price"]) / account_value),
                        reason="现金理财池赎回：释放国债 ETF 资金保障主力调仓与期权流动性",
                        desired_quantity=redeem_qty,
                        desired_notional=redeem_qty * yield_stats["price"],
                        order_notional=redeem_qty * yield_stats["price"],
                    )
                )
        elif post_cash > target_cash_buffer + min_trade_usd:
            # 闲时生息：现金超出缓冲额度且超过起投金额时，自动将闲置现金买入 SGOV
            surplus = post_cash - target_cash_buffer
            invest_qty = int(surplus // yield_stats["price"])
            if invest_qty > 0 and invest_qty * yield_stats["price"] >= min_trade_usd:
                orders.append(
                    RebalanceOrder(
                        symbol=cash_yield_symbol,
                        side="BUY",
                        quantity=invest_qty,
                        price=yield_stats["price"],
                        current_weight=yield_mv / account_value,
                        target_weight=(yield_mv + invest_qty * yield_stats["price"]) / account_value,
                        reason="闲置资金自动理财：超额闲置现金配置超短国债 ETF 获取稳健无风险收益",
                        desired_quantity=invest_qty,
                        desired_notional=invest_qty * yield_stats["price"],
                        order_notional=invest_qty * yield_stats["price"],
                    )
                )

    # 工业规范：再平衡订单严格保证先卖后买（SELL-first, BUY-second），
    # 释放流动性与购买力，避免实盘资金受限时买单抢先发出被券商拒单
    orders.sort(key=lambda o: 0 if o.side == "SELL" else 1)

    return orders, risk_on
