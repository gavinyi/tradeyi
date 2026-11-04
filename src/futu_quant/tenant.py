"""
通用多租户元数据模型与租户注册表 (N-Tenant Architecture)

本模块同时是**租户配置键的唯一拼键入口**：所有 ``{base_key}:{tenant_id}`` 形态的键
都必须在 ``resolve_tenant_config_key`` 里拼出来，禁止各处手拼。

**只有一个拼键函数**（``resolve_tenant_config_key``）。曾有第二个同义名
``tenant_setting_key``，Reviewer 指出"两个名字会引来该用哪个的犹豫"——已删除，
``campaigns_config_key`` / ``risk_limits_config_key`` / ``real_account_config_key``
一律直接调 ``resolve_tenant_config_key``。**不要再加第三个名字。**

**本层不负责拼写变体**：``strategy_enabled:core_satellite`` 与
``strategy_enabled:Core Satellite`` 的两种写法由
``futu_quant.run_service.resolve_configured_strategies`` 解释，**不归这里管**。
本层只做"基键 + 租户"的拼接 —— 把拼写问题塞进本层，会把刚修好的写读对称重新搞混。

为什么必须集中：2026-10-22 夜间两租户共用无前缀全局键（``VOLATILITY_CAMPAIGNS``、
``strategy_enabled``、风险参数），一次误判就是**跨租户连坐**（实盘成交串进模拟盘）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal


class TenantConfigKeyError(ValueError):
    """租户配置键解析失败。

    **一律 fail-closed**：租户缺失、租户未注册、base_key 缺失、base_key 已带租户后缀
    都抛本异常。**绝不静默回落到无前缀全局键** —— 静默回落正是今晚一切事故的母题。
    """


@dataclass(frozen=True)
class TenantProfile:
    """全局租户元数据契约模型"""
    tenant_id: str                      # 全局唯一租户主键，例如 "futu_real_master", "futu_paper_sandbox"
    display_name: str                   # 界面与日志可读名称，如 "富途·实盘真金主专户"
    broker_type: str                    # 底座券商："futu", "ibkr", "alpaca" 等
    trading_env: Literal["REAL", "SIMULATE"]  # 真实资金环境还是沙盒环境
    account_id: str                     # 券商层真实账号（如 "281756481476169109", "20170229"）
    gateway_host: str = "127.0.0.1"     # 网关地址
    gateway_port: int = 11111           # 网关端口
    risk_profile: str = "default"       # 绑定的风控配置文件或规则 ID
    is_active: bool = True              # 租户启用状态


DEFAULT_TENANTS: dict[str, TenantProfile] = {
    "futu_real_master": TenantProfile(
        tenant_id="futu_real_master",
        display_name="富途·实盘真金主专户",
        broker_type="futu",
        trading_env="REAL",
        account_id="281756481476169109",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="real_strict",
        is_active=True,
    ),
    "futu_paper_sandbox": TenantProfile(
        tenant_id="futu_paper_sandbox",
        display_name="富途·模拟沙盒专户",
        broker_type="futu",
        trading_env="SIMULATE",
        account_id="20170229",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="paper_sandbox",
        is_active=True,
    ),
    "moomoo_real": TenantProfile(
        tenant_id="moomoo_real",
        display_name="Moomoo·US备用专户",
        broker_type="futu",
        trading_env="REAL",
        account_id="283445327916889204",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="real_moomoo_standby",
        is_active=False,
    ),
}


def resolve_tenant(
    target: str | TenantProfile | None = None,
    *,
    account_id: str | int | None = None,
    env: str | None = None,
    registry: dict[str, TenantProfile] | None = None,
    strict: bool = False,
) -> TenantProfile | None:
    """
    根据 tenant_id / TenantProfile 对象或 (account_id, env) 解析租户配置。
    支持注入自定义 registry 供测试使用，避免全局依赖。

    **fail-closed**：``target`` / ``account_id`` / ``env`` 三者皆无时**抛异常**，
    绝不静默回落到某个默认租户（旧行为返回模拟租户，会把配置写进别人的租户）。
    仅当**显式**给出 ``env`` 时才允许按环境取默认租户（``REAL`` / ``SIMULATE``）；
    未知环境同样抛异常。

    **调用方注意**：需要"没有上下文就不解析"的语义时请用
    :func:`tenant_of_broker`（它额外提供 broker → 账户反解，且 ``strict=True``
    会在写侧 fail-closed）。本函数**不得**被当作"随便给个 broker 就能拿到租户"的入口。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS

    if isinstance(target, TenantProfile):
        return target

    if isinstance(target, str) and target.strip():
        tid = target.strip()
        if tid in reg:
            return reg[tid]
        # 支持以别名 "REAL" / "SIMULATE" / "PAPER" 解析默认租户
        upper_tid = tid.upper()
        if upper_tid == "REAL" and "futu_real_master" in reg:
            return reg["futu_real_master"]
        if upper_tid in {"SIMULATE", "PAPER"} and "futu_paper_sandbox" in reg:
            return reg["futu_paper_sandbox"]
        if strict:
            raise ValueError(f"未注册的租户标识: '{tid}'")

    acc_str = str(account_id).strip() if account_id is not None else ""
    env_str = str(env).upper().strip() if env is not None else ""

    # 精确匹配 account_id 与 env
    for profile in reg.values():
        if acc_str and profile.account_id == acc_str:
            if not env_str or profile.trading_env == env_str:
                return profile
        elif env_str and profile.trading_env == env_str and not acc_str:
            return profile

    if strict or acc_str:
        raise ValueError(f"无法依据输入解析合法租户: target={target}, account_id={account_id}, env={env}")

    # ------------------------------------------------------------------
    # fail-closed：不允许"什么都不给就静默回落到某个租户"
    # ------------------------------------------------------------------
    #
    # 旧行为：走到这里就 ``return reg.get("futu_paper_sandbox")`` —— 即**模拟租户**。
    # 那是 fail-open：``resolve_tenant(None)``、``resolve_tenant(broker.tenant_id)``
    # （生产 broker 无该属性 → 恒空）都会**静默**拿到模拟租户，于是写侧把键写成
    # ``:{futu_paper_sandbox}`` —— 跨租户污染。
    #
    # P0-9 定案（Chief/Reviewer）：**无 target / account_id / env 时抛异常**。
    # 刻意**不做静态源码守卫** —— 运行时检查约束的是**不变量本身**，
    # 而不是"哪种调用形态算合法"；静态规则会随代码形态漂移并误伤合法写法。
    if not env_str:
        raise ValueError(
            "拒绝解析租户：target / account_id / env 三者皆无 —— "
            "没有上下文的解析只能是猜，而猜错会把配置写进别人的租户。"
            f"（target={target!r}, account_id={account_id!r}, env={env!r}）"
        )
    if env_str == "REAL":
        profile = reg.get("futu_real_master")
    elif env_str in {"SIMULATE", "PAPER"}:
        profile = reg.get("futu_paper_sandbox")
    else:
        # 未知环境同样不猜：旧行为会把它当成模拟盘
        raise ValueError(
            f"无法依据输入解析合法租户: 未知交易环境 {env_str!r}"
            f"（target={target}, account_id={account_id}, env={env}）"
        )
    if profile is None:
        raise ValueError(f"注册表里没有环境 {env_str!r} 对应的默认租户（registry={sorted(reg)}）")
    return profile


