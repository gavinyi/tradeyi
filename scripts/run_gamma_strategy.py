from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import time as clock
from zoneinfo import ZoneInfo

import os

import pandas as pd

os.environ.setdefault("FUTU_LOG_ROLE", "gamma")

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import Settings, load_gamma_campaigns
from futu_quant.execution import ExecutionManager
from futu_quant.candidate_quality import resolve_candidate_quality_for_campaign
from futu_quant.execution_mode import resolve_configured_execution_mode
from futu_quant.combo_analytics import combo_market_snapshot
from futu_quant.combo_execution import (
    ComboExecutionManager, create_combo, find_open_combo_for_campaign,
    _protected_relation,
)
from futu_quant.gamma_automation import decide_gamma_action
from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues
from futu_quant.gamma_risk import (
    contract_analysis, gamma_add_allowed, iv_entry_allowed, portfolio_greeks,
    realized_volatility, normalize_iv, market_number,
)
from futu_quant.option_signal import (
    configured_direction_allowed, configured_volatility_direction_allowed,
)
from futu_quant.volatility_optimizer import (
    decide_volatility_action, sync_volatility_position_cycle,
    volatility_partial_exit_plan,
    volatility_cooldown_hours, volatility_exit_reason,
    volatility_cooldown_bypass_allowed,
    volatility_profit_protection_profile,
    volatility_direction_size_factor, volatility_entry_confirmation,
    volatility_signal_state, volatility_cooldown_reentry_allowed,
    volatility_entry_quality_allowed, volatility_entry_tier, volatility_regime,
    volatility_threshold_profile, loss_adaptive_premium_budget,
    volatility_combo_entry_allowed, volatility_hard_loss_exit_reason,
    volatility_profit_capture_ratio, volatility_profit_lock_floor, select_partial_exit_batch,
)
from futu_quant.volatility_structures import automatic_combo_id
from futu_quant.volatility_orphans import (
    volatility_write_state_key,
    without_legacy_signal_state_keys,
    legacy_volatility_signal_state_key,
)
from futu_quant.campaign_cleanup import campaign_expired
from futu_quant.data_quality import (
    option_book_status, option_quote_activity, quote_freshness,
)
from futu_quant.quote_liveness import option_book_moved
from futu_quant.market_calendar import USMarketCalendar
from futu_quant.option_lifecycle import parse_option_code
from futu_quant.store import TradingStore
from futu_quant.tenant import tenant_id_of_broker
from futu_quant.volatility_pause import volatility_new_entries_paused  # noqa: F401  单源（P1-9）


NY = ZoneInfo("America/New_York")
MARKET_CALENDAR = USMarketCalendar()
LEGACY_STATE_FILE = Path("data/gamma_state.json")


def combo_position_mark(store, broker, combo_id: str) -> tuple[int, float, float]:
    combo = store.option_combo(combo_id)
    if not combo or combo.get("status") != "OPEN":
        return 0, 0.0, 0.0
    legs = store.option_combo_legs(combo_id)
    if not legs:
        return 0, 0.0, 0.0
    quotes = broker.snapshots([leg["symbol"] for leg in legs])
    detail, package = combo_market_snapshot(legs, quotes)
    if detail.empty:
        quantity = max(int(float(leg.get("filled_quantity", 0) or 0)) for leg in legs)
        return quantity, 0.0, 0.0
    entry_debit = sum(
        (1 if leg["side"] == "BUY" else -1)
        * float(leg.get("filled_quantity", 0) or 0)
        * float(leg.get("average_fill_price", 0) or 0)
        for leg in legs
    )
    current_value = float(package.get("executable_net_debit", 0) or 0)
    quantity = max(int(float(leg.get("filled_quantity", 0) or 0)) for leg in legs)
    return quantity, max(0.01, entry_debit), current_value


#: 盘口活跃观测的有效窗口（秒）：超过它视为「观测过期」，fail-closed 拦截而不是猜测。
BOOK_OBSERVATION_MAX_AGE_SECONDS = 900


def latest_book_activity_age(
    store, code: str, now: datetime,
) -> tuple[float | None, bool]:
    """跨轮次盘口变化年龄（秒）：最近一次采集的 book_active_seconds + 该观测的年龄。

    返回 ``(age, blocked)``：

    - ``(age, False)``：可用，年龄 = ``book_active_seconds + (now - fetched_at)``；
    - ``(None, False)``：有观测行但 ``book_active_seconds`` 为 NULL → **回落 300s 旧口径**
      （不是放行，也不是按盘口放行）；
    - ``(None, True)``：**必须 BLOCK** —— 无历史行 / `fetched_at` 缺失或不可解析 /
      负时差（时钟回拨）/ 超过 ``BOOK_OBSERVATION_MAX_AGE_SECONDS``。
      观测不可信时宁可拦截，绝不 clamp 成 0 变相放行。

    为什么必须跨轮次（2026-10-27）：入场每次 attempt 只隔 1.5s，跨分钟才变一次的盘口
    在 `book_moved`（同 attempt 两次快照比较）里永远是 False，于是回落 300s 成交门禁继续
    误拦。`option_history` 采集在每次雷达轮次都在累积盘口变化（`BookActivityTracker`）。
    """
    try:
        latest = store.latest_option_history(code)
    except Exception:  # noqa: BLE001 - 读不到观测即不可信，按 fail-closed 处理
        return None, True
    if not latest:
        return None, True
    book_active = latest.get("book_active_seconds")
    if book_active is None:
        return None, False
    fetched_at = latest.get("fetched_at")
    if not fetched_at:
        return None, True
    try:
        observed_dt = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return None, True
    if observed_dt.tzinfo is None:
        observed_dt = observed_dt.replace(tzinfo=timezone.utc)
    observed_age = (now - observed_dt).total_seconds()
    if observed_age < 0 or observed_age > BOOK_OBSERVATION_MAX_AGE_SECONDS:
        # 时钟回拨 / 观测过期：不 clamp，直接判不可信 → BLOCK
        return None, True
    try:
        book_age = float(book_active)
    except (TypeError, ValueError):
        return None, True
    if book_age != book_age:  # NaN：盘口全是 NaN 时可能出现
        return None, True
    return book_age + observed_age, False


def refreshed_trade_quotes(
    broker: FutuPaperBroker, code: str, underlying: str, now: datetime,
    maximum_spread_pct: float, retries: int = 2, wait_seconds: float = 1.5,
    maximum_stale_seconds: int = 600,
    book_active_seconds: float | None = None,
    book_basis_blocked: bool = False,
) -> tuple[pd.Series, pd.Series, dict]:
    """取入场前的期权 / 正股快照，**用盘口活性判新鲜**（2026-10-26 修）。

    - `update_time` 是成交驱动的，深度 ITM 十几分钟不成交就冻结，但 bid/ask 在动；
      旧实现直接拿它当盘口年龄，导致 91 条误拦里的 82 条（`OPTION_MARKET_DATA_BLOCKED`）。
    - 每次 attempt 用 `<2s` 间隔的**两次快照**比较 bid/ask/last：有变化即视为盘口活跃；
      600s 只做兜底（深度 ITM 无成交、且相邻快照也没动时靠它降级放行）。
    - **每次 attempt 重取时钟**：旧实现三次共用同一个 `now`，实测三次 age 完全相同
      （682.40366 ×3），重试实际是空转。
    - `book_active_seconds`：**跨轮次**盘口变化年龄（来自 `option_history` 采集），
      补上「跨分钟才变一次」的场景（2026-10-27）。
    - `book_basis_blocked`：观测不可信（无历史行 / 超 900s / 时钟回拨）→ fail-closed 拦截。
    """
    attempts: list[dict] = []
    last_option = None
    last_owner = None
    previous_option = None
    for attempt in range(retries + 1):
        if attempt:
            clock.sleep(wait_seconds)  # 两次快照间隔 ≤2s（默认 1.5s）
        attempt_now = datetime.now(NY)  # 每次重取时钟，修复重试空转
        quote = broker.snapshots([code, underlying])
        option = quote[quote["code"] == code].iloc[0]
        owner = quote[quote["code"] == underlying].iloc[0]
        book_moved = option_book_moved(previous_option, option)
        option_fresh, option_reason, option_age, option_degraded = option_quote_activity(
            option, attempt_now, book_moved=book_moved,
            book_active_seconds=book_active_seconds,
            fresh_seconds=180, maximum_seconds=maximum_stale_seconds,
            maximum_spread_pct=maximum_spread_pct,
        )
        if book_basis_blocked:
            # 门禁是「not fresh 且 not degraded」才算 stale（见 run_campaign 入场判定）：
            # 成交 180~300s + 双边盘口可用时 option_quote_activity 返回 degraded=True，
            # 只置 fresh=False 会被降级放行绕过 → 必须同时清掉 degraded。
            option_fresh = False
            option_degraded = False
            option_reason = (
                "盘口活跃观测不可信（无历史行 / 超过 900s / 时钟回拨），fail-closed 拦截"
            )
        # 旧口径（纯成交年龄）单列保留：上线后要按「旧 300s 会拦多少」对账
        _, _, trade_age_seconds = quote_freshness(
            option, attempt_now, maximum_stale_seconds,
        )
        owner_fresh, owner_reason, owner_age = quote_freshness(owner, attempt_now)
        book_ok, book_reason, spread_pct = option_book_status(option, maximum_spread_pct)
        previous_bid, previous_ask = (
            (float(previous_option.get("bid_price") or 0),
             float(previous_option.get("ask_price") or 0))
            if previous_option is not None else (None, None)
        )
        attempts.append({
            "attempt": attempt + 1,
            "option_fresh": option_fresh,
            "option_reason": option_reason,
            "option_age_seconds": option_age,
            # 旧口径成交年龄（对账用）：option_age_seconds 已是 min(成交, 盘口) 的有效年龄
            "trade_age_seconds": trade_age_seconds,
            "option_degraded": option_degraded,
            "book_moved": book_moved,
            "book_basis_blocked": book_basis_blocked,
            "bid_price": float(option.get("bid_price") or 0),
            "ask_price": float(option.get("ask_price") or 0),
            "previous_bid_price": previous_bid,
            "previous_ask_price": previous_ask,
            "underlying_fresh": owner_fresh,
            "underlying_reason": owner_reason,
            "underlying_age_seconds": owner_age,
            "book_ok": book_ok,
            "book_reason": book_reason,
            "spread_pct": spread_pct,
        })
        last_option, last_owner = option, owner
        previous_option = option
        if option_fresh and owner_fresh and book_ok:
            break
    latest = attempts[-1]
    latest["attempts"] = attempts
    return last_option, last_owner, latest


def load_state() -> dict:
    store = TradingStore()
    state = store.campaign_states()
    if not state and LEGACY_STATE_FILE.exists():
        try:
            legacy = json.loads(LEGACY_STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            legacy = {}
        if isinstance(legacy, dict):
            store.save_campaign_states(legacy)
            state = legacy
    return state


def save_state(state: dict) -> None:
    # A16（2026-10-27）：**绝不持久化历史无租户信号键**。
    # 缺陷形态：某进程周期开始时 load 到的内存副本仍含旧键
    # `volatility_long_gamma:signal:US.AMD`，整份 state 回写把这个键复活 →
    # `volatility_signal_state()` 里的迁移 `pop` 白做，DB 里新旧两份并存且旧键
    # `updated_at` 每轮刷新（生产 18:27:30 实测）。落盘前统一过滤，内存副本不动
    # （迁移仍可从内存读到旧键）。
    TradingStore().save_campaign_states(without_legacy_signal_state_keys(state))


def audit_legacy_signal_state_takeover(
    store, state: dict, strategy_id: str, underlying: str, tenant_id: str,
) -> bool:
    """旧无租户信号键被某租户接管时留一条审计（A17，2026-10-27）。

    迁移是「只搬不复制」，执行完旧键就从 state 里 `pop` 掉 —— 所以本函数对每个
    (标的, 旧键) 天然只触发一次。留痕的意义：接管是**静默的状态转移**，
    时间线上一旦对不上（谁拿走了那段确认计时），没有审计就只能靠猜。
    返回 True 表示本次确实发生了接管。

    **两条纪律**（Reviewer 2026-10-27 退改）：
    1. 审计是**观测动作**，`store.audit()` 失败绝不能打断入场主流程 —— 必须兜底。
    2. **空租户直接返回 False**：空租户分支在 `volatility_signal_state()` 里提前
       return、旧键不会被 `pop` → 不拦的话同一标的每轮都会发一条「接管」审计，
       而实际什么都没接管（假事件比没事件更糟）。
    """
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        return False
    normalized_underlying = str(underlying or "").strip().upper()
    legacy_key = legacy_volatility_signal_state_key(
        normalized_underlying, prefix=f"{strategy_id}:",
    )
    legacy_value = state.get(legacy_key)
    if not isinstance(legacy_value, dict):
        return False
    try:
        store.audit(
            "VOLATILITY_LEGACY_SIGNAL_STATE_TAKEOVER",
            {
                "tenant_id": normalized_tenant,
                "underlying": normalized_underlying,
                "legacy_key": legacy_key,
                "signal_started_at": legacy_value.get("signal_started_at"),
                "signal_epoch": legacy_value.get("signal_epoch"),
            },
            strategy_id,
            actor="service:gamma",
        )
    except Exception as exc:  # silent-ok: 审计失败降级为日志，不得中断交易主流程
        print(f"[WARN] 旧信号状态键接管审计写入失败（已忽略）：{exc}")
        return False
    return True


def safe_datetime(value, default: datetime) -> datetime:
    try:
        if value is None or pd.isna(value):
            return default
    except (TypeError, ValueError):  # silent-ok: 不可解析的选填参数回落默认值
        pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "<na>"}:
        return default
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return default
    if parsed.tzinfo is None and default.tzinfo is not None:
        parsed = parsed.replace(tzinfo=default.tzinfo)
    return parsed


