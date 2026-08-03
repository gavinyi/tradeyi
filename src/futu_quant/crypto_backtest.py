from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import pandas as pd

from .backtest_metrics import calculate_backtest_metrics
from .crypto_swap import CryptoSwapConfig, decide_crypto_swap
from .crypto_swap_managed_futures import (
    CryptoSwapManagedFuturesConfig,
    decide_crypto_swap_managed_futures,
)
from .crypto_volatility import CryptoVolatilityConfig, decide_crypto_volatility


@dataclass(frozen=True)
class CryptoBacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    bars: pd.DataFrame
    metrics: dict[str, float]


def closed_trade_frame(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "pnl" not in trades:
        return pd.DataFrame()
    frame = trades.copy()
    frame["pnl"] = pd.to_numeric(frame["pnl"], errors="coerce")
    return frame[frame["pnl"].notna()].copy()


def _spot_trade_excursion(
    frame: pd.DataFrame, start_index: int | None, end_index: int, entry_price: float
) -> tuple[float, float]:
    if start_index is None or entry_price <= 0:
        return 0.0, 0.0
    window = frame.iloc[start_index : end_index + 1]
    high = window.get("high", window["close"]).astype(float)
    low = window.get("low", window["close"]).astype(float)
    mfe = float(high.max() / entry_price - 1)
    mae = float(low.min() / entry_price - 1)
    return mfe, mae


def _swap_trade_excursion(
    frame: pd.DataFrame,
    start_index: int | None,
    end_index: int,
    entry_price: float,
    direction: int,
) -> tuple[float, float]:
    if start_index is None or entry_price <= 0 or direction == 0:
        return 0.0, 0.0
    window = frame.iloc[start_index : end_index + 1]
    high = window.get("high", window["close"]).astype(float)
    low = window.get("low", window["close"]).astype(float)
    if direction > 0:
        return float(high.max() / entry_price - 1), float(low.min() / entry_price - 1)
    return float(entry_price / low.min() - 1), float(entry_price / high.max() - 1)


def _bar_millis(bar: str) -> int:
    text = str(bar).upper()
    if text.endswith("H"):
        return int(float(text[:-1]) * 3_600_000)
    if text.endswith("D"):
        return int(float(text[:-1]) * 86_400_000)
    return 3_600_000


def _closed_higher_timeframe_history(
    bars: pd.DataFrame | None,
    current_timestamp: int,
    bar: str,
) -> pd.DataFrame | None:
    if bars is None or bars.empty:
        return None
    frame = bars.sort_values("timestamp").reset_index(drop=True).copy()
    cutoff = int(current_timestamp) - _bar_millis(bar)
    return frame[pd.to_numeric(frame["timestamp"], errors="coerce") <= cutoff].copy()


def trade_quality_summary(trades: pd.DataFrame) -> pd.DataFrame:
    frame = closed_trade_frame(trades)
    if frame.empty:
        return pd.DataFrame()
    frame["entry_type"] = frame.get("entry_type", "未分类").fillna("未分类")
    frame["pnl_ratio"] = pd.to_numeric(frame.get("pnl_ratio"), errors="coerce")
    frame["mfe"] = pd.to_numeric(frame.get("mfe"), errors="coerce")
    frame["mae"] = pd.to_numeric(frame.get("mae"), errors="coerce")
    grouped = frame.groupby("entry_type", dropna=False)
    summary = grouped.agg(
        交易笔数=("pnl", "size"),
        胜率=("pnl", lambda values: float((values > 0).mean())),
        总盈亏=("pnl", "sum"),
        平均盈亏=("pnl", "mean"),
        平均收益率=("pnl_ratio", "mean"),
        平均最大浮盈=("mfe", "mean"),
        平均最大浮亏=("mae", "mean"),
    ).reset_index(names="入场类型")
    return summary.sort_values(["总盈亏", "胜率"], ascending=[False, False])


def _safe_mean(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return 0.0
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.mean()) if len(values) else 0.0


def backtest_quality_metrics(
    result: CryptoBacktestResult,
    min_closed_trades: int = 2,
) -> dict[str, float | str | bool]:
    closed = closed_trade_frame(result.trades)
    closed_count = len(closed)
    trade_count = int(result.metrics.get("trade_count", 0))
    win_rate = float(result.metrics.get("win_rate", 0.0))
    profit_factor = float(result.metrics.get("profit_factor", 0.0))
    excess_return = float(result.metrics.get("excess_return", 0.0))
    avg_mfe = _safe_mean(closed, "mfe")
    avg_mae = _safe_mean(closed, "mae")
    avg_pnl_ratio = _safe_mean(closed, "pnl_ratio")
    wins = closed[closed["pnl"] > 0]["pnl"] if not closed.empty else pd.Series(dtype=float)
    losses = closed[closed["pnl"] < 0]["pnl"] if not closed.empty else pd.Series(dtype=float)
    payoff_ratio = (
        float(wins.mean()) / abs(float(losses.mean()))
        if len(wins) and len(losses) and float(losses.mean()) != 0 else 0.0
    )
    mfe_capture = avg_pnl_ratio / avg_mfe if avg_mfe > 0 else 0.0
    acceptable = closed_count >= min_closed_trades
    issues: list[str] = []
    if trade_count == 0:
        status = "无交易"
        issues.append("没有触发交易")
    elif not acceptable:
        status = "样本不足"
        issues.append(f"已平仓 {closed_count}/{min_closed_trades}")
    elif profit_factor >= 1 and excess_return >= 0:
        status = "有效盈利"
    elif profit_factor >= 1:
        status = "绝对盈利但跑输持有"
        issues.append("相对持有为负")
    else:
        status = "有效亏损"
    if acceptable:
        if win_rate < 0.4:
            issues.append("胜率偏低")
        if profit_factor < 1:
            issues.append("盈利因子低于1")
        if excess_return < 0:
            issues.append("跑输持有")
        if avg_mfe > 0 and avg_mae < 0 and abs(avg_mae) > avg_mfe:
            issues.append("平均浮亏大于浮盈")
        if avg_mfe > 0 and mfe_capture < 0.25:
            issues.append("浮盈兑现率偏低")
    if acceptable:
        score = (
            35.0
            + win_rate * 20.0
            + min(max(profit_factor, 0.0), 3.0) * 10.0
            + max(-20.0, min(20.0, excess_return * 200.0))
            + max(-10.0, min(10.0, avg_pnl_ratio * 500.0))
            + max(-10.0, min(10.0, mfe_capture * 10.0))
        )
    elif trade_count > 0:
        score = min(20.0, closed_count / max(min_closed_trades, 1) * 20.0)
    else:
        score = 0.0
    return {
        "验收状态": status,
        "可验收": bool(acceptable),
        "质量评分": round(max(0.0, min(100.0, score)), 2),
        "平均最大浮盈": avg_mfe,
        "平均最大浮亏": avg_mae,
        "平均平仓收益率": avg_pnl_ratio,
        "盈亏比": payoff_ratio,
        "浮盈兑现率": mfe_capture,
        "主要问题": "；".join(issues) if issues else "无明显阻塞",
    }


def run_crypto_volatility_parameter_grid(
    bars: pd.DataFrame,
    initial_cash: float,
    base_config: CryptoVolatilityConfig | None = None,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    pullback_depths: tuple[float, ...] = (0.03, 0.04, 0.06),
    rsi_limits: tuple[float, ...] = (60.0, 65.0, 70.0),
    bollinger_limits: tuple[float, ...] = (0.70, 0.80, 0.90),
    min_closed_trades: int = 2,
) -> pd.DataFrame:
    base_config = base_config or CryptoVolatilityConfig()
    rows: list[dict] = []
    for depth in pullback_depths:
        for rsi_limit in rsi_limits:
            for bollinger_limit in bollinger_limits:
                config = replace(
                    base_config,
                    pullback_depth=depth,
                    pullback_max_rsi=rsi_limit,
                    pullback_max_bollinger=bollinger_limit,
                )
                result = run_crypto_volatility_backtest(
                    bars, initial_cash, config, fee_rate, slippage_bps
                )
                rows.append({
                    "回调深度": depth,
                    "RSI上限": rsi_limit,
                    "布林位置上限": bollinger_limit,
                    "总收益率": result.metrics["total_return"],
                    "持有收益": result.metrics["benchmark_return"],
                    "相对持有": result.metrics["excess_return"],
                    "最大回撤": result.metrics["max_drawdown"],
                    "交易次数": result.metrics["trade_count"],
                    "已平仓": result.metrics["closed_trades"],
                    "胜率": result.metrics["win_rate"],
                    "盈利因子": result.metrics["profit_factor"],
                    "单笔期望": result.metrics["expectancy"],
                    **backtest_quality_metrics(result, min_closed_trades),
                })
    return pd.DataFrame(rows).sort_values(
        ["可验收", "质量评分", "相对持有", "最大回撤"],
        ascending=[False, False, False, False],
    )


def run_crypto_swap_parameter_grid(
    bars: pd.DataFrame,
    initial_cash: float,
    base_config: CryptoSwapConfig | None = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 5.0,
    funding_rate_per_8h: float = 0.0,
    higher_timeframe_bars: pd.DataFrame | None = None,
    long_volume_ratios: tuple[float, ...] = (0.9, 1.1, 1.3),
    short_volume_ratios: tuple[float, ...] = (1.1, 1.3),
    confirmation_bars: tuple[int, ...] = (1, 2),
    continuation_returns: tuple[float, ...] = (0.0, 0.002, 0.005),
    min_closed_trades: int = 2,
) -> pd.DataFrame:
    base_config = base_config or CryptoSwapConfig()
    rows: list[dict] = []
    for long_volume in long_volume_ratios:
        for short_volume in short_volume_ratios:
            for confirms in confirmation_bars:
                for continuation_return in continuation_returns:
                    config = replace(
                        base_config,
                        long_min_volume_ratio=long_volume,
                        short_min_volume_ratio=short_volume,
                        confirmation_bars=confirms,
                        continuation_min_return=continuation_return,
                    )
                    result = run_crypto_swap_backtest(
                        bars,
                        initial_cash,
                        config,
                        fee_rate,
                        slippage_bps,
                        funding_rate_per_8h,
                        higher_timeframe_bars=higher_timeframe_bars,
                    )
                    rows.append({
                        "做多量比": long_volume,
                        "做空量比": short_volume,
                        "突破确认": confirms,
                        "延续收益门槛": continuation_return,
                        "总收益率": result.metrics["total_return"],
                        "持有收益": result.metrics["benchmark_return"],
                        "相对持有": result.metrics["excess_return"],
                        "最大回撤": result.metrics["max_drawdown"],
                        "交易次数": result.metrics["trade_count"],
                        "已平仓": result.metrics["closed_trades"],
                        "胜率": result.metrics["win_rate"],
                        "盈利因子": result.metrics["profit_factor"],
                        "单笔期望": result.metrics["expectancy"],
                        **backtest_quality_metrics(result, min_closed_trades),
                    })
    return pd.DataFrame(rows).sort_values(
        ["可验收", "质量评分", "相对持有", "最大回撤"],
        ascending=[False, False, False, False],
    )


def run_crypto_volatility_backtest(
    bars: pd.DataFrame,
    initial_cash: float,
    config: CryptoVolatilityConfig | None = None,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
) -> CryptoBacktestResult:
    config = config or CryptoVolatilityConfig()
    if config.higher_timeframe_enabled:
        config = replace(config, higher_timeframe_enabled=False)
    frame = bars.sort_values("timestamp").reset_index(drop=True).copy()
    if len(frame) < max(config.slow_window + 3, 55):
        raise ValueError("币波动率回测历史数据不足")
    cash, position, average_price = float(initial_cash), 0.0, None
    peak_price, partial_stage, entry_index, contraction_count = None, 0, None, 0
    entry_timestamp, entry_type = None, None
    pending = None
    equity_rows, trades = [], []
    slip = slippage_bps / 10_000
    for index, row in frame.iterrows():
        if pending:
            action, signal_timestamp, notional, fraction, metadata = pending
            open_price = float(row["open"])
            if action == "BUY" and position == 0:
                price = open_price * (1 + slip)
                spend = min(cash / (1 + fee_rate), notional)
                quantity = spend / price
                fee = spend * fee_rate
                if quantity > 0:
                    cash -= spend + fee
                    position, average_price, peak_price = quantity, price, price
                    entry_index, partial_stage, contraction_count = index, 0, 0
                    entry_timestamp = row["timestamp"]
                    entry_type = metadata.get("entry_type", "突破")
                    trades.append({
                        "timestamp": row["timestamp"], "signal_timestamp": signal_timestamp,
                        "side": "BUY", "price": price, "quantity": quantity,
                        "notional": spend, "fee": fee,
                        "entry_type": entry_type,
                    })
            elif action in {"SELL_ALL", "SELL_PARTIAL"} and position > 0:
                price = open_price * (1 - slip)
                sell_quantity = position if action == "SELL_ALL" else position * fraction
                notional_out = sell_quantity * price
                fee = notional_out * fee_rate
                entry_price = float(average_price or 0)
                pnl = sell_quantity * (price - entry_price) - fee
                pnl_ratio = price / entry_price - 1 if entry_price > 0 else 0.0
                mfe, mae = _spot_trade_excursion(frame, entry_index, index, entry_price)
                cash += notional_out - fee
                trades.append({
                    "timestamp": row["timestamp"], "signal_timestamp": signal_timestamp,
                    "side": "SELL", "price": price, "quantity": sell_quantity,
                    "notional": notional_out, "fee": fee,
                    "entry_price": entry_price,
                    "entry_timestamp": entry_timestamp,
                    "entry_type": entry_type or "未分类",
                    "exit_type": action,
                    "pnl": pnl,
                    "pnl_ratio": pnl_ratio,
                    "mfe": mfe,
                    "mae": mae,
                    "holding_bars": float(index - entry_index)
                    if entry_index is not None else 0.0,
                })
                position -= sell_quantity
                if action == "SELL_ALL" or position <= 1e-12:
                    position, average_price, peak_price = 0.0, None, None
                    entry_index, partial_stage, contraction_count = None, 0, 0
                    entry_timestamp, entry_type = None, None
                else:
                    partial_stage += 1
            pending = None
        history = frame.iloc[: index + 1]
        if position > 0:
            peak_price = max(float(peak_price or 0), float(row["close"]))
        held_hours = float(index - entry_index) if entry_index is not None else 0.0
        decision = decide_crypto_volatility(
            history, Decimal(str(position)),
            Decimal(str(average_price)) if average_price is not None else None,
            Decimal(str(peak_price)) if peak_price is not None else None,
            partial_stage=partial_stage, held_hours=held_hours,
            contraction_count=contraction_count,
            config=config,
        )
        if position > 0:
            contracted = (
                float(decision.metrics.get("volatility_expansion", 99))
                <= config.contraction_ratio
            )
            contraction_count = contraction_count + 1 if contracted else 0
        if decision.action in {"BUY", "SELL_ALL", "SELL_PARTIAL"}:
            next_entry_type = str(decision.metrics.get("entry_source") or (
                "回调" if decision.metrics.get("pullback_ok") else "突破"
            ))
            pending = (
                decision.action, row["timestamp"], decision.target_notional,
                decision.quantity_fraction, {"entry_type": next_entry_type},
            )
        close = float(row["close"])
        equity_rows.append({
            "date": pd.to_datetime(row["timestamp"], unit="ms", utc=True),
            "timestamp": row["timestamp"], "equity": cash + position * close,
            "cash": cash, "position": position, "close": close,
            "signal": decision.action,
        })
    equity = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trades)
    benchmark = initial_cash * frame["close"].astype(float) / float(frame["close"].iloc[0])
    metrics = calculate_backtest_metrics(
        equity, trade_frame, initial_cash,
        benchmark=benchmark,
        periods_per_year=24 * 365,
    )
    return CryptoBacktestResult(equity, trade_frame, frame, metrics)