def broker_scope(broker) -> tuple[str | None, str | int | None]:
    """从 broker 取出 ``(trading_env, account_id)``，非真实值一律降级为 ``None``。

    生产 ``FutuPaperBroker`` 只带 ``account_id``、不带 ``tenant_id``；而测试替身
    （``MagicMock``）的任意属性访问都会返回另一个 mock 对象，直接透传会让
    ``resolve_tenant`` 解析失败。这里按类型过滤，保证拿到的要么是可用值、要么是
    ``None``（等价于「无租户上下文」），绝不把 mock 对象带进解析链路。
    """
    env = getattr(broker, "trading_env", None)
    account = getattr(broker, "account_id", None)
    return (
        env if isinstance(env, str) and env.strip() else None,
        account if isinstance(account, (str, int)) and not isinstance(account, bool) else None,
    )


def tenant_of_broker(
    broker,
    *,
    strict: bool = False,
    registry: dict[str, TenantProfile] | None = None,
) -> "tuple[str | None, str | int | None]":
    """从 broker **一次解析**出 ``(tenant_id, account_id)`` —— 所有调用点的唯一入口。

    **为什么要有它**：``broker.tenant_id`` **从未被赋值**（生产 ``FutuPaperBroker``
    只带 ``account_id``），于是同一个"从 broker 反查租户"的推导在六个地方各写了一遍，
    其中五处是漏的 —— 漏的那几处就退化成"租户为空"，写出来的键是**无前缀全局键**，
    两个租户互相顶掉（P0-1 的跨租户隔离因此是 no-op）。

    解析顺序（高 → 低，**一次解析、不再各推导一遍**）：

    1. ``broker.tenant_id`` 是**已注册**租户 → 直接采用；
    2. 否则用 :func:`broker_scope` 取 ``(trading_env, account_id)``（带类型过滤，
       防 ``MagicMock`` 泄漏），再 ``resolve_tenant(account_id=..., env=...)``
       反查注册表；
    3. 都解析不出 → ``strict=True`` 抛 :class:`TenantConfigKeyError`；
       ``strict=False`` 返回 ``(None, account_id)``（账户本身仍可用来查账户级配置）。

    **本层不静默回落到"某个默认租户"**：解析不出就是解析不出（``None`` 或异常）。
    ``resolve_tenant`` 自身有一层"只给 env 时按 REAL→futu_real_master 兜底"的
    向下兼容分支（``tenant.py:130-133``），**本函数刻意不走那条路** ——
    那是 fail-open：未注册的账户会被静默判成实盘租户，正是今晚跨租户连坐的来源。

    **写侧（会写租户键的路径）必须传 ``strict=True``** —— 否则等于"把一处静默回落
    换成另一处静默回落"，正是 Product 定的红线要治的东西。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS
    env, account = broker_scope(broker)

    explicit = getattr(broker, "tenant_id", None)
    if isinstance(explicit, str) and explicit.strip():
        tid = explicit.strip()
        if tid in reg:
            return tid, account
        # 显式给了但未注册：不猜，按未解析继续（strict 模式下报错）

    if account is not None:
        acc = str(account)
        try:
            profile = resolve_tenant(account_id=acc, env=env, registry=reg)
        except Exception:  # noqa: BLE001 - 未注册账户：不猜，按未解析处理
            profile = None
        if profile is not None and str(profile.account_id) == acc:
            return profile.tenant_id, account
        if strict:
            raise TenantConfigKeyError(
                f"broker 账户 {acc!r} 无法反解出已注册租户（env={env!r}，注册表 "
                f"{sorted(reg)}）—— 拒绝继续：账户级配置找不到归属租户，"
                "静默按全局处理就是跨租户连坐"
            )
        return None, account

    if strict:
        raise TenantConfigKeyError(
            "broker 无租户上下文（既无 tenant_id，也无 account_id）—— 拒绝继续："
            "写租户键必须有明确归属，绝不静默回落全局键"
        )
    return None, None


def tenant_id_of_broker(
    broker,
    *,
    strict: bool = False,
    registry: dict[str, TenantProfile] | None = None,
) -> str | None:
    """:func:`tenant_of_broker` 的便捷形式，只取 ``tenant_id``。

    解析逻辑**完全复用** ``tenant_of_broker``，不另写一套。
    """
    return tenant_of_broker(broker, strict=strict, registry=registry)[0]


# --------------------------------------------------------------------------
# 租户配置键：唯一拼键入口
# --------------------------------------------------------------------------

# 已有明确归属的租户级配置基键。新增租户级配置键时在这里登记，避免散落硬编码。
VOLATILITY_CAMPAIGNS_KEY = "VOLATILITY_CAMPAIGNS"
RISK_LIMITS_KEY = "risk_limits"
REAL_ACCOUNT_CONFIG_KEY = "real_account_config"
STRATEGY_ENABLED_KEY = "strategy_enabled"
STRATEGY_RECONCILIATION_DISABLED_KEY = "strategy_reconciliation_disabled"
#: 自动雷达每批权利金预算（**钱**，必须按租户取 —— P1 缺口的对象）。
RADAR_BUDGETS_KEY = "VOLATILITY_RADAR_BUDGETS"


def resolve_tenant_config_key(
    tenant_id: str | TenantProfile | None,
    base_key: str | None,
    *,
    registry: dict[str, TenantProfile] | None = None,
    extra_ids: "tuple[str, ...] | list[str] | set[str] | None" = None,
) -> str:
    """把 ``base_key`` 与租户拼成 ``{base_key}:{tenant_id}`` —— **唯一入口**。

    ``extra_ids`` 用于**非租户注册表**的合法作用域（例如 ``real_account_config:{account_id}``
    里的券商账号），让同一把 fail-closed 校验覆盖账户级键。

    抛 :class:`TenantConfigKeyError`（fail-closed，绝不返回无前缀全局键）：

    - ``tenant_id`` 缺失 / 空 / 非字符串；
    - ``tenant_id`` 既不在 ``registry`` 也不在 ``extra_ids``；
    - ``base_key`` 缺失 / 空；
    - ``base_key`` 已带该租户后缀（防止 ``X:{t}:{t}`` 双前缀）。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS

    if isinstance(tenant_id, TenantProfile):
        tenant_id = tenant_id.tenant_id
    tid = tenant_id.strip() if isinstance(tenant_id, str) else ""

    if not tid:
        raise TenantConfigKeyError(
            f"拒绝解析租户配置键 {base_key!r}：tenant_id 缺失或为空 —— "
            "宁可报错，也不静默回落到无前缀全局键（跨租户连坐）"
        )

    allowed = set(reg) | set(extra_ids or ())
    if tid not in allowed:
        raise TenantConfigKeyError(
            f"拒绝解析租户配置键 {base_key!r}：作用域 {tid!r} 不在注册表 "
            f"({sorted(allowed)}) —— 宁可报错，也不静默回落到无前缀全局键（跨租户连坐）"
        )

    if not isinstance(base_key, str) or not base_key.strip():
        raise TenantConfigKeyError(
            f"拒绝解析租户配置键：base_key 缺失或为空（作用域 {tid}）"
        )
    base = base_key.strip()
    if base.endswith(f":{tid}"):
        raise TenantConfigKeyError(
            f"拒绝解析租户配置键：base_key {base!r} 已带作用域 {tid} 后缀 —— "
            "重复加前缀会产生双作用域键"
        )
    return f"{base}:{tid}"


