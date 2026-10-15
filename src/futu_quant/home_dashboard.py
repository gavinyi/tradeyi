from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from .account_metrics import account_pnl_metrics
from .connectivity_protection import PROTECTION_EVENT_TYPES
from .option_lifecycle import parse_option_code
from .radar_control import status as radar_status
from .service_control import status as service_status
from .store import TradingStore

CHINA_TZ = "Asia/Shanghai"
UTC = timezone.utc
POSITIVE = "#22c55e"
NEGATIVE = "#ef4444"
NEUTRAL = "#64748b"
PALETTE = ["#60a5fa", "#a78bfa", "#22c55e", "#f59e0b", "#f472b6", "#06b6d4", "#94a3b8"]


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: object) -> pd.Timestamp:
    return pd.to_datetime(value, errors="coerce", utc=True)


def _to_china_label(value: object, include_date: bool = True) -> str:
    timestamp = _to_utc(value)
    if pd.isna(timestamp):
        return "-"
    pattern = "%m-%d %H:%M" if include_date else "%H:%M"
    return timestamp.tz_convert(CHINA_TZ).strftime(pattern)


def _money(value: object) -> str:
    number = _number(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _json_loads(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_frame(rows: list[dict] | pd.DataFrame) -> pd.DataFrame:
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if frame.empty:
        return frame
    result = frame.copy()
    for column in result.columns:
        if result[column].dtype == "object":
            result[column] = result[column].map(
                lambda value: json.dumps(value, ensure_ascii=False, default=str)
                if isinstance(value, (dict, list, tuple, set))
                else value
            )
    return result


def _is_option_symbol(symbol: object) -> bool:
    return parse_option_code(str(symbol)) is not None


def _position_amount(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    for column in ("market_val", "val", "amount"):
        if column in frame.columns:
            return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())
    if {"qty", "nominal_price"}.issubset(frame.columns):
        quantity = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
        price = pd.to_numeric(frame["nominal_price"], errors="coerce").fillna(0.0)
        multiplier = frame.get("code", pd.Series(index=frame.index, dtype=str)).map(
            lambda value: 100 if _is_option_symbol(value) else 1
        )
        return float((quantity * price * multiplier).sum())
    return 0.0


def _filter_recent(frame: pd.DataFrame, time_column: str, hours: int) -> pd.DataFrame:
    if frame.empty or time_column not in frame.columns:
        return pd.DataFrame()
    result = frame.copy()
    result["_ts"] = pd.to_datetime(result[time_column], errors="coerce", utc=True)
    cutoff = pd.Timestamp(_now_utc() - timedelta(hours=hours))
    return result[result["_ts"].ge(cutoff)].copy()


def _latest_session_decisions(decisions: pd.DataFrame) -> pd.DataFrame:
    if decisions.empty or "created_at" not in decisions.columns:
        return pd.DataFrame()
    result = decisions.copy()
    result["_ts"] = pd.to_datetime(result["created_at"], errors="coerce", utc=True)
    timestamps = result["_ts"].dropna()
    if timestamps.empty:
        return pd.DataFrame()
    latest = timestamps.max()
    session_start = latest.floor("D") - pd.Timedelta(hours=6)
    if latest.hour < 12:
        session_start = latest.floor("D") - pd.Timedelta(hours=12)
    return result[result["_ts"].ge(session_start)].copy()


@st.cache_data(ttl=8, max_entries=4, show_spinner=False)
def _load_store_snapshot() -> dict[str, pd.DataFrame]:
    from .store import TradingStore

    store = TradingStore()
    return {
        "heartbeats": _safe_frame(store.heartbeats()),
        "tasks": _safe_frame(store.task_runs(limit=1000)),
        "decisions": _safe_frame(store.decisions(limit=1500)),
        "intents": _safe_frame(store.intents(limit=1000)),
        "events": _safe_frame(store.operational_events(limit=500)),
        "alerts": _safe_frame(store.alerts(limit=200)),
        "equity": _safe_frame(store.equity_history(limit=1000)),
        "incidents": _safe_frame(store.incidents(status="OPEN", limit=100)),
        "shadow_positions": _safe_frame(store.shadow_open_positions()),
        "budgets": _safe_frame(store.strategy_risk_budgets()),
        "virtual_positions": _safe_frame(store.virtual_positions()),
    }


@st.cache_data(ttl=15, max_entries=4, show_spinner=False)
def _account_snapshot(_broker, view_env: str = "DEFAULT") -> dict[str, Any]:
    store = TradingStore()
    if view_env == "SIMULATE":
        info_snap = store.broker_snapshot("account_info_sim") or store.broker_snapshot("account_info")
        pos_snap = store.broker_snapshot("positions_sim") or store.broker_snapshot("positions")
        tag = "模拟盘"
    elif view_env == "REAL":
        info_snap = store.broker_snapshot("account_info_real") or store.broker_snapshot("account_info")
        pos_snap = store.broker_snapshot("positions_real") or store.broker_snapshot("positions")
        tag = "实盘"
    else:
        info_snap = store.broker_snapshot("account_info")
        pos_snap = store.broker_snapshot("positions")
        tag = "当前环境"

    info = pd.DataFrame((info_snap or {}).get("payload", []))
    positions = pd.DataFrame((pos_snap or {}).get("payload", []))
    captured = [item["captured_at"] for item in [info_snap, pos_snap] if item]
    captured_at = max(captured) if captured else "无"
    if info.empty and positions.empty:
        return {"ok": False, "error": f"暂无{tag}券商快照；等待交易服务刷新", "info": info, "positions": positions}
    return {
        "ok": True,
        "error": f"使用{tag}交易快照（{captured_at}）",
        "info": info,
        "positions": positions,
    }


def _latest_data_age(snapshot: dict[str, pd.DataFrame]) -> tuple[str, str]:
    candidates: list[pd.Timestamp] = []
    for key, column in (("heartbeats", "updated_at"), ("equity", "created_at")):
        frame = snapshot[key]
        if not frame.empty and column in frame.columns:
            parsed = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
            if not parsed.empty:
                candidates.append(parsed.max())
    if not candidates:
        return "无数据", "off"
    age = max(0.0, (_now_utc() - max(candidates).to_pydatetime()).total_seconds())
    if age <= 90:
        return f"{age:.0f} 秒前", "ok"
    if age <= 600:
        return f"{age / 60:.1f} 分钟前", "warn"
    return f"{age / 60:.0f} 分钟前", "bad"


def _service_snapshot(snapshot: dict[str, pd.DataFrame]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        service = service_status()
        rows.append({
            "label": "统一交易服务",
            "state": "ok" if service.running else "bad",
            "detail": "运行中" if service.running else "未运行",
        })
    except Exception:
        rows.append({"label": "统一交易服务", "state": "bad", "detail": "读取失败"})
    try:
        radar = radar_status()
        rows.append({
            "label": "独立雷达",
            "state": "ok" if radar.running else "bad",
            "detail": "运行中" if radar.running else "未运行",
        })
    except Exception:
        rows.append({"label": "独立雷达", "state": "bad", "detail": "读取失败"})

    data_detail, data_state = _latest_data_age(snapshot)
    rows.append({"label": "最新数据", "state": data_state, "detail": data_detail})
    recent_tasks = _filter_recent(snapshot["tasks"], "started_at", 24)
    failed = (
        int(recent_tasks["status"].astype(str).str.upper().isin({"FAILED", "TIMEOUT"}).sum())
        if not recent_tasks.empty and "status" in recent_tasks.columns
        else 0
    )
    rows.append({
        "label": "24小时任务失败",
        "state": "ok" if failed == 0 else "warn" if failed < 5 else "bad",
        "detail": f"{failed} 次",
    })
    return rows


def _render_status_strip(snapshot: dict[str, pd.DataFrame]) -> None:
    colors = {
        "ok": ("#14532d", "#86efac", "正常"),
        "warn": ("#713f12", "#fde68a", "注意"),
        "bad": ("#7f1d1d", "#fecaca", "异常"),
        "off": ("#374151", "#d1d5db", "离线"),
    }
    with st.container(horizontal=True):
        for row in _service_snapshot(snapshot):
            bg, fg, status_label = colors[row["state"]]
            with st.container(border=True):
                st.caption(row["label"])
                st.markdown(
                    f"<span style='color:{fg};background:{bg};padding:3px 9px;border-radius:999px;"
                    f"font-weight:700'>{status_label}</span>&nbsp;&nbsp;{row['detail']}",
                    unsafe_allow_html=True,
                )


def _opend_incident(alerts: pd.DataFrame, account_ok: bool = True) -> dict[str, Any] | None:
    """Summarize the latest OpenD outage without treating recovered history as live."""
    if alerts.empty or not {"event_type", "created_at"}.issubset(alerts.columns):
        return None
    frame = alerts.copy()
    frame["_ts"] = pd.to_datetime(frame["created_at"], errors="coerce", utc=True)
    event_type = frame["event_type"].astype(str).str.upper()
    message = frame.get("message", pd.Series("", index=frame.index)).astype(str)
    outage_mask = (
        event_type.isin({"OPEND_UNAVAILABLE", "EQUITY_SNAPSHOT_FAILED"})
        | message.str.contains("OpenD.*不可|OpenD.*断|Connect timeout", case=False, regex=True, na=False)
    )
    recovery_mask = (
        event_type.str.contains("RECOVERED", na=False)
        & (event_type.str.contains("EQUITY|OPEND|BROKER", na=False) | message.str.contains("账户权益|OpenD", na=False))
    )
    outages = frame[outage_mask & frame["_ts"].notna()].sort_values("_ts")
    if outages.empty:
        return None
    latest_outage = outages.iloc[-1]
    outage_time = latest_outage["_ts"]
    recoveries = frame[recovery_mask & frame["_ts"].gt(outage_time)].sort_values("_ts")
    recovery_time = recoveries.iloc[0]["_ts"] if not recoveries.empty else pd.NaT
    age_hours = (_now_utc() - outage_time.to_pydatetime()).total_seconds() / 3600
    if age_hours > 48:
        return None

    # 若当前券商快照完全正常，说明底层连接早已恢复；若中断已发生超过 1 小时，完全无需在首页报错
    if account_ok:
        if age_hours > 1.0:
            return None
        recovered = True
        if pd.isna(recovery_time):
            recovery_time = _to_utc(_now_utc())
    else:
        recovered = not pd.isna(recovery_time)

    duration = (
        max(0.0, (recovery_time - outage_time).total_seconds())
        if recovered else max(0.0, (_to_utc(_now_utc()) - outage_time).total_seconds())
    )
    return {
        "recovered": recovered,
        "outage_time": outage_time,
        "recovery_time": recovery_time,
        "duration_minutes": duration / 60,
        "message": str(latest_outage.get("message") or "富途 OpenD 暂不可用"),
    }


def latest_strategy_interruption_from_frame(events: pd.DataFrame) -> dict[str, Any] | None:
    if events.empty or not {"event_type", "created_at"}.issubset(events.columns):
        return None
    frame = events.copy()
    frame["_ts"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
    frame = frame[frame["_ts"].notna()].copy()
    if frame.empty:
        return None
    cutoff = pd.Timestamp.now(tz=UTC) - pd.Timedelta(hours=48)
    frame = frame[frame["_ts"].ge(cutoff)]
    if frame.empty:
        return None
    event_type = frame["event_type"].astype(str).str.upper()
    start_mask = event_type.isin(PROTECTION_EVENT_TYPES) | event_type.eq("BROKER_CONNECTIVITY_FAILURE")
    recovery_mask = event_type.isin({"PROTECTIVE_RECOVERY_CONFIRMED", "BROKER_CONNECTIVITY_RECOVERED"})
    candidates = frame[start_mask].sort_values("_ts")
    if candidates.empty:
        return None
    latest = candidates.iloc[-1]
    latest_ts = latest["_ts"]
    related = candidates[candidates["_ts"].le(latest_ts)].tail(50)
    start_ts = latest_ts
    failures = 0
    for _, row in reversed(list(related.iterrows())):
        row_ts = row["_ts"]
        if (start_ts - row_ts).total_seconds() > 600:
            break
        start_ts = row_ts
        if str(row.get("event_type", "")).upper() == "BROKER_CONNECTIVITY_FAILURE":
            failures += 1
    recoveries = frame[recovery_mask & frame["_ts"].gt(latest_ts)].sort_values("_ts")
    recovered = not recoveries.empty
    end_ts = recoveries.iloc[0]["_ts"] if recovered else pd.Timestamp.now(tz=UTC)
    payload = _json_loads(latest.get("payload_json"))
    return {
        "started_at_china": _to_china_label(start_ts),
        "last_at_china": _to_china_label(latest_ts),
        "duration_minutes": max(0.0, (end_ts - start_ts).total_seconds() / 60),
        "failures": max(failures, int(_number(payload.get("failures"), 0))),
        "open_option_count": int(_number(payload.get("open_option_count"), 0)),
        "task": str(payload.get("task") or payload.get("trigger_task") or ""),
        "message": str(payload.get("message") or payload.get("reason") or ""),
        "recovered": recovered,
    }


def _large_loss_positions(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty or not {"code", "qty", "pl_val"}.issubset(positions.columns):
        return pd.DataFrame()
    frame = positions.copy()
    frame["数量"] = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
    frame["累计亏损"] = pd.to_numeric(frame["pl_val"], errors="coerce").fillna(0.0)
    frame["盈亏比例"] = pd.to_numeric(frame.get("pl_ratio", 0), errors="coerce").fillna(0.0)
    frame = frame[
        frame["数量"].abs().gt(1e-9)
        & frame["累计亏损"].lt(0)
        & (frame["累计亏损"].le(-300) | frame["盈亏比例"].le(-10))
    ].copy()
    if frame.empty:
        return frame
    frame["资产类型"] = frame["code"].map(lambda value: "期权" if _is_option_symbol(value) else "股票/ETF")
    return frame.sort_values("累计亏损").head(5)


def _duration_label(minutes: float) -> str:
    if minutes < 60:
        return f"约 {minutes:.0f} 分钟"
    return f"约 {minutes / 60:.1f} 小时"


def _render_attention_center(
    snapshot: dict[str, pd.DataFrame], account: dict[str, Any], account_page=None,
) -> None:
    """Put actionable exceptions above normal dashboard analytics."""
    incident = _opend_incident(snapshot["alerts"], account_ok=account["ok"])
    interruption = latest_strategy_interruption_from_frame(snapshot["events"])
    current_broker_fault = not account["ok"]
    open_incidents = len(snapshot["incidents"])
    needs_attention = (
        current_broker_fault or incident is not None
        or interruption is not None or open_incidents > 0
    )

    if not needs_attention:
        st.success("🟢 系统运行平稳 · 富途 OpenD 在线 · 无活跃风险事件", icon=":material/verified:")
        return

    with st.container(border=True):
        st.subheader("需要关注", anchor=False)
        if current_broker_fault:
            st.error(
                f"当前无法读取富途账户，持仓止盈止损和盈亏监控可能失去实时数据：{account['error']}",
                icon=":material/wifi_off:",
            )
        if interruption is not None:
            duration_label = _duration_label(interruption["duration_minutes"])
            open_options = interruption["open_option_count"]
            task = interruption["task"] or "券商/行情链路"
            if interruption["recovered"]:
                st.warning(
                    f"策略中断已恢复：{interruption['started_at_china']} 开始，"
                    f"最后异常 {interruption['last_at_china']}，影响 {duration_label}。"
                    f"当时存在 {open_options} 个未平期权，触发来源：{task}。",
                    icon=":material/history:",
                )
            else:
                st.error(
                    f"策略中断：自 {interruption['started_at_china']} 起，券商/行情连接异常已持续 "
                    f"{duration_label}；当时存在 {open_options} 个未平期权，系统已进入持仓保护恢复。",
                    icon=":material/report:",
                )
        if incident is not None:
            outage_label = _to_china_label(incident["outage_time"])
            duration_label = _duration_label(incident["duration_minutes"])
            if incident["recovered"] and not current_broker_fault:
                recovery_label = _to_china_label(incident["recovery_time"])
                st.info(
                    f"历史波动记录：{outage_label} 曾发生短时 OpenD 波动，{recovery_label} 已恢复正常（影响 {duration_label}）。",
                    icon=":material/history:",
                )
            else:
                st.error(
                    f"OpenD 自 {outage_label} 起异常，已持续 {duration_label}，等待网关恢复。",
                    icon=":material/link_off:",
                )

        if open_incidents > 0:
            c_msg, c_act = st.columns([1, 0.28], vertical_alignment="center")
            with c_msg:
                st.warning(f"系统有 {open_incidents} 个未归档历史事件记录。", icon=":material/info:")
            with c_act:
                if st.button("一键归档所有事件", key="dismiss_incidents_btn", width="stretch"):
                    from .store import TradingStore
                    TradingStore().recover_all_incidents()
                    st.rerun()


def _render_loss_attention(positions: pd.DataFrame, account_page=None) -> None:
    losses = _large_loss_positions(positions)
    if losses.empty:
        return
    total_loss = float(losses["累计亏损"].sum())
    stock_losses = losses[losses["资产类型"] == "股票/ETF"]
    opt_losses = losses[losses["资产类型"] == "期权"]
    stock_loss_sum = float(stock_losses["累计亏损"].sum()) if not stock_losses.empty else 0.0
    opt_loss_sum = float(opt_losses["累计亏损"].sum()) if not opt_losses.empty else 0.0

    with st.container(border=True):
        st.subheader("持仓亏损分布")
        breakdown_msg = []
        if not stock_losses.empty:
            breakdown_msg.append(f"股票/ETF {len(stock_losses)} 只（合计 {_money(stock_loss_sum)}）")
        if not opt_losses.empty:
            breakdown_msg.append(f"期权 {len(opt_losses)} 张（合计 {_money(opt_loss_sum)}）")
        else:
            breakdown_msg.append("期权无大额亏损合约")

        st.error(
            f"大额亏损持仓共 {len(losses)} 个，合计 {_money(total_loss)}。其中：" + "；".join(breakdown_msg) + "。",
            icon=":material/trending_down:",
        )
        loss_chart_frame = losses.copy()
        loss_chart_frame["亏损金额"] = loss_chart_frame["累计亏损"].abs()
        loss_chart_frame["代码显示"] = loss_chart_frame["code"].astype(str)
        loss_chart = alt.Chart(loss_chart_frame).mark_bar(
            cornerRadiusEnd=5,
        ).encode(
            x=alt.X("亏损金额:Q", title="亏损金额（美元）", axis=alt.Axis(format="$,.0f")),
            y=alt.Y("代码显示:N", title=None, sort="-x"),
            color=alt.Color(
                "资产类型:N",
                title="资产类型",
                scale=alt.Scale(domain=["股票/ETF", "期权"], range=["#38bdf8", "#f472b6"]),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=[
                alt.Tooltip("代码显示:N", title="代码"),
                alt.Tooltip("资产类型:N", title="资产类型"),
                alt.Tooltip("累计亏损:Q", title="累计亏损", format="$,.2f"),
                alt.Tooltip("盈亏比例:Q", title="盈亏比例", format=".1f"),
                alt.Tooltip("数量:Q", title="持仓数量", format=".2f"),
            ],
        ).properties(height=max(150, min(260, 54 * len(loss_chart_frame))))
        st.altair_chart(loss_chart, width="stretch")
        if account_page is not None:
            st.page_link(
                account_page, label="查看持仓与成本详情",
                icon=":material/account_balance_wallet:",
            )


def _equity_view(equity: pd.DataFrame) -> pd.DataFrame:
    if equity.empty or not {"created_at", "total_assets"}.issubset(equity.columns):
        return pd.DataFrame()
    frame = equity.copy()
    frame["时间"] = pd.to_datetime(frame["created_at"], errors="coerce", utc=True).dt.tz_convert(CHINA_TZ)
    frame["权益"] = pd.to_numeric(frame["total_assets"], errors="coerce")
    frame = frame.dropna(subset=["时间", "权益"]).sort_values("时间")
    if frame.empty:
        return frame
    cutoff = frame["时间"].max() - pd.Timedelta(hours=24)
    frame = frame[frame["时间"].ge(cutoff)].tail(600).copy()
    frame["区间盈亏"] = frame["权益"] - frame["权益"].iloc[0]
    frame["峰值"] = frame["权益"].cummax()
    frame["回撤"] = frame["权益"] / frame["峰值"] - 1
    return frame


def _render_account_kpis(account: dict[str, Any], snapshot: dict[str, pd.DataFrame]) -> None:
    info = account["info"]
    positions = account["positions"]
    row = info.iloc[0] if not info.empty else {}
    pnl = account_pnl_metrics(positions) if not positions.empty else {
        "today_position_pnl": 0.0,
        "total_position_pnl": 0.0,
    }
    equity_view = _equity_view(snapshot["equity"])
    equity_sparkline = equity_view["权益"].tolist()[-80:] if not equity_view.empty else []
    pnl_sparkline = equity_view["区间盈亏"].tolist()[-80:] if not equity_view.empty else []
    total_assets = _number(row.get("total_assets"))
    cash = _number(row.get("cash"))
    cash_ratio = cash / total_assets if total_assets > 0 else 0.0

    stock_count = 0
    option_count = 0
    stock_today_pnl = 0.0
    stock_total_pnl = 0.0
    stock_mv = 0.0
    opt_today_pnl = 0.0
    opt_total_pnl = 0.0
    opt_mv = 0.0

    if not positions.empty and "code" in positions.columns:
        option_mask = positions["code"].map(_is_option_symbol)
        stock_df = positions[~option_mask]
        opt_df = positions[option_mask]

        # 仅统计当前数量 > 0 的有效持仓，排除券商当天已平仓归零记录
        stock_qty = pd.to_numeric(stock_df.get("qty", 0), errors="coerce").fillna(0)
        opt_qty = pd.to_numeric(opt_df.get("qty", 0), errors="coerce").fillna(0)
        stock_count = int((stock_qty > 0).sum())
        option_count = int((opt_qty > 0).sum())

        stock_today_pnl = float(pd.to_numeric(stock_df.get("today_pl_val", 0), errors="coerce").fillna(0).sum())
        stock_total_pnl = float(pd.to_numeric(stock_df.get("pl_val", 0), errors="coerce").fillna(0).sum())
        stock_mv = _position_amount(stock_df[stock_qty > 0])

        opt_today_pnl = float(pd.to_numeric(opt_df.get("today_pl_val", 0), errors="coerce").fillna(0).sum())
        opt_total_pnl = float(pd.to_numeric(opt_df.get("pl_val", 0), errors="coerce").fillna(0).sum())
        opt_mv = _position_amount(opt_df[opt_qty > 0])

    with st.container(horizontal=True):
        st.metric(
            "账户总权益", _money(total_assets), border=True,
            chart_data=equity_sparkline or None, chart_type="line",
        )
        st.metric(
            "当日浮动盈亏", _money(pnl["today_position_pnl"]), border=True,
            delta=f"股票 {stock_today_pnl:+,.0f} · 期权 {opt_today_pnl:+,.0f}",
            delta_color="normal",
        )
        st.metric(
            "总持仓浮动盈亏", _money(pnl["total_position_pnl"]), border=True,
            delta=f"股票 {stock_total_pnl:+,.0f} · 期权 {opt_total_pnl:+,.0f}",
            chart_data=pnl_sparkline or None, chart_type="line",
        )
        st.metric("现金缓冲", f"{cash_ratio:.1%}", _money(cash), border=True)
        st.metric(
            "当前在仓标的", f"{stock_count + option_count} 个",
            f"股票 {stock_count} 只 · 期权 {option_count} 张",
            border=True,
        )

    # 股票与期权盈亏及亏损独立分拆栏
    col_stock, col_option = st.columns(2)
    with col_stock:
        with st.container(border=True):
            st.markdown("##### 📈 股票/ETF 现货底仓盈亏")
            with st.container(horizontal=True):
                st.metric("在仓标的", f"{stock_count} 只", border=True)
                st.metric("现货总市值", _money(stock_mv), border=True)
                st.metric(
                    "当日盈亏", _money(stock_today_pnl),
                    f"{stock_today_pnl:+,.2f}", delta_color="normal", border=True,
                )
                st.metric(
                    "累计持仓盈亏", _money(stock_total_pnl),
                    f"{stock_total_pnl:+,.2f}", delta_color="normal", border=True,
                )
    with col_option:
        with st.container(border=True):
            st.markdown("##### ⚡ 波动率期权战区盈亏")
            with st.container(horizontal=True):
                st.metric("在仓合约", f"{option_count} 张", border=True)
                st.metric("权利金市值", _money(opt_mv), border=True)
                st.metric(
                    "当日盈亏", _money(opt_today_pnl),
                    f"{opt_today_pnl:+,.2f}", delta_color="normal", border=True,
                )
                st.metric(
                    "累计持仓盈亏", _money(opt_total_pnl),
                    f"{opt_total_pnl:+,.2f}", delta_color="normal", border=True,
                )


def _pnl_chart(frame: pd.DataFrame) -> alt.Chart:
    base = alt.Chart(frame).encode(
        x=alt.X("时间:T", title=None, axis=alt.Axis(format="%m-%d %H:%M", labelOverlap=True)),
        y=alt.Y("区间盈亏:Q", title="盈亏（美元）", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("时间:T", title="时间"), alt.Tooltip("区间盈亏:Q", title="区间盈亏", format="$,.2f")],
    )
    area = base.mark_area(opacity=0.18).encode(
        color=alt.condition("datum['区间盈亏'] >= 0", alt.value(POSITIVE), alt.value(NEGATIVE))
    )
    line = base.mark_line(strokeWidth=2.4).encode(
        color=alt.condition("datum['区间盈亏'] >= 0", alt.value(POSITIVE), alt.value(NEGATIVE))
    )
    zero = alt.Chart(pd.DataFrame({"基准": [0]})).mark_rule(color=NEUTRAL, strokeDash=[4, 4]).encode(y="基准:Q")
    return (area + line + zero).properties(height=310)


def _render_equity_pnl(equity: pd.DataFrame) -> None:
    frame = _equity_view(equity)
    with st.container(border=True):
        st.subheader("最近运行区间盈亏")
        if frame.empty:
            st.info("还没有权益快照，统一交易服务产生数据后会自动显示。")
            return
        latest_pnl = float(frame["区间盈亏"].iloc[-1])
        worst_drawdown = float(frame["回撤"].min())
        st.caption(
            f"最近 24 小时（以最新有效快照为终点） · 当前 {_money(latest_pnl)} · "
            f"区间最大回撤 {worst_drawdown:.2%}"
        )
        st.altair_chart(_pnl_chart(frame), width="stretch")


def _asset_pnl_rows(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty or "code" not in positions.columns:
        return pd.DataFrame()
    option_mask = positions["code"].map(_is_option_symbol)
    rows: list[dict[str, object]] = []
    for label, frame in (("股票/ETF", positions[~option_mask]), ("期权", positions[option_mask])):
        for scope, column in (("当日", "today_pl_val"), ("累计浮动", "pl_val")):
            value = (
                float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())
                if column in frame.columns else 0.0
            )
            rows.append({"项目": f"{label} · {scope}", "盈亏": value, "方向": "盈利" if value >= 0 else "亏损"})
    return pd.DataFrame(rows)


def _render_pnl_contribution(positions: pd.DataFrame) -> None:
    with st.container(border=True):
        st.subheader("当前盈亏贡献")
        frame = _asset_pnl_rows(positions)
        if frame.empty:
            st.info("当前没有可展示的券商持仓。")
            return
        chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=5).encode(
            x=alt.X("盈亏:Q", title="盈亏（美元）"),
            y=alt.Y("项目:N", title=None, sort="-x"),
            color=alt.Color(
                "方向:N", title=None,
                scale=alt.Scale(domain=["盈利", "亏损"], range=[POSITIVE, NEGATIVE]),
                legend=None,
            ),
            tooltip=["项目:N", alt.Tooltip("盈亏:Q", format="$,.2f")],
        ).properties(height=310)
        zero = alt.Chart(pd.DataFrame({"基准": [0]})).mark_rule(color=NEUTRAL).encode(x="基准:Q")
        st.altair_chart(chart + zero, width="stretch")


def _donut(frame: pd.DataFrame, category: str, value: str, title: str) -> alt.Chart:
    return alt.Chart(frame).mark_arc(innerRadius=65, outerRadius=105).encode(
        theta=alt.Theta(f"{value}:Q", stack=True),
        color=alt.Color(
            f"{category}:N", title=title,
            scale=alt.Scale(range=PALETTE),
            legend=alt.Legend(orient="bottom", columns=2, labelLimit=160),
        ),
        tooltip=[alt.Tooltip(f"{category}:N", title=title), alt.Tooltip(f"{value}:Q", format="$,.2f")],
    ).properties(height=285)


def _asset_allocation(account: dict[str, Any]) -> pd.DataFrame:
    info = account["info"]
    positions = account["positions"]
    row = info.iloc[0] if not info.empty else {}
    cash = max(0.0, _number(row.get("cash")))
    if positions.empty or "code" not in positions.columns:
        return pd.DataFrame([{"资产": "现金", "金额": cash}])
    option_mask = positions["code"].map(_is_option_symbol)
    rows = [
        {"资产": "现金", "金额": cash},
        {"资产": "股票/ETF", "金额": abs(_position_amount(positions[~option_mask]))},
        {"资产": "期权", "金额": abs(_position_amount(positions[option_mask]))},
    ]
    return pd.DataFrame([row for row in rows if _number(row["金额"]) > 0])


def _strategy_exposure(snapshot: dict[str, pd.DataFrame], positions: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    virtual = snapshot["virtual_positions"]
    if not virtual.empty and not positions.empty and "code" in positions.columns:
        broker_map = positions.set_index("code").to_dict("index")
        rows: list[dict[str, object]] = []
        for _, item in virtual.iterrows():
            quantity = _number(item.get("quantity"))
            symbol = str(item.get("symbol", ""))
            broker_row = broker_map.get(symbol, {})
            price = _number(broker_row.get("nominal_price"), _number(broker_row.get("last_price")))
            exposure = abs(quantity * price * (100 if _is_option_symbol(symbol) else 1))
            if exposure > 0:
                rows.append({"策略": str(item.get("strategy_id") or "未归属"), "金额": exposure})
        if rows:
            result = pd.DataFrame(rows).groupby("策略", as_index=False)["金额"].sum()
            return result.sort_values("金额", ascending=False), "当前策略持仓敞口"

    budgets = snapshot["budgets"]
    if not budgets.empty:
        enabled = budgets[pd.to_numeric(budgets.get("enabled", 0), errors="coerce").fillna(0).gt(0)].copy()
        enabled["金额"] = pd.to_numeric(enabled.get("capital_ratio", 0), errors="coerce").fillna(0.0) * 100
        enabled = enabled[enabled["金额"].gt(0)].rename(columns={"strategy_id": "策略"})
        if not enabled.empty:
            return enabled[["策略", "金额"]], "暂无持仓，显示策略资金预算（%）"
    return pd.DataFrame(), "策略分布"


def _render_distribution(snapshot: dict[str, pd.DataFrame], account: dict[str, Any]) -> None:
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.subheader("资产当前分布")
            assets = _asset_allocation(account)
            if assets.empty:
                st.info("暂无资产分布数据。")
            else:
                st.altair_chart(_donut(assets, "资产", "金额", "资产"), width="stretch")
    with right:
        with st.container(border=True):
            strategy, label = _strategy_exposure(snapshot, account["positions"])
            st.subheader(label)
            if strategy.empty:
                st.info("暂无可归属的策略敞口。")
            else:
                st.altair_chart(_donut(strategy, "策略", "金额", "策略"), width="stretch")


def _render_decision_flow(decisions: pd.DataFrame) -> None:
    session = _latest_session_decisions(decisions)
    with st.container(border=True):
        st.subheader("最近交易时段策略漏斗")
        if session.empty or "decision" not in session.columns:
            st.info("暂无策略决策数据。")
            return
        names = {
            "BUY": "买入", "SELL": "卖出", "EXIT": "退出", "HOLD": "观察",
            "BLOCKED": "阻断", "REJECTED": "拒绝", "SKIP": "跳过",
        }
        frame = (
            session.assign(阶段=session["decision"].astype(str).str.upper().map(names).fillna("其他"))
            .groupby("阶段", as_index=False).size().rename(columns={"size": "次数"})
        )
        color_scale = alt.Scale(
            domain=["买入", "卖出", "退出", "观察", "阻断", "拒绝", "跳过", "其他"],
            range=[POSITIVE, "#38bdf8", "#a78bfa", "#f59e0b", NEGATIVE, "#fb7185", NEUTRAL, "#94a3b8"],
        )
        chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=5).encode(
            x=alt.X("次数:Q", title="决策次数"),
            y=alt.Y("阶段:N", title=None, sort="-x"),
            color=alt.Color("阶段:N", scale=color_scale, legend=None),
            tooltip=["阶段:N", "次数:Q"],
        ).properties(height=270)
        st.altair_chart(chart, width="stretch")


def _render_strategy_activity(snapshot: dict[str, pd.DataFrame]) -> None:
    heartbeats = snapshot["heartbeats"]
    with st.container(border=True):
        st.subheader("策略当前运行分布")
        if heartbeats.empty or "status" not in heartbeats.columns:
            st.info("暂无策略心跳数据。")
            return
        latest = heartbeats.copy()
        if "strategy_id" in latest.columns:
            latest = latest.drop_duplicates("strategy_id", keep="last")
        status = latest["status"].astype(str).str.upper()
        latest["状态"] = status.map({
            "HEALTHY": "健康", "RUNNING": "运行中", "WARNING": "提醒",
            "ERROR": "异常", "FAILED": "失败", "STOPPED": "已停止",
        }).fillna("其他")
        frame = latest.groupby("状态", as_index=False).size().rename(columns={"size": "策略数"})
        chart = alt.Chart(frame).mark_arc(innerRadius=58, outerRadius=100).encode(
            theta="策略数:Q",
            color=alt.Color(
                "状态:N", title="运行状态",
                scale=alt.Scale(
                    domain=["健康", "运行中", "提醒", "异常", "失败", "已停止", "其他"],
                    range=[POSITIVE, "#38bdf8", "#f59e0b", NEGATIVE, "#be123c", NEUTRAL, "#94a3b8"],
                ),
                legend=alt.Legend(orient="bottom"),
            ),
            tooltip=["状态:N", "策略数:Q"],
        ).properties(height=270)
        st.altair_chart(chart, width="stretch")


def _render_risk_pulse(snapshot: dict[str, pd.DataFrame]) -> None:
    alerts = _filter_recent(snapshot["alerts"], "created_at", 24)
    incidents = snapshot["incidents"]
    with st.container(border=True):
        st.subheader("风险脉搏")
        open_incidents = len(incidents)
        alert_count = len(alerts)
        with st.container(horizontal=True):
            st.metric("24小时告警", alert_count, border=True)
            st.metric("未恢复事件", open_incidents, border=True)
        if alerts.empty or "severity" not in alerts.columns:
            st.success("最近 24 小时没有风险告警。")
            return
        frame = alerts.assign(
            级别=alerts["severity"].astype(str).str.upper().map({
                "INFO": "信息", "WARNING": "提醒", "WARN": "提醒",
                "ERROR": "异常", "CRITICAL": "严重",
            }).fillna("其他")
        ).groupby("级别", as_index=False).size().rename(columns={"size": "数量"})
        chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=5).encode(
            x=alt.X("数量:Q", title="告警数量"),
            y=alt.Y("级别:N", title=None, sort="-x"),
            color=alt.Color(
                "级别:N", legend=None,
                scale=alt.Scale(
                    domain=["信息", "提醒", "异常", "严重", "其他"],
                    range=["#38bdf8", "#f59e0b", NEGATIVE, "#be123c", NEUTRAL],
                ),
            ),
            tooltip=["级别:N", "数量:Q"],
        ).properties(height=145)
        st.altair_chart(chart, width="stretch")


@st.fragment(run_every="30s")
def _render_live_dashboard(_broker, _account_page=None) -> None:
    snapshot = _load_store_snapshot()

    # 双轨战区一键切换选择器
    is_real = getattr(_broker.settings, "trading_env", "SIMULATE") == "REAL"
    if is_real:
        col_view1, col_view2 = st.columns([1.2, 1], vertical_alignment="center")
        with col_view1:
            view_channel = st.radio(
                "双轨战区切换",
                ["🔴 实盘真钱战区", "🟢 100万模拟实验战区"],
                horizontal=True,
                label_visibility="collapsed",
                key="home_dual_track_view",
            )
        with col_view2:
            st.caption("双轨并发运行中：实盘真金受控打猎，模拟盘大资金长线孵化，两边数据独立呈现。")
        selected_env = "REAL" if "实盘" in view_channel else "SIMULATE"
    else:
        selected_env = "SIMULATE"

    account = _account_snapshot(_broker, view_env=selected_env)

    if not account["ok"]:
        st.warning(f"账户快照暂不可用，系统与策略图仍可查看：{account['error']}")

    _render_attention_center(snapshot, account, _account_page)
    _render_status_strip(snapshot)
    
    header_title = "账户与持仓总览 · 🔴 实盘真金白银战区" if selected_env == "REAL" else "账户与持仓总览 · 🟢 100万模拟实验战区"
    st.subheader(header_title)
    st.caption("全景双轨资产核算 · 股票与期权盈亏独立区分；所有数据每 10 秒自动刷新。")
    _render_account_kpis(account, snapshot)

    left, right = st.columns([1.45, 1])
    with left:
        _render_equity_pnl(snapshot["equity"])
    with right:
        _render_loss_attention(account["positions"], _account_page)

    _render_pnl_contribution(account["positions"])
    _render_distribution(snapshot, account)

    left, middle, right = st.columns([1.1, 0.9, 1])
    with left:
        _render_decision_flow(snapshot["decisions"])
    with middle:
        _render_strategy_activity(snapshot)
    with right:
        _render_risk_pulse(snapshot)


def _render_quick_links(account_page=None) -> None:
    with st.container(border=True):
        st.subheader("需要明细或操作？")
        st.caption("驾驶舱只负责判断全局状态；订单、逐笔盈亏和诊断明细保留在各专业页面。")
        with st.container(horizontal=True):
            if account_page is not None:
                st.page_link(account_page, label="账户总览", icon=":material/account_balance_wallet:")
            st.page_link("pages/28_options_pnl_ledger.py", label="期权盈亏台账", icon=":material/price_check:")
            st.page_link("pages/39_universe_incubator_center.py", label="标的赛马中心", icon=":material/rocket_launch:")
            st.page_link("pages/29_strategy_review_execution_quality.py", label="每日复盘", icon=":material/summarize:")
            st.page_link("pages/26_trade_diagnostics_center.py", label="交易诊断", icon=":material/troubleshoot:")
            st.page_link("pages/7_risk_center.py", label="风控中心", icon=":material/shield:")


def _render_kill_switch_banner(store: TradingStore) -> None:
    """Show the persisted emergency-stop state prominently on the home dashboard only when enabled."""
    enabled = store.kill_switch()
    if not enabled:
        return
    recent = [
        item for item in store.audit_events(limit=200)
        if item.get("event_type") in {"DAILY_LOSS_KILL_SWITCH", "DAILY_LOSS_KILL_SWITCH_CLEARED"}
    ]
    latest = recent[0] if recent else None
    payload = _json_loads(latest.get("payload_json")) if latest else {}
    detail = (
        f"最近事件：{_to_china_label(latest.get('created_at'))}；"
        f"记录亏损：{_money(payload.get('loss'))}；"
        "新增风险订单已暂停，请在核对账户权益快照后人工确认恢复。"
        if latest else
        "新增风险订单已暂停，请在核对账户权益快照后人工确认恢复。"
    )
    st.error(f"🚨 系统急停已开启\n\n{detail}", icon=":material/pause_circle:")


def render_home_dashboard(settings, broker, store, account_page=None) -> None:
    # 顶部专业轻量 Header (动态环境胶囊：实盘显示鲜红高亮，模拟盘显示浅蓝)
    is_real = getattr(settings, "trading_env", "SIMULATE") == "REAL"
    env_badge = (
        "<span style='font-size:12px;padding:3px 10px;border-radius:12px;background:#fef2f2;color:#dc2626;border:1px solid #fecaca;font-weight:700;margin-left:8px;vertical-align:middle'>🔴 实盘 REAL</span>"
        if is_real else
        "<span style='font-size:12px;padding:3px 9px;border-radius:12px;background:#e0f2fe;color:#0369a1;font-weight:600;margin-left:8px;vertical-align:middle'>模拟盘</span>"
    )
    col_title, col_status = st.columns([1.6, 1], vertical_alignment="center")
    with col_title:
        st.markdown(
            f"### 📈 量化系统驾驶舱 {env_badge}"
            "<span style='font-size:12px;padding:3px 9px;border-radius:12px;background:#dcfce7;color:#15803d;font-weight:600;margin-left:6px;vertical-align:middle'>风控正常 · 急停未触发</span>",
            unsafe_allow_html=True,
        )
        if is_real:
            st.caption("🔴 实盘运行中 · 绑定义务账户受控交易 · 仅放行白名单策略")
        else:
            st.caption("全局资产监控、策略运行状态与实时风险雷达看板。")

    with col_status:
        c_time, c_btn = st.columns([1, 0.42], vertical_alignment="center")
        with c_time:
            china_now = pd.Timestamp.now(tz=CHINA_TZ).strftime("%H:%M:%S")
            st.caption(f":material/autorenew: 30s 自动刷新 ({china_now})")
        with c_btn:
            if st.button("立即刷新", icon=":material/refresh:", width="stretch", key="dash_top_refresh"):
                _load_store_snapshot.clear()
                st.rerun()

    # 仅当急停被激活时才弹出高亮红框
    _render_kill_switch_banner(store)

    _render_live_dashboard(broker, account_page)
    _render_quick_links(account_page)
