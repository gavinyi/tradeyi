from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from dotenv import dotenv_values, set_key

from .filelock import exclusive_lock


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
ENV_FILE = DEFAULT_ENV_FILE
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
PROFILE_BUDGET_DEFAULTS = {
    "conservative": (0.55, 0.15),
    "balanced": (0.65, 0.15),
    "aggressive": (0.70, 0.20),
}


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_dumps(value) -> str:
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, default=_json_default,
    )


# Windows caps the entire environment block at 32767 characters, and a single
# variable must stay well under that.  Campaign/configuration payloads mirrored
# into .env routinely exceed it (e.g. a 46KB+ VOLATILITY_CAMPAIGNS value that is
# fine on macOS).  SQLite runtime_config is the runtime authority, so oversized
# mirror values are skipped for os.environ while still loading from the database.
_ENV_MAX_SAFE_VALUE = 30000


def _set_env(key: str, value: str) -> None:
    """Set *key* in os.environ, skipping values that cannot fit on Windows."""
    if len(value) > _ENV_MAX_SAFE_VALUE:
        os.environ.pop(key, None)
        return
    try:
        os.environ[key] = value
    except ValueError:
        # Cumulative overflow of the whole environment block: drop this one.
        os.environ.pop(key, None)


def _apply_dotenv(override: bool = False) -> None:
    """Load .env into os.environ without tripping Windows' environment limit."""
    for key, value in dotenv_values(ENV_FILE).items():
        if value is None:
            continue
        if override or key not in os.environ:
            _set_env(key, str(value))


def _mirror_env(key: str, value: str) -> None:
    """Keep .env as a compatibility mirror; SQLite is the runtime authority."""
    ENV_FILE.touch(exist_ok=True)
    lock_path = ENV_FILE.with_suffix(".env.lock")
    try:
        import time
        with exclusive_lock(lock_path, blocking=True):
            for attempt in range(3):
                try:
                    set_key(str(ENV_FILE), key, value, quote_mode="never")
                    break
                except (PermissionError, OSError):
                    if attempt == 2:
                        raise
                    time.sleep(0.1)
    except Exception as exc:
        import logging
        logging.getLogger("futu_quant.config").warning(
            "镜像写入 .env 失败（SQLite 权威配置已生效，不影响运行）：%s", exc
        )
    _set_env(key, value)


def _load_runtime_value(key: str, default):
    # Isolated tests and alternate deployments may deliberately point at another file.
    if ENV_FILE != DEFAULT_ENV_FILE:
        raw = dotenv_values(ENV_FILE).get(key)
        try:
            return json.loads(raw) if raw is not None else default
        except (TypeError, json.JSONDecodeError):
            return default
    from .store import TradingStore
    store = TradingStore()
    saved = store.get_runtime_config(key)
    if saved is not None:
        return saved[0]
    raw = dotenv_values(ENV_FILE).get(key)
    if raw is None:
        value = default
    else:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            value = default
    store.set_runtime_config(key, value, "env_migration")
    return value


def normalize_us_symbol(value: str) -> str:
    """Accept AAPL or US.AAPL and return the Futu symbol form."""
    symbol = value.strip().upper()
    if symbol.startswith("US."):
        symbol = symbol[3:]
    # 兼容历史配置里被错误压平的 Berkshire B 股代码。富途要求保留点号。
    if symbol == "BRKB":
        symbol = "BRK.B"
    elif symbol.startswith(("HK.", "SH.", "SZ.", "JP.", "SG.")):
        raise ValueError("请输入有效美股代码，例如 AAPL、BRK.B 或 US.TSLA")
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("请输入有效美股代码，例如 AAPL、BRK.B 或 US.TSLA")
    return f"US.{symbol}"


