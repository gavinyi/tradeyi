from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, cast

from dotenv import dotenv_values, set_key

from .filelock import exclusive_lock
from .tenant import (
    RADAR_BUDGETS_KEY,
    VOLATILITY_CAMPAIGNS_KEY,
    TenantConfigKeyError,
    campaigns_config_key,
    radar_budgets_config_key,
)

if TYPE_CHECKING:  # 仅类型检查用，运行期由调用方/惰性导入提供，避免循环依赖
    from .store import TradingStore


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


# --------------------------------------------------------------- Settings 缓存
# 失效口径（Reviewer 设计稿 2026-11-04）：版本戳 + mtime 双条件，不靠纯 TTL。
# 签名 = (.env 路径, mtime_ns, size, ENV_SETTINGS.version, 本进程写入计数)。
# 前三项与写入计数都是 10µs 级、每次调用都算；SQLite 的 ENV_SETTINGS.version
# 单键读约 2.4ms，用 ttl_probe 节流探测作为正确性兜底（最坏可见延迟 ≤2 秒）。
_SETTINGS_CACHE: dict[str, object] = {
    "signature": None,
    "settings": None,
    "probe_at": 0.0,
    "version": None,
}
_SETTINGS_CACHE_LOCK = threading.Lock()
_SETTINGS_CACHE_EPOCH = 0
_SETTINGS_CACHE_OFF_VALUES = {"off", "0", "false", "no"}


def settings_cache_disabled() -> bool:
    """`SETTINGS_CACHE=off` 一键直通 `load()`；用 os.getenv 直读，避免自举死锁。"""
    return os.getenv("SETTINGS_CACHE", "").strip().lower() in _SETTINGS_CACHE_OFF_VALUES


def _bump_settings_epoch() -> None:
    """本进程写过配置就作废缓存（即使 .env 镜像写失败被吞也必须作废）。"""
    global _SETTINGS_CACHE_EPOCH
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE_EPOCH += 1


def reset_settings_cache() -> None:
    """清空缓存并作废版本戳（测试隔离与「保存后立刻读」共用）。"""
    global _SETTINGS_CACHE_EPOCH
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE.update(
            {"signature": None, "settings": None, "probe_at": 0.0, "version": None}
        )
        _SETTINGS_CACHE_EPOCH += 1


def _env_file_signature() -> tuple[str, int, int]:
    """.env 的路径 + mtime_ns + size：外部编辑器改 .env 时下一次 stat 即失效。"""
    try:
        status = ENV_FILE.stat()
    except OSError:
        return (str(ENV_FILE), -1, -1)
    return (str(ENV_FILE), int(status.st_mtime_ns), int(status.st_size))


def _runtime_settings_version(now: float, ttl_probe: float, store: "TradingStore | None" = None):
    """ENV_SETTINGS 的 runtime_config.version，按 ttl_probe 节流探测。"""
    if now - float(cast(float, _SETTINGS_CACHE["probe_at"])) < ttl_probe:
        return _SETTINGS_CACHE["version"]
    _SETTINGS_CACHE["probe_at"] = now
    active_store = _get_active_store(store)
    if active_store is None:
        return _SETTINGS_CACHE["version"]
    try:
        saved = active_store.get_runtime_config("ENV_SETTINGS")
    except Exception as exc:
        _warn_store_read_fallback("读取 ENV_SETTINGS 版本失败", exc)
        return _SETTINGS_CACHE["version"]
    version = int(saved[1]) if saved else 0
    _SETTINGS_CACHE["version"] = version
    return version


def _settings_signature(now: float, ttl_probe: float, store: "TradingStore | None" = None) -> tuple:
    return (
        _env_file_signature(),
        _SETTINGS_CACHE_EPOCH,
        _runtime_settings_version(now, ttl_probe, store),
    )


