from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from typing import Any

from .config import Settings
from .risk import RiskLimits, ny_trading_day_start_utc
from .risk_limits import RiskLimitsConfigError, resolve_risk_limits
from .store import USED_NOTIONAL_STATUSES, TradingStore
from .strategy_toggle import resolve_strategy_enabled
from .tenant import equity_snapshot_env, resolve_tenant_active


SESSION_KEY = "real_trading_session"
OPTION_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$")


@dataclass(frozen=True)
class RealTradeDecision:
    allowed: bool
    reason: str
    details: dict[str, Any]


@dataclass(frozen=True)
class RealAccountLimits:
    """一个实盘账户的生效限额 —— 账户级优先、全局兜底，**唯一解析口径**。

    为什么抽出来：账户级 `real_account_config:<id>` 与全局 Settings 此前由
    **三处各自解析**（`real_trading.check_real_trade`、`execution.submit_limit` 的
    账户开关、`ui.is_acc_enabled`），字段名/回落规则靠各处记忆 —— 10-22 就发生过
    「改了全局以为生效、真正生效的是账户级」这类漂移。

    铁律：**任何账户级配置的读取都必须走这里**，不得在别处再解析一遍。
    """

    enabled: bool
    allowed_strategies: frozenset[str]
    max_order_notional: float
    max_daily_notional: float
    require_manual_confirm: bool
    config_present: bool = False
    # 坏键标记（Reviewer 第 1 条）：必须由解析函数**显式返回**，不让上层靠捕获异常去猜 ——
    # 异常会把「配置坏了」和「代码有 bug」混成一类，两类处置完全不同。
    config_corrupt: bool = False
    corrupt_reason: str = ""
    # 派生限额解析失败时必须独立标记，禁止回落旧金额（红区 fail-closed）。
    limit_profile_unresolved: bool = False
    limit_profile_reason: str = ""


def _setting_or_env(settings: Any, attr: str, env_key: str, default: Any) -> Any:
    """读取一个限额：优先取 settings 上的字段，缺字段时回落环境变量。

    为什么需要回落：调用方传入的未必是完整的 `Settings`（历史测试与部分内部
    调用用的是轻量对象，只带自己关心的字段）。此时**不得**因为缺字段就抛异常或
    返回 0 —— 那会让整条下单链路崩掉或把限额变成 0（等于禁止一切交易）。
    """
    value = getattr(settings, attr, None)
    if value is not None:
        return value
    raw = os.getenv(env_key)
    if raw is None or raw == "":
        return default
    try:
        return type(default)(raw) if default is not None else raw
    except (TypeError, ValueError):
        return default


def _strict_float(value: Any) -> tuple[float | None, str]:
    """账户级数字字段严格解析：非法返回 ``(None, 原因)``，绝不静默回落。"""
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return None, f"值 {value!r} 无法转换为数字"


def _account_tenant_id(store: TradingStore, account_id: Any) -> str:
    """账户号 → 租户 id（飞书租户标签用）；解析失败返回空串（标签走【未标记租户】）。"""
    try:
        from .tenant import resolve_tenant

        profile = resolve_tenant(account_id=str(account_id), env="REAL")
        return profile.tenant_id if profile else ""
    except Exception:  # noqa: BLE001
        return ""


def notify_corrupt_account_config(store: TradingStore, account_id: Any, reason: str) -> None:
    """坏键强告警（不可静音，Reviewer 第 4 条）。

    为什么必须真推：坏键的直接后果是「开仓被拦」，如果这条告警能被静音或只落库，
    门禁坏了没人知道 —— 与既有的静默拦截事故同一母题。

    **防轰炸（Reviewer 第 1 条）**：必须带稳定 `incident_key`，否则每笔尝试都会
    新开一条 incident 并推一张飞书卡。稳定键 + `AlertManager` 的
    `ALERT_DEDUPE_SECONDS`（默认 900s）精确去重共同保证「一个坏键只刷一张卡」。
    **调用方职责**：只有 BUY 被拦时才调本函数，SELL 一律不推（见 execution / check_real_trade）。
    """
    from .alerts import AlertManager  # 局部导入：alerts 依赖 store，避免模块环

    # 租户标签（用户 2026-11-06 拍板）：实盘/模拟盘推送必须可辨。
    _tenant = _account_tenant_id(store, account_id)
    try:
        AlertManager(store).send(
            "ERROR",
            "REAL_ACCOUNT_CONFIG_CORRUPT",
            f"实盘账户 {account_id} 的账户级配置损坏：{reason} —— 已禁止 BUY 开仓，SELL 仍走原有安全校验",
            incident_key=f"REAL_ACCOUNT_CONFIG_CORRUPT:{account_id}",
            tenant_id=_tenant,
        )
    except Exception:  # noqa: BLE001
        pass  # silent-ok: 告警投递失败不得反过来阻断门禁判定本身


