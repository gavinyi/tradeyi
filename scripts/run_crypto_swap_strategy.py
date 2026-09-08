from __future__ import annotations

import argparse
import json
import os
import time

from futu_quant.config import refresh_runtime_environment
from futu_quant.crypto_swap import crypto_swap_config_from_env
from futu_quant.crypto_swap_execution import execute_crypto_swap_cycle
from futu_quant.crypto_swap_radar import managed_exit_decision, scan_crypto_swap_radar
from futu_quant.crypto_swap_watchlist import (
    crypto_swap_radar_config_from_env,
    load_swap_radar_managed_symbols,
    load_swap_watchlist,
    promote_swap_radar_symbol,
    remove_swap_watchlist_symbol,
    save_swap_watchlist,
)
from futu_quant.okx_broker import OkxDemoBroker
from futu_quant.okx_config import OkxSettings
from futu_quant.store import TradingStore


def swap_symbols_from_env() -> tuple[str, ...]:
    return load_swap_watchlist()


def run_once() -> None:
    refresh_runtime_environment(override=True)
    settings = OkxSettings.from_env()
    broker = OkxDemoBroker(settings)
    store = TradingStore()
    config = crypto_swap_config_from_env()
    mode = os.getenv("CRYPTO_SWAP_EXECUTION_MODE", "SHADOW").upper()
    symbols = list(load_swap_watchlist())
    radar = crypto_swap_radar_config_from_env()
    managed_symbols = list(load_swap_radar_managed_symbols())
    if radar.enabled:
        inferred = list(managed_symbols)
        for row in store.decisions(limit=2_000):
            if row.get("strategy_id") != "crypto_volatility_swap_radar":
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            symbol = str(row.get("symbol") or "")
            if payload.get("added_to_watchlist") and symbol in symbols and symbol not in inferred:
                inferred.append(symbol)
        if inferred != managed_symbols:
            managed_symbols = inferred
            save_swap_watchlist(symbols, managed_symbols)
        candidates = list(radar.candidates)
        cursor = int(store.get_setting("crypto_swap_radar_cursor", "0") or 0)
        if candidates:
            offset = cursor % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        radar_rows = scan_crypto_swap_radar(
            broker, candidates, symbols, config,
            max_symbols=radar.max_symbols_per_cycle,
            auto_add=radar.auto_add,
            add_symbol=promote_swap_radar_symbol,
            managed_count=len(managed_symbols),
            max_managed_symbols=radar.max_managed_symbols,
            max_abs_funding_rate=radar.max_abs_funding_rate,
            max_spread_ratio=radar.max_spread_ratio,
            full_scan=radar.full_scan_enabled,
            min_auto_add_score=radar.min_auto_add_score,
        )
        store.set_setting(
            "crypto_swap_radar_cursor",
            str(cursor + (len(candidates) if radar.full_scan_enabled else radar.max_symbols_per_cycle)),
        )
        for row in radar_rows:
            store.decision(
                "crypto_volatility_swap_radar", row["symbol"], row["action"],
                row["reason"], {
                    "added_to_watchlist": row["added"],
                    "eligible": row["eligible"], "score": row["score"],
                    "funding_rate": row["funding_rate"],
                    "spread_ratio": row["spread_ratio"],
                    "open_interest": row["open_interest"],
                    "managed_short_score": row.get("managed_short_score", 0),
                    "relative_weakness": row.get("relative_weakness", 0),
                    "volume_ratio": row.get("volume_ratio", 0),
                    "metrics": row["metrics"], "checks": row["checks"],
                },
            )
            if row["added"] and row["symbol"] not in symbols:
                symbols.append(row["symbol"])
                managed_symbols.append(row["symbol"])
    store.heartbeat(
        "okx_crypto_swap_service", "RUNNING",
        f"正在扫描 {len(symbols)} 个永续合约；合约雷达{'已启用' if radar.enabled else '未启用'}",
        os.getpid(),
    )
    cycle_actions: dict[str, str] = {}
    for symbol in symbols:
        try:
            result = execute_crypto_swap_cycle(
                broker, symbol, config, mode, store,
            )
            cycle_actions[symbol] = result.decision.action
            store.decision(
                "crypto_volatility_swap", symbol, result.decision.action,
                result.decision.reason,
                {
                    "mode": result.mode, "metrics": result.decision.metrics,
                    "checks": result.decision.checks, "order": result.order,
                },
            )
        except Exception as exc:
            cycle_actions[symbol] = "BLOCKED"
            store.decision_if_changed(
                "crypto_volatility_swap", symbol, "BLOCKED",
                f"永续合约策略执行异常：{exc}",
                repeat_after_seconds=300,
            )
    if radar.enabled and managed_symbols:
        allocations = {
            row["symbol"] for row in store.virtual_allocations()
            if row["strategy_id"] == "crypto_volatility_swap"
            and float(row.get("quantity") or 0) != 0
        }
        platform_positions: set[str] = set()
        pending_symbols: set[str] = set()
        if mode == "OKX_DEMO" and settings.credentials_configured:
            try:
                platform_positions = {
                    str(row.get("instId")) for row in broker.positions("SWAP")
                    if float(row.get("pos", 0) or 0) != 0
                }
                pending_symbols = {
                    str(row.get("instId")) for row in broker.pending_orders("SWAP")
                    if str(row.get("state", "")).lower() in {"live", "partially_filled"}
                }
            except Exception as exc:
                store.decision_if_changed(
                    "crypto_volatility_swap_radar_lifecycle", None, "BLOCKED",
                    f"合约雷达退出检查失败：{exc}",
                    repeat_after_seconds=300,
                )
        for symbol in tuple(managed_symbols):
            key = f"crypto_swap_radar_inactive_cycles:{symbol}"
            inactive = int(store.get_setting(key, "0") or 0)
            has_position = symbol in allocations or symbol in platform_positions
            next_cycles, remove, reason = managed_exit_decision(
                has_position=has_position,
                has_pending_order=symbol in pending_symbols,
                action=cycle_actions.get(symbol, "BLOCKED"),
                inactive_cycles=inactive,
                exit_after_cycles=radar.exit_after_inactive_cycles,
            )
            store.set_setting(key, str(next_cycles))
            if remove:
                remove_swap_watchlist_symbol(symbol)
                managed_symbols.remove(symbol)
                store.set_setting(key, "0")
            store.decision_if_changed(
                "crypto_volatility_swap_radar_lifecycle", symbol,
                "REMOVED" if remove else "RETAINED", reason,
                {
                    "inactive_cycles": next_cycles,
                    "exit_after_cycles": radar.exit_after_inactive_cycles,
                    "has_position": has_position,
                    "has_pending_order": symbol in pending_symbols,
                },
                repeat_after_seconds=max(
                    60, int(os.getenv("CRYPTO_SWAP_INTERVAL_SECONDS", "300"))
                ),
            )
    store.heartbeat(
        "okx_crypto_swap_service", "IDLE",
        "本轮永续合约扫描完成", os.getpid(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行欧易 Demo 永续合约策略")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--interval", type=int,
        default=int(os.getenv("CRYPTO_SWAP_INTERVAL_SECONDS", "300")),
    )
    args = parser.parse_args()
    while True:
        run_once()
        if not args.loop:
            break
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    main()
