from __future__ import annotations

import os
import time

os.environ.setdefault("FUTU_LOG_ROLE", "volatility")

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.campaign_cleanup import reclaim_exit_only_orphans
from futu_quant.config import Settings, load_volatility_campaigns, upsert_campaign, delete_campaign
from futu_quant.execution import ExecutionManager
from futu_quant.combo_execution import find_open_combo_for_campaign
from futu_quant.volatility_roll import roll_campaign, should_replace_campaign
from futu_quant.volatility_optimizer import volatility_entry_quality_allowed
from futu_quant.volatility_orphans import (
    manage_volatility_orphans,
    protected_volatility_state_keys,
    protection_coverage,
    retain_campaign_if_at_risk,
    active_volatility_state_keys,
    # A15（2026-10-27）：信号键判定只此一处（过滤/剪枝/测试共用），不再本地实现。
    is_signal_state_key,
    # A16（2026-10-27）：历史无租户信号键的清理/落盘过滤共用判定
    is_legacy_signal_state_key,
)
from futu_quant.store import state_key_code
from futu_quant.tenant import campaigns_config_key_lenient, tenant_id_of_broker, DEFAULT_TENANTS
from futu_quant.option_lifecycle import parse_option_code
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd
if __package__:
    from scripts.run_gamma_strategy import load_state, run_campaign, save_state
else:
    from run_gamma_strategy import load_state, run_campaign, save_state

_VOL_STATE_PREFIX = "volatility_long_gamma:"


# 平仓路径的日期判定必须用纽约日期：北京日期比纽约早 12–13 小时，
# 会让「纽约当日到期」的 combo 提前约 4 小时被 `close_stale_expired_combos` 平掉。
NY = ZoneInfo("America/New_York")


def prunable_state_keys(
    store, tenant_id: str, active_keys: set[str], active_codes: set[str],
) -> list[str]:
    """返回**可以安全删除**的波动率状态键；其他租户的键一律不碰。

    **为什么必须按租户过滤**（2026-10-23 开盘实测缺陷）：`run_volatility_once` 是
    逐租户执行的，而 `campaign_state` 是共享表。旧实现只按「本租户 active_keys +
    本租户 active_codes」判定 stale —— 另一租户的带租户键（code 不在本租户
    active_codes 里）会被当成 stale 删掉，对方下一轮再重建 → `peak` 等利润保护
    状态每两分钟归零一次。**跨租户剪枝 = 静默清掉别人的持仓状态。**

    口径：明确属于**其他已注册租户**的键永不删除；本租户键与历史无租户形态
    （含 `signal:` 等共享段）仍按 active_keys / active_codes 判定。
    """
    foreign_prefixes = tuple(
        f"{_VOL_STATE_PREFIX}{tid}:" for tid in DEFAULT_TENANTS if tid != tenant_id
    )
    candidates = [
        key for key in store.campaign_states() if key.startswith(_VOL_STATE_PREFIX)
    ]
    return [
        key for key in candidates
        if not key.startswith(foreign_prefixes)
        # A14：共享信号键不是 campaign/合约键，绝不能被本函数判 stale 删除
        and not is_signal_state_key(key)
        and key not in active_keys
        and state_key_code(key, _VOL_STATE_PREFIX) not in active_codes
    ]


def drop_legacy_signal_state_rows(store, state: dict) -> list[str]:
    """**一次性**清掉 DB 里残留的历史无租户信号键（A16，2026-10-27）。

    A15 的迁移是「只搬不复制」，但另一进程整份 state 回写会把已被 `pop` 的旧键
    复活（生产 18:27:30 实测：旧键 `updated_at` 每轮刷新）→ DB 里新旧两份并存。
    落盘侧已在 `save_state()` 过滤；这里把**存量行**删干净。

    `state` 只读不改：内存里的旧键还要留给 `volatility_signal_state()` 迁移，
    删内存等于把一次确认计时丢掉。
    """
    legacy_keys = [key for key in state if is_legacy_signal_state_key(key)]
    if legacy_keys:
        store.delete_campaign_states(legacy_keys)
    return legacy_keys


