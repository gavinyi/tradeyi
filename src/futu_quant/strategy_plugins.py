from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class StrategyPlugin:
    strategy_id: str
    service_name: str
    display_name: str
    asset_class: str
    universe_provider: str
    signal_provider: str
    position_model: str
    exit_model: str
    risk_profile: str
    backtest_adapter: str
    execution_adapter: str
    runner_key: str | None = None
    approval_required: bool = True
    automatically_scheduled: bool = True

    def as_record(self) -> dict:
        return asdict(self)


PLUGINS = (
    StrategyPlugin(
        "core_satellite", "Core Satellite", "Core Satellite 核心-卫星", "STOCK_ETF",
        "configured_core_symbols", "core_regime_and_weights", "target_weight_rebalance",
        "tolerance_or_regime_change", "core_portfolio", "core_satellite_backtest",
        "unified_limit_execution", approval_required=False,
    ),
    StrategyPlugin(
        "earnings_gamma", "Earnings Gamma", "财报 Gamma", "OPTION",
        "earnings_campaigns", "earnings_event_gamma", "premium_staging",
        "profit_stop_trailing_or_timeout", "long_gamma", "option_event_backtest",
        "unified_limit_execution",
    ),
    StrategyPlugin(
        "volatility_long_gamma", "Volatility Long Gamma", "波动率期权", "OPTION",
        "volatility_symbols", "dynamic_volatility_regime", "premium_staging",
        "profit_stop_trailing_or_roll", "long_gamma", "option_event_backtest",
        "unified_limit_execution",
    ),
    StrategyPlugin(
        "earnings_gamma_manual", "Earnings Gamma Manual", "财报 Gamma 手动单", "OPTION",
        "manual_contract", "manual_decision", "manual_quantity",
        "manual_or_shared_exit", "long_gamma", "option_event_backtest",
        "unified_limit_execution", automatically_scheduled=False,
    ),
    StrategyPlugin(
        "SMA Cross", "SMA Cross", "均线交叉策略", "STOCK_ETF",
        "watchlist", "sma_cross", "fixed_quantity", "opposite_signal",
        "stock_directional", "stock_event_backtest", "unified_limit_execution",
        runner_key="SMA Cross",
    ),
    StrategyPlugin(
        "RSI Mean Reversion", "RSI Mean Reversion", "RSI 均值回归策略", "STOCK_ETF",
        "watchlist", "rsi_reversion", "fixed_quantity", "opposite_signal",
        "stock_directional", "stock_event_backtest", "unified_limit_execution",
        runner_key="RSI Reversion",
    ),
    StrategyPlugin(
        "20 Day Breakout", "20 Day Breakout", "20日突破策略", "STOCK_ETF",
        "watchlist", "breakout_20d", "fixed_quantity", "opposite_signal",
        "stock_directional", "stock_event_backtest", "unified_limit_execution",
        runner_key="20-day Breakout",
    ),
)

BY_ID = {plugin.strategy_id: plugin for plugin in PLUGINS}
BY_SERVICE_NAME = {plugin.service_name: plugin for plugin in PLUGINS}


def strategy_plugins(scheduled_only: bool = False) -> tuple[StrategyPlugin, ...]:
    if scheduled_only:
        return tuple(plugin for plugin in PLUGINS if plugin.automatically_scheduled)
    return PLUGINS


def validate_plugins() -> None:
    if len(BY_ID) != len(PLUGINS) or len(BY_SERVICE_NAME) != len(PLUGINS):
        raise ValueError("策略插件 ID 或服务名称重复")
    required = (
        "universe_provider", "signal_provider", "position_model", "exit_model",
        "risk_profile", "backtest_adapter", "execution_adapter",
    )
    for plugin in PLUGINS:
        if any(not getattr(plugin, field) for field in required):
            raise ValueError(f"{plugin.strategy_id} 插件协议不完整")


validate_plugins()