def save_trading_preferences(
    watchlist: tuple[str, ...], quantities: dict[str, int] | int
) -> None:
    if not watchlist:
        raise ValueError("至少保留一个标的")
    normalized = tuple(dict.fromkeys(normalize_us_symbol(symbol) for symbol in watchlist))
    if isinstance(quantities, int):
        quantity_map = {symbol: quantities for symbol in normalized}
    else:
        quantity_map = {
            symbol: int(quantities.get(symbol, 1)) for symbol in normalized
        }
    if any(not 1 <= quantity <= 10_000 for quantity in quantity_map.values()):
        raise ValueError("每个标的的下单数量必须在 1 到 10000 股之间")
    values = {
        "WATCHLIST": ",".join(normalized),
        "ORDER_QUANTITIES": json_dumps(quantity_map),
    }
    save_runtime_settings(values)


def load_option_contracts() -> list[dict]:
    _apply_dotenv()
    try:
        value = json.loads(os.getenv("OPTION_CONTRACTS", "[]"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("OPTION_CONTRACTS must be valid JSON") from exc
    return value if isinstance(value, list) else []


def save_option_contracts(contracts: list[dict]) -> None:
    value = json_dumps(contracts)
    save_runtime_settings({"OPTION_CONTRACTS": value})


def save_core_satellite_profile(profile: str) -> None:
    if profile not in {"conservative", "balanced", "aggressive"}:
        raise ValueError("无效的 Core Satellite 参数档案")
    save_runtime_settings({"CORE_SAT_PROFILE": profile})


def save_core_satellite_settings(
    profile: str, frequency: str, tolerance: float, min_trade_usd: float,
    core_budget: float = 0.65, satellite_budget: float = 0.15,
    cash_buffer: float = 0.0, max_single_rebalance_pct: float = 0.50,
    max_order_notional: float = 50_000.0,
    high_volatility_threshold: float = 0.025,
    high_volatility_trade_factor: float = 0.50,
    satellite_daily_scan: bool = True,
    satellite_min_score: float = 4.0,
    max_target_change: float = 0.05,
) -> None:
    save_core_satellite_profile(profile)
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("无效的调仓频率")
    if not 0 <= tolerance <= 0.20 or min_trade_usd < 0:
        raise ValueError("容忍带或最低交易金额无效")
    if not 0 <= core_budget <= 0.85:
        raise ValueError("Core 预算必须在 0% 到 85% 之间")
    if not 0 <= satellite_budget <= 0.50:
        raise ValueError("Satellite 预算必须在 0% 到 50% 之间")
    if not 0 <= cash_buffer <= 0.50:
        raise ValueError("现金缓冲必须在 0% 到 50% 之间")
    if core_budget + satellite_budget + cash_buffer > 0.95:
        raise ValueError("Core + Satellite + 现金缓冲合计不能超过 95%")
    if not 0.05 <= max_single_rebalance_pct <= 1.0:
        raise ValueError("单次调仓比例必须在 5% 到 100% 之间")
    if max_order_notional < 0:
        raise ValueError("单笔计划金额不能为负数")
    if not 0 <= high_volatility_threshold <= 0.20:
        raise ValueError("高波动阈值必须在 0% 到 20% 之间")
    if not 0.05 <= high_volatility_trade_factor <= 1.0:
        raise ValueError("高波动降速比例必须在 5% 到 100% 之间")
    if not 0 <= satellite_min_score <= 6.5:
        raise ValueError("Satellite 机会门槛必须在 0 到 6.5 分之间")
    if not 0.01 <= max_target_change <= 0.20:
        raise ValueError("单次目标权重变化上限必须在 1% 到 20% 之间")
    values = {
        "CORE_SAT_REBALANCE_FREQUENCY": frequency,
        "CORE_SAT_WEIGHT_TOLERANCE": str(tolerance),
        "CORE_SAT_MIN_TRADE_USD": str(min_trade_usd),
        "CORE_SAT_CORE_BUDGET": str(core_budget),
        "CORE_SAT_SATELLITE_BUDGET": str(satellite_budget),
        "CORE_SAT_CASH_BUFFER": str(cash_buffer),
        "CORE_SAT_MAX_SINGLE_REBALANCE_PCT": str(max_single_rebalance_pct),
        "CORE_SAT_MAX_ORDER_NOTIONAL": str(max_order_notional),
        "CORE_SAT_HIGH_VOL_DAILY_STD": str(high_volatility_threshold),
        "CORE_SAT_HIGH_VOL_TRADE_FACTOR": str(high_volatility_trade_factor),
        "CORE_SAT_SATELLITE_DAILY_SCAN": "true" if satellite_daily_scan else "false",
        "CORE_SAT_SATELLITE_MIN_SCORE": str(satellite_min_score),
        "CORE_SAT_MAX_TARGET_CHANGE": str(max_target_change),
    }
    save_runtime_settings(values)


def save_core_satellite_symbols(symbols: tuple[str, ...]) -> None:
    normalized = tuple(dict.fromkeys(normalize_us_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("Core Satellite 至少需要一个标的")
    value = ",".join(normalized)
    save_runtime_settings({"CORE_SAT_SYMBOLS": value})


def save_gamma_symbols(symbols: tuple[str, ...]) -> None:
    normalized = tuple(dict.fromkeys(normalize_us_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("财报 Gamma 策略至少需要一个标的")
    value = ",".join(normalized)
    save_runtime_settings({"GAMMA_SYMBOLS": value})


def save_volatility_symbols(symbols: tuple[str, ...]) -> None:
    normalized = tuple(dict.fromkeys(normalize_us_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("波动率期权策略至少需要一个标的")
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().set_runtime_config("VOLATILITY_SYMBOLS", list(normalized), "streamlit_or_service")
    value = ",".join(normalized)
    _mirror_env("VOLATILITY_SYMBOLS", value)


DEFAULT_VOLATILITY_SYMBOLS: tuple[str, ...] = (
    "US.COIN", "US.AMD", "US.NVDA", "US.AVGO", "US.AAPL",
    "US.META", "US.AMZN", "US.GOOG", "US.MSFT", "US.TSM",
    "US.QQQ", "US.SPY", "US.GLD",
)


def load_volatility_symbols() -> tuple[str, ...]:
    value = _load_runtime_value("VOLATILITY_SYMBOLS", DEFAULT_VOLATILITY_SYMBOLS)
    if isinstance(value, str):
        value = value.split(",")
    base_symbols = tuple(str(symbol).strip().upper() for symbol in value if str(symbol).strip())
    symbols = list(base_symbols or DEFAULT_VOLATILITY_SYMBOLS)

    # 动态并集扩展: 核心底仓 (13只) ∪ 当前有效晋级外援 (promoted_universe)
    try:
        if ENV_FILE == DEFAULT_ENV_FILE:
            from .store import TradingStore
            store = TradingStore()
            promoted_rows = store.active_promoted_symbols()
            if promoted_rows:
                symbols.extend(r["symbol"] for r in promoted_rows if r.get("symbol"))
    except Exception:
        pass

    return tuple(dict.fromkeys(symbols))


def load_volatility_radar_budgets() -> dict:
    value = _load_runtime_value(
        "VOLATILITY_RADAR_BUDGETS", {"default": 1000.0, "symbols": {}},
    )
    if not isinstance(value, dict):
        value = {}
    try:
        default = float(value.get("default", 1000.0))
    except (TypeError, ValueError):
        default = 1000.0
    raw_symbols = value.get("symbols", {})
    symbols = {}
    if isinstance(raw_symbols, dict):
        for symbol, budget in raw_symbols.items():
            try:
                normalized = normalize_us_symbol(str(symbol))
                amount = float(budget)
            except (TypeError, ValueError):
                continue
            if 1 <= amount <= 1_000_000:
                symbols[normalized] = amount
    return {"default": max(1.0, min(default, 1_000_000.0)), "symbols": symbols}


def save_volatility_radar_budgets(
    default_budget: float, symbol_budgets: dict[str, float],
) -> dict:
    default = float(default_budget)
    if not 1 <= default <= 1_000_000:
        raise ValueError("自动雷达默认权利金预算必须在 1 到 1,000,000 美元之间")
    normalized = {}
    for symbol, budget in symbol_budgets.items():
        amount = float(budget)
        if not 1 <= amount <= 1_000_000:
            raise ValueError(f"{symbol} 的权利金预算超出有效范围")
        normalized[normalize_us_symbol(symbol)] = amount
    value = {"default": default, "symbols": normalized}
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().set_runtime_config(
            "VOLATILITY_RADAR_BUDGETS", value, "streamlit_or_service",
        )
    _mirror_env("VOLATILITY_RADAR_BUDGETS", json_dumps(value))
    return value


def resolve_volatility_radar_budget(symbol: str) -> float:
    budgets = load_volatility_radar_budgets()
    normalized = normalize_us_symbol(symbol)
    return float(budgets["symbols"].get(normalized, budgets["default"]))


def load_gamma_campaigns() -> list[dict]:
    # Read the file directly on every scheduling cycle. ``load_dotenv`` does not
    # overwrite an existing process environment by default, which caused deleted
    # campaigns to remain active until the trading service was restarted.
    value = _load_runtime_value("GAMMA_CAMPAIGNS", [])
    return value if isinstance(value, list) else []


def save_gamma_campaigns(campaigns: list[dict]) -> None:
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().set_runtime_config("GAMMA_CAMPAIGNS", campaigns, "streamlit_or_service")
    value = json_dumps(campaigns)
    _mirror_env("GAMMA_CAMPAIGNS", value)


def load_volatility_campaigns() -> list[dict]:
    value = _load_runtime_value("VOLATILITY_CAMPAIGNS", [])
    return value if isinstance(value, list) else []


def save_volatility_campaigns(campaigns: list[dict]) -> None:
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().set_runtime_config("VOLATILITY_CAMPAIGNS", campaigns, "streamlit_or_service")
    value = json_dumps(campaigns)
    _mirror_env("VOLATILITY_CAMPAIGNS", value)


def upsert_campaign(config_key: str, campaign: dict) -> list[dict]:
    """Atomically add/replace one campaign without overwriting concurrent changes."""
    from .store import TradingStore
    identity = campaign.get("campaign_id", campaign.get("code"))
    def update(current):
        rows = [item for item in current if item.get("campaign_id", item.get("code")) != identity]
        rows.append(campaign)
        return rows
    value, _ = TradingStore().mutate_runtime_config(
        config_key, [], update, "streamlit_or_service",
    )
    _mirror_env(config_key, json_dumps(value))
    return value


def delete_campaign(config_key: str, identity: str) -> list[dict]:
    from .store import TradingStore
    value, _ = TradingStore().mutate_runtime_config(
        config_key, [],
        lambda current: [item for item in current
                         if item.get("campaign_id", item.get("code")) != identity],
        "streamlit_or_service",
    )
    _mirror_env(config_key, json_dumps(value))
    return value


def update_volatility_symbol(symbol: str, add: bool) -> tuple[str, ...]:
    from .store import TradingStore
    normalized = normalize_us_symbol(symbol)
    def update(current):
        values = {str(item).upper() for item in current}
        values.add(normalized) if add else values.discard(normalized)
        return sorted(values)
    value, _ = TradingStore().mutate_runtime_config(
        "VOLATILITY_SYMBOLS", [], update, "streamlit_or_service",
    )
    _mirror_env("VOLATILITY_SYMBOLS", ",".join(value))
    return tuple(value)


def save_runtime_settings(values: dict[str, str]) -> None:
    """Persist validated operational settings from the monitoring page."""
    normalized = {str(key): str(value) for key, value in values.items()}
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().mutate_runtime_config(
            "ENV_SETTINGS", {}, lambda current: {**current, **normalized},
            "streamlit_or_service",
        )
    for key, value in values.items():
        _mirror_env(key, str(value))


def save_real_trading_settings(
    trading_env: str,
    enabled: bool,
    account_id: str,
    allowed_strategies: list[str] | tuple[str, ...],
    max_order_notional: float,
    max_daily_notional: float,
    require_manual_confirm: bool = True,
) -> None:
    """Persist guarded live-trading configuration.

    This intentionally stores only non-secret controls. Futu trade passwords must
    remain outside the app and be unlocked in OpenD.
    """
    env = str(trading_env).upper()
    if env not in {"SIMULATE", "REAL"}:
        raise RuntimeError("交易环境只能是 SIMULATE 或 REAL")
    normalized_strategies = ",".join(
        sorted({str(item).strip() for item in allowed_strategies if str(item).strip()})
    )
    if float(max_order_notional) <= 0 or float(max_daily_notional) <= 0:
        raise RuntimeError("实盘额度必须大于 0")
    if float(max_order_notional) > float(max_daily_notional):
        raise RuntimeError("实盘单笔额度不能超过每日额度")
    if env == "REAL":
        if not enabled:
            raise RuntimeError("切换 REAL 前必须开启实盘总开关")
        if not str(account_id).strip():
            raise RuntimeError("切换 REAL 前必须填写 REAL_ACCOUNT_ID")
        if not normalized_strategies:
            raise RuntimeError("切换 REAL 前必须至少选择一个实盘策略白名单")
    save_runtime_settings({
        "FUTU_TRADING_ENV": env,
        "REAL_TRADING_ENABLED": "true" if enabled else "false",
        "REAL_ACCOUNT_ID": str(account_id).strip(),
        "REAL_ALLOWED_STRATEGIES": normalized_strategies,
        "REAL_MAX_ORDER_NOTIONAL": f"{float(max_order_notional):.2f}",
        "REAL_MAX_DAILY_NOTIONAL": f"{float(max_daily_notional):.2f}",
        "REAL_REQUIRE_MANUAL_CONFIRM": "true" if require_manual_confirm else "false",
    })


def refresh_runtime_environment(override: bool = False) -> dict[str, str]:
    """Load file + SQLite dynamic settings into the current process."""
    values = {key: str(value) for key, value in dotenv_values(ENV_FILE).items() if value is not None}
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        store = TradingStore()
        saved = store.get_runtime_config("ENV_SETTINGS")
        if saved is None:
            dynamic_prefixes = (
                "CORE_", "GAMMA_", "VOLATILITY_", "RISK_", "EXEC_", "STRATEGY_",
                "APPROVAL_", "SHADOW_", "SERVICE_", "ORDER_", "EARNINGS_", "REAL_",
            )
            explicit = {
                "WATCHLIST", "ORDER_QUANTITIES", "ORDER_QUANTITY", "OPTION_CONTRACTS",
                "ENABLE_PAPER_ORDERS", "EXECUTION_MODE", "ENFORCE_VIRTUAL_POSITIONS",
                "REQUIRE_STRATEGY_APPROVAL", "DATA_RETENTION_DAYS", "BACKUP_RETENTION_DAYS",
                "FUTU_TRADING_ENV",
            }
            migrated = {
                key: value for key, value in values.items()
                if key in explicit or key.startswith(dynamic_prefixes)
            }
            store.set_runtime_config("ENV_SETTINGS", migrated, "env_migration")
            saved = (migrated, 1)
        if saved is not None and isinstance(saved[0], dict):
            values.update({key: str(value) for key, value in saved[0].items()})
    for key, value in values.items():
        if override or key not in os.environ:
            _set_env(key, value)
    return values


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    security_firm: str
    trading_env: str
    market: str
    acc_id: int | None
    real_trading_enabled: bool
    real_account_id: int | None
    real_allowed_strategies: tuple[str, ...]
    real_max_order_notional: float
    real_max_daily_notional: float
    real_require_manual_confirm: bool
    enable_paper_orders: bool
    watchlist: tuple[str, ...]
    order_quantity: int
    order_quantities: dict[str, int]
    interval_seconds: int
    core_satellite_max_stock_weight: float
    core_satellite_min_trade_usd: float
    core_satellite_profile: str
    core_satellite_rebalance_frequency: str
    core_satellite_weight_tolerance: float
    core_satellite_core_budget: float
    core_satellite_satellite_budget: float
    core_satellite_cash_buffer: float
    core_satellite_max_single_rebalance_pct: float
    core_satellite_max_order_notional: float
    core_satellite_high_volatility_threshold: float
    core_satellite_high_volatility_trade_factor: float
    core_satellite_satellite_daily_scan: bool
    core_satellite_satellite_min_score: float
    core_satellite_max_target_change: float
    core_satellite_symbols: tuple[str, ...]
    gamma_symbols: tuple[str, ...]
    volatility_symbols: tuple[str, ...]
    ml_enabled: bool
    ml_model_path: str
    ml_threshold: float
    ml_weight: float

    @classmethod
    def load(cls, refresh: bool = False) -> "Settings":
        _apply_dotenv(override=refresh)
        refresh_runtime_environment(override=refresh)
        env = os.getenv("FUTU_TRADING_ENV", "SIMULATE").upper()
        market = os.getenv("FUTU_MARKET", "US").upper()
        if env not in {"SIMULATE", "REAL"}:
            raise RuntimeError("FUTU_TRADING_ENV must be SIMULATE or REAL")
        if market != "US":
            raise RuntimeError("This project is locked to the US paper market")
        raw_acc_id = os.getenv("FUTU_ACC_ID", "").strip()
        raw_real_account_id = os.getenv("REAL_ACCOUNT_ID", "").strip()
        real_enabled = os.getenv("REAL_TRADING_ENABLED", "false").lower() == "true"
        real_allowed = tuple(
            item.strip() for item in os.getenv("REAL_ALLOWED_STRATEGIES", "").split(",")
            if item.strip()
        )
        real_max_order = float(os.getenv("REAL_MAX_ORDER_NOTIONAL", "500"))
        real_max_daily = float(os.getenv("REAL_MAX_DAILY_NOTIONAL", "1000"))
        real_require_confirm = (
            os.getenv("REAL_REQUIRE_MANUAL_CONFIRM", "true").lower() == "true"
        )
        if real_max_order <= 0 or real_max_daily <= 0:
            raise RuntimeError("REAL_MAX_ORDER_NOTIONAL and REAL_MAX_DAILY_NOTIONAL must be positive")
        if real_max_order > real_max_daily:
            raise RuntimeError("REAL_MAX_ORDER_NOTIONAL cannot exceed REAL_MAX_DAILY_NOTIONAL")
        if env == "REAL":
            if not real_enabled:
                raise RuntimeError("实盘环境已选择，但 REAL_TRADING_ENABLED 未开启")
            if not raw_real_account_id:
                raise RuntimeError("实盘环境必须配置 REAL_ACCOUNT_ID")
            if not real_allowed:
                raise RuntimeError("实盘环境必须配置 REAL_ALLOWED_STRATEGIES 策略白名单")
        watchlist = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("WATCHLIST", "US.SPY,US.QQQ,US.AAPL").split(",")
            if symbol.strip()
        )
        if any(not symbol.startswith("US.") for symbol in watchlist):
            raise RuntimeError("Every WATCHLIST symbol must start with US.")
        default_quantity = max(1, int(os.getenv("ORDER_QUANTITY", "1")))
        try:
            saved_quantities = json.loads(os.getenv("ORDER_QUANTITIES", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("ORDER_QUANTITIES must be valid JSON") from exc
        order_quantities = {
            symbol: max(1, int(saved_quantities.get(symbol, default_quantity)))
            for symbol in watchlist
        }
        max_stock_weight = float(os.getenv("CORE_SAT_MAX_STOCK_WEIGHT", "0.70"))
        if not 0 < max_stock_weight <= 0.85:
            raise RuntimeError("CORE_SAT_MAX_STOCK_WEIGHT must be between 0 and 0.85")
        min_trade_usd = float(os.getenv("CORE_SAT_MIN_TRADE_USD", "500"))
        if min_trade_usd < 0:
            raise RuntimeError("CORE_SAT_MIN_TRADE_USD cannot be negative")
        profile = os.getenv("CORE_SAT_PROFILE", "balanced").lower()
        if profile not in {"conservative", "balanced", "aggressive"}:
            raise RuntimeError("CORE_SAT_PROFILE must be conservative, balanced, or aggressive")
        frequency = os.getenv("CORE_SAT_REBALANCE_FREQUENCY", "weekly").lower()
        if frequency not in {"daily", "weekly", "monthly"}:
            raise RuntimeError("CORE_SAT_REBALANCE_FREQUENCY must be daily, weekly, or monthly")
        tolerance = float(os.getenv("CORE_SAT_WEIGHT_TOLERANCE", "0.02"))
        if not 0 <= tolerance <= 0.20:
            raise RuntimeError("CORE_SAT_WEIGHT_TOLERANCE must be between 0 and 0.20")
        core_budget = float(
            os.getenv("CORE_SAT_CORE_BUDGET", str(PROFILE_BUDGET_DEFAULTS[profile][0]))
        )
        if not 0 <= core_budget <= 0.85:
            raise RuntimeError("CORE_SAT_CORE_BUDGET must be between 0 and 0.85")
        satellite_budget = float(
            os.getenv("CORE_SAT_SATELLITE_BUDGET", str(PROFILE_BUDGET_DEFAULTS[profile][1]))
        )
        if not 0 <= satellite_budget <= 0.50:
            raise RuntimeError("CORE_SAT_SATELLITE_BUDGET must be between 0 and 0.50")
        cash_buffer = float(os.getenv("CORE_SAT_CASH_BUFFER", "0.00"))
        if not 0 <= cash_buffer <= 0.50:
            raise RuntimeError("CORE_SAT_CASH_BUFFER must be between 0 and 0.50")
        if core_budget + satellite_budget + cash_buffer > 0.95:
            raise RuntimeError("CORE_SAT budgets plus cash buffer must not exceed 0.95")
        max_single_rebalance_pct = float(os.getenv("CORE_SAT_MAX_SINGLE_REBALANCE_PCT", "0.50"))
        if not 0.05 <= max_single_rebalance_pct <= 1.0:
            raise RuntimeError("CORE_SAT_MAX_SINGLE_REBALANCE_PCT must be between 0.05 and 1.0")
        max_order_notional = float(os.getenv("CORE_SAT_MAX_ORDER_NOTIONAL", "50000"))
        if max_order_notional < 0:
            raise RuntimeError("CORE_SAT_MAX_ORDER_NOTIONAL cannot be negative")
        high_volatility_threshold = float(os.getenv("CORE_SAT_HIGH_VOL_DAILY_STD", "0.025"))
        if not 0 <= high_volatility_threshold <= 0.20:
            raise RuntimeError("CORE_SAT_HIGH_VOL_DAILY_STD must be between 0 and 0.20")
        high_volatility_trade_factor = float(os.getenv("CORE_SAT_HIGH_VOL_TRADE_FACTOR", "0.50"))
        if not 0.05 <= high_volatility_trade_factor <= 1.0:
            raise RuntimeError("CORE_SAT_HIGH_VOL_TRADE_FACTOR must be between 0.05 and 1.0")
        satellite_daily_scan = (
            os.getenv("CORE_SAT_SATELLITE_DAILY_SCAN", "true").lower() == "true"
        )
        satellite_min_score = float(os.getenv("CORE_SAT_SATELLITE_MIN_SCORE", "4.0"))
        if not 0 <= satellite_min_score <= 6.5:
            raise RuntimeError("CORE_SAT_SATELLITE_MIN_SCORE must be between 0 and 6.5")
        max_target_change = float(os.getenv("CORE_SAT_MAX_TARGET_CHANGE", "0.05"))
        if not 0.01 <= max_target_change <= 0.20:
            raise RuntimeError("CORE_SAT_MAX_TARGET_CHANGE must be between 0.01 and 0.20")
        core_symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("CORE_SAT_SYMBOLS", ",".join(watchlist)).split(",")
            if symbol.strip()
        )
        if not core_symbols or any(not symbol.startswith("US.") for symbol in core_symbols):
            raise RuntimeError("CORE_SAT_SYMBOLS must contain US.* symbols")
        gamma_symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("GAMMA_SYMBOLS", "US.NVDA").split(",")
            if symbol.strip()
        )
        if not gamma_symbols or any(not symbol.startswith("US.") for symbol in gamma_symbols):
            raise RuntimeError("GAMMA_SYMBOLS must contain US.* symbols")
        volatility_symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("VOLATILITY_SYMBOLS", ",".join(gamma_symbols)).split(",")
            if symbol.strip()
        )
        if not volatility_symbols or any(not symbol.startswith("US.") for symbol in volatility_symbols):
            raise RuntimeError("VOLATILITY_SYMBOLS must contain US.* symbols")
        ml_enabled = os.getenv("ML_ENABLED", "false").lower() == "true"
        ml_model_path = os.getenv("ML_MODEL_PATH", "data/ml_models/volatility_scorer_v3.pkl")
        ml_threshold = float(os.getenv("ML_THRESHOLD", "0.5"))
        if not 0 <= ml_threshold <= 1.0:
            raise RuntimeError("ML_THRESHOLD must be between 0 and 1")
        ml_weight = float(os.getenv("ML_WEIGHT", "0.6"))
        if not 0 <= ml_weight <= 1.0:
            raise RuntimeError("ML_WEIGHT must be between 0 and 1")
        return cls(
            host=os.getenv("FUTU_HOST", "127.0.0.1"),
            port=int(os.getenv("FUTU_PORT", "11111")),
            security_firm=os.getenv("FUTU_SECURITY_FIRM", "FUTUSECURITIES").upper(),
            trading_env=env,
            market=market,
            acc_id=int(raw_acc_id) if raw_acc_id else None,
            real_trading_enabled=real_enabled,
            real_account_id=int(raw_real_account_id) if raw_real_account_id else None,
            real_allowed_strategies=real_allowed,
            real_max_order_notional=real_max_order,
            real_max_daily_notional=real_max_daily,
            real_require_manual_confirm=real_require_confirm,
            enable_paper_orders=os.getenv("ENABLE_PAPER_ORDERS", "false").lower() == "true",
            watchlist=watchlist,
            order_quantity=default_quantity,
            order_quantities=order_quantities,
            interval_seconds=max(60, int(os.getenv("STRATEGY_INTERVAL_SECONDS", "300"))),
            core_satellite_max_stock_weight=max_stock_weight,
            core_satellite_min_trade_usd=min_trade_usd,
            core_satellite_profile=profile,
            core_satellite_rebalance_frequency=frequency,
            core_satellite_weight_tolerance=tolerance,
            core_satellite_core_budget=core_budget,
            core_satellite_satellite_budget=satellite_budget,
            core_satellite_cash_buffer=cash_buffer,
            core_satellite_max_single_rebalance_pct=max_single_rebalance_pct,
            core_satellite_max_order_notional=max_order_notional,
            core_satellite_high_volatility_threshold=high_volatility_threshold,
            core_satellite_high_volatility_trade_factor=high_volatility_trade_factor,
            core_satellite_satellite_daily_scan=satellite_daily_scan,
            core_satellite_satellite_min_score=satellite_min_score,
            core_satellite_max_target_change=max_target_change,
            core_satellite_symbols=core_symbols,
            gamma_symbols=gamma_symbols,
            volatility_symbols=volatility_symbols,
            ml_enabled=ml_enabled,
            ml_model_path=ml_model_path,
            ml_threshold=ml_threshold,
            ml_weight=ml_weight,
        )

    def quantity_for(self, symbol: str) -> int:
        return self.order_quantities.get(symbol, self.order_quantity)
