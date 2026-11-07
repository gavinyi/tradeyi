from __future__ import annotations

import json
import hashlib
import hmac
import os
import time
from urllib.request import Request, urlopen

from .store import TradingStore

# 严格限制推送飞书群的致命事件白名单 (少而精，杜绝告警疲劳)
FEISHU_GROUP_ALERT_WHITELIST = {
    "OPEND_DISCONNECTED",                         # OpenD 网关掉线/通信中断
    "KILL_SWITCH_ENGAGED",                        # 账户风控急停熔断触发
    "LEDGER_MISMATCH",                            # 三账对账不一致/疑似残腿或丢单
    "STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",     # 持仓期权时网络连续失败进入紧急保护
    "PROTECTIVE_RADAR_RESTART_FAILED",            # 雷达自动重启失败
    "OPEND_AUTO_RESTART_FAILED",                  # OpenD 自动重启失败
    "OPTION_LIFECYCLE_RISK",                      # 期权临近到期/行权将产生裸空正股敞口（必须人工可见）
    # 以下三条同属「涉及真金敞口/风控门禁」的 ERROR 级事件，历史上一律 delivered=0（仅落库、
    # 从不推送）。产品口径：这类事件必须让人知道，不因「怕吵」而静默。
    "DAILY_LOSS_KILL_SWITCH",                     # 账户当日亏损触及急停线，已自动急停
    # 日亏线解析失败 → 退回 .env 值。退回意味着**阈值可能被放宽**（实盘租户 400
    # 退回 env 10,000 就是 25 倍宽松），这属于"风控自己降级"，必须让人看见；
    # 不进白名单就会变成「库里有、用户看不到」——本文件反复踩过的同一种形态。
    "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK",
    "LIMIT_PROFILE_LEGACY_FALLBACK",
    "LIMIT_PROFILE_LEGACY_FALLBACK_RECOVERED",
    "REAL_TRADE_REJECTED",                        # 实盘下单被门禁拒绝（含超额/隔离铁律）
    "ORDER_ENV_MISMATCH",                         # 订单执行环境与券商环境不一致，已禁止改单撤单
    # 硬止损限额非法（<=0）。不修则 `usd_limit > 0` 恒假 → 止损在两层同时失效
    # （共同失败点），而这是唯一的线索 —— 不落推送等于让用户在无声中失去止损。
    "HARD_STOP_LIMIT_INVALID",
    # 开仓被限额门禁拒绝（限额缺失 / 限额非法）。这两条是**开仓拒绝**，比退出路径的
    # 回落更该让人看见：一旦 record_incident 置为 open 而首次投递恰好失败，后续每次
    # 拒绝都会被 `send()` 的 `not opened and not in whitelist` 分支提前 return ——
    # campaign 会**永久静默地被拒绝开仓**（10-22 item 36「检出但 delivered=0」同形态）。
    "VOLATILITY_STOP_LIMIT_MISSING",
    "VOLATILITY_STOP_LIMIT_INVALID",
    # 纸面账户的亏损守卫分两层，两条都要推群：
    # * PAPER_DAILY_LOSS_WARN —— **告警线**：纸面亏损不是真钱，自动急停会中断正在进行的
    #   策略验证（2026-10-22 模拟账户亏 $10,351 被全局 $10,000 线误杀，根因在此）。
    #   只告警、不拦单，但必须让人看见 —— 否则"放宽"就成了"没人知道"。
    #   作用域：**仅 `trading_env=SIMULATE` 的租户**，禁止配到实盘账户（真金无"只告警"口径）。
    # * PAPER_DAILY_LOSS_HARD_STOP —— **硬停线**（= `max_daily_loss`，$100,000）：拦单。
    "PAPER_DAILY_LOSS_WARN",
    "PAPER_DAILY_LOSS_HARD_STOP",
    # ML 模型管理（2026-10-23）。三条都是「库里有、用户看不到」的形态，必须推群：
    # * ML_MODEL_STALE        —— 模型超过新鲜度阈值未重训。评分质量退化是渐进的、
    #   不会报错，唯一线索就是年龄，不推等于没有守卫。
    # * ML_SCORING_DEGRADED  —— **本该用 ML 却回退了规则打分**（依赖缺失/文件缺失/
    #   反序列化失败）。ml_scorer 把异常吞在 INFO 级，这是唯一的对外信号。
    # * ML_ALERT_NOT_DELIVERED —— ML 守卫自己投递失败。守卫静默失效是最坏形态，
    #   必须递归可见（否则「守卫坏了」这件事本身也没人知道）。
    "ML_MODEL_STALE",
    "ML_SCORING_DEGRADED",
    "ML_ALERT_NOT_DELIVERED",
    # 调度任务超时会使健康检查/对账等关键任务失效；此前 WARNING 仅落库，形成监控盲区。
    "TASK_TIMEOUT",
    # 组合多腿计划因保护腿不足等被拒（定义风险组合无法建仓，防止静默拒单）
    "COMBO_LEG_PLAN_INVALID",
}

