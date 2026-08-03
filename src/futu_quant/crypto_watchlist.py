from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable

from .config import save_runtime_settings
from .crypto_models import normalize_crypto_symbol


DEFAULT_CRYPTO_WATCHLIST = ("BTC-USDT", "ETH-USDT")
DEFAULT_CRYPTO_RADAR_UNIVERSE = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT",
)


def normalize_crypto_symbols(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        symbol = normalize_crypto_symbol(value)
        if symbol not in normalized:
            normalized.append(symbol)
    return tuple(normalized)


def _env_symbols(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, ",".join(defaults))
    return normalize_crypto_symbols(item for item in raw.split(",") if item.strip())


def load_crypto_watchlist() -> tuple[str, ...]:
    return _env_symbols("CRYPTO_VOLATILITY_SYMBOLS", DEFAULT_CRYPTO_WATCHLIST)


def load_crypto_radar_managed_symbols() -> tuple[str, ...]:
    return _env_symbols("CRYPTO_RADAR_MANAGED_SYMBOLS", ())


def save_crypto_watchlist(
    symbols: Iterable[str], managed_symbols: Iterable[str] | None = None,
) -> tuple[str, ...]:
    normalized = normalize_crypto_symbols(symbols)
    values = {"CRYPTO_VOLATILITY_SYMBOLS": ",".join(normalized)}
    if managed_symbols is not None:
        managed = tuple(
            symbol for symbol in normalize_crypto_symbols(managed_symbols)
            if symbol in normalized
        )
        values["CRYPTO_RADAR_MANAGED_SYMBOLS"] = ",".join(managed)
    save_runtime_settings(values)
    return normalized


def add_crypto_watchlist_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_crypto_symbol(symbol)
    managed = tuple(
        item for item in load_crypto_radar_managed_symbols() if item != target
    )
    return save_crypto_watchlist((*load_crypto_watchlist(), target), managed)


def promote_crypto_radar_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_crypto_symbol(symbol)
    return save_crypto_watchlist(
        (*load_crypto_watchlist(), target),
        (*load_crypto_radar_managed_symbols(), target),
    )


def pin_crypto_watchlist_symbol(symbol: str) -> tuple[str, ...]:
    return add_crypto_watchlist_symbol(symbol)


def remove_crypto_watchlist_symbol(symbol: str) -> tuple[str, ...]:
    target = normalize_crypto_symbol(symbol)
    return save_crypto_watchlist(
        (item for item in load_crypto_watchlist() if item != target),
        (item for item in load_crypto_radar_managed_symbols() if item != target),
    )


@dataclass(frozen=True)
class CryptoRadarConfig:
    enabled: bool = False
    auto_add: bool = False
    candidates: tuple[str, ...] = DEFAULT_CRYPTO_RADAR_UNIVERSE
    max_symbols_per_cycle: int = 5
    max_managed_symbols: int = 10
    exit_after_inactive_cycles: int = 24
    full_scan_enabled: bool = True
    min_auto_add_score: float = 60.0


def crypto_radar_config_from_env() -> CryptoRadarConfig:
    candidates = _env_symbols("CRYPTO_RADAR_UNIVERSE", DEFAULT_CRYPTO_RADAR_UNIVERSE)
    limit = max(1, min(50, int(os.getenv("CRYPTO_RADAR_MAX_SYMBOLS_PER_CYCLE", "5"))))
    max_managed = max(1, min(100, int(os.getenv("CRYPTO_RADAR_MAX_MANAGED_SYMBOLS", "10"))))
    exit_cycles = max(
        2, min(10_000, int(os.getenv("CRYPTO_RADAR_EXIT_INACTIVE_CYCLES", "24")))
    )
    return CryptoRadarConfig(
        enabled=os.getenv("CRYPTO_RADAR_ENABLED", "false").lower() == "true",
        auto_add=os.getenv("CRYPTO_RADAR_AUTO_ADD", "false").lower() == "true",
        candidates=candidates,
        max_symbols_per_cycle=limit,
        max_managed_symbols=max_managed,
        exit_after_inactive_cycles=exit_cycles,
        full_scan_enabled=os.getenv(
            "CRYPTO_RADAR_FULL_SCAN_ENABLED", "true"
        ).lower() == "true",
        min_auto_add_score=max(
            0.0, min(100.0, float(os.getenv("CRYPTO_RADAR_MIN_AUTO_ADD_SCORE", "60")))
        ),
    )
