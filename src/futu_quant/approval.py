from __future__ import annotations

import hashlib
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GLOBAL_PREFIXES = ("EXEC_", "RISK_", "STRATEGY_")
DEFAULT_APPROVAL_EXEMPT_STRATEGIES = ("core_satellite",)


def strategy_requires_approval(strategy_id: str) -> bool:
    """Return whether PAPER BUY orders need a current-version approval."""
    configured = os.getenv(
        "APPROVAL_EXEMPT_STRATEGIES", ",".join(DEFAULT_APPROVAL_EXEMPT_STRATEGIES)
    )
    exempt = {value.strip() for value in configured.split(",") if value.strip()}
    return strategy_id not in exempt


def strategy_fingerprint(strategy_id: str) -> str:
    from .config import refresh_runtime_environment
    from .store import TradingStore
    refresh_runtime_environment()
    digest = hashlib.sha256(strategy_id.encode())
    strategy_prefixes = {
        "core_satellite": ("CORE_",),
        "earnings_gamma": ("GAMMA_",),
        "earnings_gamma_manual": ("GAMMA_",),
        "volatility_long_gamma": ("VOLATILITY_",),
    }.get(strategy_id, ())
    for key, value in sorted(os.environ.items()):
        if key.startswith((*GLOBAL_PREFIXES, *strategy_prefixes)):
            digest.update(f"{key}={value}\n".encode())
    store = TradingStore()
    config_keys = {
        "earnings_gamma": ("GAMMA_CAMPAIGNS",),
        "earnings_gamma_manual": ("GAMMA_CAMPAIGNS",),
        "volatility_long_gamma": ("VOLATILITY_CAMPAIGNS", "VOLATILITY_SYMBOLS"),
    }.get(strategy_id, ())
    for key in config_keys:
        value = store.get_runtime_config(key)
        if value is not None:
            import json
            digest.update(
                f"{key}={json.dumps(value[0], sort_keys=True, separators=(',', ':'), default=str)}\n".encode()
            )
    for relative in (
        "src/futu_quant/strategies.py", "src/futu_quant/core_satellite.py",
        "src/futu_quant/gamma_automation.py", "src/futu_quant/execution.py",
        "src/futu_quant/risk.py", "src/futu_quant/gamma_risk.py",
        "src/futu_quant/option_signal.py", "src/futu_quant/volatility_optimizer.py",
        "src/futu_quant/volatility_roll.py", "src/futu_quant/volatility_scanner.py",
        "src/futu_quant/earnings_optimizer.py", "src/futu_quant/config.py",
        "src/futu_quant/strategy_plugins.py", "src/futu_quant/experiments.py",
        "src/futu_quant/attribution.py", "src/futu_quant/portfolio_allocator.py",
        "src/futu_quant/stress.py", "src/futu_quant/option_lifecycle.py",
        "src/futu_quant/option_history.py", "src/futu_quant/option_event_backtest.py",
        "scripts/run_strategies.py", "scripts/run_gamma_strategy.py",
        "scripts/run_volatility_strategy.py",
    ):
        path = ROOT / relative
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()