def resolve_real_account_limits(
    store: TradingStore, settings: Settings, account_id: int | str, *, notify_fallback: bool = True,
) -> RealAccountLimits:
    """解析某实盘账户的生效限额：账户级优先、全局兜底；**坏键 fail-closed**。

    三种情形必须分开（Product 裁定，任何一条含糊都会变成 fail-open）：
    1. 键不存在 / 空串 → 未配置，走全局兜底，**不是**故障（本仓有大量空串占位键）；
    2. 键存在但损坏（JSON 非法 / 顶层非对象 / 字段非法）→ 坏键：
       ``config_corrupt=True`` 且 ``enabled=False``，由上层只拦 BUY；
    3. 键存在且合法 → 账户级覆盖全局。
    """
    raw = store.get_setting(f"real_account_config:{str(account_id)}", "")
    cfg: dict = {}
    corrupt_reason = ""
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - 解析失败即坏键，原因落到 corrupt_reason
            parsed = None
            corrupt_reason = f"JSON 解析失败：{exc}"
        if not corrupt_reason:
            if isinstance(parsed, dict):
                cfg = parsed
            else:
                corrupt_reason = f"顶层不是 JSON 对象（{type(parsed).__name__}）"

    allowed = cfg.get("allowed_strategies")
    if allowed and not isinstance(allowed, (list, tuple, set, frozenset)):
        corrupt_reason = corrupt_reason or (
            f"allowed_strategies 类型非法（{type(allowed).__name__}）"
        )
        allowed = None
    if not allowed:
        allowed = _setting_or_env(
            settings, "real_allowed_strategies", "REAL_ALLOWED_STRATEGIES", ()
        )

    def _number(key: str, attr: str, env_key: str, default: Any) -> float:
        """账户级数字：非空才覆盖；非法则记坏键并回落全局。

        **显式约定（Reviewer 第 3 条，改动前须先改这条注释与守卫）**：
        坏键下数值字段**回落全局口径，绝不置 0** —— 因为 SELL 仍要使用这份限额，
        一旦变成 0 就等于「配置写坏 ⇒ 减仓也被锁死」，平仓通道会被自己的坏配置堵死。
        对应守卫：`test_corrupt_uses_global_limits_so_sell_not_deadlocked`。
        """
        nonlocal corrupt_reason
        value = cfg.get(key)
        if value:
            number, reason = _strict_float(value)
            if number is None:
                corrupt_reason = corrupt_reason or f"{key} {reason}"
                return float(_setting_or_env(settings, attr, env_key, default))
            return number
        return float(_setting_or_env(settings, attr, env_key, default))

    max_order = _number(
        "max_order_notional", "real_max_order_notional", "REAL_MAX_ORDER_NOTIONAL", 0.0
    )
    max_daily = _number(
        "max_daily_notional", "real_max_daily_notional", "REAL_MAX_DAILY_NOTIONAL", 0.0
    )
    # 派生层只替换金额来源；门禁顺序、确认码、白名单和 submit_limit 均不动。
    limit_profile_unresolved = False
    limit_profile_reason = ""
    # 租户解析/配置读取失败不等于派生层已启用：迁移期必须继续使用旧键。
    # 只有明确读到 derived:limit_profile 后，权益或派生计算异常才 fail-closed。
    from .tenant import resolve_tenant
    from .limit_profile import resolve_limit_chain
    from .limit_profile_store import load_limit_profile
    try:
        tenant_profile = resolve_tenant(account_id=str(account_id), env="REAL")
        tenant_id = tenant_profile.tenant_id if tenant_profile else ""
        profile_resolution = load_limit_profile(store, tenant_id) if tenant_id else None
        if profile_resolution and profile_resolution.source == "derived:limit_profile":
            # 故障恢复必须真实调用 recover：若此前发生过旧键回落，恢复时关闭对应 incident。
            from .alerts import AlertManager
            AlertManager(store).recover(
                "LIMIT_PROFILE_LEGACY_FALLBACK",
                f"账户 {account_id} 限额配置已恢复，已使用租户派生配置",
                incident_key=f"LIMIT_PROFILE_LEGACY_FALLBACK:{account_id}",
            )
    except Exception as exc:
        tenant_id = ""
        profile_resolution = None
        limit_profile_reason = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).error(
            "LIMIT_PROFILE_LEGACY_FALLBACK account=%s reason=%s", account_id, limit_profile_reason
        )
        # 回落仍兼容旧限额，但必须 ERROR 告警、不可静音，并携带完整解析上下文。
        try:
            tenant_id = ""
            env = str(getattr(settings, "trading_env", "") or "").upper()
            try:
                tenant_id = resolve_tenant(account_id=str(account_id), env=env).tenant_id
            except Exception:
                pass
            profile_version = ""
            if tenant_id:
                profile_version = str(store.get_setting(f"limit_profile:{tenant_id}:version", "") or "")
            payload = {
                "account_id": str(account_id), "tenant_id": tenant_id, "env": env,
                "reason": limit_profile_reason, "limit_profile_version": profile_version,
            }
            store.audit("LIMIT_PROFILE_LEGACY_FALLBACK", payload, "real_trading", actor="service:real_gate")
            from .alerts import AlertManager
            if not corrupt_reason:
                AlertManager(store).send(
                    "ERROR", "LIMIT_PROFILE_LEGACY_FALLBACK",
                    f"账户 {account_id} 限额配置回落旧键：{limit_profile_reason}",
                    incident_key=f"LIMIT_PROFILE_LEGACY_FALLBACK:{account_id}",
                    tenant_id=tenant_id,
                )
        except Exception:  # noqa: BLE001 - 告警/审计失败不得改变兼容回落结论
            logging.getLogger(__name__).exception("记录 LIMIT_PROFILE_LEGACY_FALLBACK 失败")

    if profile_resolution and profile_resolution.source == "derived:limit_profile":
        try:
            # 统一使用租户环境入口，禁止按 account_id 猜测快照键，防止 paper/real 串读。
            env = equity_snapshot_env(tenant_id, account_id)
            rows = store.equity_history(limit=1, env=env)
            if not rows:
                raise ValueError("equity 快照缺失")
            snapshot = rows[0]
            raw_timestamp = snapshot.get("created_at") or snapshot.get("updated_at")
            if not raw_timestamp:
                raise ValueError("equity 快照时间缺失")
            snapshot_at = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - snapshot_at).total_seconds() > 600:
                raise ValueError("equity 快照已过期（超过 10 分钟）")
            equity = float(snapshot.get("total_assets") or 0)
            chain = resolve_limit_chain(store, tenant_id, equity, profile_resolution.profile)
            max_order = min(chain.per_order, chain.capacity_line) if chain.capacity_line is not None else chain.per_order
            max_daily = chain.daily_entries
        except Exception as exc:
            # 派生层已启用但权益/配置解析失败时，绝不回落旧金额（红区 fail-closed）。
            limit_profile_unresolved = True
            limit_profile_reason = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).error(
                "LIMIT_PROFILE_UNRESOLVED account=%s reason=%s", account_id, limit_profile_reason
            )

    confirm_raw = cfg.get("require_manual_confirm")
    require_confirm = (
        bool(_setting_or_env(
            settings, "real_require_manual_confirm", "REAL_REQUIRE_MANUAL_CONFIRM", False
        ))
        if confirm_raw is None
        else bool(confirm_raw)
    )

    corrupt = bool(corrupt_reason)
    return RealAccountLimits(
        # 坏键一律按「开关关闭」返回：BUY 由上层拒绝，SELL 由上层显式放行到既有校验链。
        enabled=False if corrupt else bool(cfg.get("enabled", True)),
        allowed_strategies=frozenset(allowed),
        max_order_notional=max_order,
        max_daily_notional=max_daily,
        require_manual_confirm=require_confirm,
        config_present=bool(raw),
        config_corrupt=corrupt,
        corrupt_reason=corrupt_reason,
        limit_profile_unresolved=limit_profile_unresolved,
        limit_profile_reason=limit_profile_reason,
    )