def campaign_expiry_date(campaign: dict, code: str, default: datetime):
    parsed_contract = parse_option_code(code)
    if parsed_contract is not None:
        return parsed_contract.expiration
    return safe_datetime(campaign.get("strike_time"), default).date()


def volatility_exit_close_legs(
    campaign: dict,
    code: str,
    legs: list[dict],
    *,
    combo_available: bool = True,
) -> tuple[list[dict], str | None]:
    """Choose the safest close route for a volatility exit.

    Exit-maintenance campaigns and orphan-adopted positions may carry stale
    multi-leg metadata. When the live risk is a single broker position, a hard
    stop must never be blocked by a missing combo ledger.
    """
    if campaign.get("trigger_mode") != "VOLATILITY":
        return list(legs), None
    if campaign.get("exit_only"):
        return [{"code": code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}], "EXIT_ONLY"
    if len(legs) > 1 and not combo_available:
        return [{"code": code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}], "MISSING_COMBO"
    return list(legs), None


def volatility_near_expiry_itm_exit_reason(
    campaign: dict,
    code: str,
    *,
    now: datetime,
    underlying_price: float,
    quantity: int,
) -> str | None:
    """Force earlier exits for near-expiry in-the-money long options.

    Long ITM options close to expiration carry exercise/assignment and liquidity
    risk that is not captured by ordinary PnL stops. Treat this as a lifecycle
    invariant: if a volatility option is still held and is ITM within the
    configured DTE window, exit before waiting for the generic expiry cutoff.
    """
    if campaign.get("trigger_mode") != "VOLATILITY" or quantity <= 0:
        return None
    contract = parse_option_code(code)
    if contract is None:
        return None
    dte = (contract.expiration - now.date()).days
    exit_days = int(campaign.get("near_expiry_itm_exit_days", 1))
    if dte < 0 or dte > exit_days:
        return None
    intrinsic = (
        max(0.0, underlying_price - contract.strike)
        if contract.option_type == "CALL"
        else max(0.0, contract.strike - underlying_price)
    )
    min_intrinsic = float(campaign.get("near_expiry_itm_min_intrinsic", 0.01))
    if intrinsic < min_intrinsic:
        return None
    return (
        f"临近到期价内合约强制提前退出：剩余 {dte} 天，"
        f"{contract.option_type} 行权价 ${contract.strike:.2f}，"
        f"正股 ${underlying_price:.2f}，内在价值 ${intrinsic:.2f}"
    )


def volatility_exit_limit_price(
    quote: pd.Series,
    side: str,
    *,
    urgent: bool,
    campaign: dict | None = None,
) -> tuple[float, dict]:
    """Choose explicit exit prices for normal and urgent option closes."""
    campaign = campaign or {}
    bid = float(quote.get("bid_price", 0) or 0)
    ask = float(quote.get("ask_price", 0) or 0)
    last = float(quote.get("last_price", 0) or 0)
    side = side.upper()
    reference = ask if side == "BUY" else bid
    if reference <= 0:
        reference = last
    adjustment = 0.0
    if urgent and reference > 0:
        adjustment = max(
            0.0,
            float(campaign.get(
                "urgent_exit_price_improvement_pct",
                campaign.get("urgent_exit_chase_pct", 0.02),
            )),
        )
        if side == "SELL":
            reference *= max(0.0, 1 - adjustment)
        else:
            reference *= 1 + adjustment
    price = round(max(0.01, reference), 2) if reference > 0 else 0.0
    return price, {
        "bid_price": bid,
        "ask_price": ask,
        "last_price": last,
        "urgent_exit": urgent,
        "urgent_exit_price_improvement_pct": adjustment,
        "exit_limit_rule": (
            "紧急退出：卖出按买一主动降价，买回按卖一主动加价"
            if urgent else
            "普通退出：卖出按买一，买回按卖一"
        ),
    }


def _campaign_naked_short_rows(campaign: dict, positions: pd.DataFrame) -> pd.DataFrame:
    """Return only genuinely uncovered short legs for this campaign.

    A multi-leg option structure may intentionally contain a short leg.  The
    broker position table exposes that leg as ``qty < 0`` even when a matching
    long protective leg is present.  Naked-short takeover must therefore use
    the campaign structure plus live quantities, not a sign-only test.
    """
    legs = campaign.get("legs") or []
    if positions is None or positions.empty or "qty" not in positions.columns:
        return positions.iloc[0:0] if positions is not None else pd.DataFrame()
    normalized = positions.copy()
    normalized["_code"] = normalized["code"].astype(str).str.upper()
    normalized["_qty"] = pd.to_numeric(normalized["qty"], errors="coerce").fillna(0.0)
    parsed = {
        str(leg.get("code", "")).upper(): parse_option_code(str(leg.get("code", "")).upper())
        for leg in legs
    }
    short_codes = [
        code for code, contract in parsed.items()
        if contract is not None and any(
            str(leg.get("code", "")).upper() == code
            and str(leg.get("side", "")).upper() == "SELL"
            for leg in legs
        )
    ]
    long_rows = normalized[normalized["_qty"] > 0]
    uncovered: list[str] = []
    for code in short_codes:
        short_contract = parsed[code]
        short_qty = max(0.0, -float(normalized.loc[normalized["_code"] == code, "_qty"].sum()))
        if short_qty <= 0:
            continue
        protected_qty = 0.0
        for long_code, long_contract in parsed.items():
            if long_contract is None or long_code == code:
                continue
            leg_side = next(
                (str(leg.get("side", "")).upper() for leg in legs
                 if str(leg.get("code", "")).upper() == long_code),
                "",
            )
            if leg_side != "BUY" or not _protected_relation(long_contract, short_contract):
                continue
            protected_qty += max(0.0, float(long_rows.loc[long_rows["_code"] == long_code, "_qty"].sum()))
        if protected_qty + 1e-9 < short_qty:
            uncovered.append(code)
    return normalized[normalized["_code"].isin(uncovered)].drop(columns=["_code", "_qty"])


def _stop_limit_problems(campaign: dict) -> tuple[list[str], list[str]]:
    """校验 campaign 的单笔止损限额 —— 返回 ``(缺失键, 非法值)``。

    **开仓前强制校验**（P1-3 + Chief 10-23 裁定）：
    - 缺键 → 拒绝开仓（不得静默兜底）；
    - **值非法同样拒绝**：`ratio` 须 ∈ (0, 1]、`usd` 须 > 0。
      `0` 或负数会让 `volatility_hard_loss_exit_reason` 的
      `usd_limit > 0 and ...` 恒为假 → **止损永不触发**（静默失效）。

    抽成独立函数是为了**可直接测试**（内联在 `run_campaign` 里就只能端到端测），
    并与退出路径的 `_hard_stop_limits` 形成对照：
    **开仓拒绝、退出保守默认** —— 配置错误不该把退出管理一起锁死
    （那正是 `strategies_paused` 踩过的坑）。
    """
    missing = [
        key for key in ("max_position_loss_usd", "max_position_loss_ratio")
        if key not in campaign
    ]
    invalid: list[str] = []
    ratio_raw = campaign.get("max_position_loss_ratio")
    if "max_position_loss_ratio" in campaign:
        try:
            if not 0 < float(ratio_raw) <= 1:
                invalid.append(f"max_position_loss_ratio={ratio_raw!r}（须 ∈ (0, 1]）")
        except (TypeError, ValueError):
            invalid.append(f"max_position_loss_ratio={ratio_raw!r}（不可解析）")
    usd_raw = campaign.get("max_position_loss_usd")
    if "max_position_loss_usd" in campaign:
        try:
            if not float(usd_raw) > 0:
                invalid.append(f"max_position_loss_usd={usd_raw!r}（须 > 0）")
        except (TypeError, ValueError):
            invalid.append(f"max_position_loss_usd={usd_raw!r}（不可解析）")
    return missing, invalid


def _hard_stop_limits(campaign: dict, executor, code: str) -> dict:
    """解析单笔硬止损限额 —— **两层共用这一处实现**（Reviewer 保留意见）。

    为什么必须共享：硬止损纵深防御的**第 1 层**（`:740` 分支内）与**第 2 层**
    （`:1169` 分支后）在控制流上是两道独立检查，但**数据上是耦合的** ——
    它们读同一份 campaign 限额。**共享数据源就是一个共同失败点**：
    数据层出错会同时击败两层，"两道锁"在数据层其实只有一道。

    本函数把解析收敛到一处，并补上**数据层保护**：
    限额 `<= 0` 时 `volatility_hard_loss_exit_reason` 的
    `usd_limit > 0 and ...` 恒为假 → **止损永不触发**（静默失效）。
    这不是"用 0 做限额"，是配置错误 —— 必须**记 ERROR 并回落保守默认**。
    """
    usd_raw = campaign.get("max_position_loss_usd")
    ratio_raw = campaign.get("max_position_loss_ratio")
    usd = float(usd_raw) if usd_raw not in (None, "") else 300.0
    ratio = float(ratio_raw) if ratio_raw not in (None, "") else 0.25
    if usd <= 0 or ratio <= 0:
        # 配置错误：静默用 0 会让止损在两层同时失效（共同失败点）
        if executor is not None:
            detail = {
                "code": code,
                "max_position_loss_usd": usd_raw,
                "max_position_loss_ratio": ratio_raw,
                "fallback": {"max_position_loss_usd": 300.0, "max_position_loss_ratio": 0.25},
                "reason": "限额 <= 0 会让硬止损永不触发（两层同时失效）——已回落保守默认",
            }
            executor.store.operational_event(
                "volatility_long_gamma", "HARD_STOP_LIMIT_INVALID", "ERROR", detail,
            )
            # P1-1：这条 ERROR 此前**只落库、从不推送** —— 用户看不到，等于静默失效
            # （与 10-22 那条 P0 同一形态："库里有、用户看不到"）。
            # 它同时是"止损永不触发"的唯一线索：不落推送，下次开仓就还是非法限额。
            executor.alerts.send(
                "ERROR", "HARD_STOP_LIMIT_INVALID",
                f"{code}: 硬止损限额非法（ratio={ratio_raw!r} / usd={usd_raw!r}）"
                f"—— 已回落保守默认 0.25/300；不修正则硬止损永不触发，请修正 campaign 配置",
                incident_key="HARD_STOP_LIMIT_INVALID",
            )
        return {"max_loss_usd": 300.0, "max_loss_ratio": 0.25}
    return {"max_loss_usd": usd, "max_loss_ratio": ratio}


def hard_issues_block_action(hard_issues: list[str], quantity: int) -> bool:
    """硬诊断是否应当**阻断本次动作**。

    **只拦新开仓（`quantity <= 0`），不拦退出管理（`quantity > 0`）。**

    此前 `run_campaign` 在函数开头对 `hard_issues` 无条件 `return`，会在任何动作之前
    冻结**全部**动作 —— 包括已持仓的止盈/止损/平仓，结果是「诊断出问题」反而让风险
    敞口无人管理。判据刻意用**持仓量**而非诊断类别：退出路径与开仓路径的分界点就是
    「有没有仓」，用类别判断会随诊断项增减而失效。
    """
    return bool(hard_issues) and int(quantity) <= 0


