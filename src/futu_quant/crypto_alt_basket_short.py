from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .backtest_metrics import calculate_backtest_metrics
from .crypto_backtest import CryptoBacktestResult


@dataclass(frozen=True)
class AltWeakBasketShortConfig:
    market_symbols: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    candidate_symbols: tuple[str, ...] = (
        "SOL-USDT-SWAP",
        "DOGE-USDT-SWAP",
        "XRP-USDT-SWAP",
        "ADA-USDT-SWAP",
        "AVAX-USDT-SWAP",
        "LINK-USDT-SWAP",
    )
    lookback_bars: int = 72
    rebalance_bars: int = 6
    basket_size: int = 3
    per_symbol_margin: float = 200.0
    leverage: float = 2.0
    min_relative_weakness: float = 0.060
    market_max_return: float = 0.015
    market_short_lookback_bars: int = 24
    market_short_max_return: float = 0.005
    market_reversal_exit: float = 0.025
    candidate_short_lookback_bars: int = 24
    candidate_min_short_return: float = -0.08
    candidate_max_short_return: float = 0.0
    candidate_min_lookback_return: float = -0.005
    require_ema_downtrend: bool = True
    ema_fast_bars: int = 12
    ema_slow_bars: int = 48
    rank_invalidation_exit: bool = True
    rank_exit_min_hold_bars: int = 6
    rank_exit_weakness: float = 0.010
    hard_stop: float = 0.045
    take_profit: float = 0.075
    profit_protection_start: float = 0.04
    profit_giveback: float = 0.35
    max_hold_bars: int = 12
    fee_rate: float = 0.0005
    slippage_bps: float = 5.0


def _prepared(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    if "confirm" in result:
        result = result[result["confirm"].astype(bool)].copy()
    required = ["timestamp", "open", "high", "low", "close"]
    result = result.dropna(subset=[column for column in required if column in result])
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=required).sort_values("timestamp").reset_index(drop=True)


def _aligned_closes(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    series: dict[str, pd.Series] = {}
    for symbol, frame in data.items():
        prepared = _prepared(frame)
        if prepared.empty:
            continue
        series[symbol] = prepared.set_index("timestamp")["close"].astype(float)
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1, join="inner").sort_index()


def _relative_strength_rows(
    closes: pd.DataFrame,
    index: int,
    config: AltWeakBasketShortConfig,
) -> tuple[float, list[dict]]:
    start = index - config.lookback_bars
    market_columns = [symbol for symbol in config.market_symbols if symbol in closes]
    candidate_columns = [symbol for symbol in config.candidate_symbols if symbol in closes]
    if start < 0 or not market_columns or not candidate_columns:
        return 0.0, []
    market_return = _market_return(closes, index, config.lookback_bars, market_columns)
    rows: list[dict] = []
    for symbol in candidate_columns:
        start_price = float(closes.iloc[start][symbol])
        end_price = float(closes.iloc[index][symbol])
        if start_price <= 0 or end_price <= 0:
            continue
        own_return = end_price / start_price - 1
        relative_strength = own_return - market_return
        short_return = _symbol_return(closes, symbol, index, config.candidate_short_lookback_bars)
        ema_fast, ema_slow = _ema_pair(closes[symbol].iloc[: index + 1], config)
        ema_downtrend = bool(ema_fast and ema_slow and ema_fast < ema_slow)
        rows.append({
            "symbol": symbol,
            "own_return": float(own_return),
            "short_return": float(short_return),
            "market_return": market_return,
            "relative_strength": float(relative_strength),
            "weakness": float(-relative_strength),
            "ema_fast": float(ema_fast or 0.0),
            "ema_slow": float(ema_slow or 0.0),
            "ema_downtrend": ema_downtrend,
        })
    rows.sort(key=lambda item: item["relative_strength"])
    return market_return, rows