def legacy_config_key(base_key: str) -> str:
    """无前缀旧键 —— **仅用于只读兜底**。

    集中定义是为了让"旧键长什么样"只有一个出处；调用方必须把它当作 inert 兼容键，
    不得再写入。
    """
    if not isinstance(base_key, str) or not base_key.strip():
        raise TenantConfigKeyError("无前缀旧键需要非空的 base_key")
    return base_key.strip()


def tenant_config_key_candidates(
    tenant_id: str | None,
    base_key: str,
    *,
    registry: dict[str, TenantProfile] | None = None,
    extra_ids: "tuple[str, ...] | list[str] | set[str] | None" = None,
    record: bool = False,
) -> "tuple[str | None, str]":
    """读侧候选键：``(租户键, 无前缀旧键)``。

    读侧规则固定为**租户键优先、无前缀旧键兜底**（迁移前的存量状态）。
    租户键解析不出来时返回 ``None``，并**由本函数自己留痕**：

    - 始终写一条 ``WARNING`` 日志（带 ``base_key`` + 失败原因）；
    - 始终累加进程内计数器 :func:`legacy_fallback_count`（界面可读）；
    - ``record=True`` 时再写一条 ``LEGACY_KEY_FALLBACK`` 运行事件（持久审计）。

    **为什么不由调用方负责**：今晚反复付代价的就是"靠每个调用方记得记录回落"。
    fail-closed 的最后一公里要由工具保证 —— 调用方忘了，日志和计数器也不会漏。
    但要注意：**本层不静默回落到"另一个数值"**，回落到的只是同一个 ``base_key``
    的无前缀旧键；真正的 fail-closed 在写侧（:func:`resolve_tenant_config_key`）。

    拼写变体（``core_satellite`` / ``Core Satellite``）**不归本层管** ——
    那是 :func:`futu_quant.run_service.resolve_configured_strategies` 的职责。
    本层只做"基键 + 租户"的拼接，不解释名字。
    """
    legacy = legacy_config_key(base_key)
    try:
        tenant_key: str | None = resolve_tenant_config_key(
            tenant_id, base_key, registry=registry, extra_ids=extra_ids,
        )
    except TenantConfigKeyError as exc:
        _record_legacy_fallback(base_key, tenant_id, legacy, exc, record=record)
        tenant_key = None
    return tenant_key, legacy


