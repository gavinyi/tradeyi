from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import logging
import os
import re
import math

from .alerts import AlertManager
from .store import USED_NOTIONAL_STATUSES, TradingStore
from .tenant import broker_scope, tenant_config_key_candidates, tenant_of_broker
from .portfolio_risk import projected_portfolio_allowed
from .portfolio_allocator import strategy_budget_allowed
from .risk_breaker import (
    ACCOUNT_HALT,
    DAILY_LOSS,
    DRAWDOWN_ABSOLUTE,
    INTRADAY_DRAWDOWN,
    INTRADAY_LOSS,
    MODE_ENFORCE,
    MODE_SHADOW,
    RECONCILIATION_LOCK,
    BlockReason,
    BreakerConfigError,
    BreakerUnsupportedEnv,
    evaluate as evaluate_breaker,
    initial_peaks,
    load_thresholds,
    resolve_equity_env,
    validate_equity_sample,
)


logger = logging.getLogger(__name__)


OPTION_PATTERN = re.compile(r"^US\..+\d{6}[CP]\d+$")
NY = ZoneInfo("America/New_York")

#: 纸面账户的**告警线**配置键 —— **与硬停线（`max_daily_loss`）是两个字段**。
#:
#: B 方案（Chief 10-23 裁定，Product 确认）：``max_daily_loss`` 的既有急停逻辑
#: **一行不动**，只在它前面加一条「纸面账户过告警线 → 发 WARNING → 继续放行」的旁路。
#: 引擎里因此**不得出现"这个数字是告警还是硬停"的语义分叉**：
#: 告警线有自己的键（本键，仅纸面账户读），硬停线就是 ``max_daily_loss``（全账户统一）。
#:
#: 为什么告警线不能复用 ``max_daily_loss``：同一个数字承载两种语义（纸面=只告警、
#: 实盘=真急停）时，"这个数字是告警还是硬停"必然在某处被分叉，而分叉的那一处
#: 就是下一次事故的地方。分两个键，语义写在键名上，不需要任何分支去解释。
#:
#: **作用域铁律（Product 10-23）**：本键**仅对 ``trading_env=SIMULATE`` 的租户生效**，
#: **禁止配到实盘账户上**。实盘账户的亏损上限只有 ``max_daily_loss`` 一条，
#: 不存在"只告警"的口径 —— 真金账户的每一条亏损线都必须拦单。
PAPER_DAILY_LOSS_WARN_KEY = "paper_daily_loss_warn"