def _symbol_return(
    closes: pd.DataFrame,
    symbol: str,
    index: int,
    lookback: int,
) -> float:
    if symbol not in closes or index - lookback < 0:
        return 0.0
    start = float(closes.iloc[index - lookback][symbol])
    end = float(closes.iloc[index][symbol])
    return end / start - 1 if start > 0 else 0.0


def _market_return(
    closes: pd.DataFrame,
    index: int,
    lookback: int,
    market_columns: list[str] | None = None,
) -> float:
    market_columns = market_columns or [symbol for symbol in closes if symbol in {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP"
    }]
    if index - lookback < 0 or not market_columns:
        return 0.0
    return float(
        (
            closes.iloc[index][market_columns]
            / closes.iloc[index - lookback][market_columns]
            - 1
        ).mean()
    )


def _ema_pair(
    close: pd.Series,
    config: AltWeakBasketShortConfig,
) -> tuple[float | None, float | None]:
    if len(close) < max(config.ema_slow_bars, 3):
        return None, None
    fast = float(close.astype(float).ewm(span=config.ema_fast_bars, adjust=False).mean().iloc[-1])
    slow = float(close.astype(float).ewm(span=config.ema_slow_bars, adjust=False).mean().iloc[-1])
    return fast, slow


def _market_state_allows_entry(
    closes: pd.DataFrame,
    index: int,
    config: AltWeakBasketShortConfig,
    market_return: float,
) -> tuple[bool, float, str]:
    market_columns = [symbol for symbol in config.market_symbols if symbol in closes]
    short_market_return = _market_return(
        closes, index, config.market_short_lookback_bars, market_columns
    )
    allowed = (
        market_return <= config.market_max_return
        and short_market_return <= config.market_short_max_return
    )
    reason = (
        f"市场长周期 {market_return:.2%} ≤ {config.market_max_return:.2%}，"
        f"短周期 {short_market_return:.2%} ≤ {config.market_short_max_return:.2%}"
    )
    return allowed, short_market_return, reason


def _candidate_allows_entry(
    row: dict,
    config: AltWeakBasketShortConfig,
) -> tuple[bool, str]:
    checks = [
        (
            row["weakness"] >= config.min_relative_weakness,
            f"相对弱势 {row['weakness']:.2%} / ≥ {config.min_relative_weakness:.2%}",
        ),
        (
            row["own_return"] <= config.candidate_min_lookback_return,
            (
                f"自身长周期涨跌 {row['own_return']:.2%} / "
                f"≤ {config.candidate_min_lookback_return:.2%}"
            ),
        ),
        (
            row["short_return"] >= config.candidate_min_short_return,
            (
                f"自身短周期不过度下跌 {row['short_return']:.2%} / "
                f"≥ {config.candidate_min_short_return:.2%}"
            ),
        ),
        (
            row["short_return"] <= config.candidate_max_short_return,
            (
                f"自身短周期涨跌 {row['short_return']:.2%} / "
                f"≤ {config.candidate_max_short_return:.2%}"
            ),
        ),
    ]
    if config.require_ema_downtrend:
        checks.append((
            bool(row["ema_downtrend"]),
            f"EMA空头排列 {'通过' if row['ema_downtrend'] else '未通过'}",
        ))
    allowed = all(passed for passed, _ in checks)
    return allowed, "；".join(text for _, text in checks)


def rank_alt_weakness(
    data: dict[str, pd.DataFrame],
    config: AltWeakBasketShortConfig | None = None,
) -> pd.DataFrame:
    config = config or AltWeakBasketShortConfig()
    closes = _aligned_closes(data)
    if closes.empty:
        return pd.DataFrame()
    market_return, rows = _relative_strength_rows(closes, len(closes) - 1, config)
    for row in rows:
        row["market_return"] = market_return
    return pd.DataFrame(rows)