def filter_volatility_state(
    state: dict, active_keys: set[str], active_codes: set[str],
) -> dict:
    """剪掉 stale 的 campaign 状态键；**共享信号键必须保留**（A14，2026-10-27）。

    旧实现把所有 `volatility_long_gamma:` 开头但不属于 active campaign/合约的键丢掉，
    包括 `volatility_long_gamma:signal:<UNDERLYING>` —— 入场确认计时因此每轮归零。
    """
    def _is_active(key: str) -> bool:
        if is_signal_state_key(key):
            return True
        if key in active_keys:
            return True
        return state_key_code(key, _VOL_STATE_PREFIX) in active_codes

    return {
        key: value for key, value in state.items()
        if not key.startswith(_VOL_STATE_PREFIX) or _is_active(key)
    }

# 独立命名空间：此前复用 data/trading-service.stop，会让这个独立脚本顺带停掉
# 全部租户 Worker（含实盘真金），属跨脚本污染。
STOP_FILE = Path("data/volatility-strategy.stop")
RESTART_FILE = Path("data/volatility-strategy.restart")


def stop_requested() -> bool:
    return STOP_FILE.exists() or RESTART_FILE.exists()


def close_stale_expired_combos(store, positions: pd.DataFrame, today: date) -> int:
    """Close local combo records that cannot map to a live broker position anymore.

    This is a bookkeeping cleanup only. It deliberately requires all combo legs
    to be expired and absent from the broker account, so it does not hide a real
    position that still needs to be exited.
    """
    position_codes = set()
    if positions is not None and not positions.empty and "code" in positions.columns:
        for _, row in positions.iterrows():
            try:
                qty = float(row.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if abs(qty) > 1e-9:
                position_codes.add(str(row.get("code", "")).upper())

    cleaned = 0
    for combo in store.option_combos(limit=500):
        if combo.get("strategy_id") != "volatility_long_gamma" or combo.get("status") != "OPEN":
            continue
        legs = store.option_combo_legs(combo["combo_id"])
        contracts = [parse_option_code(str(leg.get("symbol", "")).upper()) for leg in legs]
        if not contracts or any(contract is None for contract in contracts):
            continue
        leg_codes = {str(leg.get("symbol", "")).upper() for leg in legs}
        if any(code in position_codes for code in leg_codes):
            continue
        if max(contract.expiration for contract in contracts if contract is not None) >= today:
            continue
        store.update_option_combo(combo["combo_id"], "CLOSED", "过期组合在富途已无持仓，自动完成本地收尾")
        store.option_combo_event(
            combo["combo_id"], "STALE_EXPIRED_COMBO_CLOSED", "CLOSED",
            "过期组合在富途已无持仓，已自动完成本地账本收尾",
            {"leg_codes": sorted(leg_codes)},
        )
        cleaned += 1
    return cleaned


def run_once(
    reconcile: bool = True,
    broker: FutuPaperBroker | None = None,
    executor: ExecutionManager | None = None,
) -> None:
    settings = Settings.load()
    if not settings.enable_paper_orders:
        raise RuntimeError("ENABLE_PAPER_ORDERS=false; automatic paper execution is disabled")
    broker = broker or FutuPaperBroker(settings)
    executor = executor or ExecutionManager(broker)
    if reconcile:
        executor.reconcile()
    positions = broker.positions()
    orders = broker.orders()
    # 回收已了结的 exit_only 孤儿 campaign（无持仓/无挂单/无虚拟持仓才回收）。
    # 不回收的话它们会长期留存：每轮被遍历、被护栏静默跳过、白烧周期，
    # 还会盖住真正需要管的那条（10-23 生产配置里坐实过）。
    try:
        reclaimed = reclaim_exit_only_orphans(executor.store, broker)
        if reclaimed.get("reclaimed"):
            executor.store.audit("VOLATILITY_ORPHANS_RECLAIMED", reclaimed, "volatility_long_gamma", actor="service:volatility")
            print(f"VOLATILITY ORPHANS RECLAIMED: {len(reclaimed['reclaimed'])}")
    except Exception as exc:
        executor.store.operational_event(
            "volatility_long_gamma", "ORPHAN_RECLAIM_FAILED", "WARNING",
            {"error": str(exc)},
        )
    stale_cleaned = close_stale_expired_combos(executor.store, positions, datetime.now(NY).date())
    if stale_cleaned:
        executor.store.audit(
            "VOLATILITY_STALE_EXPIRED_COMBOS_CLOSED",
            {"count": stale_cleaned},
            "volatility_long_gamma",
         actor="service:volatility")
    state = load_state()
    # #1 键级分区：本脚本所有 campaign 读写都走**本租户**的键（读写共用一个 helper）。
    # 回落到全局键时留 ERROR —— 不静默。
    _tenant_id = tenant_id_of_broker(broker) or ""
    _campaigns_key, _key_fell_back = campaigns_config_key_lenient(_tenant_id)
    if _key_fell_back:
        executor.store.operational_event(
            "volatility_long_gamma", "CAMPAIGNS_KEY_TENANT_UNRESOLVED", "ERROR",
            {
                "tenant_id": _tenant_id or None,
                "broker_account_id": getattr(broker, "account_id", None),
                "message": "无法把 campaign 清单解析到租户键 —— 回落无前缀旧键（未隔离，已留痕）",
            },
        )
    campaigns = load_volatility_campaigns(_tenant_id)
    # A3（2026-10-27）：存量 campaign 可能**没有 tenant_id**（生产 `VOLATILITY_CAMPAIGNS`
    # 8 条全缺）——于是在这里用**执行上下文租户**注入，保证后续决策日志/事件都能归户。
    # 只补缺失值，不覆盖已有 tenant_id；不写回 DB（纯读侧补全）。
    if _tenant_id:
        campaigns = [
            item if str(item.get("tenant_id") or "").strip()
            else {**item, "tenant_id": _tenant_id}
            for item in campaigns
        ]
    # Clean malformed contracts before they reach the decision loop.  A bad
    # code otherwise produces hundreds of repetitive BLOCKED decisions and
    # obscures the actionable diagnostics for valid campaigns.
    valid_campaigns = []
    for item in campaigns:
        code = str(item.get("code", ""))
        parsed_code = parse_option_code(code)
        if parsed_code is None:
            valid_legs = [
                str(leg.get("code", "")).upper()
                for leg in (item.get("legs") or [])
                if parse_option_code(str(leg.get("code", "")).upper()) is not None
            ]
            if valid_legs:
                normalized = dict(item)
                normalized["code"] = next(
                    (
                        str(leg.get("code", "")).upper()
                        for leg in (item.get("legs") or [])
                        if str(leg.get("side", "")).upper() == "BUY"
                        and parse_option_code(str(leg.get("code", "")).upper()) is not None
                    ),
                    valid_legs[0],
                )
                parsed = parse_option_code(normalized["code"])
                normalized["strike_time"] = parsed.expiration.isoformat() if parsed else ""
                executor.store.audit("VOLATILITY_CAMPAIGN_CODE_RECOVERED", {
                    "old_code": code, "new_code": normalized["code"],
                    "underlying": normalized.get("underlying"),
                    "structure": normalized.get("structure"),
                    "valid_legs": valid_legs,
                }, "volatility_long_gamma", actor="service:volatility")
                valid_campaigns.append(normalized)
                continue
            executor.store.decision_if_changed(
                "volatility_long_gamma", code or None, "BLOCKED",
                "候选池已过滤：期权代码格式无效或无法解析到期日",
                {"code": code, "underlying": item.get("underlying", "")},
                repeat_after_seconds=3600,
                tenant_id=str(item.get("tenant_id") or _tenant_id),
            )
            continue
        normalized = dict(item)
        normalized["code"] = code.upper()
        strike_time = pd.to_datetime(normalized.get("strike_time"), errors="coerce")
        if pd.isna(strike_time):
            normalized["strike_time"] = parsed_code.expiration.isoformat()
            executor.store.audit("VOLATILITY_CAMPAIGN_EXPIRY_RECOVERED", {
                "code": normalized["code"],
                "old_strike_time": str(item.get("strike_time")),
                "new_strike_time": normalized["strike_time"],
            }, "volatility_long_gamma", actor="service:volatility")
        valid_campaigns.append(normalized)
    campaigns = valid_campaigns
    orphan_results = manage_volatility_orphans(
        broker=broker,
        executor=executor,
        campaigns=campaigns,
        positions=positions,
        orders=orders,
        now=datetime.now(timezone.utc),
    )
    if orphan_results:
        print(f"VOLATILITY ORPHANS HANDLED: {len(orphan_results)}")
        adopted_campaigns = [
            item.get("campaign") for item in orphan_results
            if item.get("action") == "ADOPTED" and isinstance(item.get("campaign"), dict)
        ]
        existing_codes = {str(item.get("code", "")).upper() for item in campaigns}
        for adopted in adopted_campaigns:
            if str(adopted.get("code", "")).upper() not in existing_codes:
                campaigns.append(adopted)
                existing_codes.add(str(adopted.get("code", "")).upper())
        state = load_state()
    # 常驻保护覆盖率：任一时刻所有可解析的期权券商持仓都必须有归属（campaign 内或已 ADOPT）。
    # 覆盖率 < 1.0 立即告警 —— 缺口是静默的，靠人工翻日志发现正是 10-22 事故的情报失效点。
    try:
        coverage, uncovered = protection_coverage(positions, campaigns)
        executor.store.set_setting("volatility_protection_coverage", f"{coverage:.6f}")
        if coverage < 1.0:
            executor.alerts.send(
                "ERROR", "VOLATILITY_PROTECTION_COVERAGE_GAP",
                f"保护覆盖率 {coverage:.2%} < 100%：{len(uncovered)} 个期权持仓无归属"
                f"（{'、'.join(uncovered[:10])}），止损评估看不到这些仓位，已暂停自动处置并等待人工复核",
                incident_key="VOLATILITY_PROTECTION_COVERAGE_GAP",
            )
            print(f"VOLATILITY PROTECTION COVERAGE GAP: {coverage:.2%} uncovered={uncovered}")
        else:
            executor.store.recover_incident("VOLATILITY_PROTECTION_COVERAGE_GAP")
    except Exception as exc:
        executor.store.operational_event(
            "volatility_long_gamma", "PROTECTION_COVERAGE_CHECK_FAILED", "WARNING",
            {"error": str(exc)},
        )
    protected_keys = protected_volatility_state_keys(
        executor.store, positions, orders, broker,
    )
    # P0-9：active_keys 必须**同时**包含带租户与不带租户两种形态。
    # 此前只认 \olatility_long_gamma:{code}\，而 order_push 写的是
    # \olatility_long_gamma:{tenant}:{code}\ → 刚写进去的键同一个周期就被
    # prune_campaign_states 当 stale 删掉（P0-1 的「成交即登记」被完全抵消）。
    # 两处都从共享助手 volatility_state_keys 取，不再各自拼字符串。
    # 走共享入口 tenant_of_broker —— 裸 getattr(broker, "tenant_id") 恒空，
    # 会让"第一道防御"只剩无租户形态，带租户键失去保护（P0-9 的第一处漏）。
    _tenant_id = tenant_id_of_broker(broker) or ""
    active_keys = active_volatility_state_keys(campaigns, _tenant_id, protected_keys)
    # P0-9 第二道保护：按**合约代码**再认一次。即使拿不到租户上下文
    # （broker 无 tenant_id → 只产出去租户形态），带租户的键也不会被误删。
    active_codes = {
        str(item.get("code") or "").strip().upper()
        for item in campaigns if str(item.get("code") or "").strip()
    }
    # A14（2026-10-27）：抽成 filter_volatility_state —— 共享信号键
    # `volatility_long_gamma:signal:<UNDERLYING>` 必须保留，否则入场确认计时每轮归零。
    state = filter_volatility_state(state, active_keys, active_codes)
    # A16（2026-10-27）：清掉 DB 里残留的历史无租户信号键（一次性；之后不会再有）。
    legacy_signal_keys = drop_legacy_signal_state_rows(executor.store, state)
    if legacy_signal_keys:
        executor.store.audit(
            "VOLATILITY_LEGACY_SIGNAL_KEYS_DROPPED",
            {"keys": legacy_signal_keys, "count": len(legacy_signal_keys)},
            "volatility_long_gamma",
            actor="service:volatility",
        )
    # 逐租户剪枝：只删本租户（或历史无租户形态）的 stale 键。
    # 不再调用 store.prune_campaign_states（它按全局前缀扫描，会删掉其他租户的
    # 带租户键 —— 2026-10-23 开盘实测：实盘状态键被模拟那轮反复删除、peak 归零）。
    stale_keys = prunable_state_keys(
        executor.store, _tenant_id, active_keys, active_codes,
    )
    if stale_keys:
        executor.store.delete_campaign_states(stale_keys)
    rolled_pairs = []
    prepared = []
    failures = []
    interrupted = False

    def execute_campaign(campaign):
        if not campaign.get("enabled", True):
            return
        campaign = {**campaign, "trigger_mode": "VOLATILITY"}
        try:
            leg_codes = {
                str(leg.get("code", "")).upper()
                for leg in (campaign.get("legs") or [{"code": campaign.get("code")}])
            }
            position_codes = set(positions.get("code", []))
            if not campaign.get("code") or (
                campaign.get("exit_only") and not leg_codes & position_codes
            ):
                # 10-22 事故：这条 return 此前**完全静默** —— 合约身份被滚动覆写后，
                # 策略认为「没有持仓」，实际持仓裸奔（无止损、无盈利保护、无到期强平），
                # 日志里一个字都没有，只有决策表 position_quantity=0 能暴露。
                # 因此这里必须留一条 WARN，否则同类故障永远无声。
                held = sorted(position_codes & {str(campaign.get("code", "")).upper()})
                executor.store.operational_event(
                    "volatility_long_gamma", "EXIT_ONLY_CAMPAIGN_SKIPPED", "WARNING",
                    {
                        "code": campaign.get("code"),
                        "campaign_id": campaign.get("campaign_id"),
                        "legs": sorted(leg_codes),
                        "broker_holds_code": bool(held),
                        "broker_positions": sorted(position_codes),
                        "reason": (
                            "exit_only campaign 的 legs 与券商持仓不匹配 —— "
                            "若券商仍持有该 code，则该仓位本轮**无人管理**"
                            if held else
                            "exit_only campaign 的 legs 已不在持仓中（正常跳过）"
                        ),
                    },
                )
                return
            started = time.monotonic()
            try:
                run_campaign(
                    broker, executor, campaign, state, "volatility_long_gamma",
                    positions_snapshot=positions, orders_snapshot=orders,
                )
            finally:
                executor.store.operational_event(
                    "volatility_long_gamma", "CAMPAIGN_EXECUTION_DURATION", "INFO",
                    {"code": campaign.get("code"),
                     "duration_seconds": round(time.monotonic() - started, 3)},
                )
        except (BrokerError, ValueError, KeyError) as exc:
            failures.append(f"{campaign.get('code')}: {type(exc).__name__}: {exc}")
            executor.store.operational_event(
                "volatility_long_gamma", "CAMPAIGN_EXECUTION_FAILED", "ERROR",
                {"code": campaign.get("code"), "exception_type": type(exc).__name__,
                 "error": str(exc)},
            )

    def held_quantity(campaign):
        code = str(campaign.get("code", ""))
        broker_rows = positions[positions["code"] == code] if not positions.empty else positions
        broker_qty = int(broker_rows.iloc[0]["qty"]) if not broker_rows.empty else 0
        if getattr(getattr(broker, "settings", None), "trading_env", "SIMULATE") == "REAL":
            # 实盘模式下持仓判定严格且仅以真实券商返回为准，绝对不受模拟盘/Shadow 虚拟残留干扰
            return broker_qty
        shadow_qty, _ = executor.store.shadow_position("volatility_long_gamma", code)
        legs = {
            str(leg.get("code", "")).upper()
            for leg in (campaign.get("legs") or [])
            if parse_option_code(str(leg.get("code", "")).upper()) is not None
        }
        combo = find_open_combo_for_campaign(
            executor.store, strategy_id="volatility_long_gamma",
            underlying=str(campaign.get("underlying", "")),
            structure=str(campaign.get("structure", "")), leg_symbols=legs,
        ) if len(legs) > 1 else None
        return max(broker_qty, int(shadow_qty), 1 if combo else 0)

    def has_recorded_fill(code: str) -> bool:
        """该合约是否存在已登记的成交（账本侧）。

        券商持仓快照可能滞后于成交（成交经轮询入账），仅凭快照会把「刚成交」判成
        「无持仓」。账本侧的成交记录是更早、更可靠的信号，用于兜底判定归属。
        """
        if not code:
            return False
        try:
            rows = executor.store.fills(
                strategy_id="volatility_long_gamma",
                symbol=str(code).upper(),
                limit=50,
            )
        except Exception:
            return False
        return any(
            abs(float(row.get("quantity") or 0)) > 0 for row in rows
        )

    # Perform all held-position exits before any potentially blocking roll/review.
    held_codes = set()
    for campaign in campaigns:
        if stop_requested():
            interrupted = True
            break
        if held_quantity(campaign) > 0:
            held_codes.add(str(campaign.get("code", "")))
            execute_campaign(campaign)
    now = datetime.now(timezone.utc)
    maintenance_interval = max(
        60, int(os.getenv("VOLATILITY_EXPRESSION_REVIEW_SECONDS", "300"))
    )
    for campaign in campaigns:
        if interrupted or str(campaign.get("code", "")) in held_codes:
            continue
        if stop_requested():
            executor.store.audit(
                "VOLATILITY_LOOP_INTERRUPTED_FOR_SERVICE_CONTROL",
                {"reason": "检测到服务停止或重启请求，提前结束本轮波动率策略循环"},
                "volatility_long_gamma",
             actor="service:volatility")
            interrupted = True
            break
        # 滚动判定必须看「此刻」的持仓，不能用本轮开头的快照：成交可能发生在本轮
        # 快照之后，若沿用旧快照，已成交仓位会被判成「无持仓」，旧合约被删 → 仓位成孤儿。
        # 每次滚动前重取一次，成本是一次券商查询，换来的是归属关系不被陈旧数据打断。
        try:
            positions = broker.positions()
            orders = broker.orders()
        except Exception as exc:
            executor.store.operational_event(
                "volatility_long_gamma", "ROLL_REFRESH_FAILED", "WARNING",
                {"error": str(exc)},
            )
        current_code = str(campaign.get("code", ""))
        underlying = str(campaign.get("underlying", ""))
        leg_symbols = {
            str(leg.get("code", "")).upper()
            for leg in (campaign.get("legs") or [])
            if parse_option_code(str(leg.get("code", "")).upper()) is not None
        }
        open_combo = find_open_combo_for_campaign(
            executor.store,
            strategy_id="volatility_long_gamma",
            underlying=underlying,
            structure=str(campaign.get("structure", "")),
            leg_symbols=leg_symbols,
        ) if len(leg_symbols) > 1 else None
        broker_position = (
            positions[positions["code"] == current_code] if not positions.empty
            else positions
        )
        broker_quantity = int(broker_position.iloc[0]["qty"]) if not broker_position.empty else 0
        shadow_quantity, _ = executor.store.shadow_position(
            "volatility_long_gamma", current_code,
        )
        quantity = max(broker_quantity, int(shadow_quantity), 1 if open_combo else 0)
        pending = (
            orders[
                (orders["code"] == current_code)
                & orders["order_status"].map(broker.is_cancellable_status)
            ]
            if not orders.empty and current_code else pd.DataFrame()
        )
        maintenance_key = f"volatility_expression_review:{underlying}"
        last_review = state.get(maintenance_key)
        due_for_review = True
        if last_review:
            try:
                due_for_review = (
                    now - datetime.fromisoformat(str(last_review))
                ).total_seconds() >= maintenance_interval
            except ValueError:
                due_for_review = True
        expiry = pd.to_datetime(campaign.get("strike_time"), errors="coerce")
        min_dte = max(1, int(float(campaign.get("maturity_min_dte", 5) or 5)))
        ny_today = datetime.now(ZoneInfo("America/New_York")).date()
        # `exit_only` campaign 只管理退出，到期临近时由退出逻辑（临近到期价内退出）处理，
        # **不得靠滚动改写成另一个合约** —— 那会让真实持仓失去归属。
        needs_expiry_roll = (
            not campaign.get("exit_only")
            and (pd.isna(expiry) or (expiry.date() - ny_today).days < min_dte)
        )
        quality_allowed, quality_reason, quality_checks = volatility_entry_quality_allowed(
            campaign,
            default_min_score=max(70.0, float(campaign.get("minimum_entry_quality_score", 70))),
            default_min_expected_return=float(
                campaign.get("minimum_model_expected_return", .02)
            ),
        )
        needs_quality_replacement = bool(
            campaign.get("optimizer_version") == 2
            and quantity <= 0
            and pending.empty
            and not quality_allowed
        )
        # `exit_only` campaign 是被接管的在险持仓，只管理退出，**不得重选合约**：
        # 表达复核对它们照常跑，会把 `code` 从实际持仓合约改写成「当前最佳表达」，
        # 于是 campaign 指向一个不存在的合约、真实持仓瞬间失去归属（10-22 META 事故根因）。
        should_review_expression = (
            campaign.get("enabled", True)
            and not campaign.get("exit_only")
            and quantity <= 0
            and pending.empty
            and not has_recorded_fill(current_code)
            and (due_for_review or needs_quality_replacement)
        )
        if needs_expiry_roll or should_review_expression:
            rolled, reason = roll_campaign(broker, campaign, executor.store)
            replace, replace_reason = (
                (
                    parse_option_code(str(rolled.get("code", ""))) is not None
                    and rolled.get("code") != campaign.get("code"),
                    reason,
                ) if needs_expiry_roll
                else should_replace_campaign(campaign, rolled)
            )
            if needs_quality_replacement:
                executor.store.audit("VOLATILITY_QUALITY_REPLACEMENT_REQUESTED", {
                    "old_code": campaign.get("code"),
                    "underlying": underlying,
                    "quality_reason": quality_reason,
                    "quality_checks": quality_checks,
                    "roll_reason": reason,
                }, "volatility_long_gamma", actor="service:volatility")
            state[maintenance_key] = now.isoformat()
            if replace and rolled.get("code") != campaign.get("code"):
                rolled_pairs.append((
                    campaign.get("campaign_id", campaign.get("code")),
                    campaign,
                    rolled,
                ))
                executor.store.audit("VOLATILITY_CONTRACT_ROLLED", {
                    "old_code": campaign.get("code"), "new_code": rolled.get("code"),
                    "reason": reason, "replace_reason": replace_reason,
                    "maintenance_type": (
                        "EXPIRY_ROLL" if needs_expiry_roll else
                        "QUALITY_REPLACEMENT" if needs_quality_replacement else
                        "BEST_EXPRESSION_REVIEW"
                    ),
                }, "volatility_long_gamma", actor="service:volatility")
                executor.store.decision(
                    "volatility_long_gamma", rolled.get("code"), "HOLD",
                    f"已维护为当前最佳期权表达：{replace_reason}",
                    {"old_code": campaign.get("code"), "new_code": rolled.get("code"),
                     "roll_reason": reason,
                     "diagnostics": rolled.get("_roll_diagnostics", {})},
                    tenant_id=str(campaign.get("tenant_id") or _tenant_id),
                )
            else:
                executor.store.decision_if_changed(
                    "volatility_long_gamma", campaign.get("code"), "HOLD",
                    f"期权表达复核未替换：{replace_reason if should_review_expression else reason}",
                    {"roll_reason": reason, "diagnostics": rolled.get("_roll_diagnostics", {})},
                    repeat_after_seconds=900,
                    tenant_id=str(campaign.get("tenant_id") or _tenant_id),
                )
            campaign = rolled
        prepared.append(campaign)
    for old_identity, old_campaign, rolled in rolled_pairs:
        old_code = str(old_campaign.get("code") or "").upper()
        old_has_broker_position = False
        old_has_pending_order = False
        # 共享不变量：持有券商持仓或挂单的 campaign 永不被删除，只降级为 exit_only。
        # 抽成 volatility_orphans.retain_campaign_if_at_risk，雷达滚动路径同样走它。
        retained_campaign = retain_campaign_if_at_risk(old_campaign, positions, orders, broker)
        if old_code and retained_campaign is None and has_recorded_fill(old_code):
            # 券商快照尚未反映成交，但成交账本已有记录 → 同样视为在险
            retained_campaign = {
                **old_campaign,
                "exit_only": True,
                "replacement_code": rolled.get("code"),
                "replacement_reason": "成交账本已有该合约记录（券商快照未反映），保留为退出维护配置",
            }
        if retained_campaign is not None:
            retained = {
                **retained_campaign,
                # 显式带租户：否则身份退化成裸 {code}，与 ADOPT 产出的
                # {tenant}|{code} 同 code 并存两条（P0-9）。
                "tenant_id": tenant_id_of_broker(broker) or "",
                "replacement_code": rolled.get("code"),
                "replacement_reason": retained_campaign.get("retained_reason")
                or "旧合约仍有富途风险，保留为退出维护配置",
            }
            upsert_campaign(_campaigns_key, retained)
            executor.store.audit("VOLATILITY_OLD_CAMPAIGN_RETAINED_FOR_EXIT", {
                "old_code": old_code,
                "new_code": rolled.get("code"),
                "has_broker_position": old_has_broker_position,
                "has_pending_order": old_has_pending_order,
            }, "volatility_long_gamma", actor="service:volatility")
        else:
            delete_campaign(_campaigns_key, old_identity)
        upsert_campaign(_campaigns_key, {
            **rolled,
            # 显式带租户（同上）：滚动写入方必须与 ADOPT 用同一身份口径。
            "tenant_id": _tenant_id,
        })
    if not interrupted:
        for campaign in prepared:
            if stop_requested():
                executor.store.audit(
                    "VOLATILITY_EXECUTION_INTERRUPTED_FOR_SERVICE_CONTROL",
                    {"reason": "检测到服务停止或重启请求，提前结束本轮波动率交易执行"},
                    "volatility_long_gamma",
                 actor="service:volatility")
                interrupted = True
                break
            execute_campaign(campaign)
    save_state(state)
    if failures:
        raise RuntimeError(f"波动率策略 {len(failures)} 个活动执行失败：" + "；".join(failures[:5]))
    if interrupted:
        raise RuntimeError("检测到服务停止或重启请求，波动率策略本轮未完整执行")


if __name__ == "__main__":
    run_once()
