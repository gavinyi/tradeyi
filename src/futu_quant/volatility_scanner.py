from __future__ import annotations

from datetime import datetime, timedelta
import os
import signal
import threading
from zoneinfo import ZoneInfo

from .alerts import AlertManager
from .config import (
    load_volatility_campaigns, load_volatility_symbols,
    resolve_volatility_radar_budget, upsert_campaign,
)
from .volatility_optimizer import volatility_regime, volatility_threshold_profile
from .volatility_roll import roll_campaign


NY = ZoneInfo("America/New_York")


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


def _timed_call(callback, timeout_seconds: float | None = None):
    """Bound Futu calls so one symbol cannot freeze the entire radar cycle."""
    timeout = max(1.0, float(
        timeout_seconds or os.getenv("RADAR_API_TIMEOUT_SECONDS", "12")
    ))
    if threading.current_thread() is not threading.main_thread():
        return callback()

    def interrupt(_signum, _frame):
        raise RadarCallTimeout(f"行情调用超过 {timeout:g} 秒")

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return callback()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def scan_volatility_watchlist(
    broker, store, symbols: tuple[str, ...] | None = None,
) -> list[dict]:
    results = []
    if symbols is None:
        scan_symbols = load_volatility_symbols()
    else:
        scan_symbols = tuple(dict.fromkeys(symbols))
    if not scan_symbols:
        store.heartbeat(
            "volatility_scanner", "HEALTHY",
            "未配置波动率策略标的，跳过雷达扫描", os.getpid(),
        )
        return results
    snapshot_frame = _timed_call(
        lambda: broker.snapshots_tolerant(list(scan_symbols)), timeout_seconds=15,
    )
    owners = snapshot_frame.set_index("code").to_dict("index") if not snapshot_frame.empty else {}
    market_move = 0.0
    try:
        spy = _timed_call(lambda: broker.snapshots(["US.SPY"])).iloc[0]
        market_move = float(spy["last_price"]) / float(spy["prev_close_price"]) - 1
    except Exception:
        pass
    total_symbols = len(scan_symbols)
    for index, symbol in enumerate(scan_symbols, 1):
        store.heartbeat(
            "volatility_scanner", "SCANNING",
            f"正在扫描 {index}/{total_symbols}：{symbol}", os.getpid(),
        )
        try:
            if store.get_setting(f"volatility_scanner_paused:{symbol}", "false") == "true":
                continue
            owner = owners.get(symbol)
            if owner is None:
                reason = "未取得正股实时行情，跳过本轮扫描"
                result = {"symbol": symbol, "active": False, "reason": reason}
                results.append(result)
                store.decision("volatility_scanner", symbol, "BLOCKED", reason, result)
                continue
            bars = _timed_call(lambda: broker.candles(symbol, count=120))
            profile = volatility_threshold_profile(bars)
            z_target = profile["entry_zscore"]
            move_target = profile["minimum_underlying_move"]
            regime = volatility_regime(owner, bars, z_target, move_target)
            relative_strength = float(regime["intraday_return"]) - market_move
            market_independent = abs(relative_strength) >= .005 or abs(float(regime["intraday_return"])) >= .03
            if regime["active"] and not market_independent:
                regime["active"] = False
                regime["reason"] = (
                    f"主要随大盘同步波动：相对SPY仅 {relative_strength:+.2%}，目标绝对值≥0.50%"
                )
            previous_active = store.get_setting(f"volatility_signal_active:{symbol}", "false") == "true"
            previous_z = float(store.get_setting(f"volatility_signal_z:{symbol}", "0") or 0)
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
            store.set_setting(f"volatility_signal_active:{symbol}", str(bool(regime["active"])).lower())
            store.set_setting(f"volatility_signal_z:{symbol}", str(regime["zscore_20d"]))
            result = {
                "symbol": symbol, "active": bool(regime["active"]),
                "reason": regime["reason"], "checks": regime["checks"],
                "lifecycle": lifecycle, "market_return": market_move,
                "relative_strength": relative_strength,
                "profile": profile,
                "intraday_return": regime["intraday_return"],
                "zscore_20d": regime["zscore_20d"], "atr_ratio": regime["atr_ratio"],
                "volume_ratio": regime["volume_ratio"], "direction_score": regime["direction_score"],
            }
            results.append(result)
            store.decision(
                "volatility_scanner", symbol,
                "BUY" if regime["active"] else "HOLD",
                "达到动态波动监测条件" if regime["active"] else regime["reason"],
                result,
            )
            if regime["active"]:
                trigger_key = f"volatility_scanner_notified:{symbol}"
                today = datetime.now(NY).date().isoformat()
                if store.get_setting(trigger_key) != today:
                    AlertManager(store).send(
                        "INFO", "VOLATILITY_SIGNAL_DETECTED",
                        f"{symbol} 达到动态波动条件，雷达将生成期权自动策略。{regime['reason']}",
                    )
                    store.set_setting(trigger_key, today)
                    store.audit("VOLATILITY_SIGNAL_CONFIRMED", {
                        "symbol": symbol, "regime": regime,
                    }, "volatility_scanner")
                campaigns = load_volatility_campaigns()
                if not any(item.get("underlying") == symbol and item.get("enabled", True) for item in campaigns):
                    retry_until_raw = store.get_setting(
                        f"volatility_candidate_retry_until:{symbol}", "",
                    )
                    retry_signal_z = _setting_float(
                        store, f"volatility_candidate_retry_z:{symbol}", 0,
                    )
                    try:
                        retry_until = datetime.fromisoformat(str(retry_until_raw))
                    except (TypeError, ValueError):
                        retry_until = None
                    now = datetime.now(NY)
                    if (
                        retry_until is not None
                        and now < retry_until
                        and float(regime["zscore_20d"]) < retry_signal_z + .5
                    ):
                        remaining = max(1, int((retry_until - now).total_seconds() // 60) + 1)
                        result.update({
                            "campaign_created": False,
                            "campaign_code": "",
                            "campaign_reason": f"候选合约冷却中，约 {remaining} 分钟后重试",
                            "candidate_retry_at": retry_until.isoformat(),
                        })
                        continue
                    premium_budget = resolve_volatility_radar_budget(symbol)
                    result["campaign_premium_budget"] = premium_budget
                    seed = {
                        "underlying": symbol, "code": symbol, "event_date": datetime.now(NY).date().isoformat(),
                        "event_session": "行情触发", "trigger_mode": "VOLATILITY",
                        "minimum_underlying_move": move_target, "entry_zscore": z_target,
                        "contracts_per_stage": 1, "max_stages": 3,
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
                        "profit_target": .5,
                        "stop_loss": _setting_float(
                            store, "volatility_default_stop_loss", .35,
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
                            store, "volatility_default_minimum_locked_profit_strong", .10,
                        ),
                        "partial_profit_enabled": True,
                        "partial_profit_tier1": _setting_float(
                            store, "volatility_default_partial_profit_tier1", .18,
                        ),
                        "partial_profit_tier1_fraction": _setting_float(
                            store, "volatility_default_partial_profit_tier1_fraction", .50,
                        ),
                        "partial_profit_tier2": _setting_float(
                            store, "volatility_default_partial_profit_tier2", .35,
                        ),
                        "partial_profit_tier2_fraction": _setting_float(
                            store, "volatility_default_partial_profit_tier2_fraction", .25,
                        ),
                        "single_contract_take_profit": _setting_float(
                            store, "volatility_default_single_contract_take_profit", .18,
                        ),
                        "tiered_profit_lock_enabled": _setting_bool(
                            store, "volatility_default_tiered_profit_lock_enabled", True,
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
                        "cooldown_take_profit_hours": 4,
                        "cooldown_profit_protection_hours": 8,
                        "cooldown_stop_loss_hours": 24,
                        "cooldown_expiry_hours": 0,
                        "cooldown_decay_hours": 8,
                        "cooldown_contraction_hours": 6,
                        "cooldown_bypass_enabled": _setting_bool(
                            store, "volatility_default_cooldown_bypass_enabled", True,
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
                        "cooldown_hours": 24, "enabled": True,
                    }
                    rolled, roll_reason = _timed_call(
                        lambda: roll_campaign(broker, seed, store), timeout_seconds=45,
                    )
                    if rolled.get("code") != symbol:
                        store.set_setting(f"volatility_candidate_retry_until:{symbol}", "")
                        store.set_setting(f"volatility_candidate_retry_z:{symbol}", "0")
                        upsert_campaign("VOLATILITY_CAMPAIGNS", rolled)
                        store.set_setting("strategy_enabled:Volatility Long Gamma", "true")
                        execution_mode = (
                            store.get_setting("strategy_execution_mode:volatility_long_gamma", "")
                            or store.get_setting("execution_mode", "PAPER")
                        )
                        store.audit("VOLATILITY_AUTO_CAMPAIGN_CREATED", {
                            "underlying": symbol, "code": rolled["code"], "reason": roll_reason,
                            "execution_mode": execution_mode, "auto_monitor_enabled": True,
                        }, "volatility_scanner")
                        result.update({
                            "campaign_created": True,
                            "campaign_code": rolled["code"],
                            "campaign_reason": roll_reason,
                            "campaign_risk_tier": rolled.get("adaptive_risk_tier", "常规"),
                        })
                    else:
                        diagnostics = rolled.get("_roll_diagnostics", {})
                        result.update({
                            "campaign_created": False,
                            "campaign_code": "",
                            "campaign_reason": roll_reason,
                            "campaign_diagnostics": diagnostics,
                        })
                        fingerprint = (
                            f"{datetime.now(NY).date().isoformat()}|{roll_reason}|"
                            f"{(diagnostics.get('top_candidates') or [{}])[0].get('code', '')}"
                        )
                        rejection_key = f"volatility_campaign_rejection:{symbol}"
                        retry_seconds = max(
                            300, int(os.getenv("VOLATILITY_CANDIDATE_RETRY_SECONDS", "1200"))
                        )
                        store.set_setting(
                            f"volatility_candidate_retry_until:{symbol}",
                            (datetime.now(NY) + timedelta(seconds=retry_seconds)).isoformat(),
                        )
                        store.set_setting(
                            f"volatility_candidate_retry_z:{symbol}",
                            str(regime["zscore_20d"]),
                        )
                        if store.get_setting(rejection_key) != fingerprint:
                            # 同一交易日、同一最高候选和同一阻塞原因只记录一次，
                            # 防止雷达每个循环刷出大量重复诊断。
                            store.decision(
                                "volatility_scanner", symbol, "CANDIDATE_BLOCKED",
                                roll_reason, result,
                            )
                            store.audit("VOLATILITY_AUTO_CAMPAIGN_REJECTED", {
                                "underlying": symbol, "reason": roll_reason,
                                "diagnostics": diagnostics,
                            }, "volatility_scanner")
                            store.set_setting(rejection_key, fingerprint)
        except Exception as exc:
            results.append({"symbol": symbol, "active": False, "reason": f"扫描异常：{exc}"})
            store.decision("volatility_scanner", symbol, "BLOCKED", f"扫描异常：{exc}")
    return results
