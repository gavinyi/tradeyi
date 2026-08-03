from __future__ import annotations

import argparse
import json
import os
import time

from futu_quant.config import refresh_runtime_environment
from futu_quant.crypto_execution import execute_crypto_cycle
from futu_quant.crypto_protections import reconcile_okx_demo_fills
from futu_quant.crypto_radar import managed_exit_decision, scan_crypto_radar
from futu_quant.crypto_volatility import crypto_volatility_config_from_env
from futu_quant.crypto_watchlist import (
    crypto_radar_config_from_env,
    load_crypto_radar_managed_symbols,
    load_crypto_watchlist,
    promote_crypto_radar_symbol,
    remove_crypto_watchlist_symbol,
    save_crypto_watchlist,
)
from futu_quant.okx_broker import OkxDemoBroker
from futu_quant.okx_config import OkxSettings
from futu_quant.store import TradingStore


def run_once() -> None:
    refresh_runtime_environment(override=True)
    settings = OkxSettings.from_env()
    broker = OkxDemoBroker(settings)
    store = TradingStore()
    symbols = list(load_crypto_watchlist())
    mode = os.getenv("CRYPTO_EXECUTION_MODE", "SHADOW").upper()
    config = crypto_volatility_config_from_env()
    radar = crypto_radar_config_from_env()
    managed_symbols = list(load_crypto_radar_managed_symbols())
    if radar.enabled:
        inferred_managed = list(managed_symbols)
        for row in store.decisions(limit=2_000):
            if row.get("strategy_id") != "crypto_volatility_radar":
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            symbol = str(row.get("symbol") or "")
            if (
                payload.get("added_to_watchlist")
                and symbol in symbols
                and symbol not in inferred_managed
            ):
                inferred_managed.append(symbol)
        if inferred_managed != managed_symbols:
            managed_symbols = inferred_managed
            save_crypto_watchlist(symbols, managed_symbols)
        candidates = list(radar.candidates)
        cursor = int(store.get_setting("crypto_radar_cursor", "0") or 0)
        if candidates:
            offset = cursor % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        radar_rows = scan_crypto_radar(
            broker, candidates, symbols, config,
            max_symbols=radar.max_symbols_per_cycle,
            auto_add=radar.auto_add,
            add_symbol=promote_crypto_radar_symbol,
            managed_count=len(managed_symbols),
            max_managed_symbols=radar.max_managed_symbols,
            full_scan=radar.full_scan_enabled,
            min_auto_add_score=radar.min_auto_add_score,
        )
        store.set_setting(
            "crypto_radar_cursor",
            str(cursor + (len(candidates) if radar.full_scan_enabled else radar.max_symbols_per_cycle)),
        )
        for row in radar_rows:
            store.decision(
                "crypto_volatility_radar", row["symbol"], row["action"], row["reason"],
                {
                    "added_to_watchlist": row["added"],
                    "eligible": row.get("eligible", False),
                    "score": row.get("score", 0),
                    "spread_ratio": row.get("spread_ratio", 0),
                    "signal_strength": row["signal_strength"],
                    "metrics": row["metrics"], "checks": row["checks"],
                },
            )
            if row["added"] and row["symbol"] not in symbols:
                symbols.append(row["symbol"])
                managed_symbols.append(row["symbol"])
    if mode == "OKX_DEMO" and settings.credentials_configured:
        reconcile_okx_demo_fills(broker, store)
    store.heartbeat(
        "okx_crypto_service", "RUNNING",
        f"正在扫描 {len(symbols)} 个策略自选；自动雷达{'已启用' if radar.enabled else '未启用'}",
        os.getpid(),
    )
    cycle_actions: dict[str, str] = {}
    for symbol in symbols:
        try:
            result = execute_crypto_cycle(broker, symbol, config, mode, store)
            cycle_actions[symbol] = result.decision.action
            store.decision(
                "crypto_volatility_spot", symbol, result.decision.action,
                result.decision.reason,
                {
                    "mode": result.mode, "metrics": result.decision.metrics,
                    "checks": result.decision.checks, "order": result.order,
                },
            )
        except Exception as exc:
            cycle_actions[symbol] = "BLOCKED"
            store.decision_if_changed(
                "crypto_volatility_spot", symbol, "BLOCKED", f"币策略执行异常：{exc}"
                , repeat_after_seconds=300
            )
    if radar.enabled and managed_symbols:
        allocations = {
            row["symbol"] for row in store.virtual_allocations()
            if row["strategy_id"] == "crypto_volatility_spot"
            and float(row.get("quantity") or 0) > 0
        }
        pending_symbols: set[str] = set()
        if mode == "OKX_DEMO" and settings.credentials_configured:
            try:
                pending_symbols = {
                    str(row.get("instId")) for row in broker.pending_orders()
                    if str(row.get("state", "")).lower() in {"live", "partially_filled"}
                }
            except Exception as exc:
                store.decision(
                    "crypto_volatility_radar_lifecycle", None, "BLOCKED",
                    f"雷达退出检查无法读取待成交订单：{exc}",
                )
        for symbol in tuple(managed_symbols):
            counter_key = f"crypto_radar_inactive_cycles:{symbol}"
            inactive_cycles = int(store.get_setting(counter_key, "0") or 0)
            next_cycles, should_remove, reason = managed_exit_decision(
                has_position=symbol in allocations,
                has_pending_order=symbol in pending_symbols,
                action=cycle_actions.get(symbol, "BLOCKED"),
                inactive_cycles=inactive_cycles,
                exit_after_cycles=radar.exit_after_inactive_cycles,
            )
            store.set_setting(counter_key, str(next_cycles))
            if should_remove:
                remove_crypto_watchlist_symbol(symbol)
                managed_symbols.remove(symbol)
                store.set_setting(counter_key, "0")
            store.decision_if_changed(
                "crypto_volatility_radar_lifecycle", symbol,
                "REMOVED" if should_remove else "RETAINED", reason,
                {
                    "inactive_cycles": next_cycles,
                    "exit_after_cycles": radar.exit_after_inactive_cycles,
                    "has_position": symbol in allocations,
                    "has_pending_order": symbol in pending_symbols,
                },
                repeat_after_seconds=max(60, int(os.getenv("CRYPTO_INTERVAL_SECONDS", "300"))),
            )
    try:
        if mode == "OKX_DEMO" and settings.credentials_configured:
            account = broker.account_balance()
            equity = float(account[0].get("totalEq", 0) or 0) if account else 0.0
        else:
            allocations = [
                row for row in store.virtual_allocations()
                if row["strategy_id"] == "crypto_volatility_spot"
                and float(row["quantity"]) > 0
            ]
            cost = sum(
                float(row["quantity"]) * float(row["average_cost"]) for row in allocations
            )
            market_value = sum(
                float(row["quantity"])
                * float(broker.ticker(row["symbol"]).get("last", 0) or 0)
                for row in allocations
            )
            equity = market_value / cost if cost > 0 else 0.0
        if equity > 0:
            high = float(store.get_setting("crypto_portfolio_high_watermark", "0") or 0)
            store.set_setting("crypto_portfolio_equity", str(equity))
            store.set_setting("crypto_portfolio_high_watermark", str(max(high, equity)))
    except Exception as exc:
        store.decision_if_changed(
            "crypto_volatility_spot", None, "BLOCKED", f"组合权益更新失败：{exc}",
            repeat_after_seconds=300,
        )
    store.heartbeat("okx_crypto_service", "IDLE", "本轮币策略扫描完成", os.getpid())


def main() -> None:
    parser = argparse.ArgumentParser(description="运行欧易币波动率策略（默认 Shadow）")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--interval", type=int, default=int(os.getenv("CRYPTO_INTERVAL_SECONDS", "300")),
    )
    args = parser.parse_args()
    while True:
        run_once()
        if not args.loop:
            break
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    main()
