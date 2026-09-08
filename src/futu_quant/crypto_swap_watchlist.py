from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable

from .config import save_runtime_settings
from .crypto_swap_models import normalize_swap_symbol


DEFAULT_SWAP_WATCHLIST = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
DEFAULT_SWAP_RADAR_UNIVERSE = (
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP",
    "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
)


def normalize_swap_symbols(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        symbol = normalize_swap_symbol(value)
        if symbol not in result:
            result.append(symbol)
    return tuple(result)


def _env_symbols(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, ",".join(defaults))
    return normalize_swap_symbols(item for item in raw.split(",") if item.strip())


def load_swap_watchlist() -> tuple[str, ...]:
    return _env_symbols("CRYPTO_SWAP_SYMBOLS", DEFAULT_SWAP_WATCHLIST)


def load_swap_radar_managed_symbols() -> tuple[str, ...]:
    return _env_symbols("CRYPTO_SWAP_RADAR_MANAGED_SYMBOLS", ())


def save_swap_watchlist(
    symbols: Iterable[str], managed_symbols: Iterable[str] | None = None,
) -> tuple[str, ...]:
    normalized = normalize_swap_symbols(symbols)
    values = {"CRYPTO_SWAP_SYMBOLS": ",".join(normalized)}
    if managed_symbols is not None:
        managed = tuple(
            symbol for symbol in normalize_swap_symbols(managed_symbols)
            if symbol in normalized
        )
        values["CRYPTO_SWAP_RADAR_MANAGED_SYMBOLS"] = ",".join(managed)
    save_runtime_settings(values)
    return normalized


def add_swap_watchlist_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_swap_symbol(symbol)
    managed = tuple(
        item for item in load_swap_radar_managed_symbols() if item != target
    )
    return save_swap_watchlist((*load_swap_watchlist(), target), managed)


def promote_swap_radar_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_swap_symbol(symbol)
    return save_swap_watchlist(
        (*load_swap_watchlist(), target),
        (*load_swap_radar_managed_symbols(), target),
    )


def remove_swap_watchlist_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_swap_symbol(symbol)
    return save_swap_watchlist(
        (item for item in load_swap_watchlist() if item != target),
        (item for item in load_swap_radar_managed_symbols() if item != target),
    )


@dataclass(frozen=True)
class CryptoSwapRadarConfig:
    enabled: bool = False
    auto_add: bool = False
    candidates: tuple[str, ...] = DEFAULT_SWAP_RADAR_UNIVERSE
    max_symbols_per_cycle: int = 5
    max_managed_symbols: int = 8
    exit_after_inactive_cycles: int = 24
    max_abs_funding_rate: float = 0.001
    max_spread_ratio: float = 0.002
    full_scan_enabled: bool = True
    min_auto_add_score: float = 60.0


def crypto_swap_radar_config_from_env() -> CryptoSwapRadarConfig:
    return CryptoSwapRadarConfig(
        enabled=os.getenv("CRYPTO_SWAP_RADAR_ENABLED", "false").lower() == "true",
        auto_add=os.getenv("CRYPTO_SWAP_RADAR_AUTO_ADD", "false").lower() == "true",
        candidates=_env_symbols(
            "CRYPTO_SWAP_RADAR_UNIVERSE", DEFAULT_SWAP_RADAR_UNIVERSE,
        ),
        max_symbols_per_cycle=max(
            1, min(50, int(os.getenv("CRYPTO_SWAP_RADAR_MAX_PER_CYCLE", "5")))
        ),
        max_managed_symbols=max(
            1, min(50, int(os.getenv("CRYPTO_SWAP_RADAR_MAX_MANAGED", "8")))
        ),
        exit_after_inactive_cycles=max(
            2, min(10_000, int(os.getenv("CRYPTO_SWAP_RADAR_EXIT_CYCLES", "24")))
        ),
        max_abs_funding_rate=max(
            0.00001, float(os.getenv("CRYPTO_SWAP_MAX_ABS_FUNDING_RATE", "0.001"))
        ),
        max_spread_ratio=max(
            0.00001, float(os.getenv("CRYPTO_SWAP_MAX_SPREAD_RATIO", "0.002"))
        ),
        full_scan_enabled=os.getenv(
            "CRYPTO_SWAP_RADAR_FULL_SCAN_ENABLED", "true"
        ).lower() == "true",
        min_auto_add_score=max(
            0.0, min(100.0, float(os.getenv("CRYPTO_SWAP_RADAR_MIN_AUTO_ADD_SCORE", "60")))
        ),
    )