TENANT_ACTIVE_KEY_PREFIX = "tenant_active"


def tenant_active_key(tenant_id: str) -> str:
    """租户启用开关的配置键（``system_settings``）。"""
    return f"{TENANT_ACTIVE_KEY_PREFIX}:{str(tenant_id or '').strip()}"


def resolve_tenant_active(
    tenant_id: str,
    *,
    store: Any | None = None,
    registry: dict[str, TenantProfile] | None = None,
) -> bool:
    """租户是否启用：**配置键优先，缺省回落注册表默认值**。

    2026-10-28 用户裁定：租户启用必须由开关决定，「开了几个就跑几个」。
    原先 `TenantProfile.is_active` 是 `tenant.py` 里的代码常量，界面上关掉实盘
    账号并不会改变它 → `launch_workers.get_active_tenants()` 永远拉起两个 Worker
    （实盘租户仍按 REAL 走预检，实盘交易已关闭 → 预检必然失败 → 误报
    「OpenD 掉线」并推送 CRITICAL）。

    约定：键缺失（``""``）→ 用注册表默认值；键存在 → 以其为准，非法值按 **关闭**
    处理（fail-closed：宁可少起一个 Worker，也不要凭空多起一个真金 Worker）。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS
    profile = reg.get(str(tenant_id or "").strip())
    default = bool(getattr(profile, "is_active", False))
    if store is None:
        return default
    raw = store.get_setting(tenant_active_key(tenant_id), "")
    text = str(raw or "").strip().lower()
    if text == "":
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return False


def active_tenants(
    registry: dict[str, TenantProfile] | None = None,
    store: Any | None = None,
) -> list[TenantProfile]:
    """返回启用的租户，按 ``tenant_id`` 排序（确定性顺序）。

    #2 雷达扇出的入口：共享市场层跑一次，然后**按此列表逐租户扇出**。
    顺序确定是为了让日志、审计、指标可复现（同一次运行两次结果一致）。

    ``store`` 给出时按 `resolve_tenant_active`（配置键优先）判定，否则沿用注册表
    默认值 —— 保持既有调用方（无 store 的纯函数用法）行为不变。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS
    return sorted(
        (
            profile
            for profile in reg.values()
            if resolve_tenant_active(profile.tenant_id, store=store, registry=reg)
        ),
        key=lambda profile: profile.tenant_id,
    )