# 「不可静音清单」（P0-2）：**代码常量，不可配置** —— 静音它们等于把安全系统变哑。
# 与 `FEISHU_GROUP_ALERT_WHITELIST` 语义不同：白名单管「哪些值得推群」，
# 本清单管「哪些**任何配置都不能关**」。两者可以有交集，但判据不能混用。
ALERT_NEVER_MUTE = {
    "DAILY_LOSS_KILL_SWITCH",
    "REAL_TRADE_REJECTED",
    "ORDER_ENV_MISMATCH",
    "LEDGER_MISMATCH",
    "RISK_REJECTED",
    "OPTION_LIFECYCLE_RISK",
    "HARD_STOP_LIMIT_INVALID",
    "KILL_SWITCH_ENGAGED",
    "OPEND_DISCONNECTED",
    # 投递探针自身（设计 §4）：**探针必须真实投递** —— 「静音」只能理解为页面折叠 /
    # 不重复刷屏，**绝不能是不投递**。不投递的探针永远是 -1，什么也验不到。
    "ALERT_DELIVERY_PROBE",
    # 探针的两种失败上报：`-1` → 未配置，`0` → 投递失败。
    # 两者都必须发得出去，否则「通道坏了」这件事本身会被通道坏掉而静默。
    "ALERT_CHANNEL_NOT_CONFIGURED",
    "ALERT_DELIVERY_FAILED",
    # P1-13 进程失联：进程暴毙 / 僵死。显式列入不可静音清单 —— CRITICAL 现在
    # 默认不静音，但规则一改就可能被静音，而「服务死了没人知道」比告警噪音严重得多。
    "PROCESS_TERMINATED",
    "PROCESS_HEARTBEAT_TIMEOUT",
    # 实盘账户级配置损坏（坏键 fail-closed，2026-11-01 用户拍板）：这条一旦被静音，
    # 「开仓门禁坏了」就变成无人知晓的静默拦截 —— 必须进代码常量，不可配置。
    "REAL_ACCOUNT_CONFIG_CORRUPT",
    "LIMIT_PROFILE_LEGACY_FALLBACK",
    # 组合「长短腿不同步」（2026-11-03）：长腿已成交、短腿没成交时，真实敞口是
    # **已成交腿的全额成本**，不是价差的净支出。这条一旦被静音，
    # 「$650 敞口挂着」就变成无人知晓 —— 与 10-23 静默拦截同类，必须进代码常量。
    "COMBO_LEGGING_EXPOSURE",
}

# 探针事件名与间隔（S3）。间隔由 `ALERT_PROBE_INTERVAL_SECONDS` 覆盖。
ALERT_PROBE_EVENT = "ALERT_DELIVERY_PROBE"
ALERT_CHANNEL_NOT_CONFIGURED = "ALERT_CHANNEL_NOT_CONFIGURED"
ALERT_DELIVERY_FAILED = "ALERT_DELIVERY_FAILED"
# 投递探针间隔。**原 30 分钟太吵**：探针必须真实投递（否则验不到通道），
# 但每 30 分钟往飞书群里推一张"投递正常"的卡片，用户会以为是真告警
# （2026-10-24 实测 17 条/天，用户直接来问"推了好多告警"）。
# 改为 6 小时：仍是真实投递校验，噪音降到每天 4 条；失败路径另有独立告警，
# 不会因为间隔变长而漏掉"通道坏了"。
ALERT_PROBE_INTERVAL_DEFAULT = 6 * 3600
# 通道级上报「每天最多一次」的窗口（设计 §3.3：不刷屏）
ALERT_CHANNEL_REPORT_WINDOW_SECONDS = 86400

# **前缀匹配仅在本清单内允许**：`RECONCILIATION_*` 是一族事件，逐个列举会漏掉
# 将来新增的成员 —— 而漏掉一个就等于留一个静默缺口。静音清单里**禁止**通配。
ALERT_NEVER_MUTE_PREFIXES = ("RECONCILIATION_",)