def run_crypto_swap_backtest(
    bars: pd.DataFrame,
    initial_cash: float,
    config: CryptoSwapConfig | None = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 5.0,
    funding_rate_per_8h: float = 0.0,
    higher_timeframe_bars: pd.DataFrame | None = None,
) -> CryptoBacktestResult:
    config = config or CryptoSwapConfig()
    if config.higher_timeframe_enabled and higher_timeframe_bars is None:
        config = replace(config, higher_timeframe_enabled=False)
    frame = bars.sort_values("timestamp").reset_index(drop=True).copy()
    if len(frame) < max(config.slow_window + 3, 55):
        raise ValueError("合约波动率回测历史数据不足")
    cash = float(initial_cash)
    contracts, average_price, direction = 0.0, 0.0, 0
    peak_pnl, partial_stage, entry_index, contraction_count = 0.0, 0, None, 0
    entry_timestamp, entry_type = None, None
    pending = None
    rows, trades = [], []
    slip = slippage_bps / 10_000
    for index, row in frame.iterrows():
        if pending:
            action, signal_timestamp, fraction, metadata = pending
            open_price = float(row["open"])
            fill_price = open_price * (1 + slip if action in {"OPEN_LONG", "CLOSE_SHORT"} else 1 - slip)
            if action in {"OPEN_LONG", "OPEN_SHORT"} and contracts == 0:
                notional = min(config.margin_budget * config.leverage, cash * config.leverage)
                contracts = notional / fill_price
                direction = 1 if action == "OPEN_LONG" else -1
                average_price, entry_index, peak_pnl, partial_stage = fill_price, index, 0.0, 0
                entry_timestamp = row["timestamp"]
                entry_type = metadata.get("entry_type", action)
                cash -= notional / config.leverage + notional * fee_rate
                trades.append({
                    "timestamp": row["timestamp"], "signal_timestamp": signal_timestamp,
                    "side": action, "price": fill_price, "quantity": contracts,
                    "notional": notional, "fee": notional * fee_rate,
                    "entry_type": entry_type,
                })
            elif action in {"CLOSE_LONG", "CLOSE_SHORT"} and contracts > 0:
                close_qty = contracts * float(fraction)
                pnl_ratio = (fill_price / average_price - 1) * direction
                notional = close_qty * fill_price
                margin_release = close_qty * average_price / config.leverage
                realized_pnl = close_qty * average_price * pnl_ratio - notional * fee_rate
                mfe, mae = _swap_trade_excursion(
                    frame, entry_index, index, average_price, direction
                )
                cash += margin_release + realized_pnl
                trades.append({
                    "timestamp": row["timestamp"], "signal_timestamp": signal_timestamp,
                    "side": action, "price": fill_price, "quantity": close_qty,
                    "notional": notional, "fee": notional * fee_rate,
                    "entry_price": average_price,
                    "entry_timestamp": entry_timestamp,
                    "entry_type": entry_type or "未分类",
                    "pnl": realized_pnl,
                    "pnl_ratio": pnl_ratio,
                    "mfe": mfe,
                    "mae": mae,
                    "holding_bars": float(index - entry_index)
                    if entry_index is not None else 0.0,
                })
                contracts -= close_qty
                if contracts <= 1e-12 or fraction >= 1:
                    contracts, average_price, direction = 0.0, 0.0, 0
                    entry_index, peak_pnl, partial_stage, contraction_count = None, 0.0, 0, 0
                    entry_timestamp, entry_type = None, None
                else:
                    partial_stage += 1
            pending = None
        close = float(row["close"])
        unrealized = contracts * average_price * ((close / average_price - 1) * direction) if contracts and average_price else 0.0
        if contracts:
            funding_cost = contracts * close * funding_rate_per_8h / 8
            cash -= funding_cost
            pnl_ratio = (close / average_price - 1) * direction
            peak_pnl = max(peak_pnl, pnl_ratio)
            held_hours = float(index - entry_index) if entry_index is not None else 0.0
        else:
            pnl_ratio, held_hours = 0.0, 0.0
        history = frame.iloc[: index + 1]
        htf_history = _closed_higher_timeframe_history(
            higher_timeframe_bars,
            int(row["timestamp"]),
            config.higher_timeframe_bar,
        )
        decision = decide_crypto_swap(
            history, contracts * direction, average_price, config,
            peak_pnl=peak_pnl, partial_stage=partial_stage,
            held_hours=held_hours, contraction_count=contraction_count,
            funding_rate=funding_rate_per_8h,
            higher_timeframe_bars=htf_history,
        )
        if contracts:
            contracted = float(decision.metrics.get("volatility_expansion", 99)) <= config.contraction_ratio
            contraction_count = contraction_count + 1 if contracted else 0
        if decision.action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}:
            next_entry_type = (
                f"做多{decision.metrics.get('entry_model', '突破')}"
                if decision.action == "OPEN_LONG"
                else f"做空{decision.metrics.get('entry_model', '突破')}"
                if decision.action == "OPEN_SHORT"
                else entry_type or "未分类"
            )
            pending = (
                decision.action,
                row["timestamp"],
                decision.quantity_fraction,
                {"entry_type": next_entry_type},
            )
        equity = cash + (contracts * average_price / config.leverage if contracts else 0) + unrealized
        rows.append({
            "date": pd.to_datetime(row["timestamp"], unit="ms", utc=True),
            "timestamp": row["timestamp"], "equity": equity, "cash": cash,
            "position": contracts * direction, "close": close,
            "signal": decision.action, "pnl_ratio": pnl_ratio,
        })
    equity = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    benchmark = initial_cash * frame["close"].astype(float) / float(frame["close"].iloc[0])
    metrics = calculate_backtest_metrics(
        equity, trade_frame, initial_cash,
        benchmark=benchmark,
        periods_per_year=24 * 365,
    )
    return CryptoBacktestResult(equity, trade_frame, frame, metrics)


