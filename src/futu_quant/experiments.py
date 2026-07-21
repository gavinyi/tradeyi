from __future__ import annotations

import os

from .approval import strategy_fingerprint
from .market_data_store import CanonicalMarketDataStore
from .qlib_adapter import QlibResearchAdapter
from .strategy_plugins import strategy_plugins


def _universe(plugin, settings) -> list[str]:
    if plugin.strategy_id == "core_satellite":
        return list(settings.core_satellite_symbols)
    if plugin.strategy_id in {"earnings_gamma", "earnings_gamma_manual"}:
        return list(settings.gamma_symbols)
    if plugin.strategy_id == "volatility_long_gamma":
        return list(settings.volatility_symbols)
    return list(settings.watchlist)


def _parameters(plugin, store) -> dict:
    prefixes = {
        "core_satellite": ("CORE_",),
        "earnings_gamma": ("GAMMA_",),
        "earnings_gamma_manual": ("GAMMA_",),
        "volatility_long_gamma": ("VOLATILITY_",),
    }.get(plugin.strategy_id, ("STRATEGY_",))
    values = {
        key: value for key, value in os.environ.items()
        if key.startswith((*prefixes, "RISK_", "EXEC_"))
    }
    budget = store.strategy_risk_budget(plugin.strategy_id)
    if budget:
        values["risk_budget"] = budget
    return values


def current_data_version(store) -> str:
    coverage = store.option_history_coverage()
    samples = sum(int(row["sample_count"]) for row in coverage)
    good = sum(int(row["good_samples"]) for row in coverage)
    equity = store.equity_history(limit=1)
    equity_time = equity[-1]["created_at"] if equity else "none"
    market_catalog = CanonicalMarketDataStore().catalog()
    if market_catalog.empty:
        market_version = "none"
    else:
        versions = sorted(market_catalog["dataset_version"].astype(str).unique())
        market_version = f"{int(market_catalog['row_count'].sum())}:{'|'.join(versions)}"
    return (
        f"option-snapshots:{samples}:good:{good}:equity:{equity_time}:"
        f"market:{market_version}"
    )


def research_environment() -> dict:
    status = QlibResearchAdapter().status()
    return {
        "qlib_available": status.available,
        "qlib_version": status.version,
        "qlib_message": status.message,
        "signals_executable": False,
    }


def sync_strategy_experiments(store, settings) -> list[dict]:
    data_version = current_data_version(store)
    records = []
    for plugin in strategy_plugins():
        fingerprint = strategy_fingerprint(plugin.strategy_id)
        experiment_id = store.register_experiment(
            plugin.strategy_id, fingerprint, fingerprint[:12],
            _parameters(plugin, store), _universe(plugin, settings), data_version,
            status="CURRENT_CODE",
        )
        if store.active_experiment(plugin.strategy_id) is None:
            store.promote_experiment(experiment_id)
        records.append({
            "strategy_id": plugin.strategy_id, "experiment_id": experiment_id,
            "version_hash": fingerprint,
        })
    return records