# 静音清单存放键（`runtime_config`）。走 `mutate_runtime_config` 写入 → 自动获得
# actor/reason 留痕（P1-C）。
ALERT_MUTE_LIST_KEY = "alert_mute_list"

# 飞书通道「是否配置」的判据（设计 §3.3）。只用这两项，不用带默认值的
# `FEISHU_CHAT_ID` —— 那个常量恒有值，会把「未配置」永远判成「已配置」。
FEISHU_CONFIG_KEYS = ("FEISHU_WEBHOOK_URL", "FEISHU_GROUP_CHAT_ID")


def is_never_mutable(event_type: str) -> bool:
    """该事件是否**任何配置都不能静音**。"""
    return event_type in ALERT_NEVER_MUTE or event_type.startswith(ALERT_NEVER_MUTE_PREFIXES)


#: 「正常拦截」类事件（复盘文案三项之一，2026-11-07 用户拍板）：按设计拒绝、
#: 非故障——统一加【正常拦截】前缀且不推飞书群（落库可见），与真故障分开，
#: 消除「系统坏了」和「今天不买」的告警疲劳。**只允许经 mark_benign 标注**，
#: 禁止在各调用点手写前缀（口径集中一处，防两处默认值打架）。
BENIGN_PREFIX = "【正常拦截】"


def mark_benign(message: str) -> str:
    """给正常拦截类告警文案加统一前缀（幂等：重复调用不叠前缀）。"""
    text = str(message)
    return text if text.startswith(BENIGN_PREFIX) else f"{BENIGN_PREFIX}{text}"


# ============================================================================
# 告警分级推送（P2-②，2026-11-07，PRD prd_p2_batch_20261107.md §2）
# 分级判据**不是技术严重性**，是「这条消息需不需要人立刻动手」：
#   P0 立即——真金敞口/风控门禁/对账不匹配/急停/确认码/限额异常/下单失败：
#       逐条即时推，不聚合、不可静默；
#   P1 聚合——同因同租户反复出现（筛空/限流类）：每小时最多一条，带计数与首末时间；
#   P2 落库——正常拦截/信息类：只在库与页面可见。
# 防漏报三红线（PRD 2.2）：ALERT_NEVER_MUTE 永远 P0（配置成 P1/P2 → 必红）；
#   聚合必须带计数与首末时间，1 小时内同因超 ESCALATE_COUNT 自动升级 P0 单推；
#   降级必须走配置变更留审计（update_alert_levels，复用 mutate_runtime_config）。
# ============================================================================

#: 分级清单存放键（配置变更留审计的通道）。
ALERT_LEVELS_KEY = "alert_levels"
#: P1 聚合窗口（秒）与升级阈值（PRD 2.2-2，建议 N=20）。
P1_AGGREGATION_WINDOW_SECONDS = 3600
P1_ESCALATE_COUNT = 20

#: P0 默认清单 = 不可静音清单 + 白名单的真金/门禁类 + 显式枚举的即时类。
#: **只增不减**：任何出现在 ALERT_NEVER_MUTE 的事件天然 P0（classify 强制）。
P0_EVENT_TYPES = frozenset({
    "DAILY_LOSS_KILL_SWITCH", "KILL_SWITCH_ENGAGED", "LEDGER_MISMATCH",
    "RISK_REJECTED", "REAL_TRADE_REJECTED", "ORDER_ENV_MISMATCH",
    "HARD_STOP_LIMIT_INVALID", "VOLATILITY_STOP_LIMIT_MISSING",
    "VOLATILITY_STOP_LIMIT_INVALID", "COMBO_LEGGING_EXPOSURE",
    "STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS", "OPTION_LIFECYCLE_RISK",
    "OPEND_DISCONNECTED", "ALERT_DELIVERY_FAILED", "ALERT_CHANNEL_NOT_CONFIGURED",
    "SERVICE_CYCLE_ERROR", "PROCESS_TERMINATED", "PROCESS_HEARTBEAT_TIMEOUT",
})

#: P1 默认清单（同因同租户聚合）：反复出现的观测类/筛空类。
P1_EVENT_TYPES = frozenset({
    "VOLATILITY_CAPACITY_BUYOUT_FREQUENT", "PAPER_DAILY_LOSS_WARN",
    "TASK_TIMEOUT", "OPEND_UNAVAILABLE", "RESEARCH_BACKFILL_FALLBACK",
})


