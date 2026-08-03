from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class CryptoSwapRiskConfig:
    max_open_positions: int = 3
    max_total_margin: float = 1_500.0
    max_margin_per_position: float = 500.0
    max_leverage: float = 3.0


def crypto_swap_risk_config_from_env() -> CryptoSwapRiskConfig:
    config = CryptoSwapRiskConfig(
        max_open_positions=int(os.getenv("CRYPTO_SWAP_MAX_OPEN_POSITIONS", "3")),
        max_total_margin=float(os.getenv("CRYPTO_SWAP_MAX_TOTAL_MARGIN", "1500")),
        max_margin_per_position=float(
            os.getenv("CRYPTO_SWAP_MAX_MARGIN_PER_POSITION", "500")
        ),
        max_leverage=float(os.getenv("CRYPTO_SWAP_MAX_LEVERAGE", "3")),
    )
    if config.max_open_positions < 1 or config.max_total_margin <= 0:
        raise ValueError("合约最大持仓数或总保证金上限无效")
    if config.max_margin_per_position <= 0 or not 1 <= config.max_leverage <= 3:
        raise ValueError("合约单仓保证金或杠杆安全上限无效")
    return config


def swap_entry_block_reason(store, symbol: str, margin: float, leverage: float, config) -> str:
    if leverage > config.max_leverage:
        return f"计划杠杆 {leverage:.1f} 倍超过安全上限 {config.max_leverage:.1f} 倍"
    if margin > config.max_margin_per_position:
        return (
            f"计划保证金 ${margin:,.2f} 超过单仓上限 "
            f"${config.max_margin_per_position:,.2f}"
        )
    positions = [
        row for row in store.virtual_allocations()
        if row["strategy_id"] == "crypto_volatility_swap"
        and float(row.get("quantity") or 0) != 0
    ]
    same_symbol = any(row["symbol"] == symbol for row in positions)
    if not same_symbol and len(positions) >= config.max_open_positions:
        return f"已达到合约最大持仓数 {config.max_open_positions}"
    current_symbol_margin = float(store.get_setting(f"crypto_swap_margin:{symbol}", "0") or 0)
    if current_symbol_margin + margin > config.max_margin_per_position:
        return (
            f"{symbol} 合约保证金将达到 ${current_symbol_margin + margin:,.2f}，"
            f"超过单仓上限 ${config.max_margin_per_position:,.2f}"
        )
    current_margin = sum(
        float(store.get_setting(f"crypto_swap_margin:{row['symbol']}", "0") or 0)
        for row in positions
    )
    if current_margin + margin > config.max_total_margin:
        return (
            f"合约总保证金将达到 ${current_margin + margin:,.2f}，"
            f"超过上限 ${config.max_total_margin:,.2f}"
        )
    return ""