def ny_trading_day_start_utc(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(NY)
    return current.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def market_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(number) else number


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 20_000.0
    max_daily_notional: float = 100_000.0
    max_reduction_order_notional: float = 150_000.0
    max_daily_reduction_notional: float = 500_000.0
    max_pending_orders: int = 20
    max_symbol_weight: float = 0.25
    max_gross_exposure: float = 0.85
    max_option_premium: float = 30_000.0
    max_daily_loss: float = 5_000.0
    max_drawdown_pct: float = 0.05

    @classmethod
    def load(cls) -> "RiskLimits":
        return cls(
            max_order_notional=float(os.getenv("RISK_MAX_ORDER_NOTIONAL", "20000")),
            max_daily_notional=float(os.getenv("RISK_MAX_DAILY_NOTIONAL", "100000")),
            max_reduction_order_notional=float(
                os.getenv("RISK_MAX_REDUCTION_ORDER_NOTIONAL", "150000")
            ),
            max_daily_reduction_notional=float(
                os.getenv("RISK_MAX_DAILY_REDUCTION_NOTIONAL", "500000")
            ),
            max_pending_orders=int(os.getenv("RISK_MAX_PENDING_ORDERS", "20")),
            max_symbol_weight=float(os.getenv("RISK_MAX_SYMBOL_WEIGHT", "0.25")),
            max_gross_exposure=float(os.getenv("RISK_MAX_GROSS_EXPOSURE", "0.85")),
            max_option_premium=float(os.getenv("RISK_MAX_OPTION_PREMIUM", "30000")),
            max_daily_loss=float(os.getenv("RISK_MAX_DAILY_LOSS", "5000")),
            max_drawdown_pct=float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0.05")),
        )


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    notional: float
    #: 结构化拦截原因（audit 用；`blocked_by` 能区分具体闸门）。默认空 = 未标注，行为不变。
    blocked_by: tuple[str, ...] = ()
    #: 熔断维度（G4）：shadow 期只记录不拦；未启用的交易环境显式记录，绝不冒异常
    breaker_reasons: tuple[BlockReason, ...] = ()
    breaker_unsupported: str = ""
    breaker_would_block: bool = False


def connectivity_degraded_for(store: TradingStore, broker) -> bool:
    """连接降级判定：**本租户键优先**，键缺失才回落无前缀全局键。

    2026-10-27 事故：实盘账户从券商侧消失 → 全局 `connectivity_degraded=true` →
    模拟租户的 BUY 也被拦（一个账号出问题、全部连坐）。租户键存在即以其为准；
    无前缀全局键只用于界面横幅与迁移期兜底。
    """
    tenant_id, _ = tenant_of_broker(broker, strict=False)
    if tenant_id:
        scoped = store.get_setting(f"connectivity_degraded:{tenant_id}", "")
        if str(scoped) != "":
            return str(scoped).strip().lower() == "true"
    return store.get_setting("connectivity_degraded", "false") == "true"


class RiskEngine:
    def __init__(
        self,
        store: TradingStore,
        limits: RiskLimits | None = None,
        broker=None,
    ):
        """``limits`` 显式注入时按注入值用（测试替身不受影响）；否则走**唯一解析入口**。

        P0-7：解析动作只有一个入口 ``risk_limits.resolve_risk_limits``。
        解析失败**不在这里抛** —— 服务必须能起来，缺配置时由下单路径
        fail-closed 拒绝（见 ``execution.submit_limit``）。
        """
        self.store = store
        self.broker = broker
        self._resolution_error: str | None = None
        self._settings_cache = None
        if limits is not None:
            self.limits = limits
            return
        from .risk_limits import resolve_risk_limits

        try:
            self.limits = resolve_risk_limits(store, broker)
        except Exception as exc:
            self.limits = None
            self._resolution_error = str(exc)
            logger.warning("风控参数解析失败（下单路径将 fail-closed 拒绝）：%s", exc)

    def _is_paper_account(self, broker) -> bool:
        """这个 broker 是不是**纸面账户**（SIMULATE）。

        走共享入口 ``broker_scope``（不裸读 ``broker.trading_env``）：该入口会把
        mock 对象与非法值降级为 ``None``，避免测试替身把 mock 带进判定链路。
        """
        env, _ = broker_scope(broker)
        return str(env or "").strip().upper() == "SIMULATE"

    def _paper_warn_line(self, broker) -> float | None:
        """纸面账户的**告警线**（``paper_daily_loss_warn``）—— 与硬停线**是两个字段**。

        B 方案（Chief 10-23 裁定）：``max_daily_loss`` 的既有急停逻辑**一行不动**，
        只在它前面加一条「纸面账户过告警线 → 发 WARNING → 继续放行」的旁路。
        因此引擎里**不得出现"这个数字是告警还是硬停"的语义分叉** ——
        告警线有自己的键，硬停线就是 ``max_daily_loss``，两者互不复用。

        键的解析走共享入口 ``tenant_config_key_candidates``（租户键优先、无前缀
        全局键兜底），**不手拼**。拿不到租户时直接读全局键 —— 刻意不调用共享入口，
        否则每次都会记一条"回落"留痕、把 ``legacy_fallback_count`` 灌满（假警报）。
        """
        tenant_id = None
        if broker is not None:
            tenant_id = tenant_of_broker(broker, strict=False)[0]
        if tenant_id:
            tenant_key, _legacy = tenant_config_key_candidates(
                tenant_id, PAPER_DAILY_LOSS_WARN_KEY,
            )
            key = tenant_key or PAPER_DAILY_LOSS_WARN_KEY
        else:
            key = PAPER_DAILY_LOSS_WARN_KEY
        raw = self.store.get_setting(key, "") or ""
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _paper_warn_only(
        self, broker, broker_acc, *,
        metric_label: str, metric_value: float, assets: float,
        metric_is_ratio: bool = False,
    ) -> bool:
        """纸面账户的**告警带**旁路 —— 返回 ``True`` 表示"已告警，调用方跳过拦单"。

        **每个分支用它自己那条阈值，量纲必须对得上**（这是本函数存在的理由）：

        | 分支 | ``metric_value`` | 告警阈值 | 硬停上限 |
        | :--- | :--- | :--- | :--- |
        | 当日亏损 | 美元额 | ``paper_daily_loss_warn``（$25,000） | ``max_daily_loss``（$100,000） |
        | 日内权益损失 | 美元额 | ``paper_daily_loss_warn`` | ``max_daily_loss`` |
        | 日内回撤 | **比例**（``metric_is_ratio=True``） | ``max_drawdown_pct``（10%） | ``max_daily_loss`` 折美元 |

        **为什么必须这样分**：回撤分支原本把 ``drawdown * assets``（回撤**美元额**）传进来
        比日亏损线 —— 两个不同量纲，结果是回撤 2.5% 就误报一条
        "当日亏损 $29,100" 的告警，而实际当日亏损可能只有 $500。
        **告警金额写错比不报警更糟**，所以这里按分支各用各的阈值、文案也按分支定量纲。

        只在**过本分支告警线、未过硬停线**这条带子里生效：
        * 没到告警线 → 不该有任何动静（返回 False，交给既有逻辑 → 什么都不做）；
        * 已过硬停线 → 必须真停（返回 False，交给既有急停逻辑 → 写急停 + 拦单）；
        * 落在带子里 → 发 WARNING + 留运行事件，返回 True 跳过拦单。

        没配 ``paper_daily_loss_warn`` 时**不旁路**（fail-closed）：宁可少一条告警，
        也不能因为"读不到配置"就把急停整个放过。
        """
        if not self._is_paper_account(broker):
            return False
        warn_line = self._paper_warn_line(broker)
        if warn_line is None:
            return False
        if metric_is_ratio:
            effective_warn = self.limits.max_drawdown_pct
            metric_amount = metric_value * assets
        else:
            effective_warn = warn_line
            metric_amount = metric_value
        if metric_value < effective_warn or metric_amount >= self.limits.max_daily_loss:
            return False
        self.store.operational_event(
            "risk_engine", "PAPER_DAILY_LOSS_WARN", "WARNING",
            {
                "account_id": broker_acc or None,
                "metric": metric_label,
                "metric_value": round(metric_value, 6),
                "metric_amount_usd": round(metric_amount, 2),
                "assets": round(assets, 2),
                "warn_line": round(effective_warn, 6),
                "hard_stop_line": self.limits.max_daily_loss,
            },
        )
        # 告警必须带上下文（账户 / 该分支的实际数值 / 触发线）——Product 10-23 明确要求：
        # 只说"已过告警线"等于没说，看的人还得翻日志才知道亏了多少。
        # 代价：金额进文案后，AlertManager 的 900s 精确去重不再跨金额生效；
        # 但本告警只在 **BUY 下单检查**时触发（不是定时任务），量级有界，不会刷屏。
        if metric_is_ratio:
            detail = f"日内回撤 {metric_value:.2%}（${metric_amount:,.0f}）"
            warn_desc = f"回撤告警线 {effective_warn:.2%}"
        else:
            detail = f"{metric_label} ${metric_value:,.0f}"
            warn_desc = f"日亏损告警线 ${effective_warn:,.0f}"
        AlertManager(self.store).send(
            "WARNING", "PAPER_DAILY_LOSS_WARN",
            f"纸面账户 {broker_acc or '未知'} {detail}，已过{warn_desc}"
            f"（硬停线 ${self.limits.max_daily_loss:,.0f}），仅告警、不中断验证",
            incident_key="PAPER_DAILY_LOSS_WARN",
        )
        return True

    def _loss_guard(
        self, broker, broker_acc, *, metric_label: str,
        loss_amount: float, assets: float, drawdown: float = 0.0,
    ) -> bool:
        """亏损 / 回撤守卫的**唯一出口** —— 返回 ``True`` 表示这一单必须被拦下。

        **这里只有"硬停"一种语义**：写急停 + 拦单。
        实盘与纸面**共用**这条路径（B 方案明令禁止在这里分叉告警/硬停语义）。

        ``metric_label`` 决定文案量纲（"当日亏损" / "日内权益损失金额" / "日内回撤金额"）——
        **不能一律写"当日亏损"**：把回撤额写成当日亏损就是误报信息。

        顺带补上此前**缺失的告警**：``DAILY_LOSS_KILL_SWITCH`` 一直在飞书白名单里，
        却没有任何发送方 —— 也就是"急停发生了但没人知道"（10-22 item 36 的形态）。
        告警级别按账户性质区分：实盘是 ERROR（真金），纸面是 ERROR 但用**独立事件名**
        ``PAPER_DAILY_LOSS_HARD_STOP`` —— 它同样拦单、同样要让人立刻看到（策略可能已坏）。
        """
        alerts = AlertManager(self.store)
        self.store.set_kill_switch(True, account_id=broker_acc)
        detail = f"{metric_label} ${loss_amount:,.0f}"
        if self._is_paper_account(broker):
            # 硬停是真事件：文案**带账户与该分支的实际数值**（Product 要求），
            # 且此时 kill_switch 已置位、后续 BUY 会在急停门被拦住，不会反复推送。
            alerts.send(
                "ERROR", "PAPER_DAILY_LOSS_HARD_STOP",
                f"账户 {broker_acc or '未知'} {detail}，"
                f"已达硬停线 ${self.limits.max_daily_loss:,.0f}，已停止开仓"
                "（策略失控兜底，不是验证中断）",
                incident_key="PAPER_DAILY_LOSS_HARD_STOP",
            )
            return True
        alerts.send(
            "ERROR", "DAILY_LOSS_KILL_SWITCH",
            f"账户 {broker_acc or '未知'} {detail}，"
            f"已达硬停线 ${self.limits.max_daily_loss:,.0f}，已自动急停并停止开仓",
            incident_key="DAILY_LOSS_KILL_SWITCH",
        )
        return True

    def _settings_provider(self):
        """`REAL_ACCOUNT_ID` 等配置的单一来源（熔断 env 派生用）。

        `Settings.load()` 失败时返回 None：`resolve_equity_env` 会据此 fail-closed，
        绝不在下单路径里因为配置加载异常把异常抛给调用方。
        """
        if self._settings_cache is None:
            try:
                from .config import Settings

                self._settings_cache = Settings.load()
            except Exception as exc:  # noqa: BLE001 - 配置异常不得影响下单路径判定
                logger.warning("加载 Settings 失败，熔断按未启用处理：%s", exc)
                return None
        return self._settings_cache

    def _drawdown_breaker_for_entry(
        self, broker, broker_acc, assets: float, execution_mode: str,
    ) -> dict:
        """G4 跨日回撤熔断评估（**取数与计算都在 RiskEngine 侧**，聚合层只接收结果）。

        - 默认 `off`：直接返回空结果，零 store 写入、零行为变化；
        - `shadow`：只计算 + 写 audit（`would_block`），不拦；
        - `enforce`：返回 block 原因，由 `check()` 转成 `RiskDecision(False)`；
        - 非 REAL 环境（`SIMULATE`）：**显式记录未启用**（`breaker_unsupported`），不拦。
        """
        result: dict = {"reasons": (), "unsupported": "", "would_block": False, "blocked": False}
        tenant, _ = tenant_of_broker(broker, strict=False)
        suffix = f":{tenant}" if tenant else (f":{broker_acc}" if broker_acc else "")
        raw_mode = str(self.store.get_setting(f"risk_breaker_mode{suffix}", "") or "").strip().lower()
        mode = raw_mode or "off"
        if mode not in {"off", MODE_SHADOW, MODE_ENFORCE}:
            logger.warning("risk_breaker_mode=%r 非法，按 off 处理（fail-safe）", raw_mode)
            mode = "off"
        if mode == "off":
            return result
        trading_env = "REAL" if str(execution_mode).upper() == "REAL" else "SIMULATE"

        def getter(key: str):
            value = self.store.get_setting(f"{key}{suffix}", "")
            return value if str(value).strip() else None

        try:
            # getter 已自带租户后缀 → tenant_id 传空，避免拼成 `:tenant:tenant`
            thresholds = load_thresholds(getter, "")
        except BreakerConfigError as exc:
            # 阈值类缺键/非法 → fail-closed（设计 D4.2）：拒绝开新仓，不回落默认值
            reason = BlockReason("risk_breaker_config", f"熔断阈值配置不完整：{exc}")
            self.store.operational_event(
                "risk_breaker", "DRAWDOWN_BREAKER", "BLOCKED", {"reason": str(exc)}
            )
            result.update(reasons=(reason,), blocked=True)
            return result
        try:
            equity_env = resolve_equity_env(self._settings_provider(), trading_env)
        except BreakerUnsupportedEnv as exc:
            # 聚合层负责把它转成「熔断维度未启用」；这里只记录，绝不抛
            result["unsupported"] = exc.trading_env
            self.store.operational_event(
                "risk_breaker", "DRAWDOWN_BREAKER", "NOT_ENABLED",
                {"env": exc.trading_env, "hint": exc.page_hint()},
            )
            return result
        except BreakerConfigError as exc:
            reason = BlockReason("risk_breaker_config", f"实盘账户未配置：{exc}")
            self.store.operational_event(
                "risk_breaker", "DRAWDOWN_BREAKER", "BLOCKED", {"reason": str(exc)}
            )
            result.update(reasons=(reason,), blocked=True)
            return result

        sample = validate_equity_sample(assets, None)
        if not sample.ok:
            self.store.operational_event(
                "risk_breaker", "DRAWDOWN_BREAKER", "SKIPPED", {"reason": sample.reason}
            )
            return result

        state_key = tenant or str(broker_acc or "")
        state_row = self.store.load_risk_state(state_key)
        from .risk_breaker import RiskState

        state = RiskState.from_row(state_row) if state_row is not None else None
        if state is None:
            try:
                peak_hist = self.store.historical_peak_equity(
                    equity_env, current_equity=float(assets)
                )
                peak_hist, peak_reset = initial_peaks(
                    current_equity=float(assets), historical_high=peak_hist
                )
            except (ValueError, TypeError) as exc:
                hint = f"回撤基准不可用：{exc}"
                if mode == MODE_ENFORCE:
                    reason = BlockReason("breaker_peak_unavailable", hint)
                    result.update(reasons=(reason,), blocked=True)
                else:
                    result["unsupported"] = equity_env
                self.store.operational_event(
                    "risk_breaker", "DRAWDOWN_BREAKER",
                    "BLOCKED" if result["blocked"] else "NOT_ENABLED",
                    {"reason": hint, "env": equity_env},
                )
                return result
            from .risk_breaker import RiskState

            state = RiskState(
                tenant_id=state_key,
                peak_hist=peak_hist,
                peak_hist_at=self.store.now(),
                peak_hist_source="backfill",
                baseline_equity=float(assets),
                baseline_at=self.store.now(),
                baseline_source="live",
                peak_reset=peak_reset,
                updated_at=self.store.now(),
            )
        decision = evaluate_breaker(
            state, sample, thresholds,
            now=self.store.now(),
            current_trading_day=self.store.now()[:10],
        )
        if decision.state is not state:
            self.store.save_risk_state(decision.state.as_row())
        result["reasons"] = decision.reasons
        result["would_block"] = decision.would_block
        blocking = decision.blocked or (mode == MODE_ENFORCE and decision.would_block)
        result["blocked"] = bool(blocking)
        self.store.operational_event(
            "risk_breaker", "DRAWDOWN_BREAKER",
            "BLOCKED" if blocking else ("SHADOW" if decision.would_block else "OK"),
            {
                "level": decision.level,
                "mode": mode,
                "reasons": [reason.code for reason in decision.reasons],
                "skipped": decision.skipped_reason,
            },
        )
        return result

    def check(
        self, broker, symbol: str, side: str, quantity: int, price: float,
        multiplier: float | None = None, underlying: str | None = None,
        strategy_id: str | None = None, execution_mode: str = "PAPER",
        protected_short: bool = False,
        metadata: dict | None = None,
        limits: RiskLimits | None = None,
    ) -> RiskDecision:
        # fail-closed：调用方（submit_limit）在风控门前按**目标 broker 账户**解析一次并传入；
        # 未传入时用构造期解析结果；构造期解析失败则一律拒绝，绝不回落 env。
        effective = limits if limits is not None else self.limits
        if effective is None:
            return RiskDecision(
                False,
                f"风控参数配置不完整，拒绝开仓：{self._resolution_error or '未解析出风控参数'}",
                0,
            )
        self.limits = effective
        if not symbol.startswith("US.") or side not in {"BUY", "SELL"}:
            return RiskDecision(False, "仅允许美股 BUY/SELL 指令", 0)
        if quantity <= 0 or price <= 0:
            return RiskDecision(False, "数量和限价必须大于 0", 0)
        broker_acc = getattr(broker, "account_id", None)
        acc_prefix = f"{broker_acc}:" if broker_acc else ""
        if side == "BUY" and connectivity_degraded_for(self.store, broker):
            return RiskDecision(False, "连接恢复尚未完成对账，暂时禁止新增风险", 0)
        # 精细化对账风控隔离：仅当具体标的或具体策略自身存在对账差异时拦截，严禁全系统一刀切误伤独立策略
        if side == "BUY" and execution_mode != "SHADOW":
            # 1. 标的级阻断
            if (
                self.store.get_setting(f"symbol_blocked:{acc_prefix}{symbol}", "false") == "true"
                or self.store.get_setting(f"symbol_blocked:{symbol}", "false") == "true"
            ):
                return RiskDecision(
                    False, f"标的 {symbol} 存在持仓对账差异，该标的新增买入已临时阻断", 0,
                    blocked_by=(RECONCILIATION_LOCK,),
                )
            # 2. 策略级阻断
            if strategy_id:
                # 租户优先级：**走共享入口** `tenant_of_broker`（broker 显式携带 >
                # 按 account_id 反查注册表 > 仅无前缀键）。
                # 生产 broker（FutuPaperBroker）不带 tenant_id 属性，必须能由 account_id 反解出租户，
                # 否则对账写出的 `strategy_reconciliation_disabled:{tenant_id}:{strategy}` 将无人读取，拦多变成拦不住。
                # 这里是读取侧（只判断"是否被暂停"），解析不出**不得** fail-closed ——
                # 静默放行会让对账熔断失效；因此用 strict=False，取不到就只查无前缀键。
                broker_tenant, _ = tenant_of_broker(broker, strict=False)
                is_strat_disabled = (
                    self.store.get_setting(f"strategy_reconciliation_disabled:{strategy_id}", "false") == "true"
                    or (broker_tenant and self.store.get_setting(f"strategy_reconciliation_disabled:{broker_tenant}:{strategy_id}", "false") == "true")
                    or (broker_acc and self.store.get_setting(f"strategy_reconciliation_disabled:{broker_acc}:{strategy_id}", "false") == "true")
                )
                if is_strat_disabled:
                    return RiskDecision(
                        False,
                        f"策略 {strategy_id} 存在持仓差异已被对账暂停，禁止新开仓",
                        0,
                        blocked_by=(RECONCILIATION_LOCK,),
                    )
            # 3. 全局阻断兜底（仅当未指定策略时）
            acc_recovery_blocked = (
                self.store.get_setting(f"recovery_blocked:{broker_acc}", "false") == "true"
                if broker_acc else False
            )
            if not strategy_id and (acc_recovery_blocked or self.store.get_setting("recovery_blocked", "false") == "true"):
                return RiskDecision(
                    False, "启动恢复发现三账差异，请先完成持仓归属与对账", 0,
                    blocked_by=(RECONCILIATION_LOCK,),
                )
        contract_multiplier = multiplier or (100.0 if OPTION_PATTERN.match(symbol) else 1.0)
        notional = float(quantity) * float(price) * contract_multiplier
        breaker_info: dict = {}
        # 急停禁止新增风险，但保留卖出平仓通道；严格支持按账户隔离急停。
        broker_acc = getattr(broker, "account_id", None)
        if self.store.kill_switch(account_id=broker_acc) and side == "BUY":
            target_desc = f"账户 {broker_acc}" if broker_acc else "系统"
            return RiskDecision(
                False, f"{target_desc} 急停已开启，禁止新增仓位", notional,
                blocked_by=(ACCOUNT_HALT,),
            )
        # A regular SELL may still open risk (for example a protected option short),
        # so only verified long-position reductions receive the dedicated allowance.
        if (side != "SELL" or protected_short) and notional > self.limits.max_order_notional:
            return RiskDecision(False, f"单笔名义金额超过 ${self.limits.max_order_notional:,.0f}", notional)
        budget_allowed, budget_reason, budget_details = strategy_budget_allowed(
            self.store, broker, strategy_id, symbol, side, quantity, price, contract_multiplier,
        )
        self.store.operational_event(
            "portfolio_allocator", "PRE_TRADE_BUDGET_CHECK",
            "HEALTHY" if budget_allowed else "BLOCKED",
            {"strategy": strategy_id, "symbol": symbol, "reason": budget_reason, **budget_details},
        )
        if not budget_allowed:
            return RiskDecision(False, budget_reason, notional)
        orders = broker.orders()
        pending = 0 if orders.empty else int(orders["order_status"].map(broker.is_cancellable_status).sum())
        if pending >= self.limits.max_pending_orders:
            return RiskDecision(False, f"待成交订单已达到 {self.limits.max_pending_orders} 笔", notional)
        positions = broker.positions() if hasattr(broker, "positions") else None
        info = broker.account_info() if hasattr(broker, "account_info") else None
        risk_reducing_sell = False
        if side == "SELL" and positions is not None and not protected_short:
            owned_rows = positions[positions["code"] == symbol] if not positions.empty else positions
            owned = float(owned_rows.iloc[0].get("qty", 0)) if not owned_rows.empty else 0
            pending_sell = 0.0
            if execution_mode == "SHADOW" and strategy_id:
                owned, _ = self.store.shadow_position(strategy_id, symbol)
            elif not orders.empty and {"code", "trd_side", "qty"}.issubset(orders.columns):
                active = orders[orders["order_status"].map(broker.is_cancellable_status)]
                pending_sell = float(active[(active["code"] == symbol) &
                    (active["trd_side"].astype(str).str.upper().str.contains("SELL"))]["qty"].sum())
            if quantity + pending_sell > owned:
                return RiskDecision(False, f"卖出数量超过可用持仓 {max(0, owned - pending_sell):g}", notional)
            enforce_virtual = (
                execution_mode != "SHADOW"
                and os.getenv("ENFORCE_VIRTUAL_POSITIONS", "false").lower() == "true"
            )
            if strategy_id and enforce_virtual:
                strategy_owned = self.store.strategy_position(strategy_id, symbol)
                if quantity > strategy_owned:
                    # 检查是否有其他策略持有该标的；若无其他策略持有，且属于券商真实未认领底仓，
                    # 允许经授权的长线管理策略 (core_satellite) 参与调仓，避免误拦截用户手工建仓存量
                    # 必须按本账户/租户取账本：无账户视图是跨租户混合账本，会把别家租户的持仓
                    # 算成「其他策略持有」，使 unclaimed_broker_qty 偏大、卖出上限被放宽。
                    scoped_env, scoped_account = broker_scope(broker)
                    other_strategies_qty = sum(
                        float(r["quantity"]) for r in self.store.virtual_positions(
                            env=scoped_env,
                            account_id=scoped_account,
                        )
                        if r.get("symbol") == symbol and r.get("strategy_id") != strategy_id
                    )
                    unclaimed_broker_qty = max(0.0, owned - other_strategies_qty)
                    if strategy_id == "core_satellite" and quantity <= unclaimed_broker_qty:
                        pass
                    else:
                        return RiskDecision(
                            False, f"{strategy_id} 虚拟持仓只有 {strategy_owned:g}，禁止占用其他策略持仓",
                            notional,
                        )
            risk_reducing_sell = True
            if notional > self.limits.max_reduction_order_notional:
                return RiskDecision(
                    False,
                    f"单笔减仓金额超过 ${self.limits.max_reduction_order_notional:,.0f}",
                    notional,
                )
            if self.store.get_setting(f"symbol_blocked:{symbol}", "false") == "true":
                self.store.audit(
                    "RECONCILIATION_EXIT_ALLOWED",
                    {"symbol": symbol, "quantity": quantity, "broker_owned": owned},
                    strategy_id,
                 actor="service:risk")
        elif side == "SELL" and protected_short:
            if not OPTION_PATTERN.match(symbol):
                return RiskDecision(False, "组合保护卖出只允许标准美股期权合约", notional)
            self.store.audit(
                "PROTECTED_COMBO_SHORT_ALLOWED",
                {"symbol": symbol, "quantity": quantity, "underlying": underlying},
                strategy_id,
             actor="service:risk")
        if side == "SELL" and not risk_reducing_sell and not protected_short:
            if notional > self.limits.max_order_notional:
                return RiskDecision(
                    False, f"无法确认属于减仓，沿用单笔 ${self.limits.max_order_notional:,.0f} 限额",
                    notional,
                )
        day_start = ny_trading_day_start_utc()
        if risk_reducing_sell:
            # 减仓桶的「已用额度」与实盘闸门走**同一个累加实现**（store.reduce_notional），
            # 且状态集合一致 —— 否则同一笔 SELL 在两处算出的可用额度会分叉，错的那份先触发。
            used = self.store.reduce_notional(
                (row for row in self.store.intents(limit=50_000)
                 if str(row.get("status")) in USED_NOTIONAL_STATUSES
                 and str(row.get("created_at") or "") >= day_start.isoformat()),
                "SELL",
            )
            if used + notional > self.limits.max_daily_reduction_notional:
                return RiskDecision(
                    False,
                    f"当日累计减仓金额将超过 ${self.limits.max_daily_reduction_notional:,.0f}",
                    notional,
                )
        else:
            used = self.store.sum_notional_since(
                day_start.isoformat(), side=None if protected_short else "BUY",
            )
            if used + notional > self.limits.max_daily_notional:
                return RiskDecision(
                    False, f"当日累计名义金额将超过 ${self.limits.max_daily_notional:,.0f}",
                    notional,
                )
        if side == "BUY" and info is not None and not info.empty:
            os.environ["RISK_CORRELATION_CLUSTERS"] = self.store.get_setting(
                "correlation_clusters", os.getenv("RISK_CORRELATION_CLUSTERS", "{}")
            )
            account = info.iloc[0]
            assets = market_number(account.get("total_assets", 0))
            buying_power = market_number(account.get("power", account.get("cash", 0)))
            daily_pl = market_number(account.get("unrealized_pl", account.get("profit_val", 0)))
            if buying_power > 0 and notional > buying_power:
                return RiskDecision(False, "可用购买力不足", notional)
            if self._paper_warn_only(
                broker, broker_acc,
                metric_label="当日亏损",
                metric_value=-daily_pl, assets=assets,
            ):
                # 纸面账户落在告警带里：已发 WARNING，**跳过拦单**继续后续检查
                pass
            elif daily_pl <= -self.limits.max_daily_loss:
                if self._loss_guard(
                    broker, broker_acc,
                    metric_label="当日亏损",
                    loss_amount=-daily_pl, assets=assets,
                ):
                    return RiskDecision(
                        False,
                        f"账户 {broker_acc or ''} 亏损已达到 ${self.limits.max_daily_loss:,.0f} 急停线",
                        notional,
                        blocked_by=(INTRADAY_LOSS, DAILY_LOSS),
                    )
            if assets > 0 and positions is not None:
                shadow_positions = self.store.shadow_open_positions()
                shadow_total = sum(
                    abs(float(item["quantity"]) * float(item["average_cost"])
                        * (100 if OPTION_PATTERN.match(item["symbol"]) else 1))
                    for item in shadow_positions
                )
                if execution_mode == "SHADOW":
                    if shadow_total + notional > assets * self.limits.max_gross_exposure:
                        return RiskDecision(False, "Shadow 组合总敞口将超过账户总风险上限", notional)
                    if OPTION_PATTERN.match(symbol):
                        shadow_option = sum(
                            abs(float(item["quantity"]) * float(item["average_cost"]) * 100)
                            for item in shadow_positions if OPTION_PATTERN.match(item["symbol"])
                        )
                        if shadow_option + notional > self.limits.max_option_premium:
                            return RiskDecision(False, "Shadow 期权权利金总敞口超过上限", notional)
                day_start = ny_trading_day_start_utc()
                snapshots = self.store.equity_since(day_start.isoformat())
                # 跨环境与换账户隔离：避免将模拟盘100万美元旧快照与实盘数万美元混淆计算日内回撤
                valid_snapshots = [
                    float(row["total_assets"]) for row in snapshots
                    if 0.3 <= float(row["total_assets"]) / max(1.0, assets) <= 2.5
                ]
                peak = max(valid_snapshots + [assets])
                drawdown = 1 - assets / peak if peak > 0 else 0
                # 日内权益损失分支**不走**纸面告警线：它是"离峰值的美元损失"，
                # 与"当日亏损"不是同一个量，更与回撤同源。Product 的两条线只覆盖
                # 「当日亏损」与「日内回撤」两个分支，这里若也旁路，就等于从侧门
                # 对 2.91% 的回撤发告警 —— 与"回撤 10% 才告警"的设计自相矛盾。
                if peak - assets >= self.limits.max_daily_loss:
                    if self._loss_guard(
                        broker, broker_acc,
                        metric_label="日内权益损失金额",
                        loss_amount=peak - assets, assets=assets, drawdown=drawdown,
                    ):
                        return RiskDecision(
                            False,
                            f"账户 {broker_acc or ''} 日内权益损失达到急停线，已自动急停",
                            notional,
                        )
                # 回撤分支：**告警线是 max_drawdown_pct，不是日亏损线** ——
                # 拿回撤额去比 `paper_daily_loss_warn` 是两个不同量纲，会写出
                # "当日亏损 $29,100" 这种与事实不符的告警（10-23 缺陷）。
                if self._paper_warn_only(
                    broker, broker_acc,
                    metric_label="日内回撤",
                    metric_value=drawdown, assets=assets,
                    metric_is_ratio=True,
                ):
                    pass  # 纸面账户落在回撤告警带里：已告警，跳过拦单
                elif drawdown >= self.limits.max_drawdown_pct:
                    # 既有硬停条件**一字未改**（实盘行为不得因本次修改而变）：
                    # 纸面若回撤额已过硬停线，`_paper_warn_only` 会返回 False 落到这里。
                    if self._loss_guard(
                        broker, broker_acc,
                        metric_label="日内回撤金额",
                        loss_amount=drawdown * assets, assets=assets, drawdown=drawdown,
                    ):
                        return RiskDecision(
                            False,
                            f"账户 {broker_acc or ''} 日内回撤达到 {drawdown:.2%}，已自动急停",
                            notional,
                            blocked_by=(INTRADAY_DRAWDOWN,),
                        )
                # 回撤熔断（G4）：取数与计算在本侧完成；聚合层只接收结果
                breaker = self._drawdown_breaker_for_entry(
                    broker, broker_acc, assets, execution_mode,
                )
                breaker_info = breaker
                if breaker["blocked"]:
                    reasons = tuple(reason.code for reason in breaker["reasons"]) or (
                        DRAWDOWN_ABSOLUTE,
                    )
                    return RiskDecision(
                        False,
                        "跨日回撤熔断已触发，暂停新开仓（持仓退出不受影响）",
                        notional,
                        blocked_by=reasons,
                        breaker_reasons=breaker["reasons"],
                        breaker_would_block=breaker["would_block"],
                    )
                market_value = float(positions.get("market_val", []).sum()) if "market_val" in positions else 0
                symbol_rows = positions[positions["code"] == symbol] if not positions.empty else positions
                symbol_value = float(symbol_rows["market_val"].sum()) if "market_val" in symbol_rows else 0
                if not OPTION_PATTERN.match(symbol) and (symbol_value + notional) / assets > self.limits.max_symbol_weight:
                    return RiskDecision(False, f"单标的仓位将超过 {self.limits.max_symbol_weight:.0%}", notional)
                family = underlying or (symbol if not OPTION_PATTERN.match(symbol) else None)
                if family and not positions.empty and "market_val" in positions:
                    family_value = float(positions[
                        positions["code"].astype(str).map(
                            lambda code: code == family or code.startswith(family + "2")
                        )
                    ]["market_val"].abs().sum())
                    if (family_value + notional) / assets > self.limits.max_symbol_weight:
                        return RiskDecision(False, f"正股与期权合并敞口将超过 {self.limits.max_symbol_weight:.0%}", notional)
                if (market_value + notional) / assets > self.limits.max_gross_exposure:
                    return RiskDecision(False, f"账户总持仓将超过 {self.limits.max_gross_exposure:.0%}", notional)
                if OPTION_PATTERN.match(symbol):
                    option_value = 0.0
                    if not positions.empty and "market_val" in positions:
                        option_value = float(positions[
                            positions["code"].astype(str).map(lambda value: bool(OPTION_PATTERN.match(value)))
                        ]["market_val"].abs().sum())
                    if option_value + notional > self.limits.max_option_premium:
                        return RiskDecision(False, f"期权权利金敞口将超过 ${self.limits.max_option_premium:,.0f}", notional)
            allowed, portfolio_reason, exposure = projected_portfolio_allowed(
                broker, symbol, side, quantity, price, multiplier, underlying,
                self.store, execution_mode,
                rebalance_reduction_delta=float((metadata or {}).get("rebalance_reduction_delta", 0) or 0),
            )
            self.store.operational_event("portfolio_risk", "PRE_TRADE_CHECK",
                                         "HEALTHY" if allowed else "BLOCKED", exposure)
            if not allowed:
                return RiskDecision(False, portfolio_reason, notional)
        return RiskDecision(
            True, "风控通过", notional,
            breaker_reasons=tuple(breaker_info.get("reasons", ())),
            breaker_unsupported=str(breaker_info.get("unsupported", "")),
            breaker_would_block=bool(breaker_info.get("would_block", False)),
        )
