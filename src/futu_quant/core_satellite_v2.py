"""
Core Satellite Strategy V2 - 优化版本
修复追涨杀跌问题，改进买卖时机判断

主要改进：
1. 🔴 紧急：防追高机制（偏离度20%阈值、RSI过滤）
2. 🟡 中期：逆向买入逻辑（回调5-15%时买入）+ 分批建仓
3. 🟢 长期：持仓成本保护、最小持仓天数、弱化短期动量权重
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd


STRATEGY_NAME = "Core Satellite V2"
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
    position_cost: float = 0.0  # 新增：持仓成本
    entry_date: str = ""  # 新增：建仓日期


@dataclass
class PositionInfo:
    """持仓信息（用于成本追踪和持仓时间判断）"""
    symbol: str
    quantity: float
    cost_basis: float
    entry_date: str  # ISO格式日期字符串
    
    def holding_days(self, current_date: str) -> int:
        """计算持仓天数"""
        entry = datetime.fromisoformat(self.entry_date)
        current = datetime.fromisoformat(current_date)
        return (current - entry).days
    
    def unrealized_pnl_pct(self, current_price: float) -> float:
        """计算未实现盈亏百分比"""
        return (current_price - self.cost_basis) / self.cost_basis if self.cost_basis > 0 else 0.0


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


def calculate_rsi(close: pd.Series, period: int = 14) -> float:
    """计算 RSI 指标"""
    if len(close) < period + 1:
        return 50.0  # 数据不足时返回中性值
    
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    if loss.iloc[-1] == 0:
        return 100.0
    
    rs = gain.iloc[-1] / loss.iloc[-1]
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def indicators(frame: pd.DataFrame) -> dict[str, float]:
    """计算技术指标（增强版）"""
    close = frame["close"].astype(float)
    if len(close) < 200:
        raise ValueError("Core Satellite requires at least 200 daily bars")
    
    price = float(close.iloc[-1])
    sma20 = float(close.iloc[-20:].mean())
    sma50 = float(close.iloc[-50:].mean())
    sma200 = float(close.iloc[-200:].mean())
    high60 = float(close.iloc[-60:].max())
    
    returns = close.pct_change().dropna()
    realized_vol20 = float(returns.iloc[-20:].std()) if len(returns) >= 20 else 0.0
    
    momentum20 = price / float(close.iloc[-20]) - 1 if len(close) >= 20 and close.iloc[-20] else 0.0
    momentum60 = price / float(close.iloc[-60]) - 1 if len(close) >= 60 and close.iloc[-60] else 0.0
    
    # 新增：RSI 和均线斜率
    rsi = calculate_rsi(close, 14)
    sma200_slope = (sma200 - float(close.iloc[-220:-20].mean())) / float(close.iloc[-220:-20].mean()) if len(close) >= 220 else 0.0
    
    return {
        "price": price,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "drawdown60": price / high60 - 1,
        "extension200": price / sma200 - 1,
        "realized_vol20": realized_vol20,
        "momentum20": momentum20,
        "momentum60": momentum60,
        "rsi": rsi,
        "sma200_slope": sma200_slope,
    }


def relative_strength(
    stats: dict[str, float], benchmark_stats: dict[str, float] | None = None,
) -> tuple[float, float]:
    """计算相对强度（弱化短期权重）"""
    relative20 = stats.get("momentum20", 0.0)
    relative60 = stats.get("momentum60", 0.0)
    if benchmark_stats:
        relative20 -= benchmark_stats.get("momentum20", 0.0)
        relative60 -= benchmark_stats.get("momentum60", 0.0)
    return relative20, relative60


def satellite_quality_score_v2(
    stats: dict[str, float], 
    benchmark_stats: dict[str, float] | None = None,
    position_info: PositionInfo | None = None,
) -> tuple[float, list[str]]:
    """
    Satellite 质量评分 V2 - 逆向买入逻辑
    
    改进：
    1. 在回调中寻找机会（而非追高）
    2. 弱化短期动量权重
    3. 加入波动率和 RSI 过滤
    """
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    relative20, relative60 = relative_strength(stats, benchmark_stats)
    
    score = 0.0
    reasons: list[str] = []
    
    # 1. 长期趋势向上（必要条件，权重高）
    if above_long:
        score += 2.0
        reasons.append("站上200日线")
    else:
        return 0.0, ["跌破200日线，不考虑"]
    
    # 2. 🔴紧急修复：防追高 - RSI 和偏离度过滤
    rsi = stats.get("rsi", 50.0)
    extension200 = stats.get("extension200", 0.0)
    
    if rsi > 70:
        return 0.0, [f"RSI {rsi:.1f} 超买，等待回调"]
    
    if extension200 >= 0.20:  # 🔴 降低到 20%
        return 0.0, [f"偏离200日线 {extension200:.1%}，等待回调"]
    
    # 3. 🟡中期优化：回调买入逻辑（核心改进）
    drawdown60 = stats["drawdown60"]
    
    if -0.15 < drawdown60 < -0.05:  # 回调 5-15%，最佳买点
        score += 2.5
        reasons.append(f"健康回调 {-drawdown60:.1%}，买点")
    elif -0.05 <= drawdown60 < 0:  # 小幅回调 0-5%
        score += 1.5
        reasons.append(f"轻微回调 {-drawdown60:.1%}，可考虑")
    elif drawdown60 >= 0:  # 创新高
        score += 0.3  # 大幅降权：防追高
        reasons.append("创新高，谨慎观望")
    else:  # 回撤 > 15%
        score += 0.5
        reasons.append(f"回撤较深 {-drawdown60:.1%}，等待企稳")
    
    # 4. 中期相对强度（只看60日，忽略20日）
    if relative60 > 0.02:  # 中期跑赢大盘 2%+
        score += 1.5
        reasons.append("中期相对强势")
    elif relative60 > -0.02:  # 持平
        score += 0.5
        reasons.append("中期跟随大盘")
    
    # 5. 波动率合理性
    vol = stats.get("realized_vol20", 0.0)
    if vol < 0.025:  # 低波动
        score += 1.0
        reasons.append("波动率正常")
    elif vol < 0.04:  # 中等波动
        score += 0.5
        reasons.append("波动率偏高")
    else:
        reasons.append("波动率过高")
    
    # 6. 均线支撑
    if above_short:
        score += 0.5
        reasons.append("站上20日线")
    
    return score, reasons


def should_exit_satellite(
    stats: dict[str, float],
    position_info: PositionInfo,
    current_date: str,
    min_hold_days: int = 30,
) -> tuple[bool, str]:
    """
    🟢 长期优化：Satellite 退出逻辑 - 考虑持仓成本和时间
    
    改进：
    1. 盈利时才用技术止损
    2. 亏损时给予更多容忍度
    3. 最小持仓天数保护
    4. 硬止损 -15%
    """
    current_price = stats["price"]
    unrealized_pnl_pct = position_info.unrealized_pnl_pct(current_price)
    holding_days = position_info.holding_days(current_date)
    
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    drawdown60 = stats["drawdown60"]
    
    # 1. 硬止损：亏损 > 15%
    if unrealized_pnl_pct < -0.15:
        return True, f"硬止损：亏损 {unrealized_pnl_pct:.1%}"
    
    # 2. 跌破 SMA200：必须退出
    if not above_long:
        if unrealized_pnl_pct > 0:
            return True, f"跌破200日线，止盈退出（盈利 {unrealized_pnl_pct:.1%}）"
        elif unrealized_pnl_pct < -0.08:
            return True, f"跌破200日线且亏损 {unrealized_pnl_pct:.1%}，止损退出"
        # 亏损小于 8% 时，再观察
    
    # 3. 盈利保护：盈利 > 5% 时可以用技术止损
    if unrealized_pnl_pct > 0.05:
        if not above_short and drawdown60 < -0.08:
            return True, f"获利回吐，技术止盈（盈利 {unrealized_pnl_pct:.1%}）"
    
    # 4. 最小持仓天数保护：持仓不到 N 天不轻易止损
    if holding_days < min_hold_days:
        if unrealized_pnl_pct < -0.10:  # 除非大亏
            return True, f"持仓 {holding_days} 天亏损 {unrealized_pnl_pct:.1%}，止损"
        # 否则继续持有
        return False, f"持仓仅 {holding_days} 天，继续观察"
    
    # 5. 其他情况：持有
    return False, f"持有观察（盈亏 {unrealized_pnl_pct:+.1%}，持仓 {holding_days} 天）"


def core_quality_score(stats: dict[str, float], risk_on: bool) -> tuple[float, str]:
    """Core 资产质量评分（保持原逻辑）"""
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


def allocation_factor_v2(
    stats: dict[str, float], 
    kind: str, 
    risk_on: bool, 
    base_factor: float = 0.60,
    benchmark_stats: dict[str, float] | None = None,
    satellite_min_score: float = 3.5,  # 🟡 调整：从 4.0 降到 3.5
    position_info: PositionInfo | None = None,
) -> tuple[float, str]:
    """
    配置因子计算 V2
    
    改进：
    1. 调整 Satellite 评分门槛
    2. 使用新的评分逻辑
    """
    above_long = stats["price"] >= stats["sma200"]
    above_short = stats["price"] >= stats["sma20"]
    
    if kind == "satellite":
        if not risk_on:
            return 0.0, "大盘低于200日线，Satellite 暂停开仓"
        
        # 使用新的评分逻辑
        quality_score, reasons = satellite_quality_score_v2(stats, benchmark_stats, position_info)
        
        # 评分不足，不开仓
        if quality_score < satellite_min_score:
            return 0.0, (
                f"Satellite 评分 {quality_score:.1f}/7.5 低于门槛 "
                f"{satellite_min_score:.1f}，等待更优入场信号"
            )
        
        # 根据评分决定仓位
        if quality_score < 4.5:
            factor = 0.33  # 🟡 分批建仓：首批 1/3
        elif quality_score < 6.0:
            factor = 0.66  # 🟡 第二批 2/3
        else:
            factor = 1.0   # 满仓
        
        return factor, f"Satellite 评分 {quality_score:.1f}/7.5：" + "、".join(reasons[:4])
    
    # Core 逻辑保持不变
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
    """计算各标的最大权重（保持原逻辑）"""
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


def ranked_target_weights_v2(
    symbols: tuple[str, ...],
    stats_by_symbol: dict[str, dict[str, float]],
    spy_stats: dict[str, float],
    risk_on: bool,
    max_stock_weight: float,
    profile: str,
    core_budget: float | None,
    satellite_budget: float | None,
    satellite_min_score: float = 3.5,
    position_infos: dict[str, PositionInfo] | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """
    计算目标权重 V2
    
    改进：使用新的评分和配置逻辑
    """
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
    
    # Core 评分
    core_scores: dict[str, float] = {}
    for symbol in core_symbols:
        score, score_reason = core_quality_score(stats_by_symbol[symbol], risk_on)
        core_scores[symbol] = max(0.0, base_core.get(symbol, 0.0) * score)
        reasons[symbol] = score_reason
    core_score_total = sum(core_scores.values())
    if core_score_total > 0:
        for symbol, score in core_scores.items():
            weights[symbol] = min(CORE_MAX_WEIGHTS[symbol], core_cap * score / core_score_total)

    # Satellite 评分（使用新逻辑）
    satellite_scores: dict[str, float] = {}
    position_infos = position_infos or {}
    
    for symbol in satellite_symbols:
        position_info = position_infos.get(symbol)
        factor, factor_reason = allocation_factor_v2(
            stats_by_symbol[symbol], "satellite", risk_on,
            parameters["base_factor"], spy_stats, satellite_min_score,
            position_info,
        )
        if factor <= 0:
            reasons[symbol] = factor_reason
            continue
        
        quality_score, _ = satellite_quality_score_v2(
            stats_by_symbol[symbol], spy_stats, position_info
        )
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
    """交易参与度计算（保持原逻辑）"""
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
    min_trade_usd: float = 500.0,  # 🔴 降低到 $500
    profile: str = "conservative",
    weight_tolerance: float = 0.015,  # 🔴 收紧到 1.5%
    cash_buffer: float = 0.0,
    max_single_rebalance_pct: float = 1.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
    max_order_notional: float = 0.0,
    core_budget: float | None = None,
    satellite_budget: float | None = None,
    satellite_min_score: float = 3.5,  # 🟡 降低到 3.5
    satellite_only: bool = False,
    max_target_change: float = 0.05,
    position_infos: dict[str, PositionInfo] | None = None,  # 🟢 新增
    current_date: str = "",  # 🟢 新增
    min_hold_days: int = 30,  # 🟢 新增：最小持仓天数
) -> tuple[list[RebalanceOrder], bool]:
    """
    构建调仓计划 V2
    
    改进：
    1. 降低最小交易金额到 $500
    2. 收紧权重容忍度到 1.5%
    3. 考虑持仓成本和时间
    """
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
            market_value = float(row.get("market_val", row.get("val", 0.0)) or 0.0)
            total_market_value += max(0.0, market_value)
            position_map[str(row["code"])] = {
                "qty": float(row.get("qty", 0.0)),
                "can_sell_qty": float(row.get("can_sell_qty", row.get("qty", 0.0))),
            }

    stats_by_symbol = {symbol: indicators(frames[symbol]) for symbol in symbols}
    target_weights, allocation_reasons = ranked_target_weights_v2(
        symbols, stats_by_symbol, spy, risk_on, max_stock_weight, profile,
        core_budget, satellite_budget, satellite_min_score, position_infos,
    )
    
    max_target_change = min(0.20, max(0.01, float(max_target_change)))
    candidates = []
    position_infos = position_infos or {}
    
    for symbol in symbols:
        if satellite_only and symbol in CORE_MAX_WEIGHTS:
            continue
        
        stats = stats_by_symbol[symbol]
        target_weight = target_weights.get(symbol, 0.0)
        reason = allocation_reasons.get(symbol, "未通过择时/择标的过滤")
        held = position_map.get(symbol, {"qty": 0.0, "can_sell_qty": 0.0})
        current_value = held["qty"] * stats["price"]
        current_weight = current_value / account_value
        
        # 🟢 检查是否应该退出（针对 Satellite）
        if symbol not in CORE_MAX_WEIGHTS and held["qty"] > 0:
            position_info = position_infos.get(symbol)
            if position_info and current_date:
                should_exit, exit_reason = should_exit_satellite(
                    stats, position_info, current_date, min_hold_days
                )
                if should_exit:
                    # 强制退出
                    target_weight = 0.0
                    reason = exit_reason
        
        original_target_weight = target_weight
        target_weight = min(
            max(current_weight - max_target_change, target_weight),
            current_weight + max_target_change,
        )
        if abs(target_weight - original_target_weight) > 1e-9:
            reason = f"组合目标权重变化受限（单次最多{max_target_change:.1%}）；{reason}"
        elif target_weight < current_weight:
            reason = f"组合再平衡减仓；{reason}"
        
        delta_value = account_value * target_weight - current_value
        
        # 获取持仓成本
        position_cost = 0.0
        entry_date = ""
        if symbol in position_infos:
            position_cost = position_infos[symbol].cost_basis
            entry_date = position_infos[symbol].entry_date
        
        candidates.append((symbol, stats, held, current_weight, target_weight, delta_value, reason, position_cost, entry_date))

    buy_budget = max(0.0, account_value * max_stock_weight - total_market_value)
    requested_buys = sum(max(0.0, item[5]) for item in candidates)
    buy_scale = min(1.0, buy_budget / requested_buys) if requested_buys else 1.0

    orders = []
    for symbol, stats, held, current_weight, target_weight, delta_value, reason, position_cost, entry_date in candidates:
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
                position_cost=position_cost,
                entry_date=entry_date,
            )
        )
    return orders, risk_on
