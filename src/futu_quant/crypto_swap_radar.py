from __future__ import annotations

from collections.abc import Callable, Iterable

from .crypto_radar import managed_exit_decision
from .crypto_swap import CryptoSwapConfig, decide_crypto_swap
from .crypto_swap_managed_futures import (
    decide_crypto_swap_managed_futures,
    managed_futures_config_from_env,
)
from .crypto_swap_watchlist import normalize_swap_symbols


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rank_swap_candidate(
    decision, ticker: dict, funding: dict | None, open_interest: dict | None,
    *, max_abs_funding_rate: float, max_spread_ratio: float,
) -> dict:
    bid = _number(ticker.get("bidPx"))
    ask = _number(ticker.get("askPx"))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else _number(ticker.get("last"))
    spread = (ask - bid) / mid if mid > 0 and ask >= bid > 0 else 1.0
    funding_rate = _number((funding or {}).get("fundingRate"))
    oi = _number((open_interest or {}).get("oi"))
    metrics = decision.metrics or {}
    managed_score = metrics.get("managed_short_score")
    expansion = _number(metrics.get("volatility_expansion"))
    zscore = abs(_number(metrics.get("zscore")))
    volume_ratio = _number(metrics.get("volume_ratio"))
    relative_weakness = _number(metrics.get("relative_weakness"))
    active = decision.action in {"OPEN_LONG", "OPEN_SHORT"}
    funding_ok = abs(funding_rate) <= max_abs_funding_rate
    spread_ok = spread <= max_spread_ratio
    if managed_score is not None:
        score = (
            min(75.0, _number(managed_score) * 0.75)
            + min(10.0, max(0.0, -relative_weakness) * 350)
            + (7.5 if spread_ok else 0.0)
            + (7.5 if funding_ok else 0.0)
        )
    else:
        score = (
            min(30.0, expansion * 15)
            + min(30.0, zscore * 10)
            + min(20.0, volume_ratio * 8)
            + (10.0 if spread_ok else 0.0)
            + (10.0 if funding_ok else 0.0)
        )
    eligible = active and funding_ok and spread_ok
    blockers = []
    if not active:
        blockers.append("入场信号未激活")
        failed_checks = [
            check for check in (decision.checks or [])
            if not bool(check.get("passed"))
        ][:3]
        blockers.extend(
            f"{check.get('name', '检查项')}未通过：{check.get('current', '')}，要求 {check.get('target', '')}"
            for check in failed_checks
        )
    if not funding_ok:
        blockers.append(
            f"资金费率绝对值 {abs(funding_rate):.4%} 超过 {max_abs_funding_rate:.4%}"
        )
    if not spread_ok:
        blockers.append(f"盘口价差 {spread:.3%} 超过 {max_spread_ratio:.3%}")
    return {
        "symbol": ticker.get("instId", ""),
        "action": decision.action,
        "reason": "；".join(blockers) if blockers else decision.reason,
        "eligible": eligible,
        "score": round(score, 2),
        "funding_rate": funding_rate,
        "spread_ratio": spread,
        "open_interest": oi,
        "managed_short_score": _number(managed_score),
        "relative_weakness": relative_weakness,
        "volume_ratio": volume_ratio,
        "metrics": metrics,
        "checks": decision.checks,
        "added": False,
    }


def scan_crypto_swap_radar(
    broker,
    candidates: Iterable[str],
    watchlist: Iterable[str],
    strategy_config: CryptoSwapConfig,
    *,
    max_symbols: int = 5,
    auto_add: bool = False,
    add_symbol: Callable[[str], object] | None = None,
    managed_count: int = 0,
    max_managed_symbols: int = 8,
    max_abs_funding_rate: float = 0.001,
    max_spread_ratio: float = 0.002,
    full_scan: bool = False,
    min_auto_add_score: float = 0.0,
) -> list[dict]:
    watched = set(normalize_swap_symbols(watchlist))
    all_pending = [
        symbol for symbol in normalize_swap_symbols(candidates)
        if symbol not in watched
    ]
    pending = all_pending if full_scan else all_pending[:max(1, max_symbols)]
    rows: list[dict] = []
    for symbol in pending:
        try:
            higher_timeframe_bars = (
                broker.candles(
                    symbol, bar=strategy_config.higher_timeframe_bar,
                    limit=min(300, max(strategy_config.higher_timeframe_min_bars + 5, 100)),
                )
                if strategy_config.higher_timeframe_enabled else None
            )
            mf_config = managed_futures_config_from_env()
            one_hour_bars = broker.candles(symbol, limit=300)
            if mf_config.engine == "MANAGED_FUTURES":
                decision = decide_crypto_swap_managed_futures(
                    symbol,
                    one_hour_bars,
                    0,
                    0,
                    strategy_config,
                    mf_config,
                    funding_rate=_number(broker.funding_rate(symbol).get("fundingRate")),
                    higher_timeframe_bars=higher_timeframe_bars,
                    daily_bars=broker.candles(symbol, bar="1D", limit=220),
                    benchmark_bars=(
                        broker.candles(mf_config.benchmark_symbol, limit=300)
                        if symbol != mf_config.benchmark_symbol else one_hour_bars
                    ),
                )
            else:
                decision = decide_crypto_swap(
                    one_hour_bars, 0, 0, strategy_config,
                    higher_timeframe_bars=higher_timeframe_bars,
                )
            row = rank_swap_candidate(
                decision, broker.ticker(symbol), broker.funding_rate(symbol),
                broker.open_interest(symbol),
                max_abs_funding_rate=max_abs_funding_rate,
                max_spread_ratio=max_spread_ratio,
            )
            row["symbol"] = symbol
            rows.append(row)
        except Exception as exc:
            rows.append({
                "symbol": symbol, "action": "BLOCKED", "eligible": False,
                "score": 0.0, "reason": f"合约雷达扫描异常：{exc}",
                "funding_rate": 0.0, "spread_ratio": 0.0,
                "open_interest": 0.0, "metrics": {}, "checks": [], "added": False,
            })
    rows.sort(key=lambda row: (bool(row["eligible"]), float(row["score"])), reverse=True)
    add_limit = max(1, max_symbols)
    added_this_cycle = 0
    for row in rows:
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
            row["reason"] += f"；雷达托管上限 {max_managed_symbols} 个已满"
            continue
        add_symbol(row["symbol"])
        row["added"] = True
        managed_count += 1
        added_this_cycle += 1
    return rows


__all__ = ["managed_exit_decision", "rank_swap_candidate", "scan_crypto_swap_radar"]