def _apply_runtime_config_override(store: "TradingStore | None" = None) -> None:
    """把 SQLite 的 ENV_SETTINGS 权威值**强制**写回 os.environ。

    为什么需要这一步：`refresh_runtime_environment(override=False)` 只写「env 里
    还没有的键」。跨进程改了配置（另一个进程 / Service 写库）时旧值仍在 os.environ
    里，光靠 `load()` 会构造出旧 `Settings` —— 也就是 Reviewer 点名的「双源漂移」。
    这里只覆盖数据库里**显式存在**的键，不动 .env 与操作系统注入的其它变量。
    """
    active_store = _get_active_store(store)
    if active_store is None:
        return
    try:
        saved = active_store.get_runtime_config("ENV_SETTINGS")
    except Exception as exc:
        _warn_store_read_fallback("强制回写 ENV_SETTINGS 失败", exc)
        return
    if not saved or not isinstance(saved[0], dict):
        return
    for key, value in saved[0].items():
        _set_env(str(key), str(value))


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
    # 进程内只要发生过一次配置写入，就立刻让 Settings 缓存失效 —— 即便下面
    # 的 .env 镜像写失败被吞（SQLite 已生效），也不能让页面继续读旧值。
    _bump_settings_epoch()
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


logger = logging.getLogger("futu_quant.config")
_last_store_warn_time: float = 0.0


def _warn_store_read_fallback(context: str, exc: Exception) -> None:
    """对只读 store 访问异常去重记警告（每 900 秒一次），明确告知回退 env 镜像。"""
    global _last_store_warn_time
    now = time.time()
    if now - _last_store_warn_time >= 900:
        _last_store_warn_time = now
        logger.warning(
            "只读访问配置数据库失败 (%s: %s)，已安全回退到 .env 镜像值（注意：.env 仅为镜像，可能与数据库权威配置存在漂移）",
            context, exc,
        )


def _get_active_store(store: "TradingStore | None" = None) -> "TradingStore | None":
    """获取配置读取可用的 store：优先使用注入，无注入时仅当默认库存在且处于默认 env 文件时以只读模式连接（绝不自建空库、绝不触发迁移）。"""
    if store is not None:
        return store
    if ENV_FILE != DEFAULT_ENV_FILE:
        return None
    try:
        from .store import DEFAULT_DB, TradingStore
        if DEFAULT_DB.exists():
            return TradingStore(DEFAULT_DB, read_only=True)
    except Exception as exc:
        _warn_store_read_fallback("初始化只读配置数据库失败", exc)
    return None


def _load_runtime_value(key: str, default, store: "TradingStore | None" = None):
    # 优先从显式注入或有效只读 store 中拉取 runtime_config，且绝对不反向写库 (防启动期写锁争抢)
    active_store = _get_active_store(store)
    if active_store is not None:
        try:
            saved = active_store.get_runtime_config(key)
            if saved is not None:
                return saved[0]
        except Exception as exc:
            _warn_store_read_fallback(f"读取配置键 {key} 异常", exc)

    # 兜底回落从当前 ENV_FILE 读取
    raw = dotenv_values(ENV_FILE).get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


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


def load_volatility_symbols(tenant_id: str | None = None) -> tuple[str, ...]:
    """Worker 交易池 = 明确的 ``DEFAULT_VOLATILITY_SYMBOLS`` + 本租户已批准投放。

    **不再把全局 ``VOLATILITY_SYMBOLS`` 键当作基础池**（Reviewer 2026-10-29 阻塞项）：
    该键同时混着历史外援与人工自定义项，按符号无法证明来源 —— 既不能据此删除
    （会误伤人工核心配置），也不能据此扩展（会让历史外援泄漏进 REAL）。
    迁移 24 因此只保留该键不动，并把非核心自定义项保守迁入 PAPER 投放层。

    * REAL 租户：默认核心 + 自身已消费批准记录的投放；
    * SIMULATE 租户：默认核心 + 运行时人工配置 + 自身投放；人工配置每轮重读，
      因此升级后继续通过既有 UI 增删即可生效；
    * ``tenant_id`` 为空（页面展示 / 共享市场层扫描）：默认核心 + 运行时人工配置 +
      各租户 ACTIVE 投放的只读并集。**该并集仅用于展示与去重，禁止用于下单或风控判定。**
    """
    symbols = list(DEFAULT_VOLATILITY_SYMBOLS)

    try:
        if ENV_FILE == DEFAULT_ENV_FILE:
            from .store import TradingStore
            from .tenant import resolve_tenant

            store = TradingStore()
            # VOLATILITY_SYMBOLS 是既有 UI 的动态人工配置入口。它只能扩展 PAPER/共享扫描，
            # REAL 明确不读取，避免未批准人工标的穿透实盘。
            tenant = resolve_tenant(tenant_id, strict=True) if tenant_id else None
            if tenant is None or tenant.trading_env == "SIMULATE":
                runtime_config = store.get_runtime_config("VOLATILITY_SYMBOLS")
                configured = runtime_config[0] if runtime_config is not None else []
                if isinstance(configured, (list, tuple)):
                    symbols.extend(normalize_us_symbol(item) for item in configured)

            if tenant_id:
                promoted_rows = store.active_promoted_symbols(tenant_id)
            else:
                promoted_rows = store.all_active_promoted_symbols()
            symbols.extend(row["symbol"] for row in promoted_rows if row.get("symbol"))
    except Exception:
        pass

    return tuple(dict.fromkeys(symbols))