def campaigns_config_key_lenient(tenant_id: str | None) -> tuple[str, bool]:
    """campaign 清单键的**读/写共用**解析 —— 返回 ``(键, 是否回落到无前缀旧键)``。

    #1（键级分区）的核心：**同一个 helper 同时服务读侧与写侧**，避免"读租户键、写全局键"
    这种半状态。规则：

    * 租户可解析（在注册表里）→ ``VOLATILITY_CAMPAIGNS:{tenant}``；
    * 否则 → 无前缀旧键 ``VOLATILITY_CAMPAIGNS``，且 ``fell_back=True``。

    **``fell_back=True`` 时调用方必须留一条 ERROR 事件**（或等价的醒目留痕）——
    回落到全局键意味着这份数据**没有被租户隔离**；静默回落正是今晚一切事故的母题，
    所以这里只保证"不静默"，不假装隔离成立。

    为什么不让它直接抛异常：雷达/推送等路径的 broker 不一定来自注册表（探测、测试、
    未来租户）。抛异常会**整条链路停摆**，比"留痕 + 回落"更糟。
    """
    tid = str(tenant_id or "").strip()
    if tid:
        try:
            return resolve_tenant_config_key(tid, VOLATILITY_CAMPAIGNS_KEY), False
        except TenantConfigKeyError:
            pass
    return VOLATILITY_CAMPAIGNS_KEY, True


