from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal

from .crypto_volatility import CryptoVolatilityConfig, decide_crypto_volatility
from .crypto_watchlist import normalize_crypto_symbols


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rank_spot_candidate(
    decision,
    ticker: dict,
    *,
    max_spread_ratio: float = 0.003,
) -> dict:
    bid = _number(ticker.get("bidPx"))
    ask = _number(ticker.get("askPx"))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else _number(ticker.get("last"))
    spread = (ask - bid) / mid if mid > 0 and ask >= bid > 0 else 1.0
    metrics = decision.metrics or {}
    expansion = _number(metrics.get("volatility_expansion"))
    zscore = abs(_number(metrics.get("zscore")))
    volume_ratio = _number(metrics.get("volume_ratio"))
    breakout = _number(metrics.get("breakout"))
    active = decision.action == "BUY"
    spread_ok = spread <= max_spread_ratio
    score = (
        min(30.0, expansion * 15)
        + min(25.0, zscore * 10)
        + min(20.0, volume_ratio * 8)
        + min(15.0, breakout * 15)
        + (10.0 if spread_ok else 0.0)
    )
    eligible = active and spread_ok
    blockers = []
    if not active:
        blockers.append("入场信号未激活")
    if not spread_ok:
        blockers.append(f"盘口价差 {spread:.3%} 超过 {max_spread_ratio:.3%}")
    return {
        "action": decision.action,
        "reason": "；".join(blockers) if blockers else decision.reason,
        "eligible": eligible,
        "score": round(score, 2),
        "spread_ratio": spread,
        "metrics": metrics,
        "checks": decision.checks,
        "signal_strength": decision.signal_strength,
        "added": False,
    }


def scan_crypto_radar(
    broker,
    candidates: Iterable[str],
    watchlist: Iterable[str],
    config: CryptoVolatilityConfig,
    *,
    max_symbols: int = 5,
    auto_add: bool = False,
    add_symbol: Callable[[str], object] | None = None,
    managed_count: int = 0,
    max_managed_symbols: int = 10,
    max_spread_ratio: float = 0.003,
    full_scan: bool = False,
    min_auto_add_score: float = 0.0,
) -> list[dict]:
    """Scan configured candidates and optionally promote active signals to the watchlist."""
    watched = set(normalize_crypto_symbols(watchlist))
    all_pending = [
        symbol for symbol in normalize_crypto_symbols(candidates)
        if symbol not in watched
    ]
    pending = all_pending if full_scan else all_pending[:max(1, max_symbols)]
    results: list[dict] = []
    for symbol in pending:
        try:
            bars = broker.candles(symbol, limit=300)
            higher_timeframe_bars = (
                broker.candles(
                    symbol, bar=config.higher_timeframe_bar,
                    limit=min(300, max(config.higher_timeframe_min_bars + 5, 100)),
                )
                if config.higher_timeframe_enabled else None
            )
            decision = decide_crypto_volatility(
                bars, position_size=Decimal("0"), config=config,
                higher_timeframe_bars=higher_timeframe_bars,
            )
            row = rank_spot_candidate(
                decision, broker.ticker(symbol), max_spread_ratio=max_spread_ratio,
            )
            reason = row["reason"]
            row.update({"symbol": symbol, "reason": reason, "added": False})
            results.append(row)
        except Exception as exc:
            results.append({
                "symbol": symbol, "action": "BLOCKED",
                "reason": f"雷达扫描异常：{exc}", "signal_strength": "异常",
                "eligible": False, "score": 0.0, "spread_ratio": 0.0,
                "added": False, "metrics": {}, "checks": [],
            })
    results.sort(
        key=lambda row: (
            bool(row.get("eligible")),
            float(row.get("score", 0) or 0),
        ),
        reverse=True,
    )
    add_limit = max(1, max_symbols)
    added_this_cycle = 0
    for row in results:
        if not row["eligible"] or not auto_add or add_symbol is None:
            continue
        if float(row.get("score", 0) or 0) < min_auto_add_score:
            row["eligible"] = False
            row["reason"] = (
                f"{row['reason']}；候选评分 {float(row.get('score', 0) or 0):.1f} "
                f"低于自动加入门槛 {min_auto_add_score:.1f}"
            )
            continue
        if added_this_cycle >= add_limit:
            row["reason"] += f"；本轮自动加入上限 {add_limit} 个已满"
            continue
        if managed_count >= max_managed_symbols:
            row["reason"] = (
                f"{row['reason']}；雷达托管上限 {max_managed_symbols} 个已满，"
                "本次未自动加入"
            )
            continue
        add_symbol(row["symbol"])
        watched.add(row["symbol"])
        managed_count += 1
        added_this_cycle += 1
        row["added"] = True
    return results


def managed_exit_decision(
    *,
    has_position: bool,
    has_pending_order: bool,
    action: str,
    inactive_cycles: int,
    exit_after_cycles: int,
) -> tuple[int, bool, str]:
    if has_position:
        return 0, False, "仍有策略持仓，继续保留在策略自选"
    if has_pending_order:
        return 0, False, "仍有待成交订单，继续保留在策略自选"
    if str(action).upper() == "BUY":
        return 0, False, "当前入场信号仍有效，继续保留在策略自选"
    next_cycles = max(0, int(inactive_cycles)) + 1
    if next_cycles >= max(2, int(exit_after_cycles)):
        return (
            next_cycles, True,
            f"连续 {next_cycles} 轮无有效信号且无持仓/待成交，自动退出策略自选",
        )
    return (
        next_cycles, False,
        f"连续 {next_cycles}/{max(2, int(exit_after_cycles))} 轮无有效信号，继续观察",
    )