def run_alt_weak_basket_short_backtest(
    data: dict[str, pd.DataFrame],
    initial_cash: float = 10_000.0,
    config: AltWeakBasketShortConfig | None = None,
) -> CryptoBacktestResult:
    config = config or AltWeakBasketShortConfig()
    prepared = {symbol: _prepared(frame) for symbol, frame in data.items()}
    closes = _aligned_closes(prepared)
    min_bars = config.lookback_bars + 3
    if len(closes) < min_bars:
        raise ValueError(f"山寨币弱势篮子回测历史数据不足：{len(closes)}/{min_bars}")

    symbol_frames = {
        symbol: frame.set_index("timestamp")
        for symbol, frame in prepared.items()
        if not frame.empty and symbol in closes
    }
    cash = float(initial_cash)
    reserved_margin = 0.0
    basket_id = 0
    positions: dict[str, dict] = {}
    rows: list[dict] = []
    trades: list[dict] = []
    slip = config.slippage_bps / 10_000

    timestamps = list(closes.index)
    for index, timestamp in enumerate(timestamps[:-1]):
        timestamp = int(timestamp)
        next_timestamp = int(timestamps[index + 1])
        market_return, ranking = _relative_strength_rows(closes, index, config)
        market_allowed, short_market_return, market_state_reason = _market_state_allows_entry(
            closes, index, config, market_return
        )
        ranking_by_symbol = {row["symbol"]: row for row in ranking}

        for symbol, position in list(positions.items()):
            current_close = float(closes.loc[timestamp, symbol])
            pnl_ratio = position["entry_price"] / current_close - 1
            position["peak_pnl"] = max(float(position["peak_pnl"]), pnl_ratio)
            position["bars_held"] += 1
            rank_row = ranking_by_symbol.get(symbol, {})
            current_weakness = float(rank_row.get("weakness", 0.0))
            reason = ""
            if pnl_ratio <= -config.hard_stop:
                reason = f"硬止损：{pnl_ratio:.2%}"
            elif pnl_ratio >= config.take_profit:
                reason = f"止盈：{pnl_ratio:.2%}"
            elif (
                position["peak_pnl"] >= config.profit_protection_start
                and pnl_ratio <= position["peak_pnl"] * (1 - config.profit_giveback)
            ):
                reason = (
                    f"盈利保护：峰值 {position['peak_pnl']:.2%}，当前 {pnl_ratio:.2%}"
                )
            elif position["bars_held"] >= config.max_hold_bars:
                reason = f"最长持有：{position['bars_held']} 根K线"
            elif market_return > config.market_reversal_exit:
                reason = f"市场反弹退出：BTC/ETH {market_return:.2%}"
            elif (
                config.rank_invalidation_exit
                and position["bars_held"] >= config.rank_exit_min_hold_bars
                and current_weakness < config.rank_exit_weakness
            ):
                reason = (
                    f"弱势排名失效退出：当前弱势 {current_weakness:.2%} "
                    f"< {config.rank_exit_weakness:.2%}"
                )
            if not reason:
                continue
            next_open = float(symbol_frames[symbol].loc[next_timestamp, "open"])
            fill_price = next_open * (1 + slip)
            notional = position["contracts"] * fill_price
            pnl = position["contracts"] * position["entry_price"] * (
                position["entry_price"] / fill_price - 1
            )
            fee = notional * config.fee_rate
            cash += float(position["margin"]) + pnl - fee
            reserved_margin -= float(position["margin"])
            trades.append({
                "timestamp": next_timestamp,
                "side": "CLOSE_SHORT",
                "symbol": symbol,
                "basket_id": position["basket_id"],
                "price": fill_price,
                "quantity": position["contracts"],
                "notional": notional,
                "fee": fee,
                "entry_price": position["entry_price"],
                "entry_timestamp": position["entry_timestamp"],
                "entry_type": "山寨币弱势篮子做空",
                "exit_type": "平空",
                "reason": reason,
                "pnl": pnl - fee,
                "pnl_ratio": position["entry_price"] / fill_price - 1,
                "relative_strength": position["relative_strength"],
                "current_weakness": current_weakness,
                "market_return": market_return,
                "short_market_return": short_market_return,
                "holding_bars": position["bars_held"],
            })
            positions.pop(symbol, None)

        if (
            not positions
            and index >= config.lookback_bars
            and index % max(config.rebalance_bars, 1) == 0
            and market_allowed
        ):
            candidates = []
            for row in ranking:
                allowed, quality_reason = _candidate_allows_entry(row, config)
                if allowed:
                    candidates.append({**row, "quality_reason": quality_reason})
                if len(candidates) >= config.basket_size:
                    break
            if candidates:
                basket_id += 1
            for candidate in candidates:
                symbol = candidate["symbol"]
                if symbol not in symbol_frames or next_timestamp not in symbol_frames[symbol].index:
                    continue
                margin = min(config.per_symbol_margin, max(cash, 0.0))
                if margin <= 0:
                    break
                open_price = float(symbol_frames[symbol].loc[next_timestamp, "open"])
                fill_price = open_price * (1 - slip)
                notional = margin * config.leverage
                contracts = notional / fill_price if fill_price > 0 else 0.0
                if contracts <= 0:
                    continue
                fee = notional * config.fee_rate
                cash -= margin + fee
                reserved_margin += margin
                positions[symbol] = {
                    "basket_id": basket_id,
                    "entry_price": fill_price,
                    "contracts": contracts,
                    "margin": margin,
                    "entry_timestamp": next_timestamp,
                    "bars_held": 0,
                    "peak_pnl": 0.0,
                    "relative_strength": candidate["relative_strength"],
                }
                trades.append({
                    "timestamp": next_timestamp,
                    "side": "OPEN_SHORT",
                    "symbol": symbol,
                    "basket_id": basket_id,
                    "price": fill_price,
                    "quantity": contracts,
                    "notional": notional,
                    "fee": fee,
                    "entry_type": "山寨币弱势篮子做空",
                    "relative_strength": candidate["relative_strength"],
                    "own_return": candidate["own_return"],
                    "short_return": candidate["short_return"],
                    "market_return": market_return,
                    "short_market_return": short_market_return,
                    "reason": (
                        f"相对 BTC/ETH 弱势 {candidate['weakness']:.2%}，"
                        f"{market_state_reason}；{candidate['quality_reason']}"
                    ),
                })

        unrealized = 0.0
        gross_notional = 0.0
        for symbol, position in positions.items():
            close = float(closes.loc[timestamp, symbol])
            unrealized += position["contracts"] * position["entry_price"] * (
                position["entry_price"] / close - 1
            )
            gross_notional += position["contracts"] * close
        equity = cash + reserved_margin + unrealized
        rows.append({
            "date": pd.to_datetime(timestamp, unit="ms", utc=True),
            "timestamp": timestamp,
            "equity": equity,
            "cash": cash,
            "stock_value": gross_notional,
            "position_count": len(positions),
            "market_return": market_return,
            "short_market_return": short_market_return,
            "signal": "持仓管理" if positions else "等待弱势篮子",
        })

    equity = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    market_columns = [symbol for symbol in config.market_symbols if symbol in closes]
    benchmark_series = None
    if market_columns:
        market_curve = closes[market_columns].mean(axis=1)
        benchmark_series = initial_cash * market_curve / float(market_curve.iloc[0])
    metrics = calculate_backtest_metrics(
        equity,
        trade_frame,
        initial_cash,
        benchmark=benchmark_series,
        periods_per_year=24 * 365,
    )
    metrics["open_positions"] = float(len(positions))
    metrics["basket_count"] = float(basket_id)
    return CryptoBacktestResult(equity, trade_frame, closes.reset_index(), metrics)


__all__ = [
    "AltWeakBasketShortConfig",
    "rank_alt_weakness",
    "run_alt_weak_basket_short_backtest",
]