def campaign_belongs_to_tenant(campaign: dict, tenant_id: str | None) -> bool:
    """这条 campaign 是否属于给定租户 —— **页面显示与清理的准入判据**。

    **为什么必须有**（10-23 Reviewer 实测的失效模式）：清理候选列表与逐行删除
    此前都是**租户盲**的 —— 候选检查按 code 取持仓，实盘一张**有真实持仓**的合约
    可能读到模拟盘的持仓（=0）而进入清理列表，点一下就把实盘的 campaign 删掉、
    **持仓失去管理**（META 那次的失效形态）。跨租户记录既不显示、也不参与清理。

    无 ``tenant_id`` 的 legacy 记录视为属于当前页：它们早于租户分区写入，
    下一次带租户写入/删除时会被 ``upsert_campaign`` 收敛；把它们藏起来反而会让
    存量记录永远留在配置里无人可见。
    """
    owner = str(campaign.get("tenant_id") or "").strip()
    return not owner or owner == str(tenant_id or "").strip()


def campaigns_config_key(
    tenant_id: str | TenantProfile | None,
    *,
    registry: dict[str, TenantProfile] | None = None,
) -> str:
    """``VOLATILITY_CAMPAIGNS:{tenant_id}`` —— 波动率 campaign 清单的分区键。"""
    return resolve_tenant_config_key(tenant_id, VOLATILITY_CAMPAIGNS_KEY, registry=registry)


def risk_limits_config_key(
    tenant_id: str | TenantProfile | None,
    *,
    registry: dict[str, TenantProfile] | None = None,
) -> str:
    """``risk_limits:{tenant_id}`` —— 租户级风险参数键。"""
    return resolve_tenant_config_key(tenant_id, RISK_LIMITS_KEY, registry=registry)


def radar_budgets_config_key(
    tenant_id: str | TenantProfile | None,
    *,
    registry: dict[str, TenantProfile] | None = None,
) -> str:
    """``VOLATILITY_RADAR_BUDGETS:{tenant_id}`` —— 自动雷达每批权利金预算的分区键。

    为什么必须分区：这是**钱**。未分区时实盘与模拟共用同一个默认 `$1,000`，
    模拟盘被实盘的小额度限住（赛马/A-B 结论失真），且任一侧调值都会污染另一侧。
    与本仓既有约定一致：钱类配置按租户/账户取，行情类（乘数、合约规则）才全局。
    """
    return resolve_tenant_config_key(tenant_id, RADAR_BUDGETS_KEY, registry=registry)


def real_account_config_key(account_id: str | int | None) -> str:
    """``real_account_config:{account_id}`` —— 账户级参数键（作用域是券商账号）。"""
    if isinstance(account_id, bool) or account_id is None:
        raise TenantConfigKeyError(
            f"拒绝解析账户配置键：account_id 缺失或非法（{account_id!r}）"
        )
    acc = str(account_id).strip()
    if not acc:
        raise TenantConfigKeyError("拒绝解析账户配置键：account_id 为空")
    return resolve_tenant_config_key(acc, REAL_ACCOUNT_CONFIG_KEY, extra_ids=(acc,))


def _profile_for_equity_env(
    tenant_id: str | None, account_id: str | int | None
) -> TenantProfile | None:
    """优先按 tenant_id 解析租户，退一步按 account_id 反解；解析不到返回 None。"""
    if tenant_id:
        try:
            return resolve_tenant(str(tenant_id))
        except ValueError:
            pass
    if account_id:
        try:
            return resolve_tenant(account_id=account_id)
        except ValueError:
            return None
    return None


