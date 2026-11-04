"""风险与资金参数的租户化解析（P0-7）—— **唯一解析入口**。

设计文档：``docs/risk_limits_tenant_scoping_design.md``

背景：``RiskLimits`` 的 10 个资金/亏损类参数此前全部从 ``os.getenv`` 读取，
一个进程一套、所有租户共用。2026-10-22 夜，模拟账户 ``20170229`` 当日亏 $10,351.89
撞上**全局** ``RISK_MAX_DAILY_LOSS=10000`` 被自动急停 —— 用一个数字管两个资金量
完全不同的账户，必然一边太松、一边太紧。

三层解析链（高 → 低）：

    ① ``real_account_config:{account_id}`` 的 ``risk`` 段   账户级，最具体
    ② ``risk_limits:{tenant_id}``                           租户级
    ③ env 全局默认                                          仅兜底

铁律（与 P1-3 为止损限额定下的规矩同源，这里推广到全部参数）：

* **部分配置即拒绝**：账户级只要出现任一资金/亏损类参数，就必须齐全；缺任一项
  → 抛 ``RiskLimitsConfigError`` → 调用方拒绝开仓 + ERROR 审计，
  **不得用 env 静默补齐**。
* 只有账户级**完全没有** ``risk`` 段时才回落，且回落必须**记 WARN 审计**、
  次数**可观测**（审计事件 + ``risk_limits_env_fallback_count`` 计数）。
* **缓存键必须含 (账户, 交易日)**，并以配置原文指纹校验新鲜度 ——
  界面上改了账户参数，运行中的进程**立即重解析**，不等重启。

> 模块归属说明：本模块是 ``risk.py`` 的**下游**（``risk_limits`` → ``risk``），
> ``risk.py`` 只在 ``RiskEngine.__init__`` 内**惰性**导入本模块，避免循环导入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import uuid
from typing import Any

from .risk import RiskLimits, ny_trading_day_start_utc


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 契约常量
# --------------------------------------------------------------------------- #

#: env 回落次数的持久计数键（界面可读，满足「回落次数可观测」）。
#: 这是**计数器**不是配置，故意保持全局；每次回落的审计事件里带
#: ``account_id`` / ``tenant_id``，按租户统计从审计表即可。
ENV_FALLBACK_COUNT_KEY = "risk_limits_env_fallback_count"

#: 账户级 ``risk`` 段的**配置键** → 类型。
#: 设计文档写「11 个参数」，但 ``RiskLimits`` 实际是 10 个资金/亏损字段
#: （第 11 个 ``correlation_clusters`` 属「天然全局」，保持全局，见设计 §2）。
#:
#: **配置键与 ``RiskLimits`` 属性名不必相同**：同一个 ``real_account_config`` JSON
#: 对象里，**顶层** ``max_order_notional`` / ``max_daily_notional`` 是**实盘门禁**
#: （``real_trading.check_real_trade`` 专用、REAL only）；``risk`` 段这两个是
#: **风控引擎**（全模式）。两者**同名不同层、不同归属、不同数值**
#: （实测：门禁 2500 / 风控 100000），正是"下一个误会的起点"。
#: Chief 10-23 裁定：``risk`` 段的开仓侧两键改名，顶层保持不动
#: （顶层是活配置，改名要迁移；新键改名零成本）。
ACCOUNT_RISK_FIELDS: tuple[tuple[str, type], ...] = (
    ("max_entry_notional", float),
    ("max_entry_daily_notional", float),
    ("max_reduction_order_notional", float),
    ("max_daily_reduction_notional", float),
    ("max_pending_orders", int),
    ("max_symbol_weight", float),
    ("max_gross_exposure", float),
    ("max_option_premium", float),
    ("max_daily_loss", float),
    ("max_drawdown_pct", float),
)

#: 配置键 → ``RiskLimits`` 属性名。只有开仓侧两键需要映射，其余同名。
ACCOUNT_RISK_FIELD_TO_LIMITS: dict[str, str] = {
    "max_entry_notional": "max_order_notional",
    "max_entry_daily_notional": "max_daily_notional",
}

#: 写库用：``risk`` 段的键序列（迁移脚本按此拼 JSON）。
RISK_FIELD_NAMES: tuple[str, ...] = tuple(name for name, _ in ACCOUNT_RISK_FIELDS)

#: 取值用：对应的 ``RiskLimits`` 属性名序列（迁移脚本取生效值时按此 ``getattr``）。
RISK_LIMITS_ATTR_NAMES: tuple[str, ...] = tuple(
    ACCOUNT_RISK_FIELD_TO_LIMITS.get(name, name) for name, _ in ACCOUNT_RISK_FIELDS
)

_POSITIVE_FIELDS = (
    "max_order_notional",
    "max_daily_notional",
    "max_reduction_order_notional",
    "max_daily_reduction_notional",
    "max_option_premium",
    "max_daily_loss",
)
_RATIO_FIELDS = ("max_symbol_weight", "max_gross_exposure", "max_drawdown_pct")


class RiskLimitsConfigError(RuntimeError):
    """风控参数配置不完整 / 无法解析 —— **fail-closed**：拒绝开仓，不得回落 env。

    注意：这是**订单级**拒绝信号，不是服务启动失败信号。``RiskEngine`` 构造期
    捕获它并记录，绝不因此让进程起不来。
    """


# --------------------------------------------------------------------------- #
# 共享键入口
# --------------------------------------------------------------------------- #

def risk_limits_tenant_key(tenant_id: str) -> str:
    """租户级风控配置的键 —— **一律走共享入口** ``tenant.risk_limits_config_key``。

    铁律（Chief 10-23）：**杜绝手拼配置键**。``risk_limits:{tenant_id}`` 的拼法
    只有 ``tenant.py`` 的 ``resolve_tenant_config_key`` 一个出处；本函数只是
    转发，**不含任何本地拼法** —— 两处各拼一份，迟早拼歪，而拼歪的那一处
    会**静默读不到配置**（正是今晚一切事故的母题）。

    共享入口是 fail-closed 的：``tenant_id`` 缺失或不在注册表 → 抛
    ``TenantConfigKeyError``，**绝不**回落到无前缀全局键。
    """
    from .tenant import risk_limits_config_key

    return str(risk_limits_config_key(tenant_id))


def real_account_config_key(account_id: str) -> str:
    """账户级配置键 ``real_account_config:{account_id}`` —— 同样走共享入口。

    作用域是**券商账号**（不是 tenant_id），由 ``tenant.real_account_config_key``
    用 ``extra_ids=(account_id,)`` 覆盖校验。
    """
    from .tenant import real_account_config_key

    return str(real_account_config_key(account_id))


def legacy_risk_limits_key() -> str:
    """**无前缀旧键** ``risk_limits`` —— 读侧兜底，**永不写入**。

    读侧规则固定为「租户键优先、无前缀旧键兜底」（与 ``tenant.py`` 的
    ``tenant_config_key_candidates`` 同一口径），且**回落必须显式留痕** ——
    否则就成了今晚反复付代价的「靠调用方记得记录」。
    """
    from .tenant import RISK_LIMITS_KEY, legacy_config_key

    return str(legacy_config_key(RISK_LIMITS_KEY))


# --------------------------------------------------------------------------- #
# broker 作用域提取（不把 mock 对象带进解析链路）
# --------------------------------------------------------------------------- #

def _broker_account_id(broker: Any) -> str:
    if broker is None:
        return ""
    from .tenant import broker_scope

    _, account = broker_scope(broker)
    return str(account) if account not in (None, "") else ""


def _broker_tenant_id(broker: Any, account_id: str) -> str:
    """解析 broker 的租户标识，且**只接受注册表里存在的租户**。

    为什么要校验：下游 ``risk_limits_config_key`` 是 fail-closed 的（未注册租户
    直接抛异常）。若不在这里挡住，一个非法 ``tenant_id`` 会让整条下单链路
    拒绝 —— 那是把「键拼错」放大成「全系统停摆」，比读不到配置更糟。
    """
    from .tenant import DEFAULT_TENANTS, resolve_tenant, tenant_of_broker

    candidates: list[str] = []
    if broker is not None:
        # P0-9：走共享入口 tenant_of_broker（显式 → 账户反解）。
        # 裸 getattr(broker, "tenant_id") 在生产 broker 上恒空 —— 此处只因
        # 下面的 account_id 分支才侥幸正确；一旦将来有人调整分支顺序就会静默失效。
        tenant = tenant_of_broker(broker, strict=False)[0]
        if isinstance(tenant, str) and tenant.strip():
            candidates.append(tenant.strip())
    if account_id:
        try:
            profile = resolve_tenant(account_id=account_id)
            if profile is not None:
                candidates.append(str(profile.tenant_id))
        except Exception:
            # resolve_tenant 在「给了 account_id 但注册表里没有」时会抛 ValueError；
            # 这属于「该账户尚未登记租户」，不是错误 —— 降级为无租户上下文。
            logger.warning("无法由账户 %s 反解租户，租户级风控配置不可用", account_id)
    for tenant_id in candidates:
        if tenant_id in DEFAULT_TENANTS:
            return tenant_id
    return ""


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_json_object(raw: Any) -> dict:
    """把一段 setting 原文解析成 dict；空/非 dict/损坏一律返回 ``{}``。"""
    text = _as_text(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("风控配置 JSON 无法解析，按「未配置」处理：%r", text[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# 参数段 → RiskLimits
# --------------------------------------------------------------------------- #

def _is_present(value: Any) -> bool:
    """``0`` 是有效配置（例如 ``max_pending_orders=0``），只有 ``None``/``""`` 算缺。"""
    return value is not None and value != ""


def _risk_section_to_limits(section: dict, source: str) -> RiskLimits | None:
    """把一个 ``risk`` 段转成 ``RiskLimits``。

    * 段里**没有任何**已知风控参数 → 返回 ``None``（视为「这一层未配置」，继续往下找）；
    * 段里**部分**配置 → 抛 ``RiskLimitsConfigError``（fail-closed，**不许**用 env 补齐）；
    * 段里配置齐全 → 返回 ``RiskLimits``。

    **出现不认识的键 → 记 WARN**（不是静默忽略）：那通常是拼错的字段名或改名前的旧名
    —— 静默忽略会让"配置写了但没生效"，正是今晚反复付代价的形态。
    """
    known = {name for name, _ in ACCOUNT_RISK_FIELDS}
    unknown = sorted(key for key in section if key not in known)
    if unknown:
        logger.warning(
            "%s 的 risk 段含无法识别的字段：%s（已忽略，请核对拼写或改名）", source, unknown
        )

    present = [name for name, _ in ACCOUNT_RISK_FIELDS if _is_present(section.get(name))]
    if not present:
        return None
    missing = [name for name, _ in ACCOUNT_RISK_FIELDS if name not in present]
    if missing:
        raise RiskLimitsConfigError(
            f"{source} 的风控参数配置不完整：已配置 {len(present)}/{len(ACCOUNT_RISK_FIELDS)} 项，"
            f"缺少 {', '.join(missing)}。"
            f"按 P0-7 铁律「部分配置即拒绝」——账户级只要出现任一资金/亏损类参数就必须齐全，"
            f"拒绝开仓并记 ERROR 审计，**不得用 env 静默补齐**。"
        )
    return _coerce_risk(section, source)


def _coerce_risk(section: dict, source: str) -> RiskLimits:
    """把 ``risk`` 段转成 ``RiskLimits`` —— **配置键 → 属性名的映射在这里做**。"""
    values: dict[str, Any] = {}
    for name, kind in ACCOUNT_RISK_FIELDS:
        raw = section.get(name)
        try:
            values[ACCOUNT_RISK_FIELD_TO_LIMITS.get(name, name)] = (
                int(float(raw)) if kind is int else float(raw)
            )
        except (TypeError, ValueError) as exc:
            raise RiskLimitsConfigError(
                f"{source} 的风控参数 {name}={raw!r} 无法解析为数字"
            ) from exc
    _validate_risk_values(values, source)
    return RiskLimits(**values)


def _validate_risk_values(values: dict[str, Any], source: str) -> None:
    for name in _POSITIVE_FIELDS:
        if not values[name] > 0:
            raise RiskLimitsConfigError(f"{source} 的风控参数 {name} 必须为正数，当前为 {values[name]}")
    for name in _RATIO_FIELDS:
        if not 0 < values[name] <= 1:
            raise RiskLimitsConfigError(
                f"{source} 的风控参数 {name} 必须在 (0, 1] 区间，当前为 {values[name]}"
            )
    if values["max_pending_orders"] < 0:
        raise RiskLimitsConfigError(
            f"{source} 的风控参数 max_pending_orders 不能为负，当前为 {values['max_pending_orders']}"
        )


# --------------------------------------------------------------------------- #
# 可观测性：env 回落
# --------------------------------------------------------------------------- #

#: 进程内 env 回落次数（实时计数器）。
ENV_FALLBACK_TOTAL = 0


def env_fallback_count() -> int:
    """返回**本进程**观察到 env 回落的次数（跨账户/跨日累计）。"""
    return ENV_FALLBACK_TOTAL


def _record_env_fallback(store: Any, account_id: str, tenant_id: str) -> None:
    """env 兜底必须**留痕且可观测** —— 静默回落是 10-22 一切问题的母题。"""
    global ENV_FALLBACK_TOTAL

    ENV_FALLBACK_TOTAL += 1
    logger.warning(
        "RISK_LIMITS_ENV_FALLBACK 账户=%s 租户=%s：账户级与租户级风控配置均缺失，"
        "回落 env 全局默认（迁移前状态）",
        account_id or "<无>",
        tenant_id or "<无>",
    )
    payload = {
        "account_id": account_id or None,
        "tenant_id": tenant_id or None,
        "source": "env",
        "message": "账户级与租户级风控配置均缺失，回落 env 全局默认（迁移前状态）",
    }
    try:
        store.audit("RISK_LIMITS_ENV_FALLBACK", payload, actor="service:risk")
    except Exception:
        logger.warning("风控 env 回落审计写入失败", exc_info=True)

    try:
        raw = _as_text(store.get_setting(ENV_FALLBACK_COUNT_KEY, "0"))
        previous = int(float(raw)) if raw else 0
    except (TypeError, ValueError):
        previous = 0
    try:
        store.set_setting(ENV_FALLBACK_COUNT_KEY, str(previous + 1))
    except Exception:
        logger.warning("风控 env 回落计数写入失败", exc_info=True)


def _record_legacy_key_fallback(store: Any, account_id: str, tenant_id: str, legacy_key: str) -> None:
    """无前缀旧键兜底必须**留痕** —— 兜底本身就意味着「租户键还没建起来」。

    与 env 回落分开记：这是**读侧**的兼容路径（``tenant.legacy_config_key``），
    不是配置缺失。两者混在一张计数里，就看不出"到底是没配、还是配在旧键上"。
    """
    logger.warning(
        "RISK_LIMITS_LEGACY_KEY_FALLBACK 账户=%s 租户=%s：租户键 %s 缺失，"
        "回落无前缀旧键 %s（迁移前存量状态）",
        account_id or "<无>", tenant_id, risk_limits_tenant_key(tenant_id), legacy_key,
    )
    try:
        store.audit("RISK_LIMITS_LEGACY_KEY_FALLBACK", {
            "account_id": account_id or None,
            "tenant_id": tenant_id,
            "tenant_key": risk_limits_tenant_key(tenant_id),
            "legacy_key": legacy_key,
            "message": "租户键缺失，回落无前缀旧键（读侧兼容路径，旧键永不写入）",
        }, actor="service:risk")
    except Exception:
        logger.warning("风控旧键回落审计写入失败", exc_info=True)


# --------------------------------------------------------------------------- #
# 缓存（键含 账户 + 交易日，并以配置原文指纹保证「改了立即重解析」）
# --------------------------------------------------------------------------- #

_CACHE: dict[tuple[str, str], tuple[str, "RiskLimitsResolution"]] = {}
_CACHE_MAX = 64


def _trading_day_key(now: datetime | None = None) -> str:
    """纽约交易日（YYYY-MM-DD）—— 缓存键的一半。

    ``ny_trading_day_start_utc`` 返回的正是「纽约当日零点」换算到 UTC 的瞬时
    （即该纽约日的 UTC 零点），所以直接取 ``.date()`` 就是纽约交易日。
    """
    return ny_trading_day_start_utc(now).date().isoformat()


def _derived_fingerprint_parts(store: Any, account_id: str, tenant_id: str) -> list[str] | None:
    """派生启用时要并入指纹的两段：**profile 原文** + **当前权益快照**。

    派生金额 = 权益 × profile 里的百分比，所以这两者任一变化都必须让缓存失效：

    - **profile 原文**：页面把 6% 改成 8% 时，若指纹只看权益，改了就不生效——
      现在只是恰好靠权益快照时间在变，一旦快照停更（收盘后、权益写入失败），
      就会出现「改了但不生效且无提示」，与「发布后 ≤2 分钟生效」的承诺不符。
    - **权益快照**：权益跌了还拿旧（更高）权益算出的限额放行，是最危险的方向。

    未启用派生时返回 ``None``（指纹行为与改动前逐字节一致）。
    配置损坏时返回 **error 哨兵**（必然与旧缓存不等）：绝不允许退化成
    「当作未启用」而恰好命中旧缓存继续放行，真正拒单由 `_apply_limit_profile` 负责。
    """
    try:
        from .limit_profile_store import load_limit_profile, profile_key

        resolution = load_limit_profile(store, tenant_id)
    except Exception:  # noqa: BLE001 - 坏配置必须让缓存失效，不能退化成旧口径命中
        return [f"error:{uuid.uuid4().hex}"]
    if resolution.source != "derived:limit_profile":
        return None

    parts: list[str] = []
    reader = getattr(store, "get_runtime_config", None)
    raw_profile: Any = None
    if callable(reader):
        try:
            rows = reader(profile_key(tenant_id))
            raw_profile = rows[0] if rows else None
        except Exception:  # noqa: BLE001
            raw_profile = None
    if raw_profile is None:
        parts.append(f"profile:unreadable:{uuid.uuid4().hex}")
    else:
        parts.append(
            "profile:"
            + json.dumps(raw_profile, sort_keys=True, ensure_ascii=False, default=str)
        )

    try:
        from .tenant import equity_snapshot_env

        # 净值快照 env 走共享入口（模拟账户也有 account_id，反推会拼出 REAL_<paper acc>）
        env = equity_snapshot_env(tenant_id, account_id)
        equity_rows = store.equity_history(limit=1, env=env)
    except Exception:  # noqa: BLE001
        parts.append(f"equity:degraded:{uuid.uuid4().hex}")
        return parts
    if not equity_rows:
        parts.append(f"equity:missing:{uuid.uuid4().hex}")
        return parts
    snapshot = equity_rows[0]
    amount = snapshot.get("total_assets")
    timestamp = snapshot.get("created_at") or snapshot.get("updated_at")
    if amount is None or not timestamp:
        parts.append(f"equity:missing:{uuid.uuid4().hex}")
        return parts
    parts.append(f"equity:{amount}|{timestamp}")
    return parts


def _source_fingerprint(store: Any, account_id: str, tenant_id: str) -> str:
    """账户级 + 租户级配置原文的指纹。

    只要界面上改了账户参数，指纹就变 → 缓存立即失效 → **当场重解析**，
    不依赖重启，也不依赖 ``updated_at`` 轮询。

    派生层启用时**额外拼入 profile 原文与当前权益快照**（见
    `_derived_fingerprint_parts`）：百分比或权益一变指纹就变，同一轮内多次调用
    仍复用缓存，但不会跨轮拿着旧权益或旧百分比继续放行。
    """
    parts: list[str] = []
    if account_id:
        parts.append(_as_text(store.get_setting(real_account_config_key(account_id), "")))
    if tenant_id:
        parts.append(_as_text(store.get_setting(risk_limits_tenant_key(tenant_id), "")))
        # 无前缀旧键也进指纹 —— 否则改了旧键缓存不失效，兜底会读到旧值。
        parts.append(_as_text(store.get_setting(legacy_risk_limits_key(), "")))
        derived_parts = _derived_fingerprint_parts(store, account_id, tenant_id)
        if derived_parts:
            parts.extend(derived_parts)
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class RiskLimitsResolution:
    """一次解析的完整结果（供可观测性与测试断言使用）。"""

    limits: RiskLimits
    source: str
    account_id: str
    tenant_id: str
    trading_day: str
    env_fallback: bool


def _apply_limit_profile(store: Any, tenant_id: str, limits: RiskLimits, account_id: str) -> RiskLimits:
    """将派生配置应用到风控实际读侧；旧金额键不参与生效。"""
    if not tenant_id:
        return limits
    from .limit_profile import resolve_limit_chain
    from .limit_profile_store import load_limit_profile

    try:
        profile_resolution = load_limit_profile(store, tenant_id)
    except Exception as exc:  # noqa: BLE001 - 坏配置必须落到统一 fail-closed 出口
        # 指纹层只用 error 哨兵让缓存失效；真正拒单统一走 RiskLimitsConfigError，
        # 否则未捕获的 ValueError 会从风控解析里冒出去，调用方拿不到稳定拒单信号。
        raise RiskLimitsConfigError(f"派生限额配置无法解析：{exc}") from exc
    if profile_resolution.source != "derived:limit_profile":
        return limits
    from .tenant import TenantConfigKeyError, equity_snapshot_env

    try:
        # 净值快照 env 只从共享入口派生：按租户 trading_env 决定，不按 account_id 反推。
        # 模拟账户即使有 account_id，也必须读取 SIMULATE；解析失败严格拒单。
        env = equity_snapshot_env(tenant_id, account_id)
    except TenantConfigKeyError as exc:
        # 环境判定不了就 fail-closed，并给调用方稳定的拒单信号，不许静默猜一个 env。
        raise RiskLimitsConfigError(f"派生限额无法判定净值快照环境：{exc}") from exc
    rows = store.equity_history(limit=1, env=env)
    if not rows:
        raise RiskLimitsConfigError("派生限额需要有效权益，当前无权益快照")
    try:
        equity = float(rows[0].get("total_assets") or 0)
    except (TypeError, ValueError) as exc:
        raise RiskLimitsConfigError("派生限额权益无法解析") from exc
    chain = resolve_limit_chain(store, tenant_id, equity, profile_resolution.profile)
    values = dict(limits.__dict__)
    values["max_order_notional"] = min(chain.per_order, chain.capacity_line) if chain.capacity_line is not None else chain.per_order
    values["max_daily_notional"] = chain.daily_entries
    values["max_daily_loss"] = chain.daily_loss
    return RiskLimits(**values)


def _resolve_uncached(store: Any, account_id: str, tenant_id: str) -> RiskLimitsResolution:
    # ① 账户级：real_account_config:{account_id} 的 risk 段（键走共享入口）
    if account_id:
        account_key = real_account_config_key(account_id)
        account_cfg = _parse_json_object(store.get_setting(account_key, ""))
        account_risk = account_cfg.get("risk")
        if isinstance(account_risk, dict):
            limits = _risk_section_to_limits(account_risk, account_key)
            if limits is not None:
                limits = _apply_limit_profile(store, tenant_id, limits, account_id)
                return RiskLimitsResolution(
                    limits=limits,
                    source=account_key,
                    account_id=account_id,
                    tenant_id=tenant_id,
                    trading_day=_trading_day_key(),
                    env_fallback=False,
                )

    # ② 租户级：risk_limits:{tenant_id}，**租户键优先、无前缀旧键兜底**
    if tenant_id:
        tenant_key = risk_limits_tenant_key(tenant_id)
        tenant_risk = _parse_json_object(store.get_setting(tenant_key, ""))
        limits = _risk_section_to_limits(tenant_risk, tenant_key)
        if limits is not None:
            limits = _apply_limit_profile(store, tenant_id, limits, account_id)
            return RiskLimitsResolution(
                limits=limits,
                source=tenant_key,
                account_id=account_id,
                tenant_id=tenant_id,
                trading_day=_trading_day_key(),
                env_fallback=False,
            )
        # 迁移前存量状态：租户键缺失但无前缀旧键存在 → 兜底，且**显式留痕**
        legacy_key = legacy_risk_limits_key()
        legacy_risk = _parse_json_object(store.get_setting(legacy_key, ""))
        legacy_limits = _risk_section_to_limits(legacy_risk, legacy_key)
        if legacy_limits is not None:
            _record_legacy_key_fallback(store, account_id, tenant_id, legacy_key)
            legacy_limits = _apply_limit_profile(store, tenant_id, legacy_limits, account_id)
            return RiskLimitsResolution(
                limits=legacy_limits,
                source=legacy_key,
                account_id=account_id,
                tenant_id=tenant_id,
                trading_day=_trading_day_key(),
                env_fallback=False,
            )

    # ③ env 兜底（迁移前状态）—— 必须 WARN 且可观测
    _record_env_fallback(store, account_id, tenant_id)
    return RiskLimitsResolution(
        limits=RiskLimits.load(),
        source="env",
        account_id=account_id,
        tenant_id=tenant_id,
        trading_day=_trading_day_key(),
        env_fallback=True,
    )


def resolve_risk_limits_detail(store: Any, broker: Any = None) -> RiskLimitsResolution:
    """解析某 broker 账户的生效风控参数，返回带来源的完整结果。

    **缓存键 = (账户, 交易日)**；每次调用都用配置原文指纹校验新鲜度，
    因此跨账户、跨日、以及界面改动都会**立即重解析**。
    """
    account_id = _broker_account_id(broker)
    tenant_id = _broker_tenant_id(broker, account_id)
    day = _trading_day_key()
    fingerprint = _source_fingerprint(store, account_id, tenant_id)

    cached = _CACHE.get((account_id, day))
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    resolution = _resolve_uncached(store, account_id, tenant_id)
    _CACHE[(account_id, day)] = (fingerprint, resolution)
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return resolution


def resolve_risk_limits(store: Any, broker: Any = None) -> RiskLimits:
    """**唯一解析入口**：返回某 broker 账户的生效 ``RiskLimits``。

    三层优先级：账户级 ``risk`` 段 → 租户级 ``risk_limits:{tenant_id}`` → env 兜底。

    抛出 ``RiskLimitsConfigError`` 表示配置不完整/无法解析 —— 调用方必须
    **拒绝开仓 + ERROR 审计**，**不得**回落 env 继续下单。
    """
    return resolve_risk_limits_detail(store, broker).limits


def clear_risk_limits_cache() -> None:
    """清空解析缓存（测试用）。"""
    _CACHE.clear()


__all__ = [
    "ACCOUNT_RISK_FIELD_TO_LIMITS",
    "ACCOUNT_RISK_FIELDS",
    "ENV_FALLBACK_COUNT_KEY",
    "RISK_FIELD_NAMES",
    "RISK_LIMITS_ATTR_NAMES",
    "RiskLimitsConfigError",
    "RiskLimitsResolution",
    "clear_risk_limits_cache",
    "env_fallback_count",
    "legacy_risk_limits_key",
    "real_account_config_key",
    "resolve_risk_limits",
    "resolve_risk_limits_detail",
    "risk_limits_tenant_key",
]