#: 预算未分区时（存量）兜底用的旧键值 —— **只做读侧迁移兜底**，不是默认值的第二个出处。
_RADAR_BUDGETS_LEGACY_DEFAULT = {"default": 1000.0, "symbols": {}}
#: 「租户键还没写、暂用旧键」只提醒一次，避免每轮每个标的刷同一条日志。
_RADAR_BUDGET_LEGACY_WARNED: set[str] = set()


#: 三档作用域（Reviewer 2026-11-03 裁定）——「没有租户」与「租户非法」**不是同一件事**。
RADAR_BUDGET_SCOPE_SINGLE = "single"
RADAR_BUDGET_SCOPE_TENANT = "tenant"
RADAR_BUDGET_SCOPE_INVALID = "invalid"


def _radar_budget_scope(tenant_id: str | None) -> tuple[str, str | None]:
    """预算作用域：``(档位, 租户键 | 非法租户串)``。

    * ``single``：没有租户上下文（历史入口）。只有一份钱，按旧键取值 = 改动前行为；
    * ``tenant``：已注册租户 → 只认 ``VOLATILITY_RADAR_BUDGETS:{tenant}``；
    * ``invalid``：给了租户但解析不出来（拼错/未注册）→ **fail-closed**，
      绝不回落旧键 —— 否则一个拼写错误就会读到别人的预算。
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        return RADAR_BUDGET_SCOPE_SINGLE, None
    try:
        return RADAR_BUDGET_SCOPE_TENANT, radar_budgets_config_key(tid)
    except TenantConfigKeyError:
        return RADAR_BUDGET_SCOPE_INVALID, tid


def _warn_budget_legacy_fallback_once(
    tenant_id: str, tenant_key: str, store: "TradingStore | None" = None,
) -> None:
    """租户键尚未写入时喊一次 —— 这一档是**迁移态**，两个租户仍共用一份预算。

    Reviewer 2026-11-03 裁定：**不能只留进程内 WARNING**（重启即丢），
    必须落一条持久事件 `LEGACY_KEY_FALLBACK`，否则用户会以为隔离已经生效。
    事件按 ``tenant_key`` 每进程只写一条，避免每轮每个标的刷库。
    """
    if tenant_key in _RADAR_BUDGET_LEGACY_WARNED:
        return
    _RADAR_BUDGET_LEGACY_WARNED.add(tenant_key)
    logger.warning(
        "租户 %s 尚未写入雷达预算键 %s，暂按无前缀旧键取值（迁移态）："
        "写入该租户键后即与其它租户隔离",
        tenant_id, tenant_key,
    )
    try:
        if store is not None:
            store.operational_event(
                "volatility_radar", "LEGACY_KEY_FALLBACK", "WARNING", {
                    "tenant_id": tenant_id,
                    "expected_key": tenant_key,
                    "used_key": RADAR_BUDGETS_KEY,
                    "reason": "租户预算键尚未写入，本租户暂时与其它租户共用旧键预算",
                },
            )
    except Exception as exc:  # noqa: BLE001 - 事件写失败不得阻断取值，但必须留日志
        logger.warning("写入 LEGACY_KEY_FALLBACK 事件失败（取值照常）：%s", exc)


def _load_tenant_budget(tenant_id: str, store: "TradingStore | None" = None):
    """读租户级预算（存在 `system_settings`，与 `risk_limits:{tenant}` 同层）。

    为什么不用 `runtime_config`：runtime_config 是**全局键**的领地，
    而租户/账户级钱类配置（`risk_limits:{tenant}`、`real_account_config:{acct}`）
    在本仓一直存在 `system_settings`，用 `get_setting`/`set_setting` 读写。
    混用两处就会出现"改了不生效"的第二真值。

    返回 `None` 表示**该租户还没写过预算**（迁移态），由调用方回落旧键。
    """
    active_store = _get_active_store(store)
    if active_store is None:
        return None
    mode, target = _radar_budget_scope(tenant_id)
    if mode != RADAR_BUDGET_SCOPE_TENANT or not target:
        return None
    tenant_key = target
    try:
        raw = active_store.get_setting(tenant_key, "")
    except Exception as exc:  # noqa: BLE001
        _warn_store_read_fallback(f"读取租户预算键 {tenant_key} 异常", exc)
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def load_volatility_radar_budgets(
    tenant_id: str | None = None, store: "TradingStore | None" = None,
) -> dict:
    """读取雷达每批权利金预算：**租户键优先、旧键仅迁移兜底**（三档见 `_radar_budget_scope`）。

    不传租户时只看旧键 —— 该形态只留给页面展示与历史脚本；
    **下单/建 campaign 的取值必须传租户**（见 :func:`resolve_volatility_radar_budget`）。
    非法租户在这里直接抛错（fail-closed）：不知道该用谁的钱，就不给一个"看起来能用"的值。
    """
    mode, target = _radar_budget_scope(tenant_id)
    if mode == RADAR_BUDGET_SCOPE_INVALID:
        raise TenantConfigKeyError(
            f"拒绝解析雷达预算：租户 {target!r} 不在租户注册表 —— "
            "宁可报错，也不回落到无前缀旧键（会读到其它租户共用的预算）"
        )
    value = None
    if mode == RADAR_BUDGET_SCOPE_TENANT and target:
        value = _load_tenant_budget(str(tenant_id or ""), store)
        if value is None:
            _warn_budget_legacy_fallback_once(str(tenant_id or ""), target, store)
    if value is None:
        value = _load_runtime_value(RADAR_BUDGETS_KEY, _RADAR_BUDGETS_LEGACY_DEFAULT)
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
    tenant_id: str | None = None, store: "TradingStore | None" = None,
) -> dict:
    """保存雷达预算：给了租户就写**租户键**，没给才写旧键（兼容旧脚本）。

    写侧必须和读侧同一个键 —— 否则页面改完「保存成功」，雷达仍读租户键，
    界面上改了、实际不生效，属于最难查的那种静默失效。

    租户键存 `system_settings`（与 `risk_limits:{tenant}` 同层）；
    无租户的旧键仍是 `runtime_config` 的全局键，行为与本改动前逐字节一致。
    """
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
    mode, target_key = _radar_budget_scope(tenant_id)
    if mode == RADAR_BUDGET_SCOPE_INVALID:
        raise TenantConfigKeyError(
            f"拒绝保存雷达预算：租户 {target_key!r} 不在租户注册表 —— "
            "禁止把这份预算写进全局旧键（会污染其它租户）"
        )
    if mode == RADAR_BUDGET_SCOPE_TENANT and target_key:
        target = store
        if target is None:
            if ENV_FILE != DEFAULT_ENV_FILE:
                raise RuntimeError(
                    f"保存租户预算 {target_key} 需要显式传入 store"
                    "（当前 ENV_FILE 不是默认库，拒绝写到不确定的库）"
                )
            from .store import TradingStore
            target = TradingStore()
        target.set_setting(target_key, json_dumps(value))
        return value
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        # 字面量键：配置中心 AST 守卫要求 runtime_config 写入键可静态解析
        # （租户键已走 system_settings，见上面的分支；此路径只服务单作用域）。
        TradingStore().set_runtime_config(
            "VOLATILITY_RADAR_BUDGETS", value, "streamlit_or_service",
        )
    _mirror_env(RADAR_BUDGETS_KEY, json_dumps(value))
    return value


def resolve_volatility_radar_budget(
    symbol: str, tenant_id: str | None = None, store: "TradingStore | None" = None,
) -> float:
    """**下单口径**的雷达预算，三档（Reviewer 2026-11-03 裁定）：

    * **单作用域**（没有租户上下文，例如历史 `scan_volatility_watchlist` 入口）：
      只有一份钱、不存在跨租户共用，按旧键取值，与本改动前一致；
    * **已注册租户**：只认 `VOLATILITY_RADAR_BUDGETS:{tenant}`；租户键尚未写入时
      回落旧键（迁移态）并落**持久事件** `LEGACY_KEY_FALLBACK`；
    * **未知/未注册租户**：返回 `0`（fail-closed），由调用方记
      `RADAR_BUDGET_UNRESOLVED` —— 一个拼写错误绝不能读到别人的预算。

    隔离的判据是**多租户雷达扇出必须带租户**（AST 守卫钉住调用点），
    而不是拿一个会被误用的全局兜底当默认值。
    """
    normalized = normalize_us_symbol(symbol)
    mode, target = _radar_budget_scope(tenant_id)
    if mode == RADAR_BUDGET_SCOPE_INVALID:
        logger.warning(
            "雷达预算：租户 %r 不在租户注册表，按 fail-closed 返回 0（不回落旧键）", target,
        )
        return 0.0
    budgets = load_volatility_radar_budgets(str(tenant_id or "").strip() or None, store)
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


def _volatility_campaigns_key(tenant_id: str | None) -> str:
    """波动率 campaign 清单的键 —— **无租户时必须是无前缀旧键**（#1 第 1 步的零行为改动）。

    `tenant_id` 为空 → 无前缀旧键 ``VOLATILITY_CAMPAIGNS``：读侧、写侧、`.env` 镜像
    三处全部落在**同一个键**上，与本签名引入之前逐字节等价（守卫
    `tests/test_volatility_campaigns_key_signature.py` 钉住这一点）。

    传租户 → 走 ``tenant.campaigns_config_key`` 拼出 ``VOLATILITY_CAMPAIGNS:{tenant}``。
    键的拼接收敛在 tenant 层，**这里不手拼**。
    """
    if not tenant_id:
        return VOLATILITY_CAMPAIGNS_KEY
    return campaigns_config_key(tenant_id)


def load_volatility_campaigns(tenant_id: str | None = None) -> list[dict]:
    """读取波动率 campaign 清单。

    `tenant_id=None`（默认，所有存量调用点）→ 无前缀旧键，行为与改动前一致；
    传租户 → 读 ``VOLATILITY_CAMPAIGNS:{tenant}``（#1 第 3/4 步切流时启用）。
    """
    value = _load_runtime_value(_volatility_campaigns_key(tenant_id), [])
    return value if isinstance(value, list) else []


def load_all_volatility_campaigns() -> list[dict]:
    """跨租户读取**全部** campaign —— 仅用于活动总览这类**只读**场景。

    #1 键级分区后租户键是主存，无前缀旧键是迁移前的存量，两者都要读到才能
    给出完整的活动视图。**任何写路径都不得用本函数的结果**（那会写回全局键）。
    """
    from .tenant import DEFAULT_TENANTS

    rows = load_volatility_campaigns()
    for tenant_id in DEFAULT_TENANTS:
        rows = rows + load_volatility_campaigns(tenant_id)
    return rows


def save_volatility_campaigns(campaigns: list[dict], tenant_id: str | None = None) -> None:
    """写回波动率 campaign 清单。

    与读侧**同一个键函数**：`tenant_id=None` 写无前缀旧键，传租户写租户键 ——
    写读必须用同一把钥匙，否则会出现"写进去没人读"的半状态。
    """
    key = _volatility_campaigns_key(tenant_id)
    if ENV_FILE == DEFAULT_ENV_FILE:
        from .store import TradingStore
        TradingStore().set_runtime_config(key, campaigns, "streamlit_or_service")
    value = json_dumps(campaigns)
    _mirror_env(key, value)


def _campaign_scope(campaign: dict) -> tuple[str, str]:
    """campaign 的两个身份维度 —— ``(租户, code)``。"""
    tenant = str(campaign.get("tenant_id") or "").strip()
    code = str(campaign.get("code") or campaign.get("campaign_id") or "").strip()
    return tenant, code


def _campaign_identity(campaign: dict) -> str:
    """campaign 的唯一身份 —— **租户 + 合约 code**。

    为什么不能以 `campaign_id` 优先：`campaign_id` 是「这次活动的编号」（例如
    `orphan:<code>`），同一个合约可能因**重复接管**而带上不同的 campaign_id。
    以它为身份会让同一张合约在配置里出现多条记录 —— 10-22 实测
    `US.AMZN261028P250000` 同时存在两条（`orphan:…P252500` 与 `orphan:…P250000`），
    worker 每轮把它当两个 campaign 执行，决策重复且互相干扰。

    **旧铁律必须加边界（10-23，跨租户互相顶掉的实测）**：
    - **同一租户内**：一张合约只应有一条 campaign 记录（旧铁律，保留）；
    - **跨租户**：同一个 `code` **必须共存** —— 实盘与模拟各持一份，靠身份里的
      租户前缀区分。两租户同 code 时，`code` 单独作为身份**不再唯一**，落库只会剩
      1 条，另一个租户的归属被顶掉。

    **不要因为"看起来复杂"删掉租户前缀**：删掉之后守卫
    `test_two_tenants_adopting_same_code_do_not_overwrite` 会转红
    （设计判据：`docs/volatility_campaigns_tenant_partition_design.md` §7.1 / §8.3）。
    """
    tenant, code = _campaign_scope(campaign)
    return f"{tenant}|{code}" if tenant else code


def upsert_campaign(
    config_key: str, campaign: dict, store: "TradingStore | None" = None,
) -> list[dict]:
    """Atomically add/replace one campaign without overwriting concurrent changes.

    按 `_campaign_identity`（**租户 + 合约 code**）去重：重复接管/滚动不会产生
    重复记录；**跨租户同 code 各自共存**（10-23 P0-9 实测：只按 code 去重时
    两租户会互相顶掉，落库只剩 1 条）。

    **带租户写入时，同 code 的"无租户 legacy 记录"视为被取代，一并移除**（10-23）：
    否则两种身份形态（`:US.X` 与 `futu_real_master:US.X`）会**同时存活**，worker 每轮
    当两个 campaign 执行 —— 正是 `_campaign_identity` docstring 自己警告的那个 bug。
    这条同时兜住**存量**（迁移前写入的 legacy 记录）与**漏设写入方**造成的形态并存，
    守卫 `test_legacy_and_tenant_scoped_campaign_for_same_code_collapse_to_one` 钉住它。

    `store` 可注入：**必须注入**。本函数若不接 store 就会自建 `TradingStore()`
    直连生产库 —— 任何测试或临时脚本调用它都会写穿生产配置
    （10-23 实测：守卫测试因此在生产配置里造出一条幽灵 campaign）。
    """
    from .store import TradingStore as _TradingStore

    store = store or _TradingStore()
    identity = _campaign_identity(campaign)
    # 走 `_campaign_scope`：code 缺失时回落 `campaign_id`，与 `_campaign_identity` 同口径。
    new_tenant, new_code = _campaign_scope(campaign)

    def update(current):
        rows = []
        for item in current:
            if _campaign_identity(item) == identity:
                continue                      # 同一身份 → 替换
            item_tenant, item_code = _campaign_scope(item)
            if new_tenant and new_code and not item_tenant and item_code == new_code:
                continue                      # 同 code 的 legacy 无租户记录 → 被取代
            rows.append(item)
        rows.append(campaign)
        return rows

    value, _ = store.mutate_runtime_config(
        config_key, [], update, "streamlit_or_service",
    )
    _mirror_env(config_key, json_dumps(value))
    return value


def delete_campaign(
    config_key: str, identity: str, store: "TradingStore | None" = None,
) -> list[dict]:
    """按**身份**（`{tenant_id}|{code}`）或**合约 code / campaign_id** 删除 campaign。

    为什么两种形态都要接受（10-23）：身份改成带租户前缀后，调用方仍在传**裸 code**
    （`run_volatility_strategy.py` 的 `old_identity`、控制页的 `item["code"]`/`code`）。
    只按完整身份比对，裸 code 会**一条都匹配不上 → 静默不删**。

    **但"裸 code 删所有租户副本"更糟**（10-23 Reviewer 实测）：控制页的"清理过期/失效
    且无持仓策略"按钮正是传裸 code，而它的候选列表本身是**租户盲**的
    （`pages/10_volatility_options_strategy.py:1313-1327` 按 code 查持仓）—— 实盘一张
    **有真实持仓**的合约可能读到模拟盘的持仓（=0）而进入清理列表，点一下就把实盘的
    campaign 删掉、**持仓失去管理**（META 那次的失效模式）。

    所以采取**模糊即拒绝**（fail-closed，与今晚所有取舍一致）：
    - 传**完整身份** → 删那一条；
    - 传裸 code 且**只命中一个租户** → 删（兼容旧调用方）；
    - 传裸 code 且**命中多个租户** → **抛异常**，要求调用方给完整身份。

    **把"静默删掉别人"换成"响亮拒绝"。** B 批次再把控制页改成传完整身份（精确语义）。

    **注意**：`upsert_campaign` 仍按完整身份去重（它必须区分租户）；只有删除放宽形态。
    """
    from .store import TradingStore as _TradingStore

    store = store or _TradingStore()
    target = str(identity or "").strip()

    def _matches(item: dict) -> bool:
        if _campaign_identity(item) == target:
            return True
        return target in {
            str(item.get("code") or "").strip(),
            str(item.get("campaign_id") or "").strip(),
        }

    def update(current):
        matched = [item for item in current if _matches(item)]
        tenants = {str(item.get("tenant_id") or "").strip() for item in matched}
        if len(tenants) > 1:
            raise ValueError(
                f"拒绝模糊删除：{target!r} 在 {len(tenants)} 个租户下都存在"
                f"（{sorted(tenants)}）—— 请用完整身份 {{tenant}}|{target} 删除，"
                "避免跨租户连坐"
            )
        return [item for item in current if not _matches(item)]

    value, _ = store.mutate_runtime_config(
        config_key, [], update, "streamlit_or_service",
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


def save_runtime_settings(values: dict[str, str], store=None) -> None:
    """Persist validated operational settings from the monitoring page.

    `store`：调用方**应当**传入自己已打开的 store（UI 里是 `ui.open_trading_store()`）。
    不传时才退回无参 `TradingStore()` —— 那会在页面进程里自建连接，
    既是 2026-11-01 面板事故里那条老红线，也让「页面写的库」与「页面读的库」可能不是同一个。
    """
    normalized = {str(key): str(value) for key, value in values.items()}
    if ENV_FILE == DEFAULT_ENV_FILE:
        if store is None:
            from .store import TradingStore

            store = TradingStore()
        store.mutate_runtime_config(
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


def refresh_runtime_environment(override: bool = False, store: "TradingStore | None" = None) -> dict[str, str]:
    """Load file + SQLite dynamic settings into the current process."""
    values = {key: str(value) for key, value in dotenv_values(ENV_FILE).items() if value is not None}
    active_store = _get_active_store(store)
    if active_store is not None:
        try:
            saved = active_store.get_runtime_config("ENV_SETTINGS")
            if saved is not None and isinstance(saved[0], dict):
                values.update({key: str(value) for key, value in saved[0].items()})
        except Exception as exc:
            _warn_store_read_fallback("刷新 ENV_SETTINGS 动态配置失败", exc)
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
    core_satellite_cash_yield_enabled: bool
    core_satellite_cash_yield_symbol: str
    core_satellite_symbols: tuple[str, ...]
    gamma_symbols: tuple[str, ...]
    volatility_symbols: tuple[str, ...]
    ml_enabled: bool
    ml_model_path: str
    ml_threshold: float
    ml_weight: float
    option_exit_max_spread_pct: float
    option_exit_price_deviation_anchor_pct: float

    @classmethod
    def load(cls, refresh: bool = False, store: "TradingStore | None" = None) -> "Settings":
        _apply_dotenv(override=refresh)
        active_store = _get_active_store(store)
        refresh_runtime_environment(override=refresh, store=active_store)
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
        option_exit_max_spread_pct = float(os.getenv("OPTION_EXIT_MAX_SPREAD_PCT", "20.0"))
        if not 1.0 <= option_exit_max_spread_pct <= 50.0:
            raise RuntimeError("OPTION_EXIT_MAX_SPREAD_PCT 必须在 1.0% 到 50.0% 之间")
        option_exit_price_deviation_anchor_pct = float(os.getenv("OPTION_EXIT_PRICE_DEVIATION_ANCHOR_PCT", "0.40"))
        if not 0.01 <= option_exit_price_deviation_anchor_pct <= 0.80:
            raise RuntimeError("OPTION_EXIT_PRICE_DEVIATION_ANCHOR_PCT 必须在 0.01 到 0.80 之间")
        cash_yield_enabled = _load_runtime_value("core_satellite_cash_yield_enabled", False, store=active_store)
        cash_yield_symbol = normalize_us_symbol(str(_load_runtime_value("core_satellite_cash_yield_symbol", "US.SGOV", store=active_store)))
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
            core_satellite_cash_yield_enabled=bool(cash_yield_enabled),
            core_satellite_cash_yield_symbol=cash_yield_symbol,
            core_satellite_symbols=core_symbols,
            gamma_symbols=gamma_symbols,
            volatility_symbols=volatility_symbols,
            ml_enabled=ml_enabled,
            ml_model_path=ml_model_path,
            ml_threshold=ml_threshold,
            ml_weight=ml_weight,
            option_exit_max_spread_pct=option_exit_max_spread_pct,
            option_exit_price_deviation_anchor_pct=option_exit_price_deviation_anchor_pct,
        )

    def quantity_for(self, symbol: str) -> int:
        return self.order_quantities.get(symbol, self.order_quantity)

    @classmethod
    def load_cached(cls, *, ttl_probe: float = 2.0, store: "TradingStore | None" = None) -> "Settings":
        """带失效的 `load()`：UI 展示型页面用它，service / 下单 / 风控继续用 `load(refresh=True)`。

        为什么不是纯 TTL 或 `@st.cache_data`：正确性必须由权威源（.env stat +
        SQLite `ENV_SETTINGS.version` + 本进程写入计数）决定，TTL 只做 DB 探测节流；
        `Settings` 含 dict 不可 hash，且 `@st.cache_data` 看不见 DB 版本。

        最坏可见延迟：同进程保存后立即生效（写入即 bump epoch）；别的进程写
        SQLite ≤ `ttl_probe`（默认 2 秒）；外部改 .env 下一次调用即生效。
        """
        if settings_cache_disabled():
            return cls.load(store=store)

        signature = _settings_signature(time.monotonic(), ttl_probe, store)
        cached = cast("Settings | None", _SETTINGS_CACHE["settings"])
        if cached is not None and _SETTINGS_CACHE["signature"] == signature:
            return cached

        # 未命中时串行填充：`_apply_dotenv` / `refresh_runtime_environment` 写的是
        # 进程级 os.environ，多 session 并发 rerun 会交错出半新半旧的环境。
        with _SETTINGS_CACHE_LOCK:
            signature = _settings_signature(time.monotonic(), ttl_probe, store)
            cached = cast("Settings | None", _SETTINGS_CACHE["settings"])
            if cached is not None and _SETTINGS_CACHE["signature"] == signature:
                return cached
            # 权威值先强制覆盖 os.environ，再构造：否则跨进程写入看不见（见上）。
            _apply_runtime_config_override(store)
            settings = cls.load(store=store)
            _SETTINGS_CACHE["settings"] = settings
            _SETTINGS_CACHE["signature"] = signature
            return settings
