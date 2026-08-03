from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os


@dataclass(frozen=True)
class CryptoProtectionConfig:
    cooldown_hours: float = 6
    max_open_positions: int = 3
    max_total_exposure: float = 3_000
    max_portfolio_drawdown: float = .15
    max_daily_trades: int = 12
    max_consecutive_losses: int = 3
    loss_lock_hours: float = 24


def crypto_protection_config_from_env() -> CryptoProtectionConfig:
    config = CryptoProtectionConfig(
        cooldown_hours=float(os.getenv("CRYPTO_COOLDOWN_HOURS", "6")),
        max_open_positions=int(os.getenv("CRYPTO_MAX_OPEN_POSITIONS", "3")),
        max_total_exposure=float(os.getenv("CRYPTO_MAX_TOTAL_EXPOSURE", "3000")),
        max_portfolio_drawdown=float(os.getenv("CRYPTO_MAX_PORTFOLIO_DRAWDOWN", "0.15")),
        max_daily_trades=int(os.getenv("CRYPTO_MAX_DAILY_TRADES", "12")),
        max_consecutive_losses=int(os.getenv("CRYPTO_MAX_CONSECUTIVE_LOSSES", "3")),
        loss_lock_hours=float(os.getenv("CRYPTO_LOSS_LOCK_HOURS", "24")),
    )
    if config.cooldown_hours < 0 or config.max_open_positions < 1:
        raise ValueError("冷却时间和最大持仓数配置无效")
    if config.max_total_exposure <= 0 or not 0 < config.max_portfolio_drawdown < 1:
        raise ValueError("组合敞口或最大回撤配置无效")
    if config.max_daily_trades < 1 or config.max_consecutive_losses < 1:
        raise ValueError("每日交易次数或连续亏损限制配置无效")
    return config


def entry_protection_reason(store, symbol: str, planned_notional: float, config, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    cooldown = store.get_setting(f"crypto_cooldown_until:{symbol}", "")
    if cooldown:
        try:
            until = datetime.fromisoformat(cooldown)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if now < until:
                return f"标的冷却中，{until.astimezone().strftime('%Y-%m-%d %H:%M')} 后恢复"
        except ValueError:
            pass
    today = now.date().isoformat()
    daily_trades = int(store.get_setting(f"crypto_daily_trade_count:{today}", "0") or 0)
    if daily_trades >= config.max_daily_trades:
        return f"今日交易次数 {daily_trades} 已达到上限 {config.max_daily_trades}"
    consecutive_losses = int(store.get_setting(f"crypto_consecutive_losses:{symbol}", "0") or 0)
    if consecutive_losses >= config.max_consecutive_losses:
        return (
            f"连续亏损 {consecutive_losses} 次达到上限 "
            f"{config.max_consecutive_losses}，标的锁定中"
        )
    allocations = [
        row for row in store.virtual_allocations()
        if row["strategy_id"] == "crypto_volatility_spot" and float(row["quantity"]) > 0
    ]
    if len(allocations) >= config.max_open_positions:
        return f"已达到最大同时持仓数 {config.max_open_positions}"
    exposure = sum(float(row["quantity"]) * float(row["average_cost"]) for row in allocations)
    if exposure + planned_notional > config.max_total_exposure:
        return (
            f"组合计划敞口 ${exposure + planned_notional:,.2f} "
            f"超过上限 ${config.max_total_exposure:,.2f}"
        )
    high = float(store.get_setting("crypto_portfolio_high_watermark", "0") or 0)
    current = float(store.get_setting("crypto_portfolio_equity", "0") or 0)
    if high > 0 and current / high - 1 <= -config.max_portfolio_drawdown:
        return f"组合最大回撤保护已触发：{current / high - 1:.2%}"
    return ""


def start_cooldown(store, symbol: str, hours: float, reason: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(hours=max(0, hours))
    store.set_setting(f"crypto_cooldown_until:{symbol}", until.isoformat())
    store.set_setting(f"crypto_last_exit_reason:{symbol}", reason)


def record_crypto_trade_outcome(store, symbol: str, pnl_ratio: float | None, now=None) -> None:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    count_key = f"crypto_daily_trade_count:{today}"
    store.set_setting(count_key, str(int(store.get_setting(count_key, "0") or 0) + 1))
    if pnl_ratio is None:
        return
    loss_key = f"crypto_consecutive_losses:{symbol}"
    if pnl_ratio < 0:
        losses = int(store.get_setting(loss_key, "0") or 0) + 1
        store.set_setting(loss_key, str(losses))
        if losses >= crypto_protection_config_from_env().max_consecutive_losses:
            start_cooldown(
                store, symbol,
                crypto_protection_config_from_env().loss_lock_hours,
                f"连续亏损 {losses} 次触发标的锁定",
            )
    else:
        store.set_setting(loss_key, "0")


def reconcile_okx_demo_fills(broker, store) -> list[dict]:
    fills = [
        item for item in broker.fills()
        if str(item.get("clOrdId", "")).startswith("cq")
    ]
    grouped: dict[str, dict] = {}
    for fill in fills:
        symbol = str(fill.get("instId", ""))
        quantity = float(fill.get("fillSz", 0) or 0)
        price = float(fill.get("fillPx", 0) or 0)
        side = str(fill.get("side", "")).lower()
        row = grouped.setdefault(symbol, {"quantity": 0.0, "buy_cost": 0.0, "buy_qty": 0.0})
        row["quantity"] += quantity if side == "buy" else -quantity
        if side == "buy":
            row["buy_cost"] += quantity * price
            row["buy_qty"] += quantity
    results = []
    for symbol, row in grouped.items():
        quantity = max(0.0, row["quantity"])
        average = row["buy_cost"] / row["buy_qty"] if row["buy_qty"] else 0.0
        if quantity > 0:
            store.set_virtual_allocation(
                "crypto_volatility_spot", symbol, quantity, average,
            )
        else:
            store.delete_virtual_allocation("crypto_volatility_spot", symbol)
        results.append({"symbol": symbol, "quantity": quantity, "average_cost": average})
    return results