_MISSING_TENANT_ALERTED = False
_INVALID_TOLERANCE_WARNED = False


def inactive_tolerance_cycles(campaign: dict) -> int:
    """A13：读取 `entry_confirmation_inactive_tolerance_cycles`（默认 1）。

    非法值（非整数 / 负数 / 解析失败）→ 按 **0**（旧严格口径）处理并 WARN 一次，
    **不许静默放大容忍度**（那会改变入场行为）。
    """
    raw = campaign.get("entry_confirmation_inactive_tolerance_cycles", 1)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        global _INVALID_TOLERANCE_WARNED
        if not _INVALID_TOLERANCE_WARNED:
            _INVALID_TOLERANCE_WARNED = True
            print(
                "[WARN] entry_confirmation_inactive_tolerance_cycles 非法"
                f"（{raw!r}）→ 按 0（旧严格口径）处理"
            )
        return 0
    return value


def entry_confirmation_applicable(quantity: int) -> bool:
    """A13/Reviewer：只有**空仓**才走入场确认 → 已持仓/已入场后不会重复触发开仓。"""
    return int(quantity or 0) <= 0


def _alert_missing_campaign_tenant(executor, strategy_id: str, code: str) -> None:
    """A3（2026-10-27）：campaign 与执行器都拿不到租户时，显式 ERROR 留痕（每进程一次）。

    `store.decision()` 对空租户本身会发 WARNING；这里再补一条 ERROR 级事件，
    使「决策审计无法归户」在告警面板可见 —— 不是静默写空租户。
    """
    global _MISSING_TENANT_ALERTED
    if _MISSING_TENANT_ALERTED:
        return
    _MISSING_TENANT_ALERTED = True
    try:
        executor.store.operational_event(
            strategy_id, "DECISION_MISSING_TENANT_ID", "ERROR",
            {
                "code": code,
                "reason": "campaign 与执行器均无 tenant_id，决策审计无法归户",
            },
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断交易主循环
        # silent-ok: 磁盘/DB 写失败时不能因此打断决策主流程（下一轮会再尝试留痕）
        pass


def update_signal_epoch_state(
    signal_state: dict, *, regime_active: bool, now: datetime,
    inactive_tolerance_cycles: int = 1, volume_ratio: float = 0.0,
) -> None:
    """A13（2026-10-27）：入场确认计时不再被**单轮** inactive 清零。

    背景：判定是瞬时的，涨跌幅/Z/ATR 在阈值边缘抖动时，任一轮 `active=False` 就会
    `pop("signal_started_at")` → 下一轮从 0 重新累计，临界波动永远达不到
    `entry_confirmation_minutes`（实测 AMD 累计到 2.2/2.5 分钟被一次 blip 打回 0）。

    规则：
    - 激活：**只要计时还在就延续**（blip 恢复不得当新 epoch、不得重置计时）；
      计时不存在才开新 epoch（epoch+1、记录 started_at、streak 归零）。
    - 未激活：保持 `signal_active=False`（当轮不进场），累加 `inactive_cycles`；
      `streak <= tolerance` 时**保留** `signal_started_at`/`signal_epoch`（计时不丢）；
      `streak > tolerance` 才按旧逻辑结束 epoch（记录 exit_signal_epoch、清 started_at）。
    """
    tolerance = max(0, int(inactive_tolerance_cycles))
    if regime_active:
        if not signal_state.get("signal_started_at"):
            signal_state["signal_epoch"] = int(signal_state.get("signal_epoch", 0) or 0) + 1
            signal_state["signal_started_at"] = now.isoformat()
            signal_state["signal_start_volume_ratio"] = float(volume_ratio or 0)
        signal_state["signal_active"] = True
        signal_state["inactive_cycles"] = 0
    else:
        streak = int(signal_state.get("inactive_cycles", 0) or 0) + 1
        signal_state["inactive_cycles"] = streak
        signal_state["signal_active"] = False
        if streak > tolerance:
            # 连续 inactive 超过容忍周期 → 真结束旧确认 epoch（保持旧行为）
            signal_state["exit_signal_epoch"] = signal_state.get("signal_epoch")
            signal_state.pop("signal_started_at", None)
            signal_state.pop("signal_start_volume_ratio", None)
            signal_state["regime_was_active"] = False
        # else：容忍期内 —— 计时保留，当轮仍按未激活处理（不进场）
    signal_state["regime_was_active"] = bool(regime_active)


def run_campaign(
    broker: FutuPaperBroker, executor: ExecutionManager, campaign: dict, state: dict,
    strategy_id: str = "earnings_gamma", positions_snapshot=None, orders_snapshot=None,
) -> None:
    code = str(campaign.get("code", ""))
    _campaign_tenant = str(campaign.get("tenant_id") or "").strip()
    if not _campaign_tenant:
        # A3（2026-10-27）：生产 `VOLATILITY_CAMPAIGNS` 存量 campaign 可能缺 tenant_id
        # （当时 8 条全缺），决策日志会写空租户 → 热路径告警刷屏 + 审计无法归户。
        # 口径：**执行器所属租户优先**（由 broker 账户 + 租户注册表解析，权威来源）。
        _campaign_tenant = str(
            getattr(getattr(executor, "tenant", None), "tenant_id", "") or ""
        ).strip()
    if not _campaign_tenant:
        # 仍无租户上下文：显式 ERROR 留痕（不静默）。生产服务路径不会走到这里。
        _alert_missing_campaign_tenant(executor, strategy_id, code)
    legs = campaign.get("legs") or [{"code": code, "side": "BUY", "ratio": 1}]
    valid_leg_codes = [
        str(leg.get("code", "")).upper()
        for leg in legs if parse_option_code(str(leg.get("code", "")).upper())
    ]
    if parse_option_code(code) is None and valid_leg_codes:
        code = next(
            (
                str(leg.get("code", "")).upper()
                for leg in legs
                if str(leg.get("side", "")).upper() == "BUY"
                and parse_option_code(str(leg.get("code", "")).upper())
            ),
            valid_leg_codes[0],
        )
        campaign = {**campaign, "code": code}
    issues = diagnose_campaign(campaign, executor.store.earnings_events())
    hard_issues = hard_campaign_issues(campaign, issues)
    # 硬诊断的处置**延后**到「持仓量已解析」之后（见下方 `hard_issues and quantity <= 0`）：
    # 此前这里无条件 `return`，会在任何动作之前冻结**全部**动作（含止盈/止损/平仓），
    # 结果是「诊断出问题」反而让风险敞口无人管理。本函数被
    # `scripts/run_volatility_strategy.py:284` 直接调用 —— 现在在跑的波动率持仓走的就是这道门。
    hard_issue_reason = "；".join(hard_issues)
    now = datetime.now(NY)
    # #1 键级分区：状态键写形态收敛到**共享助手** —— 四个读写点必须用同一个拼法。
    # 此前这里手拼 `{strategy_id}:{code}`（无租户），而 order_push 写带租户形态：
    # 同一份状态裂成两个键、读侧各看各的 → 成交回写的 peak / entry_iv / position_opened_at
    # 策略侧读不到 → **止盈止损基准错**。
    _state_tenant = tenant_id_of_broker(broker) or ""
    state_key = volatility_write_state_key(code, _state_tenant, prefix=f"{strategy_id}:")
    if state_key not in state:
        # 读侧**兼容旧格式**（迁移前的无租户键，以及更早的裸 code 键）：
        # 读到后**就地收敛成写形态**。**只搬不复制** —— 两份键并存会让读侧读到旧值。
        for _old_key in (f"{strategy_id}:{code}", code):
            if _old_key != state_key and _old_key in state:
                state[state_key] = state.pop(_old_key)
                break
    campaign_state = state.setdefault(state_key, {"peak": 0.0})
    trigger_mode = campaign.get("trigger_mode", "EARNINGS")
    # Entry confirmation belongs to the underlying volatility episode, not to
    # whichever option expression happens to be the best candidate this cycle.
    # Candidate rolls are expected during an active signal and must not reset a
    # confirmation timer from (for example) MSFT C505 to MSFT C520.
    signal_state = campaign_state
    if trigger_mode == "VOLATILITY":
        # A17（2026-10-27）：旧无租户信号键被接管时留一条审计 —— 必须在
        # `volatility_signal_state()` 之前判（它内部会把旧键 pop 掉）。
        audit_legacy_signal_state_takeover(
            executor.store, state, strategy_id,
            campaign.get("underlying", ""), _campaign_tenant,
        )
        signal_state = volatility_signal_state(
            state, campaign_state, strategy_id, campaign.get("underlying", ""), code,
            # 2026-10-27：信号键必须带租户段（实盘/模拟同标的不得互相推进确认计时）
            tenant_id=_campaign_tenant,
        )
        campaign_state["active_contract_code"] = code
        campaign_state.setdefault("exit_signal_epoch", None)
        campaign_state.setdefault("signal_epoch", 0)
    if issues:
        executor.store.audit("GAMMA_VALIDATION_WARNING", {
            "code": code, "trigger_mode": trigger_mode, "warnings": issues,
        }, strategy_id, actor="service:gamma")
    if trigger_mode == "VOLATILITY":
        signal_started_at = signal_state.get("signal_started_at")
        event_time = (
            safe_datetime(signal_started_at, now) if signal_started_at else now
        ) + timedelta(
            hours=int(campaign.get("entry_hours", 6))
        )
    else:
        event_clock = time(8, 0) if campaign["event_session"] == "盘前" else time(16, 5)
        event_time = datetime.combine(
            safe_datetime(campaign.get("event_date"), now).date(), event_clock, tzinfo=NY
        )
    positions = positions_snapshot if positions_snapshot is not None else broker.positions()
    # Hard safety stop: a live uncovered short option is never left to normal
    # strategy logic. Multi-leg structures are checked by live ratio/protection,
    # so a protected backspread is not misclassified as naked.
    naked_rows = _campaign_naked_short_rows(campaign, positions)
    if not naked_rows.empty and trigger_mode == "VOLATILITY":
        executor.store.audit("VOLATILITY_NAKED_SHORT_BLOCK", {
            "underlying": campaign.get("underlying"),
            "symbols": naked_rows["code"].astype(str).tolist(),
            "quantities": pd.to_numeric(naked_rows["qty"], errors="coerce").tolist(),
        }, strategy_id, actor="service:gamma")
        executor.store.decision_if_changed(
            strategy_id, code, "BLOCKED", "检测到期权裸空残腿，禁止新开仓并继续执行风险接管",
            {"naked_short": naked_rows["code"].astype(str).tolist(), "reduce_only_takeover": True},
            repeat_after_seconds=300,
            tenant_id=_campaign_tenant,
        )
        # Do not fall through to a new entry; the combo/orphan monitor owns the
        # reduce-only BUY/SELL recovery path and must continue operating.
        return
    position = positions[positions["code"] == code] if not positions.empty else positions
    quantity = int(position.iloc[0]["qty"]) if not position.empty else 0
    cost = float(position.iloc[0]["cost_price"]) if not position.empty else 0.0
    # P1-6：读侧走单源（逐策略租户键 → 旧全局键 → 全局模式），
    # 不再自己拼 `strategy_execution_mode:{strategy_id}` —— 那样会漏读租户键。
    strategy_mode = resolve_configured_execution_mode(
        executor.store, _campaign_tenant, strategy_id,
    ) or "PAPER"
    campaign_mode = str(campaign.get("execution_mode_override") or "").strip()
    execution_mode = (campaign_mode or strategy_mode).upper()
    combo_last = 0.0
    combo_id = automatic_combo_id(
        strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
    ) if len(legs) > 1 else ""
    combo = executor.store.option_combo(combo_id) if combo_id else None
    if len(legs) > 1 and combo is None:
        recovered = find_open_combo_for_campaign(
            executor.store,
            strategy_id=strategy_id,
            underlying=str(campaign.get("underlying", "")),
            structure=str(campaign.get("structure", "")),
            leg_symbols=set(valid_leg_codes),
        )
        if recovered:
            combo_id = recovered["combo_id"]
            combo = recovered
            executor.store.audit("VOLATILITY_COMBO_RECOVERED", {
                "combo_id": combo_id, "campaign_code": code,
                "underlying": campaign.get("underlying"),
                "structure": campaign.get("structure"),
                "leg_symbols": valid_leg_codes,
            }, strategy_id, actor="service:gamma")
    if combo and combo.get("status") in {"EXECUTING", "CLOSING", "ROLLBACK_PENDING"}:
        ComboExecutionManager(broker, executor.store, executor).advance(combo_id)
        combo = executor.store.option_combo(combo_id)
        executor.store.decision_if_changed(
            strategy_id, code, "HOLD",
            f"组合订单正在执行：{combo.get('status', 'UNKNOWN')}",
            {"combo_id": combo_id, "structure": campaign.get("structure")},
            repeat_after_seconds=300,
            tenant_id=_campaign_tenant,
        )
        return
    if combo and combo.get("status") == "OPEN":
        quantity, cost, combo_last = combo_position_mark(
            executor.store, broker, combo_id
        )
    if execution_mode == "SHADOW":
        shadow_quantity, shadow_cost = executor.store.shadow_position(strategy_id, code)
        quantity, cost = int(shadow_quantity), float(shadow_cost)
    # 硬诊断**只拦新开仓，不拦退出管理**（止盈/止损/平仓/在途组合推进）。
    # 判据：`quantity > 0` = 有持仓 = 走退出管理路径，必须放行。
    # 放在这里而不是函数开头，是因为**此刻 `quantity` 才是权威值**
    # （已包含 SHADOW 虚拟持仓与 OPEN combo 的 mark）。
    if hard_issues_block_action(hard_issues, quantity):
        executor.store.decision(strategy_id, code, "BLOCKED", hard_issue_reason, tenant_id=_campaign_tenant)
        print(f"{code}: BLOCKED {hard_issue_reason}")
        return
    if trigger_mode == "VOLATILITY" and quantity <= 0:
        pending_exit_reason = str(campaign_state.get("pending_exit_reason", ""))
        cycle_transition = sync_volatility_position_cycle(
            campaign_state, quantity=0, cost=0, last=0, current_iv=0,
            current_efficiency=0, now=now,
        )
        if cycle_transition == "CLOSED":
            reason = pending_exit_reason or "波动率仓位已完成退出"
            campaign_state["entry_cycle"] = int(campaign_state.get("entry_cycle", 0)) + 1
            campaign_state["last_exit_at"] = now.isoformat()
            state[f"volatility_cooldown:{campaign['underlying']}"] = now.isoformat()
            state[f"volatility_cooldown_reason:{campaign['underlying']}"] = reason
            state[f"volatility_cooldown_hours:{campaign['underlying']}"] = (
                volatility_cooldown_hours(
                    reason, float(campaign.get("cooldown_hours", 24)),
                    {
                        "take_profit": float(campaign.get("cooldown_take_profit_hours", 4)),
                        "profit_protection": float(campaign.get("cooldown_profit_protection_hours", 8)),
                        "stop_loss": float(campaign.get("cooldown_stop_loss_hours", 24)),
                        "expiry": float(campaign.get("cooldown_expiry_hours", 0)),
                        "decay": float(campaign.get("cooldown_decay_hours", 8)),
                        "contraction": float(campaign.get("cooldown_contraction_hours", 6)),
                    },
                )
            )
    campaign_prefix = f"{campaign.get('event_date', '')}:{campaign.get('campaign_id', code)}:"
    if trigger_mode == "VOLATILITY":
        # A volatility campaign is reusable: a completed exit starts a cooldown,
        # whereas an earnings campaign represents a one-off event.
        campaign_state.pop("closed", None)
    elif quantity <= 0 and executor.store.strategy_has_completed_exit(
        strategy_id, code, campaign_prefix,
    ):
        campaign_state["closed"] = True
    if campaign_state.get("closed"):
        executor.store.decision(
            strategy_id, code, "CLOSED", "该策略活动已完成退出，不再重新开仓",
            {"position_quantity": 0, "planned_order_quantity": 0,
             "position_source": "SHADOW_LEDGER" if execution_mode == "SHADOW" else "FUTU_ACCOUNT",
             "trigger_mode": trigger_mode, "validation_warnings": issues},
            tenant_id=_campaign_tenant,
        )
        print(f"{code}: CLOSED (campaign exit completed)")
        return
    orders = orders_snapshot if orders_snapshot is not None else broker.orders()
    pending_codes = {code.upper(), *valid_leg_codes}
    combo_state = None
    if trigger_mode == "VOLATILITY" and len(valid_leg_codes) > 1:
        combo_state = find_open_combo_for_campaign(
            executor.store, strategy_id=strategy_id,
            underlying=str(campaign.get("underlying", "")),
            structure=str(campaign.get("structure", "")),
            leg_symbols=set(valid_leg_codes),
        )
        if combo_state and combo_state.get("status") in {
            "EXECUTING", "CLOSING", "ROLLBACK_PENDING", "IMBALANCED",
        }:
            executor.store.decision_if_changed(
                strategy_id, code, "HOLD",
                f"组合仍处于{combo_state.get('status')}，禁止新的开仓或重复平仓",
                {"combo_id": combo_state.get("combo_id"), "exit_state": combo_state.get("status"),
                 "leg_symbols": sorted(pending_codes)}, repeat_after_seconds=300,
                tenant_id=_campaign_tenant,
            )
            return
    pending = (
        orders[orders["code"].astype(str).str.upper().isin(pending_codes)
               & orders["order_status"].map(broker.is_cancellable_status)]
        if not orders.empty else orders
    )
    if not pending.empty:
        print(f"{code}: HOLD pending order {pending.iloc[0]['order_id']}")
        return

    if pending.empty and quantity > 0 and campaign_state.get("pending_exit_episode"):
        campaign_state["pending_exit_episode"] = False

    market_session = MARKET_CALENDAR.session(now)
    if not market_session.regular_open(now):
        reason = (
            "当前期权市场未开盘，旧期权报价仅用于参考；"
            "等待美股常规交易时段后重新校验并执行"
        )
        executor.store.decision_if_changed(
            strategy_id, code, "HOLD", reason, {
                "market_session": "OPTION_CLOSED",
                "option_quote_time": "",
            }, repeat_after_seconds=3600,
            tenant_id=_campaign_tenant,
        )
        print(f"{code}: HOLD ({reason})")
        return

    expired, expiry_reason = campaign_expired(campaign, now.date())
    if expired and quantity <= 0:
        reason = f"活动合约已全部过期且无持仓（{expiry_reason}），自动归档并不再发起开仓行情轮询"
        executor.store.decision_if_changed(
            strategy_id, code, "CLOSED", reason, {
                "campaign_expired": True,
                "expiry_reason": expiry_reason,
                "position_quantity": 0,
            }, repeat_after_seconds=86400,
            tenant_id=_campaign_tenant,
        )
        print(f"{code}: CLOSED ({reason})")
        return

    quality = resolve_candidate_quality_for_campaign(executor.store, campaign)
    max_quote_spread_pct = float(campaign.get(
        "max_spread_pct", 12.0 if trigger_mode == "VOLATILITY" else 20.0
    ))
    # 跨轮次盘口变化年龄（2026-10-27）：同 attempt 1.5s 比不出来，从采集侧取。
    # blocked=True（无历史行 / 观测超 900s / 时钟回拨）→ 入场 fail-closed。
    book_active_seconds, book_basis_blocked = latest_book_activity_age(
        executor.store, code, now,
    )
    option, owner, quote_check = refreshed_trade_quotes(
        broker, code, campaign["underlying"], now, max_quote_spread_pct,
        # 降级上限与候选层**同源**（P0-5 §5.1：900 → 300）。
        # **价差来源保持现状**：把这里改成 `quality.entry_max_spread_pct`（默认 20）
        # 会把 VOLATILITY 的 12% 放宽到 20% —— 那是**放松真金入场门禁**，
        # 不在本批授权范围内，故只接管停滞上限。
        maximum_stale_seconds=quality.entry_max_stale_seconds,
        book_active_seconds=book_active_seconds,
        book_basis_blocked=book_basis_blocked,
    )
    # `quote_age_at_submit` 埋点（P0-5 §5.1）：门禁**通过**（含降级通过）时记录当次年龄。
    # 300s 是"先上线、带埋点、一周后按数据复审" —— **不许再用"影响几乎为零"这类断言**，
    # P95 逼近 300s 就说明拦单过多，回退 600s。
    if quote_check["option_fresh"] and quote_check["underlying_fresh"] and quote_check["book_ok"]:
        try:
            executor.store.record_candidate_exclusions([{
                "tenant_id": str(campaign.get("tenant_id") or ""),
                "strategy_id": strategy_id,
                "underlying": campaign["underlying"],
                "code": code,
                "reason": "ADMITTED",
                "age_seconds": float(quote_check["option_age_seconds"]),
                "spread_pct": float(quote_check["spread_pct"]),
                "volume": None,
                "phase": "ADMITTED",
            }])
        except Exception:  # noqa: BLE001 - 埋点失败绝不能阻断下单
            pass  # silent-ok: 排除留痕是观测动作，失败不得阻断下单
    # A quote between 180s and 900s old is explicitly marked degraded by the
    # data-quality layer. Permit a small, liquid entry in that window; fully
    # stale or unusable-book quotes remain blocked.
    exit_book_bypass = False
    exit_bypass_reason = ""
    if quantity > 0:
        from futu_quant.data_quality import option_quote_freshness_for_exit
        max_spread = float(getattr(getattr(broker, "settings", None), "option_exit_max_spread_pct", 20.0))
        anchor_pct = float(getattr(getattr(broker, "settings", None), "option_exit_price_deviation_anchor_pct", 0.40))
        exit_book_bypass, exit_bypass_reason, _ = option_quote_freshness_for_exit(
            option, now, maximum_spread_pct=max_spread, price_deviation_anchor_pct=anchor_pct,
        )

    quote_stale = (
        not quote_check["underlying_fresh"]
        or (not quote_check["option_fresh"] and not quote_check["option_degraded"])
    )
    entry_book_blocked = not quote_check["book_ok"] and quantity <= 0
    entry_data_blocked = quote_stale and quantity <= 0
    exit_data_blocked = quantity > 0 and (not quote_check["underlying_fresh"] or (not quote_check["option_fresh"] and not exit_book_bypass))
    if entry_data_blocked or exit_data_blocked or entry_book_blocked:
        reason = (
            "行情/盘口检查未通过："
            f"正股 {quote_check['underlying_reason']}；"
            f"期权 {quote_check['option_reason']}；"
            f"{quote_check['book_reason']}；已重试 {len(quote_check['attempts'])} 次"
        )
        decision = "HOLD" if quantity > 0 else "BLOCKED"
        payload = {
            "market_data_status": "WAITING_EXIT_LIQUIDITY" if quantity > 0 else "PRE_TRADE_BLOCKED",
            "underlying_quote_age_seconds": quote_check["underlying_age_seconds"],
            "option_quote_age_seconds": quote_check["option_age_seconds"],
            "book_ok": quote_check["book_ok"],
            "book_reason": quote_check["book_reason"],
            "spread_pct": quote_check["spread_pct"],
            "quote_attempts": quote_check["attempts"],
            "position_quantity": quantity,
            "planned_order_quantity": 0,
            "trigger_mode": trigger_mode,
        }
        if quantity <= 0:
            should_record = executor.store.decision_if_changed(
                strategy_id, code, decision, reason, payload, repeat_after_seconds=900,
                tenant_id=_campaign_tenant,
            )
        else:
            executor.store.decision(strategy_id, code, decision, reason, payload, tenant_id=_campaign_tenant)
            should_record = True
        if should_record:
            executor.store.audit(
                "OPTION_MARKET_DATA_WAITING" if quantity > 0 else "OPTION_MARKET_DATA_BLOCKED",
                {
                    "symbol": code, "underlying": campaign["underlying"],
                    "strategy_id": strategy_id, "reason": reason,
                    "attempts": quote_check["attempts"],
                    "position_quantity": quantity,
                },
                strategy_id,
             actor="service:gamma")
        print(f"{code}: {decision} ({reason})")
        return
    if quantity > 0 and not quote_check["option_fresh"]:
        if exit_book_bypass:
            executor.store.audit("OPTION_EXIT_HEALTHY_BOOK_BYPASS_ACCEPTED", {
                "strategy_id": strategy_id, "symbol": code,
                "option_quote_age_seconds": quote_check["option_age_seconds"],
                "reason": exit_bypass_reason,
                "position_quantity": quantity,
            }, strategy_id, actor="service:gamma")
            executor.alerts.send(
                "WARNING", "OPTION_EXIT_STALE_QUOTE_BYPASS",
                f"期权 {code} 行情成交滞后但双边盘口健康，已放行平仓退出：{exit_bypass_reason}",
                incident_key=f"option-stale-exit-bypass:{code}",
            )
        executor.store.audit("OPTION_EXIT_STALE_QUOTE_ACCEPTED", {
            "strategy_id": strategy_id, "symbol": code,
            "option_quote_age_seconds": quote_check["option_age_seconds"],
            "reason": quote_check["option_reason"],
            "book_reason": quote_check["book_reason"],
            "position_quantity": quantity,
        }, strategy_id, actor="service:gamma")
    if quantity > 0 and not quote_check["book_ok"]:
        executor.store.audit("OPTION_EXIT_BOOK_DEGRADED", {
            "strategy_id": strategy_id, "symbol": code,
            "reason": quote_check["book_reason"],
            "spread_pct": quote_check["spread_pct"],
            "position_quantity": quantity,
        }, strategy_id, actor="service:gamma")
    if quote_check["option_degraded"]:
        executor.store.audit("OPTION_QUOTE_DEGRADED_ACCEPTED", {
            "strategy_id": strategy_id, "symbol": code,
            "option_quote_age_seconds": quote_check["option_age_seconds"],
            "reason": quote_check["option_reason"],
        }, strategy_id, actor="service:gamma")
    last = float(option["last_price"] or 0)
    bid, ask = float(option["bid_price"] or 0), float(option["ask_price"] or 0)
    gamma_now = abs(market_number(option.get("option_gamma", 0)))
    theta_now = abs(market_number(option.get("option_theta", 0)))
    current_efficiency = gamma_now / theta_now if theta_now > 0 else 0.0
    current_iv = normalize_iv(option.get("option_implied_volatility", 0))
    if combo and combo.get("status") == "OPEN" and combo_last > 0:
        last = combo_last
    executable_mark = (
        last if combo and combo.get("status") == "OPEN"
        else bid if quantity > 0 and bid > 0
        else last
    )
    previous_peak_mark = float(campaign_state.get("peak", 0) or 0)
    if trigger_mode == "VOLATILITY":
        sync_volatility_position_cycle(
            campaign_state, quantity=quantity, cost=cost, last=executable_mark,
            current_iv=current_iv, current_efficiency=current_efficiency, now=now,
            allocation_policy=str(campaign.get("exit_allocation_policy", "FIFO")),
        )
    else:
        campaign_state["peak"] = max(float(campaign_state.get("peak", 0)), executable_mark)
    pnl_ratio = executable_mark / cost - 1 if quantity > 0 and cost > 0 else 0.0
    position_multiplier = market_number(option.get("option_contract_multiplier"), 100.0)
    position_pnl_amount = (
        (executable_mark - cost) * quantity * position_multiplier
        if quantity > 0 and cost > 0 else 0.0
    )
    peak = float(campaign_state.get("peak", 0))
    if peak > previous_peak_mark and quantity > 0:
        campaign_state["peak_reached_at"] = now.isoformat()
    drawdown = 1 - executable_mark / peak if peak > 0 else 0.0
    peak_pnl_ratio = (
        peak / cost - 1
        if quantity > 0 and cost > 0 else 0.0
    )
    profit_giveback = (
        max(0.0, (peak_pnl_ratio - pnl_ratio) / peak_pnl_ratio)
        if peak_pnl_ratio > 0 else 0.0
    )
    profit_capture_ratio = volatility_profit_capture_ratio(pnl_ratio, peak_pnl_ratio)
    tier_lock_floor, tier_lock_label = volatility_profit_lock_floor(peak_pnl_ratio)
    underlying_bars = broker.candles(campaign["underlying"], count=90)
    # 硬止损必须绕过退出冷却：冷却分支一旦命中就 `return`，会挡住后面的硬止损判定，
    # 导致「亏损已到线却要等下一次机会」。10-22 实盘 META 看跌就是这样从 $300 线
    # 一路跑到 $366 才被动平仓 —— 冷却本意是「止损后 24h 内不再开新仓」，
    # 不该把「已持仓的退出」一起挡住。
    #
    # 硬止损必须绕过退出冷却：冷却分支一旦命中就 `return`，会挡住后面的硬止损判定，
    # 导致「亏损已到线却要等下一次机会」。10-22 实盘 META 看跌就是这样从 $300 线
    # 一路跑到 $366 才被动平仓 —— 冷却本意是「止损后 24h 内不再开新仓」，
    # 不该把「已持仓的退出」一起挡住。
    #
    # 做法：提前判定硬止损并置 `action="SELL"`（`action` 在此处尚未赋值，必须初始化），
    # 命中时打标记 `hard_stop_fired`，让后面的常规决策不覆盖它；退出由**既有路径**统一提交。
    action, reason = "HOLD", "初始：未触发任何退出条件"
    hard_stop_fired = False
    if trigger_mode == "VOLATILITY" and quantity > 0:
        _hard_reason = volatility_hard_loss_exit_reason(
            pnl_ratio=pnl_ratio,
            pnl_amount=position_pnl_amount,
            **_hard_stop_limits(campaign, executor, code),
        )
        if _hard_reason:
            action, reason = "SELL", _hard_reason
            hard_stop_fired = True
            executor.store.decision(
                strategy_id, code, "SELL", reason,
                {"position_quantity": quantity, "position_pnl_amount": position_pnl_amount,
                 "pnl_ratio": pnl_ratio, "hard_stop_bypasses_cooldown": True,
                 "trigger_mode": trigger_mode},
                tenant_id=_campaign_tenant,
            )
            print(f"{code}: SELL ({reason}) [硬止损绕过冷却]")
    regime = None
    if trigger_mode == "VOLATILITY":
        adaptive = volatility_threshold_profile(underlying_bars)
        effective_z = min(
            float(campaign.get("entry_zscore", 2.0)), adaptive["entry_zscore"]
        )
        effective_move = min(
            float(campaign.get("minimum_underlying_move", .02)),
            adaptive["minimum_underlying_move"],
        )
        regime = volatility_regime(
            owner, underlying_bars, effective_z, effective_move,
        )
        regime["adaptive_profile"] = adaptive
        # A13（2026-10-27）：单轮 inactive 不再清零确认计时；连续 inactive 超过
        # `entry_confirmation_inactive_tolerance_cycles`（默认 1，即连续 2 轮）才结束 epoch。
        update_signal_epoch_state(
            signal_state,
            regime_active=bool(regime["active"]),
            now=now,
            inactive_tolerance_cycles=inactive_tolerance_cycles(campaign),
            volume_ratio=float(regime.get("volume_ratio", 0) or 0),
        )
        signal_started_at = signal_state.get("signal_started_at")
        event_time = (
            safe_datetime(signal_started_at, now) if signal_started_at else now
        ) + timedelta(hours=int(campaign.get("entry_hours", 6)))
    nondirectional = campaign.get("structure") in {"LONG_STRADDLE", "LONG_STRANGLE"}
    if nondirectional:
        direction_ok, direction_reason, direction_metrics = True, "非方向性结构，无需强制判断涨跌方向", {}
    elif trigger_mode == "VOLATILITY":
        direction_ok, direction_reason, direction_metrics = configured_volatility_direction_allowed(
            owner, underlying_bars, campaign["option_type"], regime,
        )
    else:
        direction_ok, direction_reason, direction_metrics = configured_direction_allowed(
            owner, underlying_bars, campaign["option_type"], trigger_mode,
            float(campaign.get("minimum_underlying_move", 0.020 if trigger_mode == "VOLATILITY" else 0.003)),
        )
    if trigger_mode == "VOLATILITY":
        direction_ok = bool(direction_ok and signal_state.get("signal_active", False))
        direction_reason = f"{direction_reason}；动态状态：{regime['reason']}"
        if quantity <= 0 and campaign.get("optimizer_version") != 2:
            reason = "旧版波动率活动尚未完成动态状态和合约质量评分，请重新生成候选"
            executor.store.decision(strategy_id, code, "HOLD", reason, {"regime": regime}, tenant_id=_campaign_tenant)
            print(f"{code}: HOLD ({reason})")
            return
        if entry_confirmation_applicable(quantity):
            entry_confirmed, confirmation_reason = volatility_entry_confirmation(
                now=now,
                signal_started_at=signal_state.get("signal_started_at"),
                regime=regime,
                confirmation_minutes=float(
                    campaign.get("entry_confirmation_minutes", 5)
                ),
                signal_start_volume_ratio=signal_state.get(
                    "signal_start_volume_ratio"
                ),
                minimum_volume_ratio=float(
                    campaign.get("entry_confirmation_min_volume_ratio", 0.80)
                ),
                minimum_volume_retention=float(
                    campaign.get("entry_confirmation_min_volume_retention", 0.65)
                ),
                require_active=bool(
                    campaign.get("entry_confirmation_require_active", True)
                ),
            )
            direction_reason = f"{direction_reason}；{confirmation_reason}"
            if not entry_confirmed:
                executor.store.decision_if_changed(
                    strategy_id,
                    code,
                    "HOLD",
                    confirmation_reason,
                    {
                        "position_quantity": quantity,
                        "planned_order_quantity": 0,
                        "position_source": (
                            "SHADOW_LEDGER" if execution_mode == "SHADOW"
                            else "FUTU_ACCOUNT"
                        ),
                        "pnl_ratio": pnl_ratio,
                        "drawdown": drawdown,
                        "trigger_mode": trigger_mode,
                        "validation_warnings": issues,
                        "direction_reason": direction_reason,
                        "direction_metrics": direction_metrics,
                        "volatility_regime": regime,
                        "signal_started_at": signal_state.get("signal_started_at"),
                        "signal_start_volume_ratio": signal_state.get(
                            "signal_start_volume_ratio"
                        ),
                    },
                    repeat_after_seconds=300,
                    tenant_id=_campaign_tenant,
                )
                print(f"{code}: HOLD ({confirmation_reason})")
                return
        cooldown_key = f"volatility_cooldown:{campaign['underlying']}"
        last_exit = state.get(cooldown_key)
        if quantity <= 0 and last_exit:
            cooldown_duration = float(state.get(
                f"volatility_cooldown_hours:{campaign['underlying']}",
                campaign.get("cooldown_hours", 24),
            ))
            remaining = cooldown_duration - (
                now - safe_datetime(last_exit, now - timedelta(hours=cooldown_duration + 1))
            ).total_seconds() / 3600
            epoch_allowed, epoch_reason = volatility_cooldown_reentry_allowed(
                remaining_hours=remaining,
                last_exit_epoch=signal_state.get("exit_signal_epoch"),
                current_epoch=signal_state.get("signal_epoch"),
            )
            if not epoch_allowed:
                reason = f"同一信号周期禁止重入：{epoch_reason}"
                executor.store.decision_if_changed(
                    strategy_id, code, "HOLD", reason,
                    {"exit_state": "COOLDOWN", "signal_epoch": signal_state.get("signal_epoch"),
                     "exit_signal_epoch": signal_state.get("exit_signal_epoch")},
                    repeat_after_seconds=300,
                    tenant_id=_campaign_tenant,
                )
                print(f"{code}: HOLD ({reason})")
                return
            if remaining > 0:
                cooldown_reason = str(state.get(
                    f"volatility_cooldown_reason:{campaign['underlying']}", "未记录退出原因",
                ))
                bypass_allowed, bypass_reason = volatility_cooldown_bypass_allowed(
                    cooldown_reason, regime,
                    enabled=bool(campaign.get("cooldown_bypass_enabled", True)),
                    allow_stop_loss=bool(campaign.get("cooldown_bypass_stop_loss", False)),
                    min_zscore=float(campaign.get("cooldown_bypass_min_zscore", 3.0)),
                    min_intraday_move=float(campaign.get("cooldown_bypass_min_intraday_move", 0.035)),
                    min_atr_ratio=float(campaign.get("cooldown_bypass_min_atr_ratio", 2.0)),
                    min_volume_ratio=float(campaign.get("cooldown_bypass_min_volume_ratio", 1.5)),
                    signal_tier=volatility_entry_tier(regime),
                )
                if bypass_allowed:
                    for key in (
                        cooldown_key,
                        f"volatility_cooldown_hours:{campaign['underlying']}",
                        f"volatility_cooldown_reason:{campaign['underlying']}",
                    ):
                        state.pop(key, None)
                    executor.store.decision(strategy_id, code, "COOLDOWN_BYPASS", bypass_reason, {
                        "regime": regime, "cooldown_reason": cooldown_reason,
                        "remaining_cooldown_hours": remaining,
                    }, tenant_id=_campaign_tenant)
                    print(f"{code}: COOLDOWN_BYPASS ({bypass_reason})")
                else:
                    reason = f"同一标的退出冷却中，剩余约 {remaining:.1f} 小时；{bypass_reason}"
                    executor.store.decision(strategy_id, code, "HOLD", reason, {
                        "regime": regime, "cooldown_reason": cooldown_reason,
                        "remaining_cooldown_hours": remaining,
                    }, tenant_id=_campaign_tenant)
                    print(f"{code}: HOLD ({reason})")
                    return
    if quantity <= 0 and trigger_mode == "EARNINGS" and (
        campaign.get("optimizer_version") != 1 or not campaign.get("trade_allowed", False)
    ):
        reason = (
            "旧版财报活动尚未完成机会评分，请重新生成候选"
            if campaign.get("optimizer_version") != 1
            else "财报机会评分未达到交易门槛，自动放弃本次交易"
        )
        executor.store.decision(strategy_id, code, "HOLD", reason, {
            "opportunity_score": campaign.get("opportunity_score", 0),
            "minimum_score": campaign.get("minimum_score", 60),
            "expected_move_edge": campaign.get("expected_move_edge", 0),
            "score_reason": campaign.get("score_reason", ""),
            "optimizer_version": campaign.get("optimizer_version", 0),
        }, tenant_id=_campaign_tenant)
        print(f"{code}: HOLD ({reason})")
        return
    per_stage = int(campaign["contracts_per_stage"])
    size_factor = 1.0
    size_reason = ""
    effective_max_stages = int(campaign["max_stages"])
    if trigger_mode == "VOLATILITY":
        # 外援灰度试水仓位控制: 新晋外援在首秀阶段 (实盘交易为0笔或尚未盈利) 强制使用 1 张探路仓
        underlying_sym = str(campaign.get("underlying", "")).upper()
        promoted_record = None
        try:
            with executor.store.connection() as conn:
                row = conn.execute(
                    "SELECT live_trades_count, live_realized_pnl FROM promoted_universe WHERE symbol=? AND status='ACTIVE'",
                    (underlying_sym,),
                ).fetchone()
                if row:
                    promoted_record = dict(row)
        except Exception:  # silent-ok: 提升池记录仅用于展示，读取失败不阻断调度
            pass

        if promoted_record is not None:
            trades = int(promoted_record.get("live_trades_count", 0))
            pnl = float(promoted_record.get("live_realized_pnl", 0.0))
            if trades == 0 or pnl <= 0:
                per_stage = 1
                effective_max_stages = 1
                size_reason = f"新晋外援标的 {underlying_sym} 处于灰度试水期（实盘{trades}笔，盈利${pnl:.2f}），限制 1 张探路仓"

        size_factor, size_reason_factor = volatility_direction_size_factor(regime)
        if size_factor < 1:
            per_stage = max(1, int(per_stage * size_factor))
            effective_max_stages = min(effective_max_stages, 1)
            size_reason = size_reason or size_reason_factor
        # Mid-quality expressions may enter only as a single-contract probe;
        # high-quality expressions retain the configured staged sizing.
        quality_score = market_number(campaign.get("volatility_quality_score"), 0.0)
        quality_tier = str(campaign.get("adaptive_risk_tier", campaign.get("entry_tier", "")))
        reduced_size_floor = 62.0 if "动态" in quality_tier and "强" not in quality_tier else 60.0
        if reduced_size_floor <= quality_score < 70.0:
            per_stage = 1
            effective_max_stages = 1
            size_reason = (
                f"质量分 {quality_score:.1f} 处于 {reduced_size_floor:.0f}–69.9，"
                "仅允许单张试探仓位"
            )
    stages_filled = quantity // max(1, per_stage)
    if trigger_mode == "VOLATILITY":
        decay_reason = None
        if quantity > 0:
            if regime.get("contraction") and not regime.get("active", False):
                campaign_state["contraction_confirmations"] = int(
                    campaign_state.get("contraction_confirmations", 0)
                ) + 1
            else:
                campaign_state["contraction_confirmations"] = 0
            regime["contraction_confirmations"] = campaign_state[
                "contraction_confirmations"
            ]
            adverse_signal = bool(
                not direction_ok
                or (not regime.get("active", False) and pnl_ratio < 0)
            )
            campaign_state["adverse_signal_confirmations"] = (
                int(campaign_state.get("adverse_signal_confirmations", 0)) + 1
                if adverse_signal else 0
            )
            decay_reason = volatility_exit_reason(
                regime, current_iv,
                float(campaign_state.get(
                    "entry_iv_actual", campaign.get("entry_iv", 0)
                )),
                current_efficiency,
                float(campaign_state.get(
                    "entry_gamma_theta_efficiency_actual",
                    campaign.get("entry_gamma_theta_efficiency", 0),
                )),
                float(campaign.get("iv_decline_exit_ratio", .20)),
                float(campaign.get("efficiency_decline_exit_ratio", .40)),
                int(campaign.get("contraction_confirmations_required", 3)),
                pnl_ratio=pnl_ratio,
                directional_confirmation=direction_ok,
                current_delta=market_number(option.get("option_delta"), 0),
                minimum_effective_delta=float(
                    campaign.get("minimum_effective_delta", .18)
                ),
                adverse_signal_confirmations=int(
                    campaign_state.get("adverse_signal_confirmations", 0)
                ),
                adverse_signal_confirmations_required=int(
                    campaign.get("adverse_signal_confirmations_required", 2)
                ),
            )
        opened_at = safe_datetime(campaign_state.get("position_opened_at"), now)
        partial_exit_quantity, partial_exit_tier, partial_exit_reason = (
            volatility_partial_exit_plan(
                quantity=quantity,
                pnl_ratio=pnl_ratio,
                campaign_state=campaign_state,
                enabled=bool(campaign.get("partial_profit_enabled", True)),
                tier1_profit=float(campaign.get("partial_profit_tier1", .30)),
                tier1_fraction=float(campaign.get("partial_profit_tier1_fraction", .50)),
                tier2_profit=float(campaign.get("partial_profit_tier2", .60)),
                tier2_fraction=float(campaign.get("partial_profit_tier2_fraction", .25)),
            )
        )
        # 【硬止损纵深防御 · 第 1 层】挡的是**分支内的提前覆盖**：
        # 分批止盈 / 常规决策把已置好的 action="SELL" 改掉。
        # 硬止损在 :740-756 已置 action="SELL" + hard_stop_fired=True，
        # 此处**必须最先判定** —— 否则分批止盈分支会无条件把 action 覆盖掉，硬止损白设。
        #
        # 与第 2 层（:1169 起）算**同一个不变量**：删任一层行为不变、删两层失效
        # （变异 C 已证）。保留两层的意义是纵深 —— 将来任何一处新增的提前 return，
        # 都需要两层同时失效才会让止损无声消失。
        if hard_stop_fired:
            pass  # 硬止损已定 action/reason，退出由既有路径统一提交
        elif partial_exit_quantity > 0 and partial_exit_reason:
            batch = select_partial_exit_batch(
                campaign_state.get("tranches") or [],
                mode=str(campaign.get("partial_exit_mode", "FIFO")),
            )
            if batch:
                partial_exit_reason += f"；批次={batch.get('tranche_id', 'unknown')}"
                campaign_state["selected_exit_tranche_id"] = batch.get("tranche_id")
            action, reason = "SELL", partial_exit_reason
        else:
            protection_profile = volatility_profit_protection_profile(campaign, regime)
            adverse_exit_confirmed = int(
                campaign_state.get("adverse_signal_confirmations", 0)
            ) >= int(campaign.get("adverse_signal_confirmations_required", 2))
            action, reason = decide_volatility_action(
                now=now, opened_at=opened_at, quantity=quantity,
                stages_filled=stages_filled, max_stages=effective_max_stages,
                pnl_ratio=pnl_ratio, peak_pnl_ratio=peak_pnl_ratio,
                drawdown_from_peak=drawdown, directional_confirmation=direction_ok,
                signal_active=bool(signal_state.get("signal_active", False)),
                profit_target=float(campaign["profit_target"]),
                stop_loss=float(campaign["stop_loss"]),
                trailing_stop=float(campaign["trailing_stop"]),
                max_hold_hours=float(campaign.get("max_hold_hours", 24)),
                decay_reason=decay_reason,
                minimum_hold_minutes=(
                    min(
                        float(campaign.get("minimum_hold_minutes", 90)),
                        float(campaign.get("adverse_exit_minimum_hold_minutes", 20)),
                    )
                    if adverse_exit_confirmed else
                    float(campaign.get("minimum_hold_minutes", 90))
                ),
                profit_protection_start=float(
                    protection_profile["profit_protection_start"]
                ),
                profit_protection_drawdown=float(
                    protection_profile["profit_protection_giveback"]
                ),
                profit_giveback_from_peak=profit_giveback,
                full_profit_target_enabled=not (
                    bool(campaign.get("partial_profit_enabled", True)) and quantity > 1
                ),
                minimum_locked_profit=float(
                    protection_profile["minimum_locked_profit"]
                ),
                single_contract_take_profit=float(
                    campaign.get("single_contract_take_profit", .18)
                ),
                tiered_profit_lock_enabled=bool(
                    campaign.get("tiered_profit_lock_enabled", True)
                ),
                time_tighten_minutes=float(
                    campaign.get("profit_time_tighten_minutes", 60)
                ),
                time_tighten_giveback=float(
                    campaign.get("profit_time_tighten_giveback", .12)
                ),
                dynamic_stop_enabled=bool(
                    campaign.get("dynamic_time_stop_enabled", True)
                ),
                dynamic_stop_floor=float(
                    campaign.get("dynamic_time_stop_floor", .12)
                ),
                dynamic_stop_start_minutes=float(
                    campaign.get("dynamic_time_stop_start_minutes", 15)
                ),
                dynamic_stop_full_decay_minutes=float(
                    campaign.get("dynamic_time_stop_full_decay_minutes", 90)
                ),
                soft_profit_protection_start=float(
                    campaign.get("soft_profit_protection_start", .08)
                ),
                soft_profit_lock_floor=float(
                    campaign.get("soft_profit_lock_floor", .03)
                ),
                soft_profit_giveback=float(
                    campaign.get("soft_profit_giveback", .35)
                ),
                rapid_giveback_threshold=float(
                    campaign.get("rapid_giveback_threshold", .25)
                ),
                rapid_giveback_window_minutes=float(
                    campaign.get("rapid_giveback_window_minutes", 15)
                ),
                peak_reached_at=(
                    safe_datetime(campaign_state.get("peak_reached_at"), now)
                    if campaign_state.get("peak_reached_at") else None
                ),
                weekend_protection_enabled=bool(
                    campaign.get("weekend_protection_enabled", True)
                ),
                weekend_protection_min_profit=float(
                    campaign.get("weekend_protection_min_profit", 0.20)
                ),
                weekend_protection_minutes=float(
                    campaign.get("weekend_protection_minutes", 30.0)
                ),
            )
    else:
        partial_exit_quantity = 0
        partial_exit_tier = None
        # 原先这里有 `if not hard_stop_fired:` 包裹 —— 死代码：`hard_stop_fired`
        # 只在 VOLATILITY 分支（:740-756）被置位，EARNINGS 分支恒为 False，
        # 该条件恒真。去掉包裹，直接调用（Reviewerr 裁定）。
        action, reason = decide_gamma_action(
            now, event_time, quantity, stages_filled, int(campaign["max_stages"]),
            int(campaign["entry_hours"]), pnl_ratio, drawdown, direction_ok,
            float(campaign["profit_target"]), float(campaign["stop_loss"]),
            float(campaign["trailing_stop"]),
            float(campaign.get("max_hold_hours", 8)),
        )
    # 【硬止损纵深防御 · 第 2 层】挡的是**分支之后的重算与降级**：
    # 第 1 层只保护分支内；本层在决策分支全部走完之后独立重算一次，
    # 并覆盖到期/临近到期等下游分支。
    # 两层算同一不变量，删任一层行为不变、删两层失效（变异 C 已证）——
    # **不要因为"看起来重复"就删掉一层**：那是纵深防御，不是冗余。
    # **本层之后不得再出现把 action 从 SELL 改回非 SELL 的分支**
    #（结构守卫 test_no_branch_downgrades_sell_after_hard_loss_layer 钉住）。
    if trigger_mode == "VOLATILITY" and quantity > 0:
        hard_loss_reason = volatility_hard_loss_exit_reason(
            pnl_ratio=pnl_ratio,
            pnl_amount=position_pnl_amount,
            **_hard_stop_limits(campaign, executor, code),
        )
        if hard_loss_reason:
            partial_exit_quantity = 0
            partial_exit_tier = None
            action, reason = "SELL", hard_loss_reason
        expiry = campaign_expiry_date(campaign, code, now)
        lifecycle_reason = volatility_near_expiry_itm_exit_reason(
            campaign, code, now=now, underlying_price=float(owner["last_price"] or 0),
            quantity=quantity,
        )
        if lifecycle_reason:
            partial_exit_quantity = 0
            partial_exit_tier = None
            action, reason = "SELL", lifecycle_reason
        elif now.date() > expiry or (now.date() == expiry and now.hour >= 15):
            action, reason = "SELL", "合约到期日临近收盘，强制退出以避免自动行权"
    print(f"{code}: {action} ({reason}) qty={quantity} pnl={pnl_ratio:.2%}")
    planned_order_quantity = (
        per_stage if action == "BUY"
        else (partial_exit_quantity or quantity) if action == "SELL"
        else 0
    )
    decision_payload = {
         "position_quantity": quantity, "planned_order_quantity": planned_order_quantity,
         "position_source": "SHADOW_LEDGER" if execution_mode == "SHADOW" else "FUTU_ACCOUNT",
         "pnl_ratio": pnl_ratio, "drawdown": drawdown, "trigger_mode": trigger_mode,
         "position_pnl_amount": position_pnl_amount,
         "profit_giveback": profit_giveback,
         "profit_capture_ratio": profit_capture_ratio,
         "mae_pnl_ratio": float(campaign_state.get("mae_pnl_ratio", pnl_ratio)),
         "r_multiple": (float(position_pnl_amount) / float(campaign_state.get("initial_risk", 0))
                        if float(campaign_state.get("initial_risk", 0) or 0) > 0 else None),
         "tranche_count": len(campaign_state.get("tranches") or []),
         "selected_exit_tranche_id": campaign_state.get("selected_exit_tranche_id"),
         "exit_allocation_policy": campaign.get("exit_allocation_policy", "FIFO"),
         "executable_mark": executable_mark,
         "validation_warnings": issues, "direction_reason": direction_reason,
         "direction_metrics": direction_metrics,
         "volatility_regime": regime,
         "size_factor": size_factor,
         "size_reason": size_reason,
         "structure": campaign.get("structure", "LONG_OPTION"),
         "opportunity_score": campaign.get("opportunity_score"),
         "expected_move_edge": campaign.get("expected_move_edge"),
    }
    if trigger_mode == "VOLATILITY":
        decision_payload.update({
            "entry_tier": campaign.get("entry_tier", campaign.get("adaptive_risk_tier")),
            "base_premium_budget": campaign.get("base_premium_budget"),
            "effective_premium_budget": campaign.get(
                "effective_premium_budget", campaign.get("max_premium_per_stage")
            ),
            "signal_started_at": signal_state.get("signal_started_at"),
            "signal_start_volume_ratio": signal_state.get(
                "signal_start_volume_ratio"
            ),
            "entry_window_end": event_time.isoformat(),
            "position_opened_at": campaign_state.get("position_opened_at"),
            "peak_pnl_ratio": peak_pnl_ratio,
            "tiered_profit_lock_floor": tier_lock_floor,
            "tiered_profit_lock_label": tier_lock_label,
            "mfe_pnl_ratio": peak_pnl_ratio,
            "profit_protection_profile": (
                volatility_profit_protection_profile(campaign, regime)
                if regime is not None else {}
            ),
            "partial_exit_tier": partial_exit_tier,
            "entry_iv_actual": campaign_state.get("entry_iv_actual"),
            "entry_gamma_theta_efficiency_actual": campaign_state.get(
                "entry_gamma_theta_efficiency_actual"
            ),
            "adverse_signal_confirmations": campaign_state.get(
                "adverse_signal_confirmations", 0
            ),
            "option_delta": market_number(option.get("option_delta"), 0),
            "book_status": quote_check.get("book_reason", ""),
        })
    else:
        decision_payload.update({
            "entry_start": (
                event_time - timedelta(hours=int(campaign["entry_hours"]))
            ).isoformat(),
            "event_time": event_time.isoformat(),
        })
    # A strategy action is not a broker order.  Keep the pre-trade signal
    # distinguishable from the later BLOCKED/SUBMITTED/FILLED result.
    if action == "BUY" and quantity <= 0:
        executor.store.decision_if_changed(
            strategy_id, code, "SIGNAL_BUY", reason, decision_payload,
            repeat_after_seconds=900,
            tenant_id=_campaign_tenant,
        )
    else:
        executor.store.decision_if_changed(
            strategy_id, code, action, reason, decision_payload, repeat_after_seconds=900,
            tenant_id=_campaign_tenant,
        )
    if action == "BUY":
        if trigger_mode == "VOLATILITY" and campaign.get("exit_only"):
            block_reason = "退出维护合约已无持仓，不再重新开仓"
            executor.store.decision(
                strategy_id, code, "CLOSED", block_reason,
                {**decision_payload, "exit_only": True},
                tenant_id=_campaign_tenant,
            )
            print(f"{code}: CLOSED ({block_reason})")
            return

        def record_pretrade_block(check: str, block_reason: str, details: dict) -> None:
            payload = {**decision_payload, "pre_trade_check": check, **details}
            recorded = executor.store.decision_if_changed(
                strategy_id, code, "BLOCKED", block_reason, payload, repeat_after_seconds=900,
                tenant_id=_campaign_tenant,
            )
            if recorded:
                executor.store.audit(
                    "PRE_TRADE_BLOCKED", {
                        "symbol": code, "underlying": campaign["underlying"],
                        "check": check, "reason": block_reason, **details,
                    }, strategy_id,
                 actor="service:gamma")

        from futu_quant.strategy_pause import is_strategy_entries_paused
        if trigger_mode == "VOLATILITY" and (
            is_strategy_entries_paused(executor.store, strategy_id, _campaign_tenant)
            or volatility_new_entries_paused(executor.store, broker)
        ):
            block_reason = (
                f"策略「{strategy_id}」在租户「{_campaign_tenant or 'default'}」波动率期权新开仓已临时暂停（已开启「暂停新开仓」）："
                f"坚决不建新仓，已有持仓继续由策略执行止盈、止损与退出管理"
            )
            record_pretrade_block("STRATEGY_NEW_ENTRY_PAUSED", block_reason, {
                "entry_paused": True,
                "tenant_id": _campaign_tenant,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return

        # P1-3 限额必须显式写入 campaign：缺省不是「用默认值」，而是**配置错误**。
        # 风控额度绝不允许靠默认值静默兜底 —— 此前 100/300 两个默认值打架，
        # 就是「键不存在才决定用哪个」这种静默分歧造成的。缺省即拒绝开仓并记 ERROR。
        missing_limit, invalid_limit = _stop_limit_problems(campaign)
        if missing_limit:
            block_reason = (
                f"波动率期权开仓被拒：campaign 缺少显式止损限额 {', '.join(missing_limit)}"
                f"（禁止静默兜底，请先在策略配置中显式设置后再开仓）"
            )
            record_pretrade_block("VOLATILITY_STOP_LIMIT_MISSING", block_reason, {
                "missing_keys": missing_limit,
                "campaign_id": campaign.get("campaign_id"),
                "code": code,
            })
            executor.alerts.send(
                "ERROR", "VOLATILITY_STOP_LIMIT_MISSING",
                f"{code}: campaign {campaign.get('campaign_id')} 缺少显式止损限额 "
                f"{', '.join(missing_limit)}，已拒绝开仓（禁止静默兜底）",
                incident_key="VOLATILITY_STOP_LIMIT_MISSING",
            )
            print(f"{code}: BLOCKED ({block_reason})")
            return
        if invalid_limit:
            block_reason = (
                f"波动率期权开仓被拒：止损限额非法 —— {', '.join(invalid_limit)}"
                f"（限额为 0 会让硬止损永不触发；禁止静默兜底，请设为合法值后再开仓）"
            )
            record_pretrade_block("VOLATILITY_STOP_LIMIT_INVALID", block_reason, {
                "invalid_values": invalid_limit,
                "campaign_id": campaign.get("campaign_id"),
                "code": code,
            })
            executor.alerts.send(
                "ERROR", "VOLATILITY_STOP_LIMIT_INVALID",
                f"{code}: campaign {campaign.get('campaign_id')} 止损限额非法 —— "
                f"{', '.join(invalid_limit)}，已拒绝开仓（禁止静默兜底）",
                incident_key="VOLATILITY_STOP_LIMIT_INVALID",
            )
            print(f"{code}: BLOCKED ({block_reason})")
            return

        multiplier = float(option["option_contract_multiplier"])
        order_legs = list(legs)
        combo_entry_allowed = True
        combo_entry_reason = ""
        combo_entry_checks = {}
        if trigger_mode == "VOLATILITY" and len(order_legs) > 1:
            combo_entry_allowed, combo_entry_reason, combo_entry_checks = (
                volatility_combo_entry_allowed(campaign)
            )
            if not combo_entry_allowed:
                executor.store.audit("VOLATILITY_COMBO_BLOCKED", {
                    "symbol": code,
                    "underlying": campaign["underlying"],
                    "structure": campaign.get("structure"),
                    "reason": combo_entry_reason,
                    "checks": combo_entry_checks,
                }, strategy_id, actor="service:gamma")
                executor.store.decision(strategy_id, code, "BLOCKED", f"组合质量未通过，拒绝退化为单腿：{combo_entry_reason}", {
                    "combo_quality_checks": combo_entry_checks,
                }, tenant_id=_campaign_tenant)
                print(f"{code}: BLOCKED (组合质量未通过，拒绝退化为单腿)")
                return
        entry_unit_price = (
            float(campaign.get("structure_net_debit", ask))
            if len(order_legs) > 1 else ask
        )
        is_live_real = execution_mode == "REAL" or getattr(broker, "trading_env", "SIMULATE") == "REAL"
        if is_live_real:
            per_stage = 1
            max_real_order = float(getattr(broker.settings, "real_max_order_notional", 1500.0) or 1500.0)
            if entry_unit_price * multiplier > max_real_order:
                block_reason = (
                    f"实盘微资本安全保护：单张期权权利金 ${entry_unit_price * multiplier:,.2f} "
                    f"超过实盘单笔上限 ${max_real_order:,.2f}，拒绝超额高价合约"
                )
                record_pretrade_block("LIVE_MAX_ORDER_NOTIONAL", block_reason, {
                    "contract_price": entry_unit_price,
                    "contract_notional": entry_unit_price * multiplier,
                    "max_real_order": max_real_order,
                })
                print(f"{code}: BLOCKED ({block_reason})")
                return
        premium = entry_unit_price * multiplier * per_stage
        budget_limit, budget_reason = loss_adaptive_premium_budget(
            float(campaign["max_premium_per_stage"]), executor.store, campaign["underlying"],
        )
        if ask <= 0 or premium > budget_limit:
            block_reason = (
                f"交易前权利金检查未通过：本批权利金 ${premium:,.2f}，"
                f"活动预算 ${budget_limit:,.2f}（{budget_reason}）"
            )
            record_pretrade_block("PREMIUM_BUDGET", block_reason, {
                "premium": premium,
                "premium_budget": budget_limit,
                "base_premium_budget": float(campaign["max_premium_per_stage"]),
                "budget_adjustment_reason": budget_reason,
                "ask_price": ask,
                "order_structure": (
                    campaign.get("structure") if len(order_legs) > 1 else "LONG_OPTION"
                ),
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        option_positions = positions[positions["code"].astype(str).str.contains(r"\d{6}[CP]\d+$", regex=True)] if not positions.empty else positions
        same_underlying_structures = {
            str(item.get("structure") or "LONG_OPTION")
            for item in executor.store.option_combos(limit=500)
            if item.get("strategy_id") == "volatility_long_gamma"
            and item.get("status") in {"OPEN", "EXECUTING", "CLOSING"}
            and str(item.get("underlying", "")) == str(campaign.get("underlying", ""))
        } if trigger_mode == "VOLATILITY" else set()
        current_structure = str(campaign.get("structure") or "LONG_OPTION")
        if trigger_mode == "VOLATILITY" and quantity <= 0 and current_structure not in same_underlying_structures:
            if len(same_underlying_structures) >= 2:
                record_pretrade_block("UNDERLYING_STRUCTURE_LIMIT", "同一标的最多允许 2 个期权结构", {
                    "underlying": campaign.get("underlying"),
                    "active_structures": sorted(same_underlying_structures),
                    "max_structures": 2,
                })
                return
        option_codes = option_positions["code"].tolist() if not option_positions.empty else []
        option_quotes = broker.snapshots(option_codes) if option_codes else option_positions
        greeks = portfolio_greeks(option_positions, option_quotes)
        allowed, greek_reason = gamma_add_allowed(greeks, option, per_stage)
        if not allowed:
            block_reason = f"交易前 Greeks 风控未通过：{greek_reason}"
            record_pretrade_block("GREEKS_RISK", block_reason, {
                "greeks_reason": greek_reason,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        analysis = contract_analysis(
            option, float(owner["last_price"]),
            realized_volatility(underlying_bars),
            executor.store,
        )
        if trigger_mode == "VOLATILITY":
            quality_allowed, quality_reason, quality_checks = volatility_entry_quality_allowed(
                campaign,
                default_min_score=float(campaign.get("minimum_entry_quality_score", 70) or 70),
                default_min_expected_return=float(
                    campaign.get("minimum_model_expected_return", .02)
                ),
            )
            if not quality_allowed:
                record_pretrade_block("ENTRY_QUALITY", quality_reason, {
                    "entry_quality_checks": quality_checks,
                })
                print(f"{code}: BLOCKED ({quality_reason})")
                return
        allowed, iv_reason = iv_entry_allowed(analysis, trigger_mode)
        if not allowed:
            block_reason = f"交易前 IV 风控未通过：{iv_reason}"
            record_pretrade_block("IV_RISK", block_reason, {
                "iv_reason": iv_reason, "analysis": analysis,
            })
            print(f"{code}: BLOCKED ({block_reason})")
            return
        if trigger_mode == "VOLATILITY":
            batch_id = (
                f"{strategy_id}:{campaign.get('campaign_id', code)}:cycle"
                f"{int(campaign_state.get('entry_cycle', 0))}:stage{stages_filled + 1}"
            )
            stop_price = entry_unit_price * (1 - float(campaign.get("stop_loss", .35)))
            initial_risk = max(0.0, (entry_unit_price - stop_price) * multiplier * per_stage)
            # The batch is created on the first confirmed fill, not at intent time;
            # rejected/unfilled orders must not create phantom risk lots.
            campaign_state["active_batch_id"] = batch_id
            campaign_state["initial_risk"] = initial_risk
        if len(order_legs) > 1 and execution_mode != "SHADOW":
            active_combo_id = automatic_combo_id(
                strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
            )
            existing_combo = executor.store.option_combo(active_combo_id)
            if existing_combo and existing_combo.get("status") == "OPEN":
                executor.store.decision_if_changed(
                    strategy_id, code, "HOLD", "定义风险组合已经完成开仓",
                    {"combo_id": active_combo_id, "structure": campaign.get("structure")},
                    repeat_after_seconds=300,
                    tenant_id=_campaign_tenant,
                )
                return
            quotes = broker.snapshots([leg["code"] for leg in order_legs]).set_index("code")
            combo_legs = []
            for leg in order_legs:
                quote = quotes.loc[leg["code"]]
                side = str(leg["side"]).upper()
                combo_legs.append({
                    "role": str(leg.get("role") or (
                        "RISK" if side == "SELL" else "PROTECTIVE"
                    )),
                    "symbol": str(leg["code"]),
                    "side": side,
                    "quantity": per_stage * int(leg.get("ratio", 1)),
                    "limit_price": float(
                        quote["ask_price"] if side == "BUY" else quote["bid_price"]
                    ),
                })
            manager = ComboExecutionManager(broker, executor.store, executor)
            if existing_combo is None:
                create_combo(executor.store, {
                    "combo_id": active_combo_id,
                    "strategy_id": strategy_id,
                    "name": f"自动波动率表达 {campaign.get('underlying')}",
                    "underlying": campaign["underlying"],
                    "structure": campaign.get("structure", "DEFINED_RISK"),
                    "max_net_debit": float(campaign["max_premium_per_stage"]) / 100,
                    "min_net_credit": 0,
                    "assembly_timeout_seconds": 300,
                    "naked_timeout_seconds": 15,
                    "auto_rollback": True,
                }, combo_legs)
            manager.start(active_combo_id)
            executor.store.audit("VOLATILITY_COMBO_SUBMITTED", {
                "combo_id": active_combo_id, "symbol": code,
                "underlying": campaign["underlying"],
                "structure": campaign.get("structure"), "legs": combo_legs,
            }, strategy_id, actor="service:gamma")
            executor.store.decision(
                strategy_id, code, "BUY",
                f"已提交定义风险组合：{campaign.get('structure_label', campaign.get('structure'))}",
                {**decision_payload, "combo_id": active_combo_id, "legs": combo_legs},
                tenant_id=_campaign_tenant,
            )
            return
        leg_quotes = broker.snapshots([leg["code"] for leg in order_legs]).set_index("code")
        cycle_suffix = (
            f":cycle{int(campaign_state.get('entry_cycle', 0))}"
            if trigger_mode == "VOLATILITY" else ""
        )
        for leg_number, leg in enumerate(order_legs, 1):
            leg_quote = leg_quotes.loc[leg["code"]]
            side = str(leg["side"]).upper()
            leg_price = float(leg_quote["ask_price"] if side == "BUY" else leg_quote["bid_price"])
            result = executor.submit_limit(
                strategy_id,
                f"{campaign['event_date']}:{campaign.get('campaign_id', code)}"
                f"{cycle_suffix}:OPEN:stage{stages_filled + 1}:leg{leg_number}",
                leg["code"], side, per_stage * int(leg.get("ratio", 1)), leg_price,
                {"multiplier": float(leg_quote.get("option_contract_multiplier", multiplier)),
                 "batch_id": campaign_state.get("active_batch_id"),
                 "entry_cycle": int(campaign_state.get("entry_cycle", 0)),
                 "initial_risk": campaign_state.get("initial_risk"),
                 "reason": reason, "underlying": campaign["underlying"],
                 "structure": campaign.get("structure"), "combo_primary": code,
                 "entry_tier": campaign.get("entry_tier", campaign.get("adaptive_risk_tier")),
                 "base_premium_budget": campaign.get("base_premium_budget"),
                 "effective_premium_budget": campaign.get(
                     "effective_premium_budget", campaign.get("max_premium_per_stage")
                 )},
            )
            print(result)
    elif action == "SELL" and quantity > 0:
        if not campaign_state.get("pending_exit_episode"):
            campaign_state["exit_episode"] = int(campaign_state.get("exit_episode", 0) or 0) + 1
            campaign_state["pending_exit_episode"] = True
        close_legs, close_route = volatility_exit_close_legs(campaign, code, legs)
        if close_route == "EXIT_ONLY":
            executor.store.audit("VOLATILITY_EXIT_ONLY_SINGLE_LEG_CLOSE", {
                "symbol": code,
                "underlying": campaign["underlying"],
                "original_structure": campaign.get("structure"),
                "original_legs": legs,
                "reason": reason,
            }, strategy_id, actor="service:gamma")
        if len(close_legs) > 1 and execution_mode != "SHADOW":
            active_combo_id = combo_id or automatic_combo_id(
                strategy_id, campaign, int(campaign_state.get("entry_cycle", 0))
            )
            active_combo = executor.store.option_combo(active_combo_id)
            if not active_combo:
                close_legs, close_route = volatility_exit_close_legs(
                    campaign, code, legs, combo_available=False,
                )
                executor.store.audit("VOLATILITY_COMBO_CLOSE_FALLBACK_SINGLE_LEG", {
                    "symbol": code,
                    "underlying": campaign["underlying"],
                    "missing_combo_id": active_combo_id,
                    "original_legs": legs,
                    "reason": reason,
                }, strategy_id, actor="service:gamma")
            else:
                ComboExecutionManager(
                    broker, executor.store, executor
                ).request_close(active_combo_id)
                campaign_state["pending_exit_reason"] = reason
                executor.store.audit("VOLATILITY_COMBO_CLOSE_REQUESTED", {
                    "combo_id": active_combo_id, "symbol": code, "reason": reason,
                }, strategy_id, actor="service:gamma")
                return
        exit_quantity = int(planned_order_quantity or quantity)
        leg_quotes = broker.snapshots([leg["code"] for leg in close_legs]).set_index("code")
        cycle_suffix = (
            f":cycle{int(campaign_state.get('entry_cycle', 0))}"
            if trigger_mode == "VOLATILITY" else ""
        )
        for leg_number, leg in enumerate(close_legs, 1):
            opening_side = str(leg["side"]).upper()
            closing_side = "SELL" if opening_side == "BUY" else "BUY"
            leg_quote = leg_quotes.loc[leg["code"]]
            urgent_exit = bool(
                "止损" in str(reason)
                or "强制" in str(reason)
                or "到期" in str(reason)
                or "盈利保护" in str(reason)
                or "回撤" in str(reason)
            )
            leg_price, exit_price_context = volatility_exit_limit_price(
                leg_quote, closing_side, urgent=urgent_exit, campaign=campaign,
            )
            if leg_price <= 0:
                wait_reason = (
                    f"等待可退出盘口：{leg['code']} "
                    f"{closing_side} 可用限价为 0，下一轮继续刷新尝试退出"
                )
                executor.store.decision(strategy_id, code, "HOLD", wait_reason, {
                    **decision_payload,
                    "market_data_status": "WAITING_EXIT_LIQUIDITY",
                    "book_reason": wait_reason,
                    "planned_order_quantity": exit_quantity,
                    "exit_price_context": exit_price_context,
                }, tenant_id=_campaign_tenant)
                executor.store.audit("OPTION_EXIT_WAITING_FOR_BID", {
                    "symbol": leg["code"], "strategy_id": strategy_id,
                    "reason": wait_reason,
                    "position_quantity": quantity,
                    "planned_order_quantity": exit_quantity,
                    "exit_price_context": exit_price_context,
                }, strategy_id, actor="service:gamma")
                print(f"{code}: HOLD ({wait_reason})")
                return
            result = executor.submit_limit(
                strategy_id,
                f"{campaign['event_date']}:{campaign.get('campaign_id', code)}"
                f"{cycle_suffix}:CLOSE:episode{int(campaign_state.get('exit_episode', 0))}:leg{leg_number}",
                leg["code"], closing_side, exit_quantity * int(leg.get("ratio", 1)), leg_price,
                {"multiplier": float(leg_quote.get("option_contract_multiplier", 100)),
                 "reason": reason, "urgent": True, "structure": campaign.get("structure"),
                 "combo_primary": code, "batch_id": campaign_state.get("selected_exit_tranche_id") or campaign_state.get("active_batch_id"),
                 "exit_allocation_policy": campaign.get("exit_allocation_policy", "FIFO"),
                 "oco_id": campaign_state.get("active_oco_id"),
                 "partial_exit_tier": partial_exit_tier,
                 "planned_order_quantity": exit_quantity,
                 **exit_price_context},
            )
            print(result)
            if trigger_mode == "VOLATILITY" and result.status in {
                "SHADOW", "SHADOW_PART", "SHADOW_FILLED", "FILLED_PART", "FILLED",
            }:
                if partial_exit_tier:
                    tiers = list(campaign_state.get("partial_exit_tiers") or [])
                    if partial_exit_tier not in tiers:
                        tiers.append(partial_exit_tier)
                    campaign_state["partial_exit_tiers"] = tiers
                else:
                    # Do not start cooldown when an exit is merely submitted. The next
                    # observed position transition from positive to zero confirms it.
                    campaign_state["pending_exit_reason"] = reason


def run_once(reconcile: bool = True) -> None:
    settings = Settings.load()
    if not settings.enable_paper_orders:
        raise RuntimeError("ENABLE_PAPER_ORDERS=false; automatic paper execution is disabled")
    broker = FutuPaperBroker(settings)
    executor = ExecutionManager(broker)
    if reconcile:
        executor.reconcile()
    positions = broker.positions()
    orders = broker.orders()
    state = load_state()
    campaigns = load_gamma_campaigns()
    active_keys = {f"earnings_gamma:{item['code']}" for item in campaigns}
    state = {
        key: value for key, value in state.items()
        if not key.startswith("earnings_gamma:") or key in active_keys
    }
    executor.store.prune_campaign_states("earnings_gamma:", active_keys)
    for campaign in campaigns:
        if campaign.get("enabled", True) and campaign.get("trigger_mode", "EARNINGS") == "EARNINGS":
            try:
                run_campaign(
                    broker, executor, campaign, state,
                    positions_snapshot=positions, orders_snapshot=orders,
                )
            except (BrokerError, ValueError, KeyError) as exc:
                print(f"{campaign.get('code')}: ERROR {exc}")
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run automatic Futu paper Gamma strategies")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    while True:
        run_once()
        if not args.loop:
            break
        clock.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