def equity_snapshot_env(
    tenant_id: str | None = None, account_id: str | int | None = None
) -> str:
    """``equity_snapshots.env`` 的**唯一派生入口** —— 风控路径禁止再手拼字符串。

    规则：由**租户的 trading_env** 决定，不由 account_id 反推。
    因为模拟账户同样有 account_id（如 20170229），`f"REAL_{account_id}"` 会拼出
    `REAL_20170229`，而模拟盘快照写的是 `SIMULATE` → 查 0 行 → 派生日亏线静默回退
    到 .env 的 $10,000 → `RISK_LIMITS_CONFIG_INCOMPLETE` fail-closed 直接拒单
    （2026-11-04 生产缺陷，模拟盘开着跑但发不出单）。

    实盘租户 → ``REAL_<account_id>``；模拟/沙盒租户 → ``SIMULATE``。
    给了 account_id 却解析不出租户时**抛错**而不是猜：猜成 SIMULATE 会让实盘读模拟
    快照（限额算错），猜成 REAL_ 就是本次事故。
    """
    profile = _profile_for_equity_env(tenant_id, account_id)
    if profile is None:
        if account_id:
            raise TenantConfigKeyError(
                f"无法判定账户 {account_id} 的交易环境（租户未注册）："
                "不允许猜 SIMULATE / REAL_<account> 作为净值快照的 env"
            )
        return "SIMULATE"
    if str(profile.trading_env).strip().upper() == "REAL":
        acc = str(profile.account_id or account_id or "").strip()
        if not acc:
            raise TenantConfigKeyError(
                f"实盘租户 {profile.tenant_id} 缺少 account_id，无法派生净值快照 env"
            )
        return f"REAL_{acc}"
    return "SIMULATE"


# --------------------------------------------------------------------------
# 回落留痕：由工具保证，不靠调用方纪律
# --------------------------------------------------------------------------

_LEGACY_FALLBACK_COUNT = 0
_LEGACY_FALLBACK_LOGGER = logging.getLogger("futu_quant.tenant")


def legacy_fallback_count() -> int:
    """进程内"回落到无前缀旧键"的次数 —— 界面/巡检读这个数，非零即说明有租户键没生效。"""
    return _LEGACY_FALLBACK_COUNT


def reset_legacy_fallback_count() -> None:
    """测试与巡检归零用。"""
    global _LEGACY_FALLBACK_COUNT
    _LEGACY_FALLBACK_COUNT = 0


def _record_legacy_fallback(
    base_key: str,
    tenant_id: str | TenantProfile | None,
    legacy_key: str,
    exc: TenantConfigKeyError,
    *,
    record: bool = False,
) -> None:
    """把"回落到无前缀旧键"这件事**自己**写出去。

    三级留痕，全部不依赖调用方：

    1. ``WARNING`` 日志（带 base_key + 租户 + 原因）—— 进程日志里一定查得到；
    2. 进程内计数器 :func:`legacy_fallback_count` —— 界面可读，非零即异常；
    3. ``record=True`` 时写 ``LEGACY_KEY_FALLBACK`` 运行事件 —— 持久审计。
    """
    global _LEGACY_FALLBACK_COUNT
    tid = tenant_id.tenant_id if isinstance(tenant_id, TenantProfile) else tenant_id
    _LEGACY_FALLBACK_COUNT += 1
    payload = {
        "base_key": base_key, "tenant_id": tid, "legacy_key": legacy_key,
        "reason": str(exc), "fallback_count": _LEGACY_FALLBACK_COUNT,
    }
    _LEGACY_FALLBACK_LOGGER.warning(
        "租户配置键解析失败，回落到无前缀旧键：base_key=%s tenant_id=%r legacy=%s（第 %d 次）：%s",
        base_key, tid, legacy_key, _LEGACY_FALLBACK_COUNT, exc,
    )
    if not record:
        return
    try:
        from .store import TradingStore  # 延迟导入，避免与 store 循环依赖

        TradingStore().operational_event(
            "tenant_config_key", "LEGACY_KEY_FALLBACK", "WARNING", payload,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得影响主流程
        _LEGACY_FALLBACK_LOGGER.warning("回落留痕写运行事件失败（不影响主流程）：%s", payload)
