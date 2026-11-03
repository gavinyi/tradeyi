from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import logging
from typing import Callable
import os
import signal
import threading
from zoneinfo import ZoneInfo

from .alerts import AlertManager
from .execution_mode import resolve_configured_execution_mode
from .config import (
    load_volatility_campaigns, load_volatility_symbols,
    resolve_volatility_radar_budget, upsert_campaign,
)
from .volatility_optimizer import complete_volatility_activation, volatility_threshold_profile
from .volatility_activation import build_activation_evidence, save_activation_evidence
from .volatility_chain import campaign_intent_chain_status
from .volatility_orphans import retain_campaign_if_at_risk
from .volatility_roll import roll_campaign
from .tenant import campaigns_config_key_lenient, tenant_id_of_broker


NY = ZoneInfo("America/New_York")
logger = logging.getLogger("futu_quant.volatility_scanner")


class RadarCallTimeout(TimeoutError):
    pass


def _setting_float(store, key: str, default: float) -> float:
    try:
        return float(store.get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _setting_bool(store, key: str, default: bool) -> bool:
    raw = store.get_setting(key, str(default).lower())
    return str(raw).lower() in {"1", "true", "yes", "y", "是"}


def _tenant_scoped_setting_key(base: str, tenant_id: str | None, symbol: str) -> str:
    """``{base}:{tenant_id}:{symbol}`` —— 逐标的运行时状态的**唯一拼键处**。

    雷达按租户扇出后，同一标的会被多个租户各判一次。这些键记录的是**本租户**的
    候选退避/拒绝/通知去重状态；不带租户段时，先扫的租户（模拟）
    会把另一租户（实盘）静默压住 —— 冷却 20 分钟内实盘少发现一次，且无告警。
    空租户用空段（``base::symbol``）而不是回落无前缀键：宁可留一段可审计的
    "无归属"键，也不与真实租户共用状态。
    """
    return f"{base}:{str(tenant_id or '')}:{symbol}"


def _timed_call_thread(callback, timeout: float):
    """Windows-safe watchdog: run *callback* in a daemon thread and bound it.

    Windows has no ``SIGALRM``/``setitimer``, so the blocking call cannot be
    interrupted from the main thread.  The call runs in a daemon worker and
    the main thread raises :class:`RadarCallTimeout` when the deadline passes;
    the stranded worker is abandoned and cleaned up by the interpreter at
    process exit (radar cycles bound every Futu call the same way SIGALRM did).
    """
    box: list = []
    error_box: list = []

    def runner() -> None:
        try:
            box.append(callback())
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            error_box.append(exc)

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise RadarCallTimeout(f"行情调用超过 {timeout:g} 秒")
    if error_box:
        raise error_box[0]
    return box[0]


def _timed_call(callback, timeout_seconds: float | None = None):
    """Bound Futu calls so one symbol cannot freeze the entire radar cycle."""
    timeout = max(1.0, float(
        timeout_seconds or os.getenv("RADAR_API_TIMEOUT_SECONDS", "12")
    ))
    if threading.current_thread() is not threading.main_thread():
        return callback()
    if not hasattr(signal, "SIGALRM"):
        return _timed_call_thread(callback, timeout)

    def interrupt(_signum, _frame):
        raise RadarCallTimeout(f"行情调用超过 {timeout:g} 秒")

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return callback()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class MarketSymbolResult:
    """单个标的的**市场层**结果 —— 与账户无关，可被任意租户复用。"""

    symbol: str
    owner: dict | None
    bars: object | None
    profile: dict
    regime: dict
    relative_strength: float
    market_independent: bool
    row: dict


@dataclass(frozen=True)
class MarketScan:
    """一次扫描的共享市场层产物（昂贵部分**只算一次**）。"""

    symbols: tuple[str, ...]
    market_move: float
    results: dict
    rows: list


@dataclass
class TenantScanResult:
    """单租户扇出结果 —— 一个租户失败不影响其他租户（#2 失败隔离）。"""

    tenant_id: str
    scanned: int = 0
    triggered: int = 0
    campaigns_created: int = 0
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _capped_scan_symbols(symbols: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """标的清单 + 上限（**共享层只算一次**，避免每租户各截各的）。"""
    scan_symbols = tuple(dict.fromkeys(
        load_volatility_symbols() if symbols is None else symbols
    ))
    if not scan_symbols:
        return ()
    max_symbols = max(5, int(os.getenv("RADAR_MAX_SYMBOLS_PER_SCAN", "40")))
    return scan_symbols[:max_symbols]


def _signal_lifecycle(store, regime: dict, symbol: str) -> str:
    """信号生命周期标签 —— 读并更新**全局**信号状态键（市场事实，与账户无关）。"""
    active_key = f"volatility_signal_active:{symbol}"
    z_key = f"volatility_signal_z:{symbol}"
    previous_active = store.get_setting(active_key, "false") == "true"
    previous_z = float(store.get_setting(z_key, "0") or 0)
    if regime["active"] and not previous_active:
        lifecycle = "首次触发"
    elif regime["active"] and float(regime["zscore_20d"]) > previous_z + .5:
        lifecycle = "信号增强"
    elif regime["active"]:
        lifecycle = "持续触发"
    elif previous_active:
        lifecycle = "信号解除"
    else:
        lifecycle = "等待触发"
    active_value = str(bool(regime["active"])).lower()
    z_value = str(regime["zscore_20d"])
    if store.get_setting(active_key, "") != active_value:
        store.set_setting(active_key, active_value)
    if store.get_setting(z_key, "") != z_value:
        store.set_setting(z_key, z_value)
    return lifecycle


def scan_market_layer(
    broker, store, symbols: tuple[str, ...] | None = None,
    progress: "Callable[[int, int, str], None] | None" = None,
) -> MarketScan:
    """**共享市场层** —— 一次扫描产出全部与账户无关的中间结果。

    这是**昂贵**的那一层（逐标的 K 线 + 逐标的 regime + 快照），#2 要求它**只跑一次**：
    多租户扇出时若每租户各跑一遍，OpenD 调用量与耗时随租户数线性翻倍。

    **本函数不得触碰任何账户数据**（``positions`` / ``orders`` / campaign / 租户键）——
    那是 :func:`fan_out_to_tenant` 的职责。混在一起就等于没拆。

    ``progress`` 可选：每个标的回调一次 ``(index, total, symbol)``，
    让调用方（雷达主循环）维持心跳，而不必知道内部细节。
    """
    scan_symbols = _capped_scan_symbols(symbols)
    if not scan_symbols:
        return MarketScan(symbols=(), market_move=0.0, results={}, rows=[])

    snapshot_frame = _timed_call(
        lambda: broker.snapshots_tolerant(list(scan_symbols)), timeout_seconds=15,
    )
    owners = snapshot_frame.set_index("code").to_dict("index") if not snapshot_frame.empty else {}
    market_move = 0.0
    try:
        spy = _timed_call(lambda: broker.snapshots(["US.SPY"])).iloc[0]
        market_move = float(spy["last_price"]) / float(spy["prev_close_price"]) - 1
    except Exception as exc:  # 静默降级清零：取不到 SPY 必须留痕（否则相对强度门静默失真）
        logger.warning("SPY 相对强度基准取数失败，本轮 market_move 按 0 处理：%s", exc)

    results: dict[str, "MarketSymbolResult"] = {}
    rows: list[dict] = []
    scan_started_at = datetime.now(NY)
    scan_trace_id = f"radar-{scan_started_at.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%S%fZ')}"
    total_symbols = len(scan_symbols)
    # A3（2026-10-27）：扫描层决策也要归户。市场层本身不碰账户，但**决策留痕**需要
    # 租户（否则 decision_logs 空租户刷屏、审计无法归户）。由 broker 账户/环境解析，
    # 解析不出时保持空串，由 store 的 DECISION_MISSING_TENANT_ID 告警兜底（不静默）。
    try:
        _scanner_tenant = str(tenant_id_of_broker(broker) or "")
    except Exception:  # noqa: BLE001 - 租户归一化失败不阻断扫描；决策仍会带空租户告警
        _scanner_tenant = ""
    for index, symbol in enumerate(scan_symbols, 1):
        if progress is not None:
            progress(index, total_symbols, symbol)
        try:
            if store.get_setting(f"volatility_scanner_paused:{symbol}", "false") == "true":
                continue
            owner = owners.get(symbol)
            if owner is None:
                reason = "未取得正股实时行情，跳过本轮扫描"
                row = {"symbol": symbol, "active": False, "reason": reason}
                rows.append(row)
                store.decision_if_changed(
                    "volatility_scanner", symbol, "BLOCKED", reason, row,
                    repeat_after_seconds=900, tenant_id=_scanner_tenant,
                )
                results[symbol] = MarketSymbolResult(
                    symbol, None, None, {}, {"active": False, "reason": reason},
                    0.0, False, row,
                )
                continue
            bars = _timed_call(lambda: broker.candles(symbol, count=120))
            profile = volatility_threshold_profile(bars)
            if _scanner_tenant:
                from .volatility_trigger_config import resolve_volatility_trigger_profile
                profile = resolve_volatility_trigger_profile(store, _scanner_tenant, profile)
            regime = complete_volatility_activation(owner, bars, profile, market_move)
            relative_strength = float(
                regime.get("relative_strength", float(regime["intraday_return"]) - market_move)
            )
            market_independent = bool(regime.get(
                "market_independent",
                abs(relative_strength) >= .005 or abs(float(regime["intraday_return"])) >= .03,
            ))
            evidence = build_activation_evidence(
                symbol, regime, profile,
                observed_at=scan_started_at,
                trace_id=f"{scan_trace_id}:{symbol}",
            )
            save_activation_evidence(store, evidence)
            row = {
                "symbol": symbol, "active": bool(regime["active"]),
                "reason": regime["reason"], "checks": regime["checks"],
                "lifecycle": _signal_lifecycle(store, regime, symbol),
                "market_return": market_move,
                "relative_strength": relative_strength,
                "profile": profile,
                "activation_trace_id": evidence["trace_id"],
                "activation_observed_at": evidence["observed_at"],
                "intraday_return": regime["intraday_return"],
                "zscore_20d": regime["zscore_20d"], "atr_ratio": regime["atr_ratio"],
                "volume_ratio": regime["volume_ratio"], "direction_score": regime["direction_score"],
            }
            rows.append(row)
            results[symbol] = MarketSymbolResult(
                symbol, owner, bars, profile, regime, relative_strength, market_independent, row,
            )
        except Exception as exc:
            row = {"symbol": symbol, "active": False, "reason": f"扫描异常：{exc}"}
            rows.append(row)
            store.decision(
                "volatility_scanner", symbol, "BLOCKED", row["reason"],
                tenant_id=_scanner_tenant,
            )
            results[symbol] = MarketSymbolResult(
                symbol, None, None, {}, {"active": False, "reason": row["reason"]},
                0.0, False, row,
            )
    return MarketScan(symbols=scan_symbols, market_move=market_move, results=results, rows=rows)


def fan_out_to_tenant(
    store, broker, profile, market: "MarketScan",
    symbols: tuple[str, ...] | None = None,
) -> "TenantScanResult":
    """**租户扇出层** —— 在单个租户的账户上下文中执行。

    只使用 ``market`` 里已算好的结果 + **本租户账户**的 positions/orders/campaign。
    **不得**在此层调用 ``snapshots`` / ``candles`` —— 那是共享层的事（#2 的核心约束）。

    调用方必须自己包 ``try/except``：一个租户失败**不得**终止其他租户（失败隔离）。
    """
    tenant_id = str(getattr(profile, "tenant_id", "") or "")
    campaigns_key, key_fell_back = campaigns_config_key_lenient(tenant_id)
    if key_fell_back:
        store.operational_event(
            "volatility_scanner", "CAMPAIGNS_KEY_TENANT_UNRESOLVED", "ERROR",
            {
                "tenant_id": tenant_id or None,
                "broker_account_id": getattr(broker, "account_id", None),
                "message": (
                    "无法把 campaign 清单解析到租户键 —— 回落无前缀旧键（未隔离，已留痕）"
                ),
            },
        )
    campaigns = load_volatility_campaigns(tenant_id)
    # 共享市场层可扫描所有启用租户的并集；账户扇出层必须再次收窄到本租户授权池。
    tenant_symbols = set(load_volatility_symbols(tenant_id))
    active_underlyings = {
        str(item.get("underlying", "")) for item in campaigns
        if item.get("enabled", True)
    }
    # 本租户优先处理自己已有仓位的标的（顺序只影响处理次序，不影响结果）
    ordered = tuple(sorted(
        (item for item in market.symbols if item in tenant_symbols),
        key=lambda item: (item not in active_underlyings, item),
    ))
    outcome = TenantScanResult(tenant_id=tenant_id)
    for symbol in ordered:
        item = market.results.get(symbol)
        if item is None:
            continue
        outcome.scanned += 1
        row = item.row
        outcome.rows.append(row)
        store.decision_if_changed(
            "volatility_scanner", symbol,
            "BUY" if item.regime["active"] else "HOLD",
            "达到动态波动监测条件" if item.regime["active"] else item.regime["reason"],
            row, repeat_after_seconds=300, tenant_id=tenant_id,
        )
        if not item.regime["active"]:
            continue
        outcome.triggered += 1
        _maybe_create_tenant_campaign(
            store, broker, tenant_id, campaigns_key, symbol, item, row, outcome,
        )
    return outcome


def _maybe_create_tenant_campaign(
    store, broker, tenant_id, campaigns_key, symbol, item, row, outcome,
) -> None:
    """信号触发后：冷却检查 → 生成并写入**本租户**的 campaign。"""
    trigger_key = _tenant_scoped_setting_key("volatility_scanner_notified", tenant_id, symbol)
    today = datetime.now(NY).date().isoformat()
    if store.get_setting(trigger_key) != today:
        AlertManager(store).send(
            "INFO", "VOLATILITY_SIGNAL_DETECTED",
            f"{symbol} 达到动态波动条件，雷达将生成期权自动策略。{item.regime['reason']}",
        )
        store.set_setting(trigger_key, today)
        store.audit("VOLATILITY_SIGNAL_CONFIRMED", {
            "symbol": symbol, "regime": item.regime, "tenant_id": tenant_id,
        }, "volatility_scanner", actor="service:radar")
    campaigns = load_volatility_campaigns(tenant_id)
    existing_campaign = next(
        (entry for entry in campaigns
         if entry.get("underlying") == symbol and entry.get("enabled", True)),
        None,
    )
    if existing_campaign is not None:
        broken, chain_reason = campaign_intent_chain_status(
            existing_campaign,
            now=datetime.now(timezone.utc),
            intents=store.intents(limit=2000, tenant_id=tenant_id),
            decisions=store.decisions(limit=5000),
            tenant_id=str(tenant_id or ""),
        )
        if broken:
            # **这里才是真断链，保留 ERROR 与 BROKEN 命名。**
            # 判据见 `campaign_intent_chain_status`：campaign 已创建且超过超时窗口，
            # 但既没有订单意图、也没有任何 BLOCKED/CLOSED/HOLD 门禁留痕 ——
            # 说明决策链在 campaign 与下单之间**静默断裂**，属于必须排查的故障。
            # 与之相对，候选被入场过滤器拒绝（下方 `VOLATILITY_CANDIDATE_FILTERED`）
            # 是设计内行为，两者不可共用同一个告警名。
            trace_id = existing_campaign.get("activation_trace_id")
            AlertManager(store).send(
                "ERROR", "VOLATILITY_SIGNAL_CHAIN_BROKEN",
                f"{symbol} {chain_reason}",
                incident_key=f"volatility-campaign-intent:{tenant_id}:{symbol}:{trace_id}",
            )
        return
    retry_until_raw = store.get_setting(
        _tenant_scoped_setting_key("volatility_candidate_retry_until", tenant_id, symbol), "",
    )
    retry_signal_z = _setting_float(
        store,
        _tenant_scoped_setting_key("volatility_candidate_retry_z", tenant_id, symbol), 0,
    )
    try:
        retry_until = datetime.fromisoformat(str(retry_until_raw))
    except (TypeError, ValueError):
        retry_until = None
    now = datetime.now(NY)
    if (
        retry_until is not None
        and now < retry_until
        and float(item.regime["zscore_20d"]) < retry_signal_z + .5
    ):
        remaining = max(1, int((retry_until - now).total_seconds() // 60) + 1)
        row.update({
            "campaign_created": False, "campaign_code": "",
            "campaign_reason": f"候选合约冷却中，约 {remaining} 分钟后重试",
            "candidate_retry_at": retry_until.isoformat(),
        })
        return
    # 预算按租户取（P1）：实盘与模拟各用自己那份，任一侧调值不再污染另一侧。
    # 传 store 是为了迁移态能落持久事件（LEGACY_KEY_FALLBACK），不只是进程内日志。
    premium_budget = resolve_volatility_radar_budget(symbol, tenant_id, store)
    row["campaign_premium_budget"] = premium_budget
    if premium_budget <= 0:
        # 只有预算配置被写成 0/不可解析才会到这里 —— 建了也只会必然被拒，
        # 所以不建仓，但必须留痕（否则表现为「雷达突然不出单」）。
        row.update({
            "campaign_created": False, "campaign_code": "",
            "campaign_reason": (
                f"预算配置不可用（tenant={tenant_id or '未识别'}，"
                f"预算 ${premium_budget:,.0f}），已跳过建仓"
            ),
        })
        try:
            store.operational_event(
                "volatility_radar", "RADAR_BUDGET_UNRESOLVED", "ERROR",
                {"tenant_id": tenant_id or None, "symbol": symbol,
                 "premium_budget": premium_budget},
            )
        except Exception:  # noqa: BLE001  # silent-ok: 外层已有租户级失败隔离，留痕失败不得中断扫描
            pass
        return
    seed = {
        "underlying": symbol, "code": symbol, "event_date": datetime.now(NY).date().isoformat(),
        # 稳定身份（2026-10-30 补）：此前 seed 里**完全没有 campaign_id**，
        # 于是雷达自动建的 campaign 在拦截告警里全是 `campaign_id: null` ——
        # 10-30 COIN 那 18 条 `VOLATILITY_STOP_LIMIT_MISSING` 没有一条能定位到
        # 「哪个租户的哪张合约」，只能拿 decision_logs 反查。
        # 与孤儿收养（`{tenant}|orphan:{code}`）同构：租户前缀 + 语义前缀。
        "campaign_id": f"{tenant_id}|auto:{symbol}" if tenant_id else f"auto:{symbol}",
        "event_session": "行情触发", "trigger_mode": "VOLATILITY",
        "activation_trace_id": row.get("activation_trace_id"),
        "activation_observed_at": row.get("activation_observed_at"),
        "minimum_underlying_move": item.profile["minimum_underlying_move"],
        "entry_zscore": item.profile["entry_zscore"],
        "contracts_per_stage": int(store.get_setting("volatility_default_contracts_per_stage", "2")),
        "max_stages": 3,
        "max_premium_per_stage": premium_budget, "entry_hours": 6,
        "entry_confirmation_minutes": _setting_float(
            store, "volatility_default_entry_confirmation_minutes", 5,
        ),
        "entry_confirmation_min_volume_ratio": _setting_float(
            store, "volatility_default_entry_confirmation_min_volume_ratio", 0.80,
        ),
        "entry_confirmation_min_volume_retention": _setting_float(
            store, "volatility_default_entry_confirmation_min_volume_retention", 0.65,
        ),
        "entry_confirmation_require_active": _setting_bool(
            store, "volatility_default_entry_confirmation_require_active", True,
        ),
        "profit_target": 2.0,
        "stop_loss": _setting_float(store, "volatility_default_stop_loss", .35),
        # P1-3 硬校验要求 campaign **显式**带这两个键，缺键即拒绝开仓
        # （`run_gamma_strategy._stop_limit_problems`，禁止静默兜底）。
        # 雷达是 campaign 的第三个写入方（页面保存 / 孤儿收养 / 推送收养之外），
        # 此前只给那两条收养路径补了键，漏了这份 seed → 雷达每自动建一个
        # campaign 就必然开仓被拒一次（2026-10-30 21:43 实盘 US.COIN261120P145000
        # 告警即此）。取值与页面同源，避免又出现「两处默认值打架」。
        "max_position_loss_usd": _setting_float(
            store, "volatility_default_max_position_loss_usd", 300.0,
        ),
        "max_position_loss_ratio": _setting_float(
            store, "volatility_default_max_position_loss_ratio", 0.25,
        ),
        "trailing_stop": .25,
        "max_hold_hours": 24, "minimum_hold_minutes": 90,
        "iv_decline_exit_ratio": .20,
        "efficiency_decline_exit_ratio": .40,
        "contraction_confirmations_required": 3,
        "profit_protection_start": .15,
        "profit_protection_drawdown": .25,
        "minimum_locked_profit": .08,
        "near_expiry_protection_days": 1,
        "profit_protection_start_near_expiry": .10,
        "profit_protection_giveback_near_expiry": .20,
        "minimum_locked_profit_near_expiry": .05,
        "profit_protection_start_normal": .10,
        "profit_protection_giveback_normal": .20,
        "minimum_locked_profit_normal": .05,
        "profit_protection_start_dynamic": _setting_float(
            store, "volatility_default_profit_protection_start_dynamic", .12,
        ),
        "profit_protection_giveback_dynamic": _setting_float(
            store, "volatility_default_profit_protection_giveback_dynamic", .22,
        ),
        "minimum_locked_profit_dynamic": _setting_float(
            store, "volatility_default_minimum_locked_profit_dynamic", .07,
        ),
        "profit_protection_start_strong": _setting_float(
            store, "volatility_default_profit_protection_start_strong", .10,
        ),
        "profit_protection_giveback_strong": _setting_float(
            store, "volatility_default_profit_protection_giveback_strong", .18,
        ),
        "minimum_locked_profit_strong": _setting_float(
            store, "volatility_default_minimum_locked_profit_strong", .06,
        ),
        "partial_profit_enabled": True,
        "exit_allocation_policy": str(store.get_setting(
            "volatility_default_exit_allocation_policy", "FIFO"
        ) or "FIFO").upper(),
        "partial_profit_tier1": _setting_float(store, "volatility_default_partial_profit_tier1", .35),
        "partial_profit_tier1_fraction": _setting_float(
            store, "volatility_default_partial_profit_tier1_fraction", .50,
        ),
        "partial_profit_tier2": _setting_float(store, "volatility_default_partial_profit_tier2", .70),
        "partial_profit_tier2_fraction": _setting_float(
            store, "volatility_default_partial_profit_tier2_fraction", .25,
        ),
        "single_contract_take_profit": _setting_float(
            store, "volatility_default_single_contract_take_profit", .18,
        ),
        "tiered_profit_lock_enabled": _setting_bool(
            store, "volatility_default_tiered_profit_lock_enabled", True,
        ),
        # 方案 R：统一棘轮止盈。默认开启 —— 关掉等于回到「阶梯表不生效」的旧逻辑。
        "profit_ratchet_enabled": _setting_bool(
            store, "volatility_default_profit_ratchet_enabled", True,
        ),
        "profit_ratchet_giveback_min": _setting_float(
            store, "volatility_default_profit_ratchet_giveback_min", .10,
        ),
        "profit_ratchet_full_peak": _setting_float(
            store, "volatility_default_profit_ratchet_full_peak", 1.50,
        ),
        "profit_ratchet_tighten_factor": _setting_float(
            store, "volatility_default_profit_ratchet_tighten_factor", .60,
        ),
        "profit_time_tighten_minutes": _setting_float(
            store, "volatility_default_profit_time_tighten_minutes", 60,
        ),
        "profit_time_tighten_giveback": _setting_float(
            store, "volatility_default_profit_time_tighten_giveback", .12,
        ),
        "dynamic_time_stop_enabled": _setting_bool(
            store, "volatility_default_dynamic_time_stop_enabled", True,
        ),
        "dynamic_time_stop_floor": _setting_float(
            store, "volatility_default_dynamic_time_stop_floor", .12,
        ),
        "dynamic_time_stop_start_minutes": _setting_float(
            store, "volatility_default_dynamic_time_stop_start_minutes", 15,
        ),
        "dynamic_time_stop_full_decay_minutes": _setting_float(
            store, "volatility_default_dynamic_time_stop_full_decay_minutes", 90,
        ),
        # 软启动锁盈（2026-10-30 新增显式键）：此前这三个参数只存在于
        # run_gamma_strategy 的 `campaign.get(..., 默认值)` 里，seed 不写 →
        # 雷达自动建的 campaign 全部靠代码默认值，改默认值等于隐式改所有 campaign。
        # 写进 seed 后「配置里能看到」与「实际生效」是同一个值（同 P1-3 那一课）。
        # 启动线从 8% 降到 4%：旧值与硬止损线 8% 重合，浮盈 0~8% 完全无保护。
        "soft_profit_protection_start": _setting_float(
            store, "volatility_default_soft_profit_protection_start", .04,
        ),
        "soft_profit_lock_floor": _setting_float(
            store, "volatility_default_soft_profit_lock_floor", .03,
        ),
        "soft_profit_giveback": _setting_float(
            store, "volatility_default_soft_profit_giveback", .35,
        ),
        # 入场价差门（2026-10-30）：单腿买方「止损额撑不起 2.5 个价差」时不建仓。
        "entry_max_spread_pct": _setting_float(
            store, "volatility_default_entry_max_spread_pct", 5.0,
        ),
        "entry_min_spread_multiple": _setting_float(
            store, "volatility_default_entry_min_spread_multiple", 2.5,
        ),
        "cooldown_take_profit_hours": 4,
        "cooldown_profit_protection_hours": 8,
        "cooldown_stop_loss_hours": 24,
        "cooldown_expiry_hours": 0,
        "cooldown_decay_hours": 8,
        "cooldown_contraction_hours": 6,
        "cooldown_bypass_enabled": _setting_bool(
            store, "volatility_default_cooldown_bypass_enabled", False,
        ),
        "cooldown_bypass_stop_loss": _setting_bool(
            store, "volatility_default_cooldown_bypass_stop_loss", False,
        ),
        "cooldown_bypass_min_zscore": _setting_float(
            store, "volatility_default_cooldown_bypass_min_zscore", 3.0,
        ),
        "cooldown_bypass_min_intraday_move": _setting_float(
            store, "volatility_default_cooldown_bypass_min_intraday_move", 0.035,
        ),
        "cooldown_bypass_min_atr_ratio": _setting_float(
            store, "volatility_default_cooldown_bypass_min_atr_ratio", 2.0,
        ),
        "cooldown_bypass_min_volume_ratio": _setting_float(
            store, "volatility_default_cooldown_bypass_min_volume_ratio", 1.5,
        ),
        "cooldown_hours": 24,
        "same_contract_cooldown_hours": _setting_float(
            store, "volatility_default_same_contract_cooldown_hours", 24,
        ),
        "maturity_min_dte": _setting_float(store, "volatility_default_maturity_min_dte", 5),
        "maturity_max_dte": _setting_float(store, "volatility_default_maturity_max_dte", 21),
        "enabled": True,
    }
    rolled, roll_reason = _timed_call(
        lambda: roll_campaign(broker, seed, store), timeout_seconds=45,
    )
    if rolled.get("code") != symbol:
        store.set_setting(
            _tenant_scoped_setting_key("volatility_candidate_retry_until", tenant_id, symbol), "",
        )
        store.set_setting(
            _tenant_scoped_setting_key("volatility_candidate_retry_z", tenant_id, symbol), "0",
        )
        # 共享不变量：雷达同样不得顶掉「仍有在险头寸」的旧 campaign。
        # 缺失这道护栏时，雷达每 300–900 秒滚一次就会让已成交仓位瞬间失去归属，
        # 而止损评估只遍历 campaign 列表 → 仓位进入无保护窗口。
        try:
            existing = load_volatility_campaigns(tenant_id)
            old_for_symbol = next(
                (c for c in existing
                 if str(c.get("underlying", "")).upper() == str(symbol).upper()),
                None,
            )
            if old_for_symbol is not None:
                retained = retain_campaign_if_at_risk(
                    old_for_symbol, broker.positions(), broker.orders(), broker,
                )
                if retained is not None:
                    upsert_campaign(campaigns_key, {
                        **retained,
                        # 显式带租户：否则身份退化成裸 {code}，与 ADOPT 产出的
                        # {tenant}|{code} 同 code 并存两条（P0-9）。
                        "tenant_id": tenant_id,
                        "replacement_code": rolled.get("code"),
                        "replacement_reason": retained.get("retained_reason"),
                    })
                    store.audit("VOLATILITY_RADAR_OLD_CAMPAIGN_RETAINED", {
                        "underlying": symbol,
                        "old_code": old_for_symbol.get("code"),
                        "new_code": rolled.get("code"),
                        "retained_reason": retained.get("retained_reason"),
                        "tenant_id": tenant_id,
                    }, "volatility_scanner", actor="service:radar")
        except Exception as exc:
            store.operational_event(
                "volatility_scanner", "RETAIN_CHECK_FAILED", "WARNING",
                {"underlying": symbol, "error": str(exc), "tenant_id": tenant_id},
            )
        upsert_campaign(campaigns_key, {
            **rolled,
            # 显式带租户（同上）：雷达写入方必须与 ADOPT 用同一身份口径。
            "tenant_id": tenant_id,
            "activation_trace_id": row.get("activation_trace_id"),
            "activation_observed_at": row.get("activation_observed_at"),
            "auto_created_at": datetime.now(timezone.utc).isoformat(),
        })
        # 启用键必须落到**本租户**：写全局 `strategy_enabled:Volatility Long Gamma`
        # 等于把整个策略对**所有租户**置为启用 —— 雷达给某一个租户建单只是该租户的
        # 行为，控制面写入不得跨租户连坐（也避免用户关掉全局后被雷达静默重新启用）。
        # 两种拼写都写：调度器按 `:{tenant}:{service_name}` 查，对账/回滚按
        # `:{tenant}:{strategy_id}` 查（与 pages/9 的启用写入口径一致）。
        if tenant_id:
            store.set_setting(
                f"strategy_enabled:{tenant_id}:Volatility Long Gamma", "true",
            )
            store.set_setting(
                f"strategy_enabled:{tenant_id}:volatility_long_gamma", "true",
            )
        # P1-6：读侧走单源，本租户键优先；写进审计的模式必须与真正下单用的口径一致。
        execution_mode = resolve_configured_execution_mode(
            store, tenant_id, "volatility_long_gamma",
        ) or "PAPER"
        store.audit("VOLATILITY_AUTO_CAMPAIGN_CREATED", {
            "underlying": symbol, "code": rolled["code"], "reason": roll_reason,
            "execution_mode": execution_mode, "auto_monitor_enabled": True,
            "tenant_id": tenant_id,
            "trace_id": row.get("activation_trace_id"),
        }, "volatility_scanner", actor="service:radar")
        row.update({
            "campaign_created": True,
            "campaign_code": rolled["code"],
            "campaign_reason": roll_reason,
            "campaign_risk_tier": rolled.get("adaptive_risk_tier", "常规"),
        })
        outcome.campaigns_created += 1
        return
    diagnostics = rolled.get("_roll_diagnostics", {})
    row.update({
        "campaign_created": False, "campaign_code": "",
        "campaign_reason": roll_reason, "campaign_diagnostics": diagnostics,
    })
    fingerprint = (
        f"{datetime.now(NY).date().isoformat()}|{roll_reason}|"
        f"{(diagnostics.get('top_candidates') or [{}])[0].get('code', '')}"
    )
    rejection_key = _tenant_scoped_setting_key("volatility_campaign_rejection", tenant_id, symbol)
    retry_seconds = max(
        300, int(os.getenv("VOLATILITY_CANDIDATE_RETRY_SECONDS", "1200"))
    )
    store.set_setting(
        _tenant_scoped_setting_key("volatility_candidate_retry_until", tenant_id, symbol),
        (datetime.now(NY) + timedelta(seconds=retry_seconds)).isoformat(),
    )
    store.set_setting(
        _tenant_scoped_setting_key("volatility_candidate_retry_z", tenant_id, symbol),
        str(item.regime["zscore_20d"]),
    )
    if store.get_setting(rejection_key) != fingerprint:
        # 同一交易日、同一最高候选和同一阻塞原因只记录一次，
        # 防止雷达每个循环刷出大量重复诊断。
        store.decision(
            "volatility_scanner", symbol, "CANDIDATE_BLOCKED", roll_reason, row,
            tenant_id=tenant_id,
        )
        trace_id = row.get("activation_trace_id")
        store.audit("VOLATILITY_AUTO_CAMPAIGN_REJECTED", {
            "underlying": symbol, "reason": roll_reason,
            "diagnostics": diagnostics, "tenant_id": tenant_id,
            "trace_id": trace_id,
        }, "volatility_scanner", actor="service:radar")
        # 这是**入场过滤器按设计拒单**，不是故障：IV/RV 超门槛、价差过宽、
        # 流动性不足、权利金超预算都属于「评估后决定不买」。
        #
        # 原实现标成 ERROR + `VOLATILITY_SIGNAL_CHAIN_BROKEN`，与真正的断链
        # （`campaign_intent_chain_status` 判定：campaign 已建但既无订单意图、
        # 也无门禁留痕）共用同一个名字，首页和告警列表根本分不出「系统坏了」
        # 和「今天不买」。2026-10-29 恢复后 AMZN 被 IV 百分位 94% 拦下，
        # 却推了一条 ERROR 叫「信号链断裂」—— 典型的术语与级别双误。
        AlertManager(store).send(
            "WARNING", "VOLATILITY_CANDIDATE_FILTERED",
            f"{symbol} 触发 BUY 但候选全部被入场过滤器拒绝（非故障）：{roll_reason}（trace_id={trace_id}）",
            incident_key=f"volatility-candidate-filtered:{tenant_id}:{symbol}",
        )
        store.set_setting(rejection_key, fingerprint)


def scan_volatility_watchlist(
    broker, store, symbols: tuple[str, ...] | None = None,
    tenant_id: str | None = None,
) -> list[dict]:
    """兼容旧调用方：共享层跑一次 + 单租户扇出一次。

    ``tenant_id`` 为空时从 ``broker`` 解析（解析不出来则回落全局键，并留 ERROR）。
    **新代码请直接用** :func:`scan_market_layer` + :func:`fan_out_to_tenant`
    —— 那才是多租户扇出的入口。
    """
    resolved_tenant = str(tenant_id if tenant_id is not None else tenant_id_of_broker(broker) or "")
    market = scan_market_layer(
        broker, store, symbols,
        progress=lambda i, total, sym: _scan_heartbeat(store, i, total, sym),
    )
    if not market.symbols:
        store.heartbeat(
            "volatility_scanner", "HEALTHY",
            "未配置波动率策略标的，跳过雷达扫描", os.getpid(),
        )
        return []
    outcome = fan_out_to_tenant(store, broker, _profile_for_tenant(resolved_tenant), market, symbols)
    # 盘中顺带对沙盒孵化池标的进行轻量影子监听，累积突破信号与微观特征
    _monitor_sandbox_incubator_candidates(broker, store)
    return outcome.rows


def _profile_for_tenant(tenant_id: str):
    """按 tenant_id 构造一个最小 profile（扇出层只需要 ``tenant_id``）。"""
    from futu_quant.tenant import DEFAULT_TENANTS

    if tenant_id and tenant_id in DEFAULT_TENANTS:
        return DEFAULT_TENANTS[tenant_id]

    class _Anonymous:
        pass

    profile = _Anonymous()
    profile.tenant_id = tenant_id
    return profile


def _scan_heartbeat(store, index: int, total: int, symbol: str) -> None:
    """扫描进度心跳（每 10 个标的 + 首尾各一次）。"""
    if index == 1 or index == total or index % 10 == 0:
        store.heartbeat(
            "volatility_scanner", "SCANNING",
            f"正在扫描 {index}/{total}：{symbol}", os.getpid(),
        )


def _monitor_sandbox_incubator_candidates(broker, store) -> None:
    try:
        from futu_quant.universe.incubator_engine import IncubatorEngine
        engine = IncubatorEngine(store=store)
        candidates = store.candidate_symbols(status="ACTIVE")
        if not candidates:
            return

        live_symbols = set(load_volatility_symbols())
        sandbox_symbols = [
            c["symbol"] for c in candidates 
            if c["symbol"] not in live_symbols
        ][:15]

        if not sandbox_symbols:
            return

        frame = _timed_call(lambda: broker.snapshots_tolerant(list(sandbox_symbols)), timeout_seconds=10)
        if frame is None or frame.empty or "code" not in frame.columns:
            return

        for _, row in frame.iterrows():
            sym = str(row.get("code", "")).upper()
            last = float(row.get("last_price", 0) or 0)
            prev = float(row.get("prev_close_price", 0) or 0)
            intraday = abs(last / prev - 1) if prev > 0 else 0.0

            bid = float(row.get("bid_price", 0) or 0)
            ask = float(row.get("ask_price", 0) or 0)
            spread_pct = (ask - bid) / ((ask + bid) / 2) if (ask + bid) > 0 else 0.025

            if intraday >= 0.015:
                observed_at = datetime.now(timezone.utc).isoformat()
                engine.record_simulated_signal(
                    sym, z_score=round(intraday * 100, 2), volume_ratio=1.2,
                    spread_pct=spread_pct, triggered_at=observed_at,
                )
                engine.open_virtual_position(
                    sym, entry_price=last,
                    signal_key=f"scanner:{sym}:{observed_at[:13]}", opened_at=observed_at,
                )
                store.record_universe_lifecycle_event(
                    event_type="SANDBOX_SIGNAL_CAPTURED",
                    symbol=sym,
                    details={
                        "intraday_move": f"{intraday:.2%}",
                        "spread_pct": f"{spread_pct:.2%}",
                        "last_price": last,
                        "source": "incubator_shadow_scan",
                    },
                )
    except Exception as exc:  # 静默降级清零：孵化器沙箱扫描是观测，但失败必须留痕
        logger.warning("孵化器沙箱扫描失败（不影响主扫描）：%s", exc)