def contract_multiplier(symbol: str, metadata_multiplier: Any = None) -> float:
    if metadata_multiplier is not None:
        try:
            value = float(metadata_multiplier)
            return value if value > 0 else 1.0
        except (TypeError, ValueError):
            pass
    return 100.0 if OPTION_PATTERN.match(symbol) else 1.0


def confirmation_code(settings: Settings, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone()
    account_tail = str(settings.real_account_id or "0000")[-4:]
    return f"REAL-{current:%Y%m%d}-{account_tail}"


def _parse_session(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_session(store: TradingStore) -> dict[str, Any]:
    return _parse_session(store.get_setting(SESSION_KEY, "{}"))


def session_status(
    store: TradingStore,
    settings: Settings,
    now: datetime | None = None,
    account_id: int | str | None = None,
    require_confirm: bool | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    session = load_session(store)
    if settings.trading_env != "REAL":
        return False, "当前交易环境不是 REAL", session
    if not settings.real_trading_enabled:
        return False, "REAL_TRADING_ENABLED 未开启", session
    target_account = str(account_id or settings.real_account_id or "").strip()
    if not target_account:
        return False, "未配置 REAL_ACCOUNT_ID", session
    needs_confirm = settings.real_require_manual_confirm if require_confirm is None else bool(require_confirm)
    if needs_confirm:
        expires_at = str(session.get("expires_at") or "")
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            return False, "尚未完成实盘人工确认会话", session
        current = now or datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if current > expires:
            return False, "实盘人工确认会话已过期", session
        if str(session.get("account_id") or "") != target_account:
            return False, f"确认会话账户与目标账户 {target_account} 不一致", session
    return True, "实盘确认会话有效", session


def enable_session(
    store: TradingStore,
    settings: Settings,
    code: str,
    minutes: int = 30,
    actor: str = "streamlit_admin:panel",
    now: datetime | None = None,
) -> tuple[bool, str]:
    if settings.trading_env != "REAL":
        return False, "请先把 FUTU_TRADING_ENV 切换为 REAL 并保存"
    if not settings.real_trading_enabled:
        return False, "请先开启 REAL_TRADING_ENABLED"
    if not settings.real_account_id:
        return False, "请先配置 REAL_ACCOUNT_ID"
    expected = confirmation_code(settings, now)
    if str(code).strip() != expected:
        return False, f"确认码不正确，应输入 {expected}"
    current = now or datetime.now(timezone.utc)
    expires = current + timedelta(minutes=max(1, min(240, int(minutes))))
    payload = {
        "account_id": settings.real_account_id,
        "confirmed_at": current.isoformat(),
        "expires_at": expires.isoformat(),
        "actor": actor,
    }
    store.set_setting(SESSION_KEY, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    audit_actor = actor if ":" in str(actor) else (
        "streamlit_admin:panel" if actor == "streamlit_admin" else f"cli:{actor}"
    )
    store.audit("REAL_TRADING_SESSION_ENABLED", payload, actor=audit_actor)
    return True, f"实盘确认会话已开启，到期时间 {expires.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"


def disable_session(store: TradingStore, actor: str = "streamlit_admin:panel") -> None:
    store.set_setting(SESSION_KEY, "{}")
    audit_actor = actor if ":" in str(actor) else (
        "streamlit_admin:panel" if actor == "streamlit_admin" else f"cli:{actor}"
    )
    store.audit("REAL_TRADING_SESSION_DISABLED", {"actor": actor}, actor=audit_actor)


def reduction_notional_limits(
    store: TradingStore | None = None,
    broker=None,
) -> tuple[float, float]:
    """**减仓（SELL）**独立额度口径 —— **直接复用 `RiskLimits`，杜绝第三套实现**。

    命名铁律（10-23 Product 规范）：额度类函数/配置**命名必须显式携带方向语义**。
    名字里没有方向的 `submitted_notional_since` 正是本次事故的根源 ——
    它让后来者以为该函数统计「所有在途单」，于是不知道要过滤 `side`。
    本函数对应 `..._reduction_...`，与开仓侧 `..._entry_...` 成对，看名字就知道管哪头。

    P0-7：额度数值**不再**直接 ``RiskLimits.load()``（那是全局 env 口径），
    而是走**唯一解析入口** ``resolve_risk_limits`` —— 减仓额度同样必须按
    账户/租户解析，否则「账户 A 的减仓额度被账户 B 的参数管着」。

    `store is None` 是没有 store 上下文的**历史调用方**（单元测试）的退化路径：
    此时只能读 env，且**不产生**账户级审计。
    """
    if store is None:
        limits = RiskLimits.load()
    else:
        limits = resolve_risk_limits(store, broker)
    return (limits.max_reduction_order_notional, limits.max_daily_reduction_notional)


def _used_real_notional(
    store: TradingStore,
    iso_timestamp: str,
    side: str,
    exclude_intent_id: int | None = None,
    account_id: int | str | None = None,
) -> float:
    """当日已用名义金额（按方向）—— 实盘侧唯一实现，**累加逻辑走 `store.reduce_notional`**。

    `side="BUY"` → 开仓额度占用；`side="SELL"` → 减仓额度占用。
    两者是独立的桶：混算会让「白天开仓用满额度 → 晚上盈利的平仓单被自己的买入额度挡住」。
    """
    target_account = str(account_id).strip() if account_id is not None else ""
    rows: list[dict] = []
    for row in store.intents(limit=50_000):
        if exclude_intent_id is not None and row.get("id") == exclude_intent_id:
            continue
        if str(row.get("created_at") or "") < iso_timestamp:
            continue
        if str(row.get("status")) not in USED_NOTIONAL_STATUSES:
            continue
        metadata = _parse_session(row.get("metadata_json"))
        if metadata.get("execution_mode") != "REAL" and not str(row.get("intent_key", "")).endswith(":REAL"):
            continue
        if target_account:
            row_acc = str(metadata.get("account_id") or "").strip()
            if row_acc and row_acc != target_account:
                continue
        rows.append(row)
    return store.reduce_notional(rows, side)


def submitted_real_reduction_notional_since(
    store: TradingStore,
    iso_timestamp: str,
    exclude_intent_id: int | None = None,
    account_id: int | str | None = None,
) -> float:
    """当日已提交的实盘**减仓（SELL）**名义金额。

    与 `submitted_real_entry_notional_since`（只计 BUY 开仓）配对，构成两个独立的桶：
    **开仓额度**只计 BUY，**减仓额度**只计 SELL。
    """
    return _used_real_notional(store, iso_timestamp, "SELL", exclude_intent_id, account_id)


def submitted_real_entry_notional_since(
    store: TradingStore,
    iso_timestamp: str,
    exclude_intent_id: int | None = None,
    account_id: int | str | None = None,
) -> float:
    """当日已提交的实盘**开仓（BUY）**名义金额（不含减仓卖出）。

    **命名即契约**：函数名里的 `entry` 就是这个函数的完整语义。
    它的前任叫 `submitted_real_notional_since`（名字里没有方向）——
    于是后来者把它理解成「所有在途单的名义金额」，**不知道要过滤 `side`**，
    把卖出也算进开仓额度，导致「白天开仓用满 → 晚上盈利的平仓单被自己的买入额度挡住」。
    10-23 Product 据此立规：**额度/限额类命名必须显式携带方向与语义**
    （`..._entry_...` 开仓 / `..._reduction_...` 减仓），禁止无方向名。

    铁律：**卖出（减仓）不计入开仓额度**。这条额度管的是「新投放多少资金」，
    而卖出是**降低**敞口 —— 把两者混算会导致「该平仓时平不掉」：
    10-22 实盘 AMZN 期权盈利后想锁盈退出，却被同一道额度拦下（连续 9 笔卖出被拒），
    仓位一路持有到到期，承担被行权做成裸空的风险。

    减仓卖出有独立且更宽的额度约束（`risk.py` 的
    `RISK_MAX_REDUCTION_ORDER_NOTIONAL` / `RISK_MAX_DAILY_REDUCTION_NOTIONAL`），
    在风控引擎处生效，不需要在这里重复限制。
    """
    return _used_real_notional(store, iso_timestamp, "BUY", exclude_intent_id, account_id)


PROD_ACCOUNT_STRATEGY_MAP = {
    "283445327916889204": {"volatility_long_gamma"},
    "281756481476169109": {"core_satellite", "volatility_long_gamma"},
}

PROD_ACCOUNT_FIRM_MAP = {
    "283445327916889204": "FUTUINC",
    "281756481476169109": "FUTUSECURITIES",
}


@dataclass(frozen=True)
class _RiskBrokerScope:
    """只带账户/租户标识的**极简 broker 视图**，供 `resolve_risk_limits` 提取作用域。

    `check_real_trade` 手里只有 `store` 与 `settings`，没有 broker 对象；
    而风控解析需要「这是哪个账户」。这里给一个最小视图，避免把整个 broker
    或 settings 塞进解析链路（解析器只读 `account_id` / `tenant_id`）。
    """

    account_id: str = ""
    tenant_id: str = ""


def check_real_trade(
    store: TradingStore,
    settings: Settings,
    strategy_id: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
    exclude_intent_id: int | None = None,
) -> RealTradeDecision:
    metadata = metadata or {}
    if settings.trading_env != "REAL":
        return RealTradeDecision(False, "券商连接环境不是 REAL", {})
    if not settings.real_trading_enabled:
        return RealTradeDecision(False, "实盘总开关未开启", {})

    # REAL 闸门必须与券商实际交易环境一致；paper broker 不得借 settings=REAL 进入实盘链路。
    broker_env = str(metadata.get("trading_env") or metadata.get("broker_trading_env") or "").upper()
    if broker_env and broker_env != "REAL":
        return RealTradeDecision(False, f"券商连接环境不是 REAL（实际为 {broker_env}）", {
            "broker_trading_env": broker_env,
        })

    target_account = str(metadata.get("account_id") or settings.real_account_id or "").strip()
    if not target_account:
        return RealTradeDecision(False, "未配置或未绑定实盘账户 ID", {})

    # 1. 严格多账户实盘隔离校验：两个真实账户各司其职，坚决禁止跨账户执行
    if target_account in PROD_ACCOUNT_STRATEGY_MAP:
        allowed_for_target = PROD_ACCOUNT_STRATEGY_MAP[target_account]
        if strategy_id not in allowed_for_target:
            return RealTradeDecision(
                False,
                f"多账户隔离铁律：策略 {strategy_id} 禁止在实盘账户 {target_account} 执行 (该账户专属于 {','.join(allowed_for_target)})",
                {"account_id": target_account, "strategy_id": strategy_id},
            )
        expected_firm = PROD_ACCOUNT_FIRM_MAP[target_account]
        actual_firm = str(metadata.get("security_firm") or "").upper()
        if actual_firm and actual_firm != expected_firm:
            return RealTradeDecision(
                False,
                f"券商实体校验失败：账户 {target_account} 期望实体 {expected_firm}，当前为 {actual_firm}",
                {"account_id": target_account, "security_firm": actual_firm},
            )

    # 2. 读取各账户在 runtime_config 中的独立配置（账户级优先、全局兜底）
    limits = resolve_real_account_limits(store, settings, target_account)
    _is_sell = str(side or "").upper() == "SELL"
    if limits.limit_profile_unresolved and not _is_sell:
        reason = f"LIMIT_PROFILE_UNRESOLVED: {limits.limit_profile_reason}"
        try:
            store.audit(
                "LIMIT_PROFILE_UNRESOLVED",
                {"account_id": target_account, "strategy_id": strategy_id,
                 "symbol": symbol, "side": side, "reason": limits.limit_profile_reason},
                strategy_id,
                actor="service:real_gate",
            )
            from .alerts import AlertManager
            AlertManager(store).send(
                "ERROR", "LIMIT_PROFILE_UNRESOLVED",
                f"实盘限额派生失败，已拒绝订单：账户 {target_account}，原因：{limits.limit_profile_reason}",
                incident_key=f"LIMIT_PROFILE_UNRESOLVED:{target_account}",
                tenant_id=_account_tenant_id(store, target_account),
            )
        except Exception:  # 告警/审计失败不得改变 fail-closed 结果
            logging.getLogger(__name__).exception("记录 LIMIT_PROFILE_UNRESOLVED 失败")
        return RealTradeDecision(False, reason, {"account_id": target_account,
                                                  "reason_code": "LIMIT_PROFILE_UNRESOLVED"})
    if limits.limit_profile_unresolved and _is_sell:
        try:
            store.audit(
                "LIMIT_PROFILE_UNRESOLVED_SELL_ALLOWED",
                {"account_id": target_account, "strategy_id": strategy_id,
                 "symbol": symbol, "side": side, "reason": limits.limit_profile_reason},
                strategy_id,
                actor="service:real_gate",
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "记录 LIMIT_PROFILE_UNRESOLVED_SELL_ALLOWED 失败，保持 SELL 放行"
            )
        logging.getLogger(__name__).warning(
            "LIMIT_PROFILE_UNRESOLVED_SELL_ALLOWED account=%s reason=%s",
            target_account, limits.limit_profile_reason,
        )
    if limits.config_corrupt:
        # 坏键（Reviewer 第 2 条）：拦截点就在既有判定链里，由 submit_limit 天然调用，
        # 不另建判断点。只拦开仓方向；SELL 继续走持仓 / 减仓额度 / 对账等既有校验。
        if not _is_sell:
            notify_corrupt_account_config(store, target_account, limits.corrupt_reason)
            return RealTradeDecision(
                False,
                f"实盘账户 {target_account} 的账户级配置损坏（{limits.corrupt_reason}）："
                "已禁止开仓买入，请修复 real_account_config 后重试",
                {"account_id": target_account, "config_corrupt": True},
            )
        # SELL：只落审计、**不推送**（Reviewer 第 2 条）—— 坏键期间每笔减仓都弹卡
        # 会把真告警淹没，用户最反感这种刷屏。
        store.audit(
            "REAL_ACCOUNT_CONFIG_CORRUPT_SELL_ALLOWED",
            {
                "account_id": target_account,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "reason": limits.corrupt_reason,
            },
            strategy_id,
            actor="service:real_gate",
        )
    elif not limits.enabled:
        return RealTradeDecision(False, f"实盘账户 {target_account} 的独立交易开关已关闭", {})
    allowed_strategies = limits.allowed_strategies
    max_order = limits.max_order_notional
    max_daily = limits.max_daily_notional
    require_confirm = limits.require_manual_confirm

    # 2.1 P0-7：减仓额度走**唯一解析入口**（账户级 risk 段 → 租户级 → env）。
    # 配置不完整 → fail-closed 拒绝本单，绝不回落 env 静默补齐。
    try:
        max_reduction_order, max_reduction_daily = reduction_notional_limits(
            store, _RiskBrokerScope(account_id=target_account),
        )
    except RiskLimitsConfigError as exc:
        return RealTradeDecision(False, str(exc), {"account_id": target_account})

    if strategy_id not in allowed_strategies:
        return RealTradeDecision(False, f"策略 {strategy_id} 不在实盘白名单", {})

    active, reason, session = session_status(
        store, settings, now, account_id=target_account, require_confirm=require_confirm,
    )
    if not active:
        return RealTradeDecision(False, reason, {"session": session})

    multiplier = contract_multiplier(symbol, metadata.get("multiplier"))
    notional = abs(float(quantity) * float(price) * multiplier)
    if notional > max_order:
        return RealTradeDecision(
            False,
            f"实盘单笔名义金额 ${notional:,.2f} 超过上限 ${max_order:,.2f}",
            {"notional": notional, "account_id": target_account},
        )

    day_start = ny_trading_day_start_utc(now)
    # 开仓与减仓是两个独立的桶（Product 额度模型）：
    #   BUY  → 开仓额度（max_daily_notional）
    #   SELL → 减仓额度（RISK_MAX_REDUCTION_*，此前实盘侧从未接入）
    # 混算会让「白天开仓用满额度 → 晚上盈利的平仓单被自己的买入额度挡住」。
    # 注意：成功返回里的 used_today 必须按方向取 —— SELL 分支定义的是
    # used_reduction，若在这里引用 used 会 UnboundLocalError（core_satellite 实测报错）。
    if str(side or "").upper() == "SELL":
        if notional > max_reduction_order:
            return RealTradeDecision(
                False,
                f"实盘单笔减仓金额 ${notional:,.2f} 超过减仓上限 ${max_reduction_order:,.2f}",
                {"notional": notional, "account_id": target_account},
            )
        used_today = submitted_real_reduction_notional_since(
            store, day_start.isoformat(), exclude_intent_id, account_id=target_account,
        )
        if used_today + notional > max_reduction_daily:
            return RealTradeDecision(
                False,
                f"实盘账户 {target_account} 当日累计减仓金额将超过 ${max_reduction_daily:,.2f}",
                {"notional": notional, "used_today": used_today, "account_id": target_account},
            )
    else:
        used_today = submitted_real_entry_notional_since(
            store, day_start.isoformat(), exclude_intent_id, account_id=target_account,
        )
        if used_today + notional > max_daily:
            return RealTradeDecision(
                False,
                f"实盘账户 {target_account} 当日累计名义金额将超过 ${max_daily:,.2f}",
                {"notional": notional, "used_today": used_today, "account_id": target_account},
            )
    return RealTradeDecision(
        True, "实盘安全闸门通过",
        {"notional": notional, "used_today": used_today, "account_id": target_account},
    )


@dataclass(frozen=True)
class GateLayer:
    """实盘闸口链上的一道开关及其当前状态（供界面直读，不做二次判断）。"""

    key: str          # 稳定标识：env / master_switch / tenant / account / confirm_session
    label: str        # 人话名称
    scope: str        # 作用域：env / tenant / account / session
    ok: bool
    detail: str       # 当前值（开启显示值，关闭显示原因）


@dataclass(frozen=True)
class TradingGateOverview:
    allowed: bool
    layers: tuple[GateLayer, ...]
    blocking: tuple[GateLayer, ...]
    strategies: tuple[tuple[str, bool], ...]
    tenant_id: str
    account_id: str
    verdict: str


def trading_gate_overview(
    store: TradingStore,
    settings: Settings,
    tenant_id: str | None = None,
    account_id: int | str | None = None,
    now: datetime | None = None,
    strategies: tuple[str, ...] = (),
    strategy_defaults: dict[str, bool] | None = None,
) -> TradingGateOverview:
    """把实盘「能不能下单」的五道闸口一次算清，供控制页展示。

    为什么要有它：``check_real_trade`` 的判定散在 .env、租户设置、账户级 JSON、
    确认会话四处，界面各处只显示自己那一层 → 用户看到「账户开关是开的」却不知道为什么
    不下单。这里按**下单路径的真实顺序**逐层求值，并直接给出「被哪一层挡住」。

    注意：本函数**只读**，不改任何配置；判定口径必须与 ``check_real_trade`` 一致 ——
    两边不一致就是又一条「界面说的和实际跑的」漂移。
    """
    default_account = str(
        account_id
        or getattr(settings, "real_account_id", "")
        or ""
    ).strip()
    tenant = str(tenant_id or "").strip()
    layers: list[GateLayer] = []

    env = str(getattr(settings, "trading_env", "") or "").upper()
    layers.append(GateLayer(
        key="env", label="券商环境（FUTU_TRADING_ENV）", scope="env",
        ok=env == "REAL",
        detail=f"当前 {env or '未配置'}" + ("" if env == "REAL" else "，实盘下单直接拒绝"),
    ))

    master = bool(getattr(settings, "real_trading_enabled", False))
    layers.append(GateLayer(
        key="master_switch", label="实盘总开关（REAL_TRADING_ENABLED）", scope="env",
        ok=master, detail="已开启" if master else "已关闭 · 所有实盘路径拦截",
    ))

    if tenant:
        tenant_ok = bool(resolve_tenant_active(tenant, store=store))
        layers.append(GateLayer(
            key="tenant", label=f"租户开关（{tenant}）", scope="tenant",
            ok=tenant_ok,
            detail="worker 会拉起" if tenant_ok else "worker 不启动（实盘监控也会消失）",
        ))
    else:
        layers.append(GateLayer(
            key="tenant", label="租户开关", scope="tenant", ok=False,
            detail="未指定租户，无法判定",
        ))

    limits = None
    if default_account:
        limits = resolve_real_account_limits(store, settings, default_account)
        whitelist = ",".join(sorted(limits.allowed_strategies)) or "（空，全部拒绝）"
        layers.append(GateLayer(
            key="account", label=f"账户开关（{default_account}）", scope="account",
            ok=limits.enabled,
            detail=(
                f"{'已开启' if limits.enabled else '已关闭'}"
                f" · 白名单 {whitelist}"
                f" · 单笔 ${limits.max_order_notional:,.0f} / 单日 ${limits.max_daily_notional:,.0f}"
            ),
        ))
    else:
        layers.append(GateLayer(
            key="account", label="账户开关", scope="account", ok=False,
            detail="未配置 REAL_ACCOUNT_ID，无法判定",
        ))

    # 人工确认会话：只在账户级 require_manual_confirm 为真时才真正参与拦截。
    # 注意不能直接看「session 是否为空」——关闭确认时不写会话也放行，那才是常态。
    if limits is not None:
        if not limits.require_manual_confirm:
            layers.append(GateLayer(
                key="confirm_session", label="人工确认会话", scope="session", ok=True,
                detail="未启用（REAL_REQUIRE_MANUAL_CONFIRM=false）· 自动放行",
            ))
        else:
            active, reason, session = session_status(
                store, settings, now,
                account_id=default_account,
                require_confirm=True,
            )
            expires = str(session.get("expires_at") or "")
            layers.append(GateLayer(
                key="confirm_session", label="人工确认会话", scope="session",
                ok=active,
                detail=(f"有效至 {expires}" if active else reason),
            ))

    blocking = tuple(layer for layer in layers if not layer.ok)
    strategy_rows: list[tuple[str, bool]] = []
    defaults = strategy_defaults or {}
    for strategy_id in strategies:
        strategy_rows.append((
            strategy_id,
            resolve_strategy_enabled(
                store, strategy_id, tenant or None, default=bool(defaults.get(strategy_id, False)),
            ),
        ))

    if blocking:
        verdict = "实盘不可下单 · 被「" + "、".join(layer.label for layer in blocking) + "」拦截"
    else:
        verdict = "实盘可下单 · 五道闸口全部通过"
    return TradingGateOverview(
        allowed=not blocking,
        layers=tuple(layers),
        blocking=blocking,
        strategies=tuple(strategy_rows),
        tenant_id=tenant,
        account_id=default_account,
        verdict=verdict,
    )
