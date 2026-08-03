from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .store import TradingStore


ROOT = Path(__file__).resolve().parents[2]
GLOBAL_PREFIXES = ("EXEC_", "RISK_", "STRATEGY_")
DEFAULT_APPROVAL_EXEMPT_STRATEGIES = ("core_satellite",)

# These values are live strategy state, not a strategy release.  The volatility
# radar updates them whenever it adds a symbol, creates a campaign, or rolls a
# contract. Including them in the release fingerprint made a valid approval
# expire immediately after the first automatic campaign was generated.
MUTABLE_RUNTIME_KEYS = {
    "VOLATILITY_CAMPAIGNS",
    "VOLATILITY_SYMBOLS",
    "VOLATILITY_RADAR_BUDGETS",
    "GAMMA_CAMPAIGNS",
}


def strategy_requires_approval(strategy_id: str) -> bool:
    """Return whether PAPER BUY orders need a current-version approval."""
    configured = os.getenv(
        "APPROVAL_EXEMPT_STRATEGIES", ",".join(DEFAULT_APPROVAL_EXEMPT_STRATEGIES)
    )
    exempt = {value.strip() for value in configured.split(",") if value.strip()}
    return strategy_id not in exempt


def strategy_fingerprint(strategy_id: str) -> str:
    from .config import refresh_runtime_environment
    refresh_runtime_environment()
    digest = hashlib.sha256(strategy_id.encode())
    strategy_prefixes = {
        "core_satellite": ("CORE_",),
        "earnings_gamma": ("GAMMA_",),
        "earnings_gamma_manual": ("GAMMA_",),
        "volatility_long_gamma": ("VOLATILITY_",),
    }.get(strategy_id, ())
    for key, value in sorted(os.environ.items()):
        if (
            key not in MUTABLE_RUNTIME_KEYS
            and key.startswith((*GLOBAL_PREFIXES, *strategy_prefixes))
        ):
            digest.update(f"{key}={value}\n".encode())
    store = TradingStore()
    # A manually bumped policy revision is stable across campaign lifecycle
    # changes while still allowing risk/model policy releases to invalidate an
    # approval deliberately.
    policy_revision = store.get_setting(
        f"strategy_policy_revision:{strategy_id}", "1"
    )
    digest.update(f"POLICY_REVISION={policy_revision}\n".encode())
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