def classify_alert_level(event_type: str, *, benign: bool = False) -> str:
    """返回 "P0" / "P1" / "P2"（配置可降级 P1→P2，但 NEVER_MUTE 永远 P0，红线 1）。"""
    name = str(event_type or "")
    if is_never_mutable(name) or name in P0_EVENT_TYPES:
        return "P0"
    if benign or name in BENIGN_EVENT_TYPES:
        return "P2"
    levels = _configured_levels()
    if name in levels:
        return str(levels[name])
    if name in P1_EVENT_TYPES:
        return "P1"
    return "P2"


#: 默认 P2（正常拦截/信息类）——与 mark_benign 的调用点同源。
BENIGN_EVENT_TYPES = frozenset({
    "VOLATILITY_CANDIDATE_FILTERED", "ML_SCORING_MODE_INFO",
})

#: 运行期配置的分级覆盖（update_alert_levels 写入后由 load_alert_levels 读回注入）。
_CONFIGURED_LEVELS_CACHE: dict[str, str] = {}


def _configured_levels() -> dict[str, str]:
    return dict(_CONFIGURED_LEVELS_CACHE)


def load_alert_levels(store) -> dict[str, str]:
    """从 runtime_config 读分级覆盖并注入运行期缓存（非法值拒绝：只认 P0/P1/P2）。"""
    raw = ""
    try:
        raw = store.get_setting(ALERT_LEVELS_KEY, "")
    except Exception:  # noqa: BLE001 - 读不到按默认分级，不阻断告警
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    valid = {}
    for event_type, level in payload.items():
        if str(level) in {"P0", "P1", "P2"} and isinstance(event_type, str):
            valid[str(event_type)] = str(level)
    _CONFIGURED_LEVELS_CACHE.clear()
    _CONFIGURED_LEVELS_CACHE.update(valid)
    return dict(valid)


def update_alert_levels(store, levels: dict[str, str], actor: str, reason: str = "") -> dict:
    """降级/升级登记（PRD 2.2-3：配置变更留审计，禁止代码静默降级）。

    NEVER_MUTE 事件配成 P1/P2 → 整体拒绝（红线 1：分级不得覆盖不可静音）。
    """
    rejected: dict[str, str] = {}
    clean: dict[str, str] = {}
    for event_type, level in dict(levels).items():
        if str(level) not in {"P0", "P1", "P2"}:
            rejected[event_type] = f"非法级别 {level}"
        elif is_never_mutable(str(event_type)) and str(level) != "P0":
            rejected[event_type] = "不可静音事件必须保持 P0"
        else:
            clean[str(event_type)] = str(level)
    if rejected:
        return {"accepted": False, "rejected": rejected}
    store.mutate_runtime_config(
        ALERT_LEVELS_KEY, [], lambda _current: json.dumps(clean, sort_keys=True), actor,
    )
    if reason:
        try:
            store.audit(
                "ALERT_LEVELS_UPDATED",
                {"levels": clean, "reason": reason, "actor": actor},
                "alerting", actor="service:alerts",
            )
        except Exception:  # silent-ok: 分级审计失败不得阻断配置写入，留痕走 config_versions 版本链
            pass
    return {"accepted": True, "levels": clean}


