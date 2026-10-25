"""跨日回撤熔断（G4）—— 纯判定内核。

设计：`docs/risk_budget_circuit_breaker_design.md`（基线 `c227416`）。

本模块**只做纯计算与状态迁移**，不查库、不调券商、不改任何既有门禁：
- 回撤口径：双基准，各自维护"只涨不跌"的跟踪峰值（`dd_vs_peak` / `dd_since_reset`）；
- 状态机：off / shadow / enforce 三阶段，1 档滞回自动解除，2 档只能人工三件套解除；
- 阈值类配置**缺键即 fail-closed**（`BreakerConfigError`），不回落更大值；
- 与既有 4 套拦截的**聚合**由 `entry_gate.resolve_entry_block_reasons()` 负责，
  既有日内硬停（`risk.py` 的 `max_drawdown_pct` / `max_daily_loss`）判定位置**一字不改**。

红线：任何等级都**不影响 SELL**；本模块没有 SELL 分支。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 原因码

RECONCILIATION_LOCK = "reconciliation_lock"
ACCOUNT_HALT = "account_halt"
STRATEGY_REDUCE_ONLY = "strategy_reduce_only"
DRAWDOWN_WARN = "dd_since_reset_warn"
DRAWDOWN_STOP = "dd_since_reset_stop"
DRAWDOWN_ABSOLUTE = "dd_vs_peak_absolute"
DAILY_LOSS = "daily_loss_hard_stop"
RISK_BUDGET = "risk_budget_limit"
#: 既有日内硬停（`risk.py:463-471` / `:525-537`，判定与副作用原地不动，仅标注原因）
INTRADAY_LOSS = "intraday_loss_10k"
INTRADAY_DRAWDOWN = "intraday_dd_5pct"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

#: 采样离群阈值：单轮 |Δ| 超过 40% 视为异常（券商返回错数/账户切换），本轮跳过
MAX_EQUITY_JUMP = 0.40

#: 绝对底线的锁存间隔（2pp）
DEFAULT_REARM_GAP_PP = 0.02


class BreakerConfigError(RuntimeError):
    """阈值类配置缺失或非法 —— fail-closed：拒绝开新仓，不回落默认值。"""


@dataclass(frozen=True)
class BreakerThresholds:
    warn: float
    stop: float
    resume: float
    rearm_gap_pp: float
    mode: str

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise BreakerConfigError(f"risk_breaker_mode 非法：{self.mode!r}，只允许 {_VALID_MODES}")
        for name in ("warn", "stop", "resume", "rearm_gap_pp"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BreakerConfigError(f"{name} 必须是有限数字，得到 {value!r}")
        if not (0 < self.resume < self.warn < self.stop <= 1):
            raise BreakerConfigError(
                "阈值链必须满足 0 < resume < warn < stop <= 1，"
                f"实际 resume={self.resume} warn={self.warn} stop={self.stop}"
            )
        if self.rearm_gap_pp <= 0:
            raise BreakerConfigError(f"rearm_gap_pp 必须 > 0，得到 {self.rearm_gap_pp}")


#: 阈值键名（逐租户；后缀 `:{tenant_id}`）
_THRESHOLD_KEYS = {
    "warn": "risk_drawdown_warn_pct",
    "stop": "risk_drawdown_stop_pct",
    "resume": "risk_drawdown_resume_pct",
    "rearm_gap_pp": "risk_drawdown_rearm_gap_pp",
}


def load_thresholds(getter: Callable[[str], Any], tenant_id: str) -> BreakerThresholds:
    """从租户键解析阈值；**缺任一键即抛 `BreakerConfigError`**（fail-closed）。

    `getter(tenant_key)` 返回该租户键的值（None/"" = 未配置）。
    """
    suffix = f":{tenant_id}" if tenant_id else ""
    values: dict[str, float] = {}
    missing: list[str] = []
    for name, base in _THRESHOLD_KEYS.items():
        raw = getter(f"{base}{suffix}")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            missing.append(f"{base}{suffix}")
            continue
        try:
            values[name] = float(raw)
        except (TypeError, ValueError) as exc:
            raise BreakerConfigError(f"{base}{suffix} 不是数字：{raw!r}") from exc
    mode_raw = getter(f"risk_breaker_mode{suffix}")
    if mode_raw is None or (isinstance(mode_raw, str) and not mode_raw.strip()):
        mode = MODE_OFF
    else:
        mode = str(mode_raw).strip().lower()
    if missing:
        raise BreakerConfigError("熔断阈值缺键（fail-closed，不回落默认值）：" + ", ".join(missing))
    return BreakerThresholds(mode=mode, **values)


# ------------------------------------------------------------------ 回撤计算


def real_equity_env(settings: object) -> str:
    """真实账户的净值快照 env（**单一来源派生，禁止手写字面量**）。

    `equity_snapshots.env` 里真实账户形如 `REAL_<account_id>`；账户 ID 只从
    `Settings.real_account_id`（即 `REAL_ACCOUNT_ID`）取，换账户自动跟随。
    手抄错一位就会查不到行 → 回撤算成 0% → 10% 底线假绿（2026-10-25 实际踩过）。

    注意：派生只防手抄错位，**防不了配置本身错**（`REAL_ACCOUNT_ID` 空/配错 →
    拼出不存在的 env），所以调用侧仍必须执行「零行抛错 / peak 倒挂拒绝 / 无默认值」。
    """
    account_id = str(getattr(settings, "real_account_id", "") or "").strip()
    if not account_id:
        raise BreakerConfigError("REAL_ACCOUNT_ID 未配置，无法派生实盘净值快照 env")
    return f"REAL_{account_id}"


class BreakerUnsupportedEnv(RuntimeError):
    """该交易环境不支持熔断（**显式拒绝，不静默 off**）。"""

    code = "breaker_unsupported_env"

    def __init__(self, message: str, *, trading_env: str) -> None:
        super().__init__(message)
        self.trading_env = trading_env

    def page_hint(self) -> str:
        return (
            f"{self.trading_env} 熔断未启用：`equity_snapshots` 无账户/租户维度，"
            "多账户会共用同一峰值导致跨账户污染。启用条件：快照表具备账户维度后开放。"
        )


def resolve_equity_env(settings: object, trading_env: str) -> str:
    """熔断启用范围：本批**只对 `REAL_{account_id}` 启用**，其他环境显式拒绝。

    - `REAL` → 派生 `REAL_<account_id>`；
    - `SIMULATE`/其他 → 抛 `BreakerUnsupportedEnv`（页面据此显示「未启用 + 启用条件」，
      禁止静默 off，否则又会变成「看着有其实没有」）；
    - 保留 2.5x 跨度守卫：等 `equity_snapshots` 具备账户维度后再开模拟盘。
    """
    env = str(trading_env or "").strip().upper()
    if env != "REAL":
        raise BreakerUnsupportedEnv(
            f"交易环境 {env or '(空)'} 不支持回撤熔断（本批仅 REAL 账户启用）",
            trading_env=env or "(空)",
        )
    return real_equity_env(settings)


@dataclass(frozen=True)
class EquitySample:
    ok: bool
    equity: float | None
    reason: str = ""


def validate_equity_sample(equity: float | None, previous_equity: float | None) -> EquitySample:
    """校验券商权益采样；异常采样**不改峰值、不参与判定**。"""
    if equity is None:
        return EquitySample(ok=False, equity=None, reason="权益为空")
    if not isinstance(equity, (int, float)) or not math.isfinite(float(equity)):
        return EquitySample(ok=False, equity=None, reason=f"权益非有限数字：{equity!r}")
    value = float(equity)
    if value <= 0:
        return EquitySample(ok=False, equity=None, reason=f"权益非正数：{value}")
    if previous_equity is not None and previous_equity > 0:
        if abs(value / previous_equity - 1) > MAX_EQUITY_JUMP:
            return EquitySample(
                ok=False,
                equity=None,
                reason=f"权益离群：{previous_equity:.2f} → {value:.2f}（超过 {MAX_EQUITY_JUMP:.0%}）",
            )
    return EquitySample(ok=True, equity=value)


@dataclass(frozen=True)
class Drawdowns:
    dd_vs_peak: float
    dd_since_reset: float
    peak_hist: float
    peak_reset: float


def compute_drawdowns(
    equity: float,
    *,
    peak_hist: float,
    peak_reset: float,
    baseline_equity: float,
) -> Drawdowns:
    """更新两个跟踪峰值并计算双口径回撤（均恒在 `[0, 1)`）。

    - `peak_hist`：历史峰值，永不因重置变小；
    - `peak_reset`：自上次重置以来的跟踪峰值，起点 = `baseline_equity`；
    - 创新高时回撤恒为 0，**不会出现负回撤把之前的亏损抵掉**（Reviewer C1）。
    """
    equity = float(equity)
    if peak_hist <= 0 or peak_reset <= 0 or baseline_equity <= 0:
        raise ValueError("峰值/基准必须为正数")
    new_peak_hist = max(float(peak_hist), equity)
    new_peak_reset = max(float(peak_reset), float(baseline_equity), equity)
    dd_vs_peak = max(0.0, 1.0 - equity / new_peak_hist)
    dd_since_reset = max(0.0, 1.0 - equity / new_peak_reset)
    return Drawdowns(
        dd_vs_peak=dd_vs_peak,
        dd_since_reset=dd_since_reset,
        peak_hist=new_peak_hist,
        peak_reset=new_peak_reset,
    )


def initial_peaks(
    *,
    current_equity: float,
    historical_high: float | None,
) -> tuple[float, float]:
    """首次启用初始化：`peak_hist` 取「近 30 天快照最高值」与当前权益的**较大者**。

    🔴 若把 peak 直接设成当前权益，当前 −9.55% 的深水区会被抹成 0%（假绿），
    因此 `historical_high` 必须来自 backfill，不得省略。
    """
    if current_equity <= 0:
        raise ValueError("当前权益必须为正数")
    if historical_high is None:
        raise ValueError("首次启用必须提供 backfill 的历史峰值（禁止用当前权益冒充）")
    peak_hist = max(float(historical_high), float(current_equity))
    # 重置基准的跟踪峰值起点 = baseline_equity（首次启用时 = 当前权益）
    return peak_hist, float(current_equity)


# ------------------------------------------------------------------ 状态机


@dataclass
class RiskState:
    """`risk_state` 表的一行（纯内存表示，不含 IO）。"""

    tenant_id: str
    scope: str = "equity"
    peak_hist: float = 0.0
    peak_hist_at: str = ""
    peak_hist_source: str = "live"
    baseline_equity: float = 0.0
    baseline_at: str = ""
    baseline_source: str = "live"
    peak_reset: float = 0.0
    breaker_level: int = 0
    breaker_set_at: str | None = None
    breaker_reason: str = ""
    ack_dd_vs_peak: float | None = None
    ack_at: str | None = None
    de_risk_scale: float = 1.0
    reset_count_30d: int = 0
    last_reset_at: str | None = None
    updated_at: str = ""

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True)
class BlockReason:
    code: str
    detail: str
    gate: str = "drawdown_breaker"

    def as_audit(self) -> dict[str, str]:
        return {"blocked_by": self.code, "detail": self.detail, "gate": self.gate}


@dataclass(frozen=True)
class BreakerDecision:
    level: int
    would_block: bool
    blocked: bool
    reasons: tuple[BlockReason, ...] = ()
    state: RiskState = field(default=None)  # type: ignore[assignment]
    alerts: tuple[tuple[str, str], ...] = ()  # (级别, 文案)
    skipped_reason: str = ""


def effective_absolute_floor(ack_dd_vs_peak: float | None, gap_pp: float) -> float:
    """绝对底线：锁存后放宽到 `max(10%, ack + 2pp)`，否则恒为 10%。"""
    base = 0.10
    if ack_dd_vs_peak is None:
        return base
    return max(base, float(ack_dd_vs_peak) + float(gap_pp))


def _level_for(dd_since_reset: float, dd_vs_peak: float, thresholds: BreakerThresholds,
               ack_dd_vs_peak: float | None) -> tuple[int, str]:
    """并集判定：2 档优先（含绝对底线），其次 8% 档。返回 (level, reason_code)。"""
    floor = effective_absolute_floor(ack_dd_vs_peak, thresholds.rearm_gap_pp)
    if dd_since_reset >= thresholds.stop:
        return 2, DRAWDOWN_STOP
    if dd_vs_peak >= floor:
        return 2, DRAWDOWN_ABSOLUTE
    if dd_since_reset >= thresholds.warn:
        return 1, DRAWDOWN_WARN
    return 0, ""


def _trading_day(value: str | None) -> str:
    """从 ISO 时间串取日期部分；容错返回空串。"""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def evaluate(
    state: RiskState,
    sample: EquitySample,
    thresholds: BreakerThresholds,
    *,
    now: str | None = None,
    current_trading_day: str | None = None,
) -> BreakerDecision:
    """一轮熔断评估（纯函数：返回新状态与是否拦截）。

    - 异常采样：状态不变、不判定（`skipped_reason` 说明原因）；
    - 1 档：`dd_since_reset <= resume` 自动解除；2 档：只能人工解除；
    - `ack` 双向清除：锁存后**下一个交易日**且 `dd_vs_peak < stop`（10%）→ 清除；
    - `de_risk_scale` 在 `dd_since_reset < warn` 后恢复 1.0；
    - `mode=off`：只算状态、不拦；`shadow`：记 `would_block` 不拦；`enforce`：真拦。
    """
    timestamp = now or datetime.now().isoformat(timespec="seconds")
    if not sample.ok or sample.equity is None:
        return BreakerDecision(
            level=state.breaker_level,
            would_block=False,
            blocked=False,
            state=state,
            skipped_reason=sample.reason or "权益采样异常",
        )
    if thresholds.mode == MODE_OFF:
        return BreakerDecision(level=0, would_block=False, blocked=False, state=state,
                               skipped_reason="breaker_mode=off")

    equity = sample.equity
    dd = compute_drawdowns(
        equity,
        peak_hist=state.peak_hist,
        peak_reset=state.peak_reset,
        baseline_equity=state.baseline_equity,
    )
    new_state = replace(
        state,
        peak_hist=dd.peak_hist,
        peak_reset=dd.peak_reset,
        peak_hist_at=timestamp,
        updated_at=timestamp,
    )

    # --- ack 双向清除（只锁存不清除 = 10% 底线永久放宽）
    if new_state.ack_dd_vs_peak is not None:
        ack_day = _trading_day(new_state.ack_at)
        today = current_trading_day or _trading_day(timestamp) or date.today().isoformat()
        if ack_day and ack_day < today and dd.dd_vs_peak < thresholds.stop:
            new_state = replace(
                new_state,
                ack_dd_vs_peak=None,
                ack_at=None,
                breaker_reason=f"{new_state.breaker_reason};ack_cleared",
            )

    # --- de_risk_scale 恢复（解除后预算减半，回到 <8% 恢复全额）
    if new_state.de_risk_scale < 1.0 and dd.dd_since_reset < thresholds.warn:
        new_state = replace(new_state, de_risk_scale=1.0)

    target_level, reason_code = _level_for(
        dd.dd_since_reset, dd.dd_vs_peak, thresholds, new_state.ack_dd_vs_peak
    )

    # --- 滞回：1 档低于 resume 才自动解除；2 档绝不自动降级
    if new_state.breaker_level == 2:
        level = 2
        reason_code = new_state.breaker_reason.split(":", 1)[-1] or DRAWDOWN_STOP
    elif new_state.breaker_level == 1 and target_level == 0:
        if dd.dd_since_reset <= thresholds.resume + 1e-9:
            level = 0
        else:
            level = 1
            reason_code = DRAWDOWN_WARN
    else:
        level = target_level

    reasons: tuple[BlockReason, ...] = ()
    if level > 0:
        code = DRAWDOWN_ABSOLUTE if level == 2 and reason_code == DRAWDOWN_ABSOLUTE else (
            DRAWDOWN_STOP if level == 2 else DRAWDOWN_WARN
        )
        reasons = (
            BlockReason(
                code=code,
                detail=(
                    f"dd_since_reset={dd.dd_since_reset:.2%} "
                    f"dd_vs_peak={dd.dd_vs_peak:.2%} level={level}"
                ),
            ),
        )

    new_state = replace(
        new_state,
        breaker_level=level,
        breaker_set_at=(timestamp if level != state.breaker_level else state.breaker_set_at),
        breaker_reason=f"{level}:{reason_code}" if level else "",
    )

    would_block = level > 0
    blocked = would_block and thresholds.mode == MODE_ENFORCE
    alerts: list[tuple[str, str]] = []
    if level != state.breaker_level:
        if level == 2:
            alerts.append(("CRITICAL", f"回撤熔断 2 档：停新开仓，需人工复核解除（{reason_code}）"))
        elif level == 1:
            alerts.append(("WARNING", f"回撤熔断 1 档：停新开仓（{reason_code}）"))
        else:
            alerts.append(("INFO", "回撤熔断已自动解除"))
    return BreakerDecision(
        level=level,
        would_block=would_block,
        blocked=blocked,
        reasons=reasons,
        state=new_state,
        alerts=tuple(alerts),
    )


# ------------------------------------------------------------------ 人工解除


class ManualResetError(RuntimeError):
    """人工解除被拒绝（越权 / 频率超限 / 参数非法）。

    `retry_after`：窗口内被拒时给出「下次可解除时间」（ISO8601），
    页面必须展示（禁止静默拒绝或只报「操作失败」）。
    """

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


#: 人工解除的最小间隔（滑动窗口，懒清零：过期后计数回到 1，不是终身一次）
RESET_WINDOW_DAYS = 30


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def reset_retry_after(state: RiskState, *, now: str | None = None) -> str | None:
    """窗口内返回下次可解除时间（ISO8601）；窗口已过或从未重置返回 None。

    ⚠️ `last_reset_at` **有值但解析失败**时返回 None 只是"算不出"，**不代表可解除**：
    `manual_reset` 会在此基础上做 fail-closed 拒绝（见 `unparsable_last_reset`）。
    """
    previous = _parse_timestamp(state.last_reset_at)
    if previous is None:
        return None
    current = _parse_timestamp(now) if now else datetime.now(previous.tzinfo)
    if current is None:
        return None
    deadline = previous + timedelta(days=RESET_WINDOW_DAYS)
    if current.tzinfo is None and deadline.tzinfo is not None:
        current = current.replace(tzinfo=deadline.tzinfo)
    elif current.tzinfo is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=current.tzinfo)
    return deadline.isoformat() if current < deadline else None


def unparsable_last_reset(state: RiskState) -> bool:
    """`last_reset_at` 有值但无法解析 → 窗口状态未知，人工解除必须 fail-closed。"""
    return bool(state.last_reset_at) and _parse_timestamp(state.last_reset_at) is None


def manual_reset(
    state: RiskState,
    *,
    current_equity: float,
    dd_vs_peak: float,
    now: str,
    authorized: bool,
    reset_scale: float = 0.5,
) -> RiskState:
    """2 档人工解除的**三件套原子语义**（缺一即无效，见设计 §D2.3/§D2.4）。

    频率限制是**滑动窗口 + 懒清零**：窗口内第二次拒绝并回传 `retry_after`；
    窗口过期后允许解除，且 `reset_count_30d` 回到 1（不是终身只允许一次）。
    """
    if not authorized:
        raise ManualResetError("人工解除需要 admin 授权（RISK_BREAKER_MANUAL_RESET）")
    if current_equity <= 0:
        raise ManualResetError(f"当前权益非法：{current_equity}")
    if not (0 < reset_scale <= 1):
        raise ManualResetError(f"解除后预算系数非法：{reset_scale}")
    retry_after = reset_retry_after(state, now=now)
    if unparsable_last_reset(state):
        # fail-closed：时间戳坏了不能当成"窗口已过"，否则绕过 30 天频率限制
        logger.warning(
            "risk_state.last_reset_at 无法解析（%r），人工解除按窗口未过处理（fail-closed）",
            state.last_reset_at,
        )
        raise ManualResetError(
            f"last_reset_at 无法解析（{state.last_reset_at!r}），无法确认 {RESET_WINDOW_DAYS} 天窗口，"
            "按窗口未过拒绝；请先核对/修复 risk_state",
            retry_after=None,
        )
    if retry_after is not None:
        raise ManualResetError(
            f"人工解除窗口内（{RESET_WINDOW_DAYS} 天 ≤1 次），下次可解除：{retry_after}",
            retry_after=retry_after,
        )
    return replace(
        state,
        # ① baseline 归零（否则 1 档仍会拦）
        baseline_equity=float(current_equity),
        baseline_at=now,
        baseline_source="reset",
        peak_reset=float(current_equity),
        # ② 锁存当前 dd_vs_peak（绝对底线临时放宽到 max(10%, ack+2pp)）
        ack_dd_vs_peak=float(min(max(dd_vs_peak, 0.0), 1.0)),
        ack_at=now,
        # ③ 预算减半
        de_risk_scale=float(reset_scale),
        breaker_level=0,
        breaker_set_at=now,
        breaker_reason="manual_reset",
        # 滑动窗口内计数 = 1（过期后重新计 1，不是保留旧值/只增不减）
        reset_count_30d=1,
        last_reset_at=now,
        updated_at=now,
    )


# 原因聚合在 `entry_gate.py`（D3.1：只聚合、不判定；本模块只管判定内核）。
