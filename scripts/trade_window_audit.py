"""近 N 分钟交易窗口合规审计（默认 10 分钟）。

用途：盘中按固定节奏回看「刚刚发生了什么」，把**买入信号是否符合入场条件**、
**退出是否符合退场规则**逐条对照规则判定；一旦发现不符合条件的情况，推送飞书
提醒。没有发现问题时保持安静（不刷屏）。

设计取舍：
* 只读数据库，不下单、不改配置、不碰 broker —— 它是一条旁路的观察者，
  唯一的外部副作用是「发现违规时发一条飞书消息」。
* 判定复用**生产同一份**闸门函数（`volatility_entry_spread_gate`），
  不在这里另抄一套阈值 —— 抄一份就等于埋一个会漂移的第二事实源。
* 入场「检查项」直接读策略自己写进 decision_logs 的 checks 结果，
  而不是外部重算指标：策略怎么判的，就按它自己的判据核。
* 违规才推、且同一条违规在去重窗口内只推一次。

用法：
    python scripts/trade_window_audit.py                # 审计最近 10 分钟（不推送）
    python scripts/trade_window_audit.py --push          # 有违规时推飞书
    python scripts/trade_window_audit.py --minutes 60 --push
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 计划任务用 pythonw.exe 启动（不弹控制台），此时 sys.stdout/stderr 是 None，
# 任何 print 都会抛 AttributeError —— 在模块加载时就兜住，避免审计因打印而中断。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_PATH = ROOT / "data" / "trading.db"
STATE_PATH = ROOT / "data" / "qa" / "trade_audit_state.json"
OUT_DIR = ROOT / "data" / "qa" / "trade_audit"
LOG_PATH = ROOT / "data" / "logs" / "trade_window_audit.log"

OPTION_RE = re.compile(r"^US\.[A-Z0-9.]+\d{6}[CP]\d+(?:\.\d+)?$")
# 硬止损线：min($300, 8% × 建仓权利金)。留 50% 容忍给滑点与盘口跳动，
# 超过 12% 的实际亏损意味着「止损这条线没在起作用」，不是正常滑点。
HARD_STOP_RATIO = 0.08
SLIPPAGE_ALARM_RATIO = 0.12
# 软保护：浮盈到 4% 起保护、回落到 3% 锁利离场。
SOFT_PROTECT_START = 0.04
SOFT_LOCK_FLOOR = 0.03
# 单笔权利金预算的档位放大上限：normal 1.0 / dynamic 1.5 / strong 2.0。
MAX_TIER_MULTIPLIER = 2.05
# 持仓超过这么久既没止损也没止盈 → 提示（不是违规，只提示人工看一眼）。
STALE_POSITION_MINUTES = 240


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 - 坏 JSON 不该让审计整体失败
        return {}


def _is_option(symbol: str) -> bool:
    return bool(OPTION_RE.match(str(symbol or "")))


def _campaign_budgets(conn: sqlite3.Connection) -> dict[str, dict]:
    """租户 → {标的: campaign 配置}，用于核对「本档预算是否被放大」。"""
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT config_key, value_json FROM runtime_config WHERE config_key LIKE 'VOLATILITY_CAMPAIGNS:%'"
        ).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        tenant = str(row["config_key"]).split(":", 1)[-1]
        try:
            items = json.loads(row["value_json"])
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(items, list):
            continue
        mapping = {}
        for cfg in items:
            if not isinstance(cfg, dict):
                continue
            underlying = str(cfg.get("underlying") or "")
            if underlying:
                mapping[underlying] = cfg
        out[tenant] = mapping
    return out


def _window_orders(conn, start_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, strategy_id, symbol, side, quantity, limit_price, status,
                  metadata_json, created_at, filled_quantity, average_fill_price, tenant_id
           FROM order_intents
           WHERE created_at >= ? ORDER BY created_at""",
        (start_iso,),
    ).fetchall()


def _window_fills(conn, start_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, intent_id, symbol, side, quantity, price, fee_total, filled_at, source
           FROM fill_ledger WHERE filled_at >= ? ORDER BY filled_at""",
        (start_iso,),
    ).fetchall()


def _window_decisions(conn, start_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, symbol, decision, reason, payload_json, created_at, tenant_id
           FROM decision_logs WHERE created_at >= ? ORDER BY created_at""",
        (start_iso,),
    ).fetchall()