def parse_mute_list(raw) -> tuple[set[str], list[str]]:
    """解析静音清单，返回 ``(生效项, 被拒绝项)``。

    **禁止通配**：静音项必须是**完整 `event_type`**。通配会让「静默」变成不可审计的
    一刀切 —— 配置里写 `*` 就等于把安全系统关掉，而没人能说清静音了什么。
    `RECONCILIATION_*` 只允许出现在不可静音清单里。
    """
    if raw is None or raw == "":
        return set(), []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return set(), [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set(), [str(raw)]

    accepted: set[str] = set()
    rejected: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            rejected.append(str(item))
            continue
        candidate = item.strip()
        if not candidate or "*" in candidate or "?" in candidate or " " in candidate:
            rejected.append(item)
            continue
        accepted.add(candidate)

    if rejected:
        import logging
        logging.getLogger("futu_quant.alerts").warning(
            "解析静音清单发现非法项 (禁止通配符/特殊字符/空串)，已被严格排除生效: %s", rejected
        )

    return accepted, rejected


def feishu_configured(store) -> bool:
    """飞书通道是否配置（store 配置优先，回落环境变量）。"""
    for key in FEISHU_CONFIG_KEYS:
        try:
            if store.get_setting(key, "") or os.getenv(key, ""):
                return True
        except Exception:  # noqa: BLE001 - 配置读失败按「未配置」处理，不阻断告警
            continue
    return False


def update_mute_list(
    store, event_types, actor: str, reason: str = "",
) -> tuple[set[str], list[str]]:
    """写入静音清单，返回 ``(生效项, 被拒绝项)``。

    走 `mutate_runtime_config` → 自动获得版本 + actor 留痕（P1-C 的第一个实际收益）。
    **非法项一律拒绝且不写库** —— 静默地写入非法项，会让「配置写了但没生效」
    变成新的不可解释状态。
    """
    accepted, rejected = parse_mute_list(list(event_types))
    # 写入侧门禁 (Reviewer 铁律)：直接调用 is_never_mutable 拦截不可静音项，严禁静默写库
    for item in list(accepted):
        if is_never_mutable(item):
            rejected.append(item)
            accepted.remove(item)
    if rejected:
        return set(), rejected
    store.mutate_runtime_config(
        ALERT_MUTE_LIST_KEY, [], lambda _current: sorted(accepted), actor,
    )
    if reason:
        try:
            store.audit(
                "ALERT_MUTE_LIST_UPDATED",
                {"mute_list": sorted(accepted), "reason": reason, "actor": actor},
                "alerting",
             actor="service:alerts")
        except Exception:  # noqa: BLE001 - 留痕失败不能阻断配置写入
            pass  # silent-ok: 静音清单审计失败不得阻断配置写入
    return accepted, []


class P1Aggregator:
    """同因同租户小时聚合（PRD 2.2-2）：窗口内只出一条，带计数与首末时间。

    纯内存态（进程内聚合足够：告警发出方就是本进程）。超阈值自动升级 P0 单推。
    """

    def __init__(self, window_seconds: int = 3600, escalate_count: int = 20):
        self._window = int(window_seconds)
        self._escalate = int(escalate_count)
        self._buckets: dict[tuple[str, str], dict] = {}

    def observe(self, event_type: str, tenant_id: str, now_ts: float) -> dict:
        """记录一次出现；返回本窗口聚合态（含是否应推送/是否升级）。"""
        key = (str(event_type), str(tenant_id or ""))
        bucket = self._buckets.get(key)
        if bucket is None or now_ts - bucket["first_ts"] > self._window:
            bucket = {"first_ts": now_ts, "last_ts": now_ts, "count": 0,
                      "pushed": False}
            self._buckets[key] = bucket
        bucket["last_ts"] = now_ts
        bucket["count"] += 1
        escalated = bucket["count"] > self._escalate
        # 出窗首条推一条（唯一推送）；同因超阈值 → 该条升级为 P0 级（文案带
        # 「已超阈值」提示），**推送次数不增加**——PRD 2.4-1 的「100 条只收
        # 1 条聚合」与 PRD 2.2-2 的「恶化不吞」由同一条消息同时满足：它既是
        # 聚合摘要（计数+首末时间）又显式告警恶化。
        should_push = not bucket["pushed"]
        if should_push:
            bucket["pushed"] = True  # 本窗口的推送名额已用掉
        state = {
            "count": bucket["count"],
            "first_ts": bucket["first_ts"], "last_ts": bucket["last_ts"],
            "should_push": should_push,
            "escalated": escalated,
        }
        return state

    @staticmethod
    def format_summary(event_type: str, state: dict) -> str:
        """聚合条目文案（必须带计数与首末时间——PRD 2.2-2，守卫钉死）。"""
        from datetime import datetime
        first = datetime.fromtimestamp(state["first_ts"]).strftime("%H:%M")
        last = datetime.fromtimestamp(state["last_ts"]).strftime("%H:%M")
        return (f"{event_type}：本窗口 {state['count']} 次（首次 {first}，最近 {last}）"
                + ("——已超阈值，升级为逐条推送" if state["escalated"] else ""))


_P1_AGGREGATOR = P1Aggregator()


class AlertManager:
    """Persists every alert and optionally mirrors it to a generic JSON webhook."""

    def __init__(self, store: TradingStore):
        self.store = store
        self.webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
        self.rejected_mute_count: int = 0
        self._last_mute_read_warn_time: float = 0.0

    def _mute_list(self) -> set[str]:
        """读静音清单。优先读取 runtime_config 运行时配置，回落 system_settings。"""
        try:
            cfg = self.store.get_runtime_config(ALERT_MUTE_LIST_KEY)
            raw = cfg[0] if cfg else self.store.get_setting(ALERT_MUTE_LIST_KEY, "")
        except Exception as exc:  # noqa: BLE001 - 读配置失败绝不能阻断告警，按空处理并去重警告
            now = time.time()
            if now - self._last_mute_read_warn_time >= 900:
                self._last_mute_read_warn_time = now
                import logging
                logging.getLogger("futu_quant.alerts").warning(
                    "本轮静音清单读取失败、按空处理 (fail-open，不静音任何告警): %s", exc
                )
            return set()
        accepted, rejected = parse_mute_list(raw)
        if rejected:
            self._report_rejected_mute_entries(rejected)
        return accepted

    def _report_rejected_mute_entries(self, rejected: list[str]) -> None:
        """非法静音项留痕（**去重后**）。

        非法项必须留痕：静音清单里出现通配 = 有人想把静默范围扩大；静默地忽略它，
        会让「配置写了但没生效」变成新的不可解释状态。

        **但本方法每次 `send()` 都会被调到**（`_mute_list()` 在推送判定里），
        所以**必须去重** —— 否则静音清单写错一次，就会「每条告警写一行」，
        `alerts` 表无界增长，而且这条告警自己还压不掉。
        这里沿用 `send()` 的 `ALERT_DEDUPE_SECONDS` 窗口（同一 message 在窗口内只写一行），
        上限 = 每窗口 1 行（默认 900 秒）。
        """
        message = f"静音清单含非法项（禁止通配，须为完整 event_type）：{rejected}"
        self.rejected_mute_count += len(rejected)
        dedupe_seconds = max(0, int(os.getenv("ALERT_DEDUPE_SECONDS", "900")))
        try:
            if dedupe_seconds and self.store.alert_recently_exists(
                "WARNING", "ALERT_MUTE_LIST_ENTRY_REJECTED", message, dedupe_seconds
            ):
                return
            self.store.add_alert("WARNING", "ALERT_MUTE_LIST_ENTRY_REJECTED", message)
        except Exception:  # noqa: BLE001 - 留痕失败绝不能阻断告警
            pass  # silent-ok: 静音清单条目的拒绝留痕失败不得阻断告警主流程

    def _push_webhook(self, severity: str, event_type: str, message: str) -> int:
        """推通用 webhook。三态：**-1 通道未配置 / 0 失败 / 1 成功**。"""
        if not self.webhook:
            return -1
        try:
            payload = json.dumps({
                "severity": severity, "event_type": event_type, "message": message,
            }).encode()
            headers = {"Content-Type": "application/json"}
            secret = os.getenv("ALERT_WEBHOOK_SECRET", "").encode()
            if secret:
                headers["X-Quant-Signature-SHA256"] = hmac.new(
                    secret, payload, hashlib.sha256
                ).hexdigest()
            request = Request(self.webhook, payload, headers)
            with urlopen(request, timeout=5) as response:  # noqa: S310
                return 1 if 200 <= response.status < 300 else 0
        except Exception:  # noqa: BLE001
            return 0

    def _push_feishu(
        self, severity: str, event_type: str, message: str,
        tenant_id: str | None = None,
    ) -> int:
        """推飞书群卡片。三态：**-1 未配置 / 0 失败 / 1 成功**。

        `-1` 与 `0` 必须分开：「通道空着」是配置问题，「通道坏了」是故障，
        两者处置完全不同（设计 §3.1）。
        """
        if not feishu_configured(self.store):
            return -1
        try:
            from .feishu_reporter import FeishuReporter
            reporter = FeishuReporter(self.store)
            ok = reporter.send_alert_card(
                severity, event_type, message, is_recovered=False, tenant_id=tenant_id,
            )
            return 1 if ok else 0
        except Exception:  # noqa: BLE001
            return 0

    def send(
        self, severity: str, event_type: str, message: str, incident_key: str | None = None,
        tenant_id: str | None = None, benign: bool = False,
    ) -> None:
        if benign:
            # 正常拦截（按设计拒绝、非故障）：统一前缀 + 不推飞书群（落库可见）。
            # 白名单/ERROR 默认推送对 benign 事件不生效——它们正是要降噪的对象。
            message = mark_benign(message)
        severity_upper = severity.upper()
        if severity_upper in {"ERROR", "CRITICAL"}:
            # 原实现在此处对「事故已 OPEN 且不在白名单」直接 return —— 那个分支让事件
            # **连告警行都不写**，连线索都不留（P0-2 要修的断链之一）。已删除。
            self.store.record_incident(incident_key or event_type, severity, message)
        dedupe_seconds = max(0, int(os.getenv("ALERT_DEDUPE_SECONDS", "900")))
        if dedupe_seconds and self.store.alert_recently_exists(
            severity, event_type, message, dedupe_seconds
        ):
            return

        # 不可静音清单**优先**：两清单交集时不可静音胜出，静音配置覆盖不了它。
        never_mutable = is_never_mutable(event_type)
        muted = (not never_mutable) and event_type in self._mute_list()

        webhook_delivered = -1
        feishu_delivered = -1
        # 分级判定（P2-②）：P0 逐条推；P1 聚合窗口内推一条（含计数/首末时间，
        # 超阈值升级=该条带恶化提示，推送次数不增加）；P2 只落库。
        # **ERROR/CRITICAL 未入白名单也默认推（P0-2 契约）——分级不得吞它**，
        # 分级只对「非 ERROR 级 + 非白名单 + 非 NEVER_MUTE」的事件降噪。
        level = classify_alert_level(event_type, benign=benign)
        default_error_push = (
            severity_upper in {"ERROR", "CRITICAL"}
            and not never_mutable and not benign
            and event_type not in FEISHU_GROUP_ALERT_WHITELIST
        )
        if level == "P1" and not default_error_push:
            _state = _P1_AGGREGATOR.observe(event_type, tenant_id or "", time.time())
            message = f"{message}（{_P1_AGGREGATOR.format_summary(event_type, _state)}）"
            if not _state["should_push"]:
                level = "P2"  # 窗口内已推过 → 本条只落库（聚合摘要随行落库可见）
            elif _state["escalated"]:
                level = "P0"  # 同因恶化：该条升级为 P0 级（同一条消息，不增推送数）
        if not muted:
            webhook_delivered = self._push_webhook(severity, event_type, message)
            # 飞书：P0 逐条即时推（含 NEVER_MUTE / ERROR/CRITICAL / 升级条）；
            # P1 窗口首条；P2（含 benign 正常拦截）只落库。
            if (
                level == "P0"
                or default_error_push
                or (level == "P1"
                    and (severity_upper in {"ERROR", "CRITICAL"}
                         or event_type in FEISHU_GROUP_ALERT_WHITELIST))
            ):
                feishu_delivered = self._push_feishu(
                    severity, event_type, message, tenant_id=tenant_id,
                )

        # 静音**仍然落库**（`muted=1`、三态保持 -1 未尝试），页面可见 ——
        # 静音是人工决策，不是故障，但**不该因此消失**。
        self.store.add_alert(
            severity, event_type, message,
            webhook_delivered=webhook_delivered,
            feishu_delivered=feishu_delivered,
            muted=int(muted),
        )

    # ------------------------------------------------------------------
    # S3 投递探针
    # ------------------------------------------------------------------

    def _newest_alert(self, event_type: str) -> dict | None:
        """取最近一条该事件类型的告警行（用于回读投递结果）。"""
        try:
            for row in self.store.alerts(limit=50):
                if row.get("event_type") == event_type:
                    return row
        except Exception:  # noqa: BLE001
            return None
        return None

    def _report_channel_issue(
        self, severity: str, event_type: str, message: str,
    ) -> None:
        """通道级上报，**每天最多一次**（设计 §3.3：不刷屏）。

        通道「未配置」是常态（本部署可能只用飞书），不设窗口会每 30 分钟刷一次，
        把「未投递」视图训练成噪声 —— 而它是本次修复的核心可见性。
        """
        try:
            if self.store.alert_recently_exists(
                severity, event_type, message, ALERT_CHANNEL_REPORT_WINDOW_SECONDS
            ):
                return
        except Exception:  # noqa: BLE001
            pass  # silent-ok: 去重查询失败时宁可多发一条，不能丢告警
        self.send(severity, event_type, message)

    def probe_delivery(self) -> dict:
        """S3 投递探针：发一条**真实投递**的探针，回读它自己的三态并升级失败。

        判据 **`!= 1` 即失败** —— `-1`（未尝试）与 `0`（尝试失败）**都算失败**：
        若只认 `0`，**通道空着时探针永远不告警**，正好漏掉本次要修的核心场景。
        按取值区分上报：`-1` → `ALERT_CHANNEL_NOT_CONFIGURED`（ERROR）；
        `0` → `ALERT_DELIVERY_FAILED`（CRITICAL）。两者都在不可静音清单里。

        **递归防护**：探针自己也不投递时，**不靠再发告警去发现告警故障**（那是无限递归）——
        由页面「未投递」视图兜底。
        """
        result: dict = {"webhook": -1, "feishu": -1, "escalations": []}
        try:
            self.send(
            "ERROR", ALERT_PROBE_EVENT,
            "【投递探针·可忽略】告警通道自检（此条用于验证飞书通道真的能送达，不是业务异常）",
        )
        except Exception as exc:  # noqa: BLE001
            result["escalations"].append(f"SEND_FAILED:{exc}")
            return result

        row = self._newest_alert(ALERT_PROBE_EVENT)
        if row is None:
            result["escalations"].append("PROBE_ROW_MISSING")
            return result

        _status = self.channel_status()
        any_configured = bool(_status.get("webhook") or _status.get("feishu"))
        for channel, key in (("webhook", "webhook_delivered"), ("feishu", "feishu_delivered")):
            try:
                state = int(row.get(key, -1))
            except (TypeError, ValueError):
                state = -1
            result[channel] = state
            if state == 1:
                continue
            if state == -1:
                if not any_configured:
                    event_type, severity = ALERT_CHANNEL_NOT_CONFIGURED, "ERROR"
                    message = f"{channel} 通道未配置（未尝试投递），且没有任何告警通道可用"
                else:
                    # 该通道**本来就没启用**，而另一通道已配置（并在上面以 state==1 证明送达）。
                    # 2026-10-27 事故：`FEISHU_WEBHOOK_URL` 空、业务告警实际走飞书应用通道且
                    # 一直送达正常，但探针每次都把「未启用的第二通道」升级成 ERROR，
                    # 造成长期假告警，把真问题淹掉。这里只留审计痕迹、不升级告警。
                    # **安全底线不变**：一个通道都没配 → 仍报 ERROR；已配置通道投递失败 → 仍报 CRITICAL。
                    self.store.operational_event(
                        "alerts", "ALERT_CHANNEL_UNUSED", "INFO",
                        {"channel": channel, "state": state,
                         "other_channel_configured": True,
                         "note": "该通道未启用；另一通道已送达，不升级"},
                    )
                    result["escalations"].append(f"UNUSED:{channel}")
                    continue
            else:
                event_type, severity = ALERT_DELIVERY_FAILED, "CRITICAL"
                message = f"{channel} 通道投递失败（已尝试但未成功）"
            self._report_channel_issue(severity, event_type, message)
            result["escalations"].append(event_type)
        return result

    def channel_status(self) -> dict:
        """页面/日志用：两个通道是否配置（**只看是否配置，不实际投递**）。"""
        return {
            "webhook": bool(self.webhook),
            "feishu": feishu_configured(self.store),
        }

    def recover(
        self, event_type: str, message: str = "故障已恢复", incident_key: str | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        if not self.store.recover_incident(incident_key or event_type):
            return False

        # 飞书群故障恢复卡片广播 (仅对白名单内的事件发送恢复卡片)
        feishu_recovered = False
        if event_type in FEISHU_GROUP_ALERT_WHITELIST:
            try:
                from .feishu_reporter import FeishuReporter
                reporter = FeishuReporter(self.store)
                feishu_recovered = reporter.send_alert_card(
                    "INFO", event_type, message, is_recovered=True, tenant_id=tenant_id,
                )
            except Exception:  # silent-ok: 恢复卡片失败已由后续 feishu_state 反映，不重复抛
                pass

        feishu_state = -1
        if event_type in FEISHU_GROUP_ALERT_WHITELIST:
            # 只有白名单事件才会尝试推恢复卡片：失败时区分「未配置」(-1) 与「推失败」(0)。
            feishu_state = 1 if feishu_recovered else (0 if feishu_configured(self.store) else -1)
        inc_key = incident_key or event_type
        try:
            self.store.audit(
                "INCIDENT_RECOVERED",
                {"incident_key": inc_key, "event_type": event_type, "message": message},
                actor="service:alerts",
            )
        except Exception as exc:  # silent-ok: 审计记录失败仅记录警告，不中断恢复主链路与告警状态更新
            import logging
            logging.getLogger("futu_quant.alerts").warning(
                "incident_recovered_audit_failed: incident_key=%s, event_type=%s, err=%s",
                inc_key,
                event_type,
                exc,
            )

        self.store.add_alert(
            "INFO", f"{event_type}_RECOVERED", message, feishu_delivered=feishu_state,
        )
        return True