def run_crypto_swap_managed_futures_backtest(
    symbol: str,
    bars: pd.DataFrame,
    initial_cash: float,
    config: CryptoSwapConfig | None = None,
    mf_config: CryptoSwapManagedFuturesConfig | None = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 5.0,
    funding_rate_per_8h: float = 0.0,
    higher_timeframe_bars: pd.DataFrame | None = None,
    daily_bars: pd.DataFrame | None = None,
    benchmark_bars: pd.DataFrame | None = None,
) -> CryptoBacktestResult:
    config = config or CryptoSwapConfig()
    mf_config = mf_config or CryptoSwapManagedFuturesConfig()
    frame = bars.sort_values("timestamp").reset_index(drop=True).copy()
    if len(frame) < max(config.slow_window + 3, 130):
        raise ValueError("合约 managed futures 回测历史数据不足")
    cash = float(initial_cash)
    contracts, average_price, direction = 0.0, 0.0, 0
    peak_pnl, partial_stage, recovery_layer, entry_index = 0.0, 0, 0, None
    entry_timestamp, entry_type = None, None
    pending = None
    rows, trades = [], []
    slip = slippage_bps / 10_000
    for index, row in frame.iterrows():
        if pending:
            action, signal_timestamp, fraction, target_notional, metadata = pending
            open_price = float(row["open"])
            fill_price = open_price * (
                1 + slip if action in {"OPEN_LONG", "CLOSE_SHORT"} else 1 - slip
            )
            if action in {"OPEN_LONG", "OPEN_SHORT"}:
                new_direction = 1 if action == "OPEN_LONG" else -1
                if direction in {0, new_direction}:
                    notional = min(float(target_notional), cash * config.leverage)
                    new_contracts = notional / fill_price if fill_price > 0 else 0.0
                    if new_contracts > 0:
                        fee = notional * fee_rate
                        cash -= notional / config.leverage + fee
                        if contracts > 0:
                            average_price = (
                                contracts * average_price + new_contracts * fill_price
                            ) / (contracts + new_contracts)
                            contracts += new_contracts
                        else:
                            contracts, average_price = new_contracts, fill_price
                            direction, entry_index = new_direction, index
                            entry_timestamp = row["timestamp"]
                            entry_type = metadata.get("entry_type", action)
                            peak_pnl, partial_stage, recovery_layer = 0.0, 0, 0
                        recovery_layer = int(metadata.get("recovery_layer", recovery_layer))
                        trades.append({
                            "timestamp": row["timestamp"],
                            "signal_timestamp": signal_timestamp,
                            "side": action,
                            "price": fill_price,
                            "quantity": new_contracts,
                            "notional": notional,
                            "fee": fee,
                            "entry_type": metadata.get("entry_type", action),
                            "recovery_layer": recovery_layer,
                        })
            elif action in {"CLOSE_LONG", "CLOSE_SHORT"} and contracts > 0:
                close_qty = contracts * float(fraction)
                pnl_ratio = (fill_price / average_price - 1) * direction
                notional = close_qty * fill_price
                margin_release = close_qty * average_price / config.leverage
                realized_pnl = close_qty * average_price * pnl_ratio - notional * fee_rate
                mfe, mae = _swap_trade_excursion(
                    frame, entry_index, index, average_price, direction
                )
                cash += margin_release + realized_pnl
                trades.append({
                    "timestamp": row["timestamp"],
                    "signal_timestamp": signal_timestamp,
                    "side": action,
                    "price": fill_price,
                    "quantity": close_qty,
                    "notional": notional,
                    "fee": notional * fee_rate,
                    "entry_price": average_price,
                    "entry_timestamp": entry_timestamp,
                    "entry_type": entry_type or "未分类",
                    "exit_type": action,
                    "pnl": realized_pnl,
                    "pnl_ratio": pnl_ratio,
                    "mfe": mfe,
                    "mae": mae,
                    "holding_bars": float(index - entry_index)
                    if entry_index is not None else 0.0,
                    "recovery_layer": recovery_layer,
                })
                contracts -= close_qty
                if contracts <= 1e-12 or fraction >= 1:
                    contracts, average_price, direction = 0.0, 0.0, 0
                    entry_index, peak_pnl, partial_stage, recovery_layer = None, 0.0, 0, 0
                    entry_timestamp, entry_type = None, None
                else:
                    partial_stage += 1
            pending = None
        close = float(row["close"])
        unrealized = (
            contracts * average_price * ((close / average_price - 1) * direction)
            if contracts and average_price else 0.0
        )
        if contracts:
            funding_cost = contracts * close * funding_rate_per_8h / 8 * direction
            cash -= funding_cost
            pnl_ratio = (close / average_price - 1) * direction
            peak_pnl = max(peak_pnl, pnl_ratio)
            held_hours = float(index - entry_index) if entry_index is not None else 0.0
        else:
            pnl_ratio, held_hours = 0.0, 0.0
        htf_history = _closed_higher_timeframe_history(
            higher_timeframe_bars,
            int(row["timestamp"]),
            config.higher_timeframe_bar,
        )
        daily_history = _closed_higher_timeframe_history(
            daily_bars,
            int(row["timestamp"]),
            "1D",
        )
        benchmark_history = _closed_higher_timeframe_history(
            benchmark_bars,
            int(row["timestamp"]),
            "1H",
        )
        decision = decide_crypto_swap_managed_futures(
            symbol,
            frame.iloc[: index + 1],
            contracts * direction,
            average_price,
            config,
            mf_config,
            peak_pnl=peak_pnl,
            partial_stage=partial_stage,
            held_hours=held_hours,
            recovery_layer=recovery_layer,
            funding_rate=funding_rate_per_8h,
            higher_timeframe_bars=htf_history,
            daily_bars=daily_history,
            benchmark_bars=benchmark_history,
        )
        if decision.action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}:
            next_layer = int(decision.metrics.get("next_recovery_layer", recovery_layer))
            pending = (
                decision.action,
                row["timestamp"],
                decision.quantity_fraction,
                decision.target_notional,
                {
                    "entry_type": decision.metrics.get("entry_model", decision.action),
                    "recovery_layer": next_layer,
                },
            )
        equity = cash + (contracts * average_price / config.leverage if contracts else 0) + unrealized
        rows.append({
            "date": pd.to_datetime(row["timestamp"], unit="ms", utc=True),
            "timestamp": row["timestamp"],
            "equity": equity,
            "cash": cash,
            "position": contracts * direction,
            "close": close,
            "signal": decision.action,
            "pnl_ratio": pnl_ratio,
            "recovery_layer": recovery_layer,
        })
    equity = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    benchmark = initial_cash * frame["close"].astype(float) / float(frame["close"].iloc[0])
    metrics = calculate_backtest_metrics(
        equity, trade_frame, initial_cash,
        benchmark=benchmark,
        periods_per_year=24 * 365,
    )
    return CryptoBacktestResult(equity, trade_frame, frame, metrics)