def _nearest_signal(conn, symbol: str, before_iso: str, lookback_minutes: int = 20) -> sqlite3.Row | None:
    since = (_parse(before_iso) - timedelta(minutes=lookback_minutes)).isoformat()
    return conn.execute(
        """SELECT id, symbol, decision, reason, payload_json, created_at
           FROM decision_logs
           WHERE symbol=? AND decision='SIGNAL_BUY' AND created_at BETWEEN ? AND ?
           ORDER BY created_at DESC LIMIT 1""",
        (symbol, since, before_iso),
    ).fetchone()


def _parse(iso: str) -> datetime:
    text = str(iso).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _book_at(conn, code: str, iso: str, tolerance_minutes: int = 5) -> tuple[float, float, str] | None:
    """按时间取最近的盘口快照 —— 买入订单本身不落 bid/ask，只能靠 option_history 还原。

    容忍 5 分钟：超过这个距离的盘口已经不能代表下单那一刻的价差，宁可标记
    「无法复核」，也不要用一份错的盘口给出一个看似精确的结论。
    """
    center = _parse(iso)
    lo = (center - timedelta(minutes=tolerance_minutes)).isoformat()
    hi = (center + timedelta(minutes=tolerance_minutes)).isoformat()
    row = conn.execute(
        """SELECT fetched_at, bid, ask FROM option_history
           WHERE code=? AND fetched_at BETWEEN ? AND ?
           ORDER BY ABS(julianday(fetched_at) - julianday(?)) ASC LIMIT 1""",
        (code, lo, hi, center.isoformat()),
    ).fetchone()
    if row is None:
        return None
    bid, ask = float(row["bid"] or 0.0), float(row["ask"] or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask, str(row["fetched_at"])


def audit_entries(conn, orders, start_iso, end_iso, budgets) -> tuple[list[dict], list[dict]]:
    """买入侧：信号是否存在、入场检查项是否达标、价差门是否通过、预算是否被放大。"""
    violations: list[dict] = []
    notes: list[dict] = []
    for row in orders:
        symbol = str(row["symbol"])
        if str(row["side"]).upper() != "BUY" or not _is_option(symbol):
            continue
        if str(row["status"]).upper() not in {"SUBMITTED", "FILLED", "FILLED_PART", "PENDING"}:
            continue
        meta = _loads(row["metadata_json"])
        tenant = row["tenant_id"] or meta.get("tenant_id") or "?"
        created = str(row["created_at"])
        base = {"symbol": symbol, "tenant": tenant, "time": created, "order_id": row["id"]}

        signal = _nearest_signal(conn, symbol, created)
        if signal is None:
            violations.append({
                **base, "code": "E_NO_SIGNAL",
                "title": "买入没有对应的入场信号",
                "detail": f"下单前 20 分钟内查不到 {symbol} 的 SIGNAL_BUY 决策记录，无法确认这笔买入是由信号触发的",
            })
            continue

        payload = _loads(signal["payload_json"])
        regime = payload.get("volatility_regime") or {}
        checks = [c for c in (regime.get("checks") or []) if isinstance(c, dict)]
        # 触发条件是「三选一」（涨跌幅 / Z值 / ATR倍数），标了「之一」的必须按 OR 判，
        # 否则 AVGO 这种「ATR 未达标但涨跌幅与 Z 值都达标」的正常入场会被误报成违规 ——
        # 误报一次，这条告警的可信度就没了。其余检查项（量比、方向一致性）是 AND。
        or_group = [c for c in checks if "之一" in str(c.get("检查项"))]
        and_group = [c for c in checks if "之一" not in str(c.get("检查项"))]
        or_pass = any(str(c.get("结果")) == "达标" for c in or_group) if or_group else True
        and_failed = [c for c in and_group if str(c.get("结果")) != "达标"]
        failed = []
        if not or_pass:
            failed.append("三个触发条件全部未满足：" + "、".join(
                f"{c.get('检查项')}={c.get('当前值')}" for c in or_group
            ))
        failed += [
            f"{c.get('检查项')}（当前 {c.get('当前值')}，要求 {c.get('目标值')}）"
            for c in and_failed
        ]
        override = bool(regime.get("strong_signal_override"))
        if failed and not override:
            violations.append({
                **base, "code": "E_CHECK_FAIL",
                "title": "入场检查项未达标却仍然买入",
                "detail": "；".join(failed) + "；且未启用强信号覆盖（strong_signal_override=false）",
            })
        elif failed and override:
            notes.append({
                **base, "code": "E_CHECK_OVERRIDE",
                "title": "入场检查项未达标，但由强信号覆盖放行",
                "detail": "；".join(failed),
            })

        # 价差门：直接复用生产闸门，避免第二套阈值
        bid = float(meta.get("bid_price") or 0.0)
        ask = float(meta.get("ask_price") or 0.0)
        qty = float(row["quantity"] or 0.0)
        mult = float(meta.get("multiplier") or 100.0)
        book_source = "订单快照"
        if (bid <= 0 or ask <= 0) and qty > 0:
            restored = _book_at(conn, symbol, created)
            if restored:
                bid, ask, src_ts = restored
                book_source = f"option_history 快照 {str(src_ts)[11:19]}"
        if bid > 0 and ask > 0 and qty > 0:
            from futu_quant.volatility_optimizer import (
                ENTRY_MAX_SPREAD_PCT,
                ENTRY_MIN_SPREAD_MULTIPLE,
                volatility_entry_spread_gate,
            )

            # 还原盘口（option_history 快照）与下单那一刻的真实盘口有偏差，
            # 因此给 10% 容差：只有在明显越线时才判违规，宁少报不误报。
            tolerance = 1.0 if book_source == "订单快照" else 1.1
            allowed, reason, detail = volatility_entry_spread_gate(
                bid=bid, ask=ask, quantity=qty, multiplier=mult,
                max_entry_spread_pct=ENTRY_MAX_SPREAD_PCT * tolerance,
                min_spread_multiple=ENTRY_MIN_SPREAD_MULTIPLE / tolerance,
            )
            if not allowed:
                violations.append({
                    **base, "code": "E_SPREAD",
                    "title": "入场价差门应当拦下这笔买入",
                    "detail": f"{reason}（买一 {bid:.2f} / 卖一 {ask:.2f}，盘口来源：{book_source}）",
                })
            elif detail.get("spread_pct") is not None:
                notes.append({
                    **base, "code": "E_SPREAD_OK",
                    "title": "价差门通过",
                    "detail": (
                        f"价差 {detail['spread_pct']:.1f}%、止损额 "
                        f"{_fmt_money(detail['stop_usd'] or 0)} / 价差 {_fmt_money(detail['spread_usd'] or 0)}"
                    ),
                })
        else:
            notes.append({
                **base, "code": "E_SPREAD_SKIP",
                "title": "订单缺少盘口快照，无法复核价差门",
                "detail": f"bid={bid} ask={ask} qty={qty}",
            })

        # 预算：本档预算 vs campaign 配置；以及档位放大是否超过 2 倍
        base_budget = meta.get("base_premium_budget")
        eff_budget = meta.get("effective_premium_budget")
        underlying = str(meta.get("underlying") or "")
        cfg = (budgets.get(tenant) or {}).get(underlying) or {}
        cfg_budget = cfg.get("max_premium_per_stage")
        if base_budget and cfg_budget:
            if float(base_budget) > float(cfg_budget) * 1.01:
                violations.append({
                    **base, "code": "E_BUDGET_BASE",
                    "title": "本档预算高于 campaign 配置值",
                    "detail": (
                        f"订单记录 base_premium_budget={_fmt_money(float(base_budget))}，"
                        f"而 {underlying} 的 campaign 配置为 {_fmt_money(float(cfg_budget))}"
                    ),
                })
        if base_budget and eff_budget and float(base_budget) > 0:
            ratio = float(eff_budget) / float(base_budget)
            if ratio > MAX_TIER_MULTIPLIER:
                violations.append({
                    **base, "code": "E_BUDGET_TIER",
                    "title": "档位放大超过 2 倍上限",
                    "detail": f"effective/base = {ratio:.2f}（上限 {MAX_TIER_MULTIPLIER:.2f}）",
                })
    return violations, notes


def _peak_ratio(conn, symbol: str, entry_iso: str, exit_iso: str) -> float | None:
    """开仓到平仓之间记录到的最大浮盈比例（策略自己写在 payload 里）。"""
    rows = conn.execute(
        """SELECT payload_json FROM decision_logs
           WHERE symbol=? AND created_at BETWEEN ? AND ?""",
        (symbol, entry_iso, exit_iso),
    ).fetchall()
    peak = None
    for row in rows:
        payload = _loads(row["payload_json"])
        for key in ("peak_pnl_ratio", "mfe_pnl_ratio", "pnl_ratio"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                peak = float(value) if peak is None else max(peak, float(value))
    return peak


def audit_exits(conn, fills, orders, start_iso, end_iso) -> tuple[list[dict], list[dict]]:
    """卖出侧：有没有退出理由、止损是否严重滑点、浮盈是否被回吐。"""
    violations: list[dict] = []
    notes: list[dict] = []
    open_sells = {
        str(r["symbol"]) for r in orders
        if str(r["side"]).upper() == "SELL" and _is_option(str(r["symbol"]))
    }
    reason_by_intent = {}
    for row in orders:
        meta = _loads(row["metadata_json"])
        if meta.get("reason"):
            reason_by_intent[int(row["id"])] = str(meta.get("reason"))

    for row in fills:
        symbol = str(row["symbol"])
        if str(row["side"]).upper() != "SELL" or not _is_option(symbol):
            continue
        filled_at = str(row["filled_at"])
        reason = reason_by_intent.get(int(row["intent_id"])) or ""
        if not reason:
            decision = conn.execute(
                """SELECT reason FROM decision_logs
                   WHERE symbol=? AND decision IN ('SELL','SIGNAL_SELL','EXIT')
                     AND created_at <= ? ORDER BY created_at DESC LIMIT 1""",
                (symbol, filled_at),
            ).fetchone()
            reason = str(decision["reason"]) if decision else ""

        base = {"symbol": symbol, "time": filled_at, "fill_id": row["id"]}
        if not reason:
            violations.append({
                **base, "code": "X_NO_REASON",
                "title": "卖出没有可追溯的退出理由",
                "detail": f"{symbol} 在 {filled_at} 被卖出，但订单与决策日志里都没有退出原因",
            })
            continue

        entry = conn.execute(
            """SELECT price, filled_at FROM fill_ledger
               WHERE symbol=? AND side='BUY' AND filled_at <= ?
               ORDER BY filled_at DESC LIMIT 1""",
            (symbol, filled_at),
        ).fetchone()
        if entry is None:
            notes.append({
                **base, "code": "X_NO_ENTRY",
                "title": "找不到对应买入成交，无法算盈亏",
                "detail": reason,
            })
            continue

        entry_price = float(entry["price"] or 0.0)
        exit_price = float(row["price"] or 0.0)
        pnl_ratio = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
        base.update({
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl_ratio": pnl_ratio, "reason": reason,
        })

        if pnl_ratio <= -SLIPPAGE_ALARM_RATIO:
            violations.append({
                **base, "code": "X_SLIPPAGE",
                "title": "硬止损严重滑点（实际亏损超过止损线 50%）",
                "detail": (
                    f"建仓 {entry_price:.2f} → 退出 {exit_price:.2f}，实际 {pnl_ratio:+.1%}；"
                    f"硬止损线 {HARD_STOP_RATIO:.0%}，超线幅度 {abs(pnl_ratio) / HARD_STOP_RATIO - 1:.0%}"
                ),
            })
        else:
            notes.append({
                **base, "code": "X_OK",
                "title": "退出符合规则",
                "detail": f"{pnl_ratio:+.1%}｜{reason[:60]}",
            })

        peak = _peak_ratio(conn, symbol, str(entry["filled_at"]), filled_at)
        if peak is not None and peak >= SOFT_PROTECT_START and pnl_ratio < SOFT_LOCK_FLOOR:
            violations.append({
                **base, "code": "X_GIVEBACK",
                "title": "浮盈曾到软保护线却未锁定，回吐后离场",
                "detail": (
                    f"最大浮盈 {peak:+.1%} ≥ 保护启动 {SOFT_PROTECT_START:.0%}，"
                    f"最终 {pnl_ratio:+.1%} < 锁利底 {SOFT_LOCK_FLOOR:.0%}｜{reason[:50]}"
                ),
            })

    # 该退没退：当前持仓已经跌破硬止损线，窗口内却没有卖出动作
    latest = conn.execute(
        """SELECT symbol, payload_json, created_at FROM decision_logs
           WHERE created_at >= ? AND created_at <= ? ORDER BY created_at""",
        ((_parse(end_iso) - timedelta(minutes=15)).isoformat(), end_iso),
    ).fetchall()
    seen = {}
    for row in latest:
        payload = _loads(row["payload_json"])
        qty = payload.get("position_quantity")
        if isinstance(qty, (int, float)) and qty > 0:
            seen[str(row["symbol"])] = (float(payload.get("pnl_ratio") or 0.0), str(row["created_at"]))
    for symbol, (pnl_ratio, at) in seen.items():
        if pnl_ratio <= -HARD_STOP_RATIO and symbol not in open_sells:
            violations.append({
                "symbol": symbol, "time": at, "code": "X_NO_EXIT",
                "title": "持仓已跌破硬止损线但窗口内没有退出动作",
                "detail": (
                    f"当前浮亏 {pnl_ratio:+.1%} ≤ -{HARD_STOP_RATIO:.0%}，"
                    f"但最近 {len(open_sells)} 笔卖出中不含该合约"
                ),
            })
    return violations, notes


def audit_stale_positions(conn, end_iso) -> list[dict]:
    """持仓时间过长且没有任何退出动作 —— 只作提示，不算违规。"""
    rows = conn.execute(
        """SELECT symbol, payload_json, created_at FROM decision_logs
           WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC""",
        ((_parse(end_iso) - timedelta(minutes=15)).isoformat(), end_iso),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        symbol = str(row["symbol"])
        if symbol in latest:
            continue
        payload = _loads(row["payload_json"])
        qty = payload.get("position_quantity")
        if isinstance(qty, (int, float)) and qty > 0:
            latest[symbol] = row
    notes = []
    for symbol, row in latest.items():
        payload = _loads(row["payload_json"])
        opened = payload.get("position_opened_at")
        if not opened:
            continue
        try:
            held = (_parse(end_iso) - _parse(str(opened))).total_seconds() / 60
        except Exception:  # noqa: BLE001
            continue
        if held >= STALE_POSITION_MINUTES:
            notes.append({
                "symbol": symbol, "code": "X_STALE", "time": str(row["created_at"]),
                "title": "持仓时间偏长且未触发退出",
                "detail": f"已持有 {held:.0f} 分钟（阈值 {STALE_POSITION_MINUTES} 分钟），当前 {float(payload.get('pnl_ratio') or 0):+.1%}",
            })
    return notes


def build_report(conn, minutes: int) -> dict:
    end = _now()
    start = end - timedelta(minutes=minutes)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    orders = _window_orders(conn, start_iso)
    fills = _window_fills(conn, start_iso)
    decisions = _window_decisions(conn, start_iso)
    budgets = _campaign_budgets(conn)

    entry_v, entry_n = audit_entries(conn, orders, start_iso, end_iso, budgets)
    exit_v, exit_n = audit_exits(conn, fills, orders, start_iso, end_iso)
    stale_n = audit_stale_positions(conn, end_iso)

    blocked = [d for d in decisions if str(d["decision"]) == "BLOCKED"]
    summary = {
        "window_start": start_iso,
        "window_end": end_iso,
        "minutes": minutes,
        "orders": len(orders),
        "buy_orders": sum(1 for r in orders if str(r["side"]).upper() == "BUY"),
        "sell_orders": sum(1 for r in orders if str(r["side"]).upper() == "SELL"),
        "fills": len(fills),
        "blocked": len(blocked),
        "blocked_reasons": sorted({str(d["reason"])[:60] for d in blocked})[:6],
    }
    return {
        "summary": summary,
        "violations": entry_v + exit_v,
        "notes": entry_n + exit_n + stale_n,
    }


def render(report: dict) -> str:
    s = report["summary"]
    start = _parse(s["window_start"]).astimezone().strftime("%H:%M")
    end = _parse(s["window_end"]).astimezone().strftime("%H:%M")
    lines = [
        "### 交易窗口合规审计",
        "──────────────────────",
        f"**窗口**: {start} - {end}（近 {s['minutes']} 分钟）",
        f"**动作**: 买入 {s['buy_orders']} 笔 / 卖出 {s['sell_orders']} 笔 / 成交 {s['fills']} 笔 / 闸门拦截 {s['blocked']} 次",
    ]
    violations = report["violations"]
    if violations:
        lines.append("")
        lines.append(f"**发现 {len(violations)} 项不符合条件**")
        for idx, item in enumerate(violations, 1):
            when = ""
            if item.get("time"):
                try:
                    when = _parse(item["time"]).astimezone().strftime("%H:%M:%S")
                except Exception:  # noqa: BLE001
                    when = str(item["time"])[:19]
            lines.append("")
            lines.append(f"**{idx}. {item['title']}｜{item['symbol']}**")
            lines.append(f"- {item['detail']}")
            lines.append(f"- 时间 {when}｜租户 {item.get('tenant') or '-'}｜编号 {item.get('order_id') or item.get('fill_id') or '-'}")
    else:
        lines.append("")
        lines.append("**✅ 窗口内买入与退出均符合条件**")

    if s["blocked_reasons"]:
        lines.append("")
        lines.append("**闸门拦截原因（正常保护）**")
        for reason in s["blocked_reasons"]:
            lines.append(f"- {reason}")
    return "\n".join(lines)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def dedupe(violations: list[dict], state: dict, window_minutes: int) -> list[dict]:
    """同一条违规在 within 分钟（= max(60, 窗口×3)）内只提醒一次。"""
    key = "sent"
    sent = state.setdefault(key, {})
    now = _now()
    within = timedelta(minutes=max(60, window_minutes * 3))
    fresh = []
    for item in violations:
        sig = f"{item['code']}|{item['symbol']}|{round(float(item.get('pnl_ratio') or 0), 3)}"
        last = sent.get(sig)
        if last:
            try:
                if now - _parse(last) < within:
                    continue
            except Exception:  # noqa: BLE001
                pass
        sent[sig] = now.isoformat()
        fresh.append(item)
    # 清理过期记录，避免状态文件无限增长
    for sig in list(sent):
        try:
            if now - _parse(sent[sig]) > timedelta(days=3):
                sent.pop(sig, None)
        except Exception:  # noqa: BLE001
            sent.pop(sig, None)
    return fresh


def push_feishu(markdown: str) -> bool:
    from futu_quant.feishu_reporter import FeishuReporter, _run_lark_json

    reporter = FeishuReporter()
    chat_id = reporter.group_chat_id or getattr(reporter, "chat_id", "") or ""
    if not chat_id:
        print("[feishu] 未配置群会话，跳过推送", file=sys.stderr)
        return False
    res = _run_lark_json([
        "lark-cli", "im", "+messages-send",
        "--chat-id", chat_id, "--as", "bot", "--markdown", markdown,
    ])
    ok = bool(res.get("ok"))
    if not ok:
        print(f"[feishu] 推送失败: {str(res)[:300]}", file=sys.stderr)
    return ok


def append_log(report: dict, pushed: int) -> None:
    """每次运行留一行轨迹 —— 计划任务没有控制台，日志是唯一的可回溯证据。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        s = report["summary"]
        line = json.dumps({
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window": [s["window_start"], s["window_end"]],
            "buy": s["buy_orders"], "sell": s["sell_orders"], "fills": s["fills"],
            "blocked": s["blocked"],
            "violations": [f"{v['code']}:{v['symbol']}" for v in report["violations"]],
            "pushed": pushed,
        }, ensure_ascii=False)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - 日志写失败不该影响审计本身
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="近 N 分钟交易窗口合规审计")
    parser.add_argument("--minutes", type=int, default=10, help="回看窗口（分钟），默认 10")
    parser.add_argument("--push", action="store_true", help="发现违规时推送飞书")
    parser.add_argument("--force-push", action="store_true", help="无论有无违规都推送（人工排查用）")
    parser.add_argument("--no-dedupe", action="store_true", help="忽略去重，强制提醒")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        report = build_report(conn, args.minutes)
    finally:
        conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8",
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render(report))

    violations = report["violations"]
    if not violations and not args.force_push:
        append_log(report, 0)
        return 0
    if not args.push and not args.force_push:
        # 纯查看模式：不推送、也不写去重状态，否则一次手工复查会把后续真实提醒吞掉
        print(f"\n[审计] {len(violations)} 项不符合条件（未推送，需 --push 才发送）")
        append_log(report, 0)
        return 2

    state = load_state()
    fresh = violations if args.no_dedupe else dedupe(violations, state, args.minutes)
    save_state(state)
    if not fresh and not args.force_push:
        print(f"\n[审计] {len(violations)} 项违规均已在去重窗口内提醒过，本次不重复推送")
        append_log(report, 0)
        return 0

    if args.force_push and not violations:
        markdown = render(report) + "\n\n（人工强制推送）"
    else:
        report_for_push = {**report, "violations": fresh}
        markdown = render(report_for_push)
    pushed = 0
    if args.push or args.force_push:
        if push_feishu(markdown):
            pushed = len(fresh)
            print(f"\n[审计] 已推送飞书（{len(fresh)} 项）")
    append_log(report, pushed)
    return 2 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
