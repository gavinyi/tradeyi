from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

from futu_quant.broker import BrokerError, FutuPaperBroker
from futu_quant.config import (
    Settings, load_volatility_campaigns, load_volatility_radar_budgets,
    load_volatility_symbols, delete_campaign, save_volatility_radar_budgets,
    save_volatility_symbols,
    update_volatility_symbol, upsert_campaign,
)
from futu_quant.earnings_gamma import screen_gamma_contracts
from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues
from futu_quant.gamma_risk import contract_analysis, realized_volatility
from futu_quant.option_ranking import rank_long_option_candidates
from futu_quant.option_signal import automatic_option_direction
from futu_quant.radar_control import status as radar_status
from futu_quant.radar_schedule import (
    adaptive_interval_seconds,
    load_radar_schedule_state,
)
from futu_quant.volatility_scanner import scan_volatility_watchlist
from futu_quant.volatility_optimizer import rank_volatility_contracts, volatility_regime
from futu_quant.campaign_timing import campaign_time_display
from futu_quant.store import TradingStore
from futu_quant.security_search import search_securities, security_label
from futu_quant.ui import (
    china_time_columns, chinese_columns, render_admin_login, render_sidebar,
    translate_common_values,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.run_volatility_strategy import run_once as run_volatility_once  # noqa: E402
from scripts.run_gamma_strategy import load_state  # noqa: E402


st.set_page_config(page_title="波动率期权策略", page_icon="🌊", layout="wide")
settings = Settings.load()
render_sidebar(settings)
st.title("波动率期权策略")
st.caption("独立策略 ID：volatility_long_gamma · 不依赖财报日历 · 仅限美股模拟盘")
st.warning("高风险期权买方策略：只在异常波动已确认且 IV 成本合理时入场，最大损失仍可达全部权利金。")

store = TradingStore()
broker = FutuPaperBroker(settings)
admin = render_admin_login()


@st.cache_data(ttl=3600, show_spinner="正在从富途载入美股证券目录……")
def load_catalog() -> pd.DataFrame:
    return broker.security_catalog()


with st.expander("搜索并添加波动率策略标的"):
    with st.form("volatility_symbol_search"):
        search_mode = st.segmented_control(
            "查询方式", ["模糊查询", "代码精确查询"], default="模糊查询"
        )
        query = st.text_input("代码或名称", placeholder="例如 NVDA、英伟达、AAPL")
        submitted = st.form_submit_button("查找", icon=":material/search:")
    if submitted:
        try:
            st.session_state.volatility_symbol_results = search_securities(
                load_catalog(), query, exact=search_mode == "代码精确查询"
            )
        except (BrokerError, ValueError) as exc:
            st.error(str(exc))
    symbol_results = st.session_state.get("volatility_symbol_results", pd.DataFrame())
    if not symbol_results.empty:
        result_index = st.selectbox(
            "搜索结果", symbol_results.index.tolist(),
            format_func=lambda index: security_label(symbol_results.loc[index]),
        )
        st.dataframe(chinese_columns(symbol_results[[
            column for column in ("code", "name", "stock_type", "lot_size", "listing_date")
            if column in symbol_results
        ]]), hide_index=True)
        add_code = str(symbol_results.loc[result_index, "code"])
        if st.button(f"添加 {add_code}", type="primary", disabled=not admin):
            save_volatility_symbols(tuple(dict.fromkeys((*settings.volatility_symbols, add_code))))
            st.cache_data.clear()
            st.success(f"已添加 {add_code} 到波动率策略标的。")
            st.rerun()

st.caption("已添加标的的批量维护与删除，请前往“交易配置”页面。")

with st.expander("自动波动率雷达", expanded=True):
    radar = radar_status()
    st.status(
        f"独立雷达服务：{'运行中' if radar.running else '未运行'}"
        + (f" · PID {radar.pid}" if radar.pid else ""),
        state="complete" if radar.running else "error",
    )
    current_radar_symbols = load_volatility_symbols()
    radar_budgets = load_volatility_radar_budgets()
    schedule_state = load_radar_schedule_state(store)
    estimated_interval = adaptive_interval_seconds(len(current_radar_symbols))
    effective_interval = (
        schedule_state.interval_seconds if schedule_state else estimated_interval
    )

    def format_interval(seconds: int) -> str:
        minutes, remainder = divmod(max(0, int(seconds)), 60)
        return f"{minutes} 分 {remainder} 秒" if remainder else f"{minutes} 分钟"

    with st.container(horizontal=True):
        st.metric("扫描标的数", len(current_radar_symbols), border=True)
        st.metric("当前扫描间隔", format_interval(effective_interval), border=True)
        st.metric(
            "上次扫描耗时",
            f"{schedule_state.scan_duration_seconds:.1f} 秒"
            if schedule_state else "等待首次扫描",
            border=True,
        )

    with st.expander("自动雷达权利金预算"):
        st.caption(
            "预算按“标的单独预算 → 默认预算”取值，只限制自动雷达新生成的策略；"
            "流动性、Delta、模型预期收益和账户风控仍会独立校验。"
        )
        budget_rows = pd.DataFrame([
            {
                "正股代码": symbol,
                "使用单独预算": symbol in radar_budgets["symbols"],
                "每批最大权利金（美元）": float(
                    radar_budgets["symbols"].get(symbol, radar_budgets["default"])
                ),
            }
            for symbol in current_radar_symbols
        ])
        with st.form("volatility_radar_budget_form"):
            default_radar_budget = st.number_input(
                "自动雷达默认每批最大权利金（美元）",
                min_value=1.0, max_value=1_000_000.0,
                value=float(radar_budgets["default"]), step=100.0,
            )
            edited_budgets = st.data_editor(
                budget_rows,
                hide_index=True,
                disabled=["正股代码"],
                column_config={
                    "使用单独预算": st.column_config.CheckboxColumn(
                        "使用单独预算",
                        help="勾选后，该标的不再使用默认预算。",
                    ),
                    "每批最大权利金（美元）": st.column_config.NumberColumn(
                        "每批最大权利金（美元）", min_value=1.0,
                        max_value=1_000_000.0, step=100.0, format="$%.0f",
                    ),
                },
                key="volatility_radar_budget_editor",
            )
            save_radar_budget = st.form_submit_button(
                "保存自动雷达预算", type="primary", icon=":material/save:",
                disabled=not admin,
            )
        if save_radar_budget:
            symbol_budgets = {
                str(row["正股代码"]): float(row["每批最大权利金（美元）"])
                for _, row in edited_budgets.iterrows()
                if bool(row["使用单独预算"])
            }
            saved_budgets = save_volatility_radar_budgets(
                float(default_radar_budget), symbol_budgets,
            )
            store.audit(
                "VOLATILITY_RADAR_BUDGETS_UPDATED", saved_budgets, "streamlit",
            )
            st.success("自动雷达权利金预算已保存，下一轮扫描立即生效。")
            st.rerun()
        st.metric(
            "连续错误",
            schedule_state.consecutive_errors if schedule_state else 0,
            border=True,
        )

    if schedule_state:
        next_scan = pd.to_datetime(
            schedule_state.next_scan_at, errors="coerce", utc=True
        )
        if pd.notna(next_scan):
            st.caption(
                "预计下次扫描（中国时间）："
                f"{next_scan.tz_convert('Asia/Shanghai'):%Y-%m-%d %H:%M:%S}"
            )
        if schedule_state.backoff_active:
            st.warning(
                f"雷达正在异常退避：已连续失败 {schedule_state.consecutive_errors} 次，"
                f"当前重试间隔 {format_interval(schedule_state.interval_seconds)}。"
            )
    else:
        st.caption("预计下次扫描：等待雷达服务完成首次动态调度。")
    st.write(
        "独立雷达服务在美股常规交易时段只扫描你已加入的波动率策略标的，"
        "并按标的数量和最近扫描耗时动态调整，间隔为 2 至 15 分钟。"
        "达到动态波动条件后，会写入雷达决策、自动选择期权合约、保存为自动期权监控策略，"
        "并开启 volatility_long_gamma 的 SHADOW 自动调度。"
    )
    st.caption(
        "当前雷达扫描范围："
        + ("、".join(current_radar_symbols) if current_radar_symbols else "尚未配置")
    )
    if st.button(
        "立即扫描已选标的", icon=":material/radar:", disabled=not admin or not current_radar_symbols,
    ):
        try:
            with st.spinner(f"正在扫描 {len(current_radar_symbols)} 个波动率策略标的……"):
                scan_results = scan_volatility_watchlist(broker, store, current_radar_symbols)
            triggered = [item["symbol"] for item in scan_results if item.get("active")]
            created = [
                item.get("campaign_code") for item in scan_results
                if item.get("campaign_created") and item.get("campaign_code")
            ]
            blocked = [
                f"{item['symbol']}：{item.get('campaign_reason', '未找到合格合约')}"
                for item in scan_results
                if item.get("active") and item.get("campaign_created") is False
            ]
            if created:
                st.success(
                    "扫描完成，已生成并进入自动期权监控：" + "、".join(created)
                )
            if blocked:
                st.warning("正股信号已触发，但合约风控未通过：\n\n" + "\n\n".join(blocked))
            elif triggered and not created:
                st.info("正股信号已触发，但已有启用中的自动期权策略，本轮不重复创建。")
            elif not triggered:
                st.info("扫描完成，当前没有标的满足动态波动触发条件。")
        except Exception as exc:
            st.error(f"扫描失败：{exc}")
    scanner_rows = pd.DataFrame([
        row for row in store.decisions(limit=500)
        if row["strategy_id"] == "volatility_scanner"
    ])
    if scanner_rows.empty:
        st.info("暂无雷达扫描记录；美股常规交易时段启动服务后自动产生。")
    else:
        latest_scans = scanner_rows.drop_duplicates("symbol").copy()
        details = latest_scans["payload_json"].map(
            lambda value: json.loads(value or "{}")
        )
        latest_scans["当日涨跌"] = details.map(lambda item: item.get("intraday_return"))
        latest_scans["波动Z值"] = details.map(lambda item: item.get("zscore_20d"))
        latest_scans["ATR倍数"] = details.map(lambda item: item.get("atr_ratio"))
        latest_scans["量比"] = details.map(lambda item: item.get("volume_ratio"))
        latest_scans["方向分"] = details.map(lambda item: item.get("direction_score"))
        latest_scans["信号生命周期"] = details.map(lambda item: item.get("lifecycle"))
        latest_scans["相对SPY强弱"] = details.map(lambda item: item.get("relative_strength"))
        latest_scans["目标Z值"] = details.map(
            lambda item: (item.get("profile") or {}).get("entry_zscore")
        )
        latest_scans["目标最小涨跌"] = details.map(
            lambda item: (item.get("profile") or {}).get("minimum_underlying_move")
        )
        latest_scans["自动期权状态"] = details.map(
            lambda item: (
                "已加入自动监控" if item.get("campaign_created")
                else "合约风控未通过" if item.get("campaign_created") is False
                else "未触发生成"
            )
        )
        latest_scans["自动期权代码"] = details.map(
            lambda item: item.get("campaign_code") or "—"
        )
        latest_scans["合约生成说明"] = details.map(
            lambda item: item.get("campaign_reason") or "—"
        )
        latest_scans["本轮权利金预算"] = details.map(
            lambda item: item.get("campaign_premium_budget")
        )
        latest_scans["最高分候选详情"] = details.map(
            lambda item: "；".join(
                f"{candidate.get('code', '')}：{candidate.get('reason', '')}"
                for candidate in (
                    (item.get("campaign_diagnostics") or {}).get("top_candidates") or []
                )[:3]
            ) or "—"
        )
        display = translate_common_values(china_time_columns(latest_scans).rename(columns={
            "symbol": "正股代码", "decision": "监测结果", "reason": "原因",
            "created_at": "扫描时间（中国）",
        }).drop(columns=["id", "strategy_id", "payload_json"], errors="ignore"))
        displayed_volatility_symbols = load_volatility_symbols()
        display.insert(
            0, "操作",
            display["正股代码"].map(
                lambda code: (
                    ":material/remove: 移除波动率标的"
                    if code in displayed_volatility_symbols
                    else "非当前标的"
                )
            ),
        )

        def toggle_radar_volatility_symbol() -> None:
            if not admin:
                st.session_state["radar_volatility_action_error"] = "请先解锁管理员操作。"
                return
            click = st.session_state.get("radar_toggle_volatility")
            if not click:
                return
            code = str(display.iloc[int(click["row"])]["正股代码"])
            current_volatility_symbols = load_volatility_symbols()
            if code not in current_volatility_symbols:
                st.session_state["radar_volatility_action_error"] = (
                    f"{code} 不是当前波动率策略标的，雷达不会继续扫描它。"
                )
                return
            remaining = tuple(
                symbol for symbol in current_volatility_symbols if symbol != code
            )
            if not remaining:
                st.session_state["radar_volatility_action_error"] = (
                    "至少需要保留一个波动率策略标的，无法移除最后一个标的。"
                )
                return
            update_volatility_symbol(code, False)
            store.set_setting(f"volatility_scanner_paused:{code}", "true")
            store.audit(
                "VOLATILITY_SYMBOL_REMOVED_FROM_RADAR",
                {"symbol": code, "scanner_paused": True},
                "streamlit",
            )
            st.session_state["radar_volatility_removed"] = code

        st.dataframe(display, hide_index=True, column_config={
            "操作": st.column_config.ButtonColumn(
                "操作", pinned=True, type="tertiary",
                on_click=toggle_radar_volatility_symbol, key="radar_toggle_volatility",
            ),
            "当日涨跌": st.column_config.NumberColumn(format="percent"),
            "相对SPY强弱": st.column_config.NumberColumn(format="percent"),
            "目标最小涨跌": st.column_config.NumberColumn(format="percent"),
        })
        removed_code = st.session_state.pop("radar_volatility_removed", None)
        if removed_code:
            st.success(
                f"{removed_code} 已从波动率策略标的中移除，并暂停雷达自动重新加入。"
            )
        action_error = st.session_state.pop("radar_volatility_action_error", None)
        if action_error:
            st.error(action_error)

with st.expander("策略规则", expanded=True):
    st.markdown(
        "1. 默认根据当日涨跌、5日动量和20日趋势自动选择 Call 或 Put，也保留手动覆盖能力。\n"
        "2. 使用独立 IV 阈值，同时保留价差、权利金、Greeks 和组合风控。\n"
        "3. 分批入场，只顺势加仓，不对亏损仓位摊低成本。\n"
        "4. 按止盈、硬止损、峰值回撤或最长持有时间退出。\n"
        "5. 与 earnings_gamma 拥有独立配置、执行模式、预算、日志和 Shadow 持仓。\n"
        "6. 采用20日波动Z值、ATR、量比和趋势确认；使用滞回、冷却以及IV/波动衰减退出。"
    )

left, middle, right = st.columns(3)
underlying = left.selectbox("正股", load_volatility_symbols())
direction_choice = middle.segmented_control("方向", ["自动判断", "CALL", "PUT"], default="自动判断")
max_days = right.number_input("最远到期天数", 0, 45, 21)
min_oi = left.number_input("最低未平仓量", 0, 1_000_000, 500, step=50)
max_spread = middle.number_input("绝对最大买卖价差（%）", 1.0, 100.0, 12.0)
max_moneyness = right.number_input("最大价内/价外距离（%）", 0.0, 50.0, 10.0)
contracts_per_stage = left.number_input("每批合约数", 1, 100, 1)
stages = middle.number_input("计划分批数", 1, 5, 3)
max_premium = right.number_input("每批最大权利金（美元）", 1.0, 1_000_000.0, 1000.0)
entry_hours = left.number_input("初始入场窗口（小时）", 1, 48, 6)
profit_target = middle.number_input("止盈比例", 0.05, 10.0, 0.50, step=0.05)
stop_loss = right.number_input("硬止损比例", 0.05, 1.0, 0.40, step=0.05)
trailing_stop = left.number_input("峰值回撤退出比例", 0.05, 1.0, 0.25, step=0.05)
with st.expander("独立平仓参数", expanded=True):
    exit_left, exit_middle, exit_right = st.columns(3)
    max_hold_hours = exit_left.number_input(
        "最长持有小时", 1, 168, 24,
        help="从本轮持仓建立时间开始计算，不再使用财报事件时间。",
    )
    minimum_hold_minutes = exit_middle.number_input(
        "衰减退出最短持有分钟", 0, 1440, 30,
        help="硬止损、止盈和盈利峰值回撤始终生效；只延迟波动/IV/效率衰减退出。",
    )
    contraction_confirmations = exit_right.number_input(
        "波动收缩连续确认次数", 1, 10, 3,
        help="避免单次行情噪声造成误平仓。",
    )
    iv_decline_exit_ratio = exit_left.number_input(
        "IV较实际入场下降比例", 0.05, 0.90, 0.20, step=0.05,
    )
    efficiency_decline_exit_ratio = exit_middle.number_input(
        "Gamma/Theta效率下降比例", 0.05, 0.90, 0.40, step=0.05,
    )
minimum_move = st.number_input("自动方向最低当日涨跌幅", 0.0, 0.20, 0.020, step=0.001, format="%.3f")
entry_zscore = st.number_input("动态波动Z值门槛", 0.5, 5.0, 2.0, step=0.1)
cooldown_hours = st.number_input("退出后冷却小时", 1, 168, 24)

if st.button(
    "生成波动率期权候选", type="primary",
    icon=":material/search:", key="generate_volatility_candidates",
):
    st.session_state.pop("volatility_candidates", None)
    st.session_state.pop("volatility_candidate_summary", None)
    try:
        with st.spinner(f"正在读取 {underlying} 的到期日、期权链和实时行情……"):
            expirations = broker.option_expirations(underlying).copy()
            latest = date.today() + timedelta(days=int(max_days))
            valid = expirations[
                (pd.to_datetime(expirations["strike_time"]).dt.date >= date.today())
                & (pd.to_datetime(expirations["strike_time"]).dt.date <= latest)
            ]
            if valid.empty:
                raise ValueError("指定窗口内没有有效到期日，请增加“最远到期天数”。")
            expirations_to_scan = valid.sort_values("strike_time")[
                "strike_time"
            ].astype(str).head(3).tolist()
            chain_frames = [
                broker.option_chain(underlying, expiration)
                for expiration in expirations_to_scan
            ]
            chain = pd.concat(
                [frame for frame in chain_frames if not frame.empty],
                ignore_index=True,
            )
            if chain.empty:
                raise ValueError("富途返回的期权链为空。")
            underlying_quote = broker.snapshots([underlying]).iloc[0]
            spot = float(underlying_quote["last_price"])
            bars = broker.candles(underlying, count=120)
            regime = volatility_regime(underlying_quote, bars, float(entry_zscore))
            option_types = [direction_choice]
            direction_reason = "使用手动选择的方向"
            if direction_choice == "自动判断":
                automatic_type, direction_reason, _ = automatic_option_direction(
                    underlying_quote, bars, "VOLATILITY",
                    float(minimum_move),
                )
                if automatic_type is None:
                    option_types = ["CALL", "PUT"]
                    direction_reason = (
                        f"{direction_reason}；当前先同时展示 Call 和 Put 候选，"
                        "等待方向信号形成后再自动入场。"
                    )
                else:
                    option_types = [automatic_type]
            quotes = broker.snapshots_tolerant(chain["code"].astype(str).tolist())
            if quotes.empty:
                raise ValueError("期权链存在，但没有取得任何可用实时行情。")
            screened_frames = [
                screen_gamma_contracts(
                    chain, quotes, spot, option_type, int(min_oi), float(max_spread),
                    float(max_moneyness),
                )
                for option_type in option_types
            ]
            candidates = pd.concat(
                [frame for frame in screened_frames if not frame.empty],
                ignore_index=True,
            ) if any(not frame.empty for frame in screened_frames) else pd.DataFrame()
            screened_count = len(candidates)
            if not candidates.empty:
                candidates = rank_long_option_candidates(
                    candidates, spot, profit_target=float(profit_target),
                    stop_loss=float(stop_loss),
                )
                candidates = rank_volatility_contracts(
                    candidates, spot, realized_volatility(bars), iv_percentile=0.5,
                    signal_active=bool(regime.get("active")),
                    strong_signal=bool(
                        regime.get("strong_signal_override")
                        and regime.get("trigger_confirmed")
                    ),
                    max_premium_per_stage=float(max_premium),
                    contracts_per_stage=int(contracts_per_stage),
                )
                delta = pd.to_numeric(
                    candidates.get("option_delta"), errors="coerce"
                ).abs().fillna(0)
                candidates["候选等级"] = "不合格"
                watch = (
                    (pd.to_numeric(candidates["iv_rv"], errors="coerce") <= 1.80)
                    & delta.between(0.20, 0.85)
                )
                candidates.loc[watch, "候选等级"] = "观察"
                candidates.loc[candidates["volatility_eligible"], "候选等级"] = "合格"
            st.session_state.volatility_candidates = candidates
            st.session_state.volatility_spot = spot
            st.session_state.volatility_direction_reason = direction_reason
            st.session_state.volatility_regime = regime
            st.session_state.volatility_realized = realized_volatility(bars)
            st.session_state.volatility_candidate_summary = {
                "到期日数": len(expirations_to_scan),
                "期权链合约数": len(chain),
                "取得行情数": len(quotes),
                "通过基础筛选数": screened_count,
                "最终合格数": (
                    int(candidates["volatility_eligible"].sum())
                    if not candidates.empty else 0
                ),
                "观察候选数": (
                    int((candidates["候选等级"] == "观察").sum())
                    if not candidates.empty else 0
                ),
                "方向": " + ".join(option_types),
            }
        if candidates.empty:
            st.warning(
                "候选生成已完成，但没有合约通过当前基础筛选。"
                "可适当降低最低未平仓量、扩大最大价差或价内/价外距离。"
            )
        else:
            st.success(f"候选生成完成，共找到 {len(candidates)} 个合约。")
    except Exception as exc:
        st.session_state.volatility_candidate_error = str(exc)
        st.error(f"候选生成失败：{exc}")

candidates = st.session_state.get("volatility_candidates", pd.DataFrame())
summary = st.session_state.get("volatility_candidate_summary")
if summary:
    st.caption(
        "本次生成："
        + " · ".join(f"{key} {value}" for key, value in summary.items())
    )
    regime = st.session_state.get("volatility_regime", {})
    if not regime.get("active", False):
        st.warning(
            f"候选仅供查看，当前动态波动信号尚未激活，自动策略不会入场："
            f"{regime.get('reason', '未满足触发条件')}"
        )
        st.dataframe(pd.DataFrame(regime.get("checks", [])), hide_index=True)
if not candidates.empty:
    st.metric("正股最新价", f"${st.session_state.volatility_spot:.2f}")
    st.info(st.session_state.get("volatility_direction_reason", ""))
    regime = st.session_state.get("volatility_regime", {})
    with st.container(horizontal=True):
        st.metric("20日波动Z值", f"{regime.get('zscore_20d', 0):.2f}", border=True)
        st.metric("ATR倍数", f"{regime.get('atr_ratio', 0):.2f}", border=True)
        st.metric("量比", f"{regime.get('volume_ratio', 0):.2f}", border=True)
        st.metric("方向分", f"{regime.get('direction_score', 0):+.0f}", border=True)
    visible = [column for column in (
        "候选等级", "code", "model_expected_return", "estimated_profit_probability",
        "volatility_eligible", "volatility_quality_score", "volatility_filter_reason",
        "days_to_expiry", "iv_rv",
        "probability_confidence", "breakeven_price", "gamma_theta_efficiency",
        "option_type", "strike_price", "strike_time", "bid_price", "ask_price",
        "mid_price", "spread_pct", "moneyness_pct", "option_open_interest", "volume",
        "option_implied_volatility", "option_delta", "option_gamma", "option_theta",
        "option_vega", "option_rho", "option_contract_multiplier",
    ) if column in candidates]
    st.caption("优先按波动率合约质量排序：Delta、7–21天期限、Gamma/Theta、IV成本和流动性；末日期权保留但提高门槛。")
    candidate_view = chinese_columns(candidates[visible])
    st.dataframe(candidate_view, hide_index=True, column_config={
        "模型盈利概率": st.column_config.ProgressColumn(
            "模型盈利概率", min_value=0.0, max_value=1.0, format="percent"
        ),
        "模型期望收益": st.column_config.NumberColumn("模型期望收益", format="percent"),
        "盈亏平衡价": st.column_config.NumberColumn("盈亏平衡价", format="$%.2f"),
    })
    eligible_codes = candidates.loc[candidates["volatility_eligible"], "code"].tolist()
    selected_code = st.selectbox("选择合约", eligible_codes or candidates["code"].tolist())
    selected = candidates[candidates["code"] == selected_code].iloc[0]
    selected_option_type = str(selected["option_type"])
    cached_realized = float(
        st.session_state.get(
            "volatility_realized",
            selected.get("annual_realized_volatility", 0),
        )
        or 0
    )
    analysis = contract_analysis(
        selected, float(st.session_state.volatility_spot),
        cached_realized, store,
        peer_contracts=candidates,
    )
    with st.container(horizontal=True):
        st.metric(
            "IV 历史百分位",
            f"{analysis['IV历史百分位']:.1%}" if analysis["IV百分位可用"] else "样本不足",
            help=(
                f"样本来源：{analysis['IV样本来源']}；"
                f"样本数：{analysis['IV历史样本数']}；"
                f"置信度：{analysis['IV样本置信度']}。"
            ),
            border=True,
        )
        st.metric("IV / 实现波动", f"{analysis['IV/RV']:.2f}", border=True)
        st.metric("预期波动", f"{analysis['预期波动比例']:.1%}", border=True)
    estimated = float(selected["ask_price"]) * float(
        selected["option_contract_multiplier"]
    ) * int(contracts_per_stage)
    st.metric("本批估算权利金", f"${estimated:,.2f}")
    if not bool(selected.get("volatility_eligible", False)):
        st.error(f"当前没有合格合约：{selected.get('volatility_filter_reason', '')}")
    elif analysis["IV/RV"] > 1.5 or analysis["IV历史百分位"] > .75:
        st.error("IV成本过高：要求 IV/RV ≤ 1.50 且 IV历史百分位 ≤ 75%。")
    elif estimated > max_premium:
        st.error("本批超过最大权利金，不能保存。")
    elif st.button("保存为波动率自动策略", disabled=not admin):
        campaigns = [item for item in load_volatility_campaigns() if item.get("code") != selected_code]
        campaigns.append({
            "code": selected_code, "underlying": underlying,
            "event_date": date.today().isoformat(), "event_session": "行情触发",
            "option_type": selected_option_type, "contracts_per_stage": int(contracts_per_stage),
            "max_stages": int(stages), "max_premium_per_stage": float(max_premium),
            "entry_hours": int(entry_hours), "profit_target": float(profit_target),
            "stop_loss": float(stop_loss), "trailing_stop": float(trailing_stop),
            "max_hold_hours": int(max_hold_hours),
            "minimum_hold_minutes": int(minimum_hold_minutes),
            "iv_decline_exit_ratio": float(iv_decline_exit_ratio),
            "efficiency_decline_exit_ratio": float(efficiency_decline_exit_ratio),
            "contraction_confirmations_required": int(contraction_confirmations),
            "minimum_underlying_move": float(minimum_move),
            "entry_zscore": float(entry_zscore), "cooldown_hours": int(cooldown_hours),
            "regime_profile": st.session_state.get("volatility_regime", {}),
            "entry_iv": float(analysis["隐含波动率"]),
            "entry_gamma_theta_efficiency": float(selected.get("gamma_theta_efficiency", 0)),
            "optimizer_version": 2,
            "enabled": True, "trigger_mode": "VOLATILITY",
            **{
                column: selected.get(column) for column in (
                    "strike_price", "strike_time", "bid_price", "ask_price", "mid_price",
                    "spread_pct", "moneyness_pct", "option_open_interest", "volume",
                    "option_implied_volatility", "option_delta", "option_gamma",
                    "option_theta", "option_vega", "option_rho", "option_contract_multiplier",
                ) if column in selected.index
            },
        })
        upsert_campaign("VOLATILITY_CAMPAIGNS", campaigns[-1])
        st.success("已保存到独立的 volatility_long_gamma 策略。")

st.subheader("已保存的波动率策略")
campaigns = load_volatility_campaigns()
if campaigns:
    campaign_codes = [item["code"] for item in campaigns]
    with st.container(horizontal=True):
        refresh_saved_quotes = st.button(
            "刷新已保存策略行情", icon=":material/refresh:",
            help="按需读取实时行情；不刷新时优先快速展示已保存配置。",
        )
        if st.button("清除行情缓存", icon=":material/delete_sweep:"):
            st.session_state.pop("saved_volatility_live_quotes", None)
            st.session_state.pop("saved_volatility_quotes_time", None)
            st.rerun()
    if refresh_saved_quotes:
        try:
            with st.spinner(f"正在批量读取 {len(campaign_codes)} 条期权行情……"):
                quote_frame = broker.snapshots(campaign_codes)
            st.session_state.saved_volatility_live_quotes = (
                quote_frame.set_index("code").to_dict("index")
                if not quote_frame.empty else {}
            )
            st.session_state.saved_volatility_quotes_time = pd.Timestamp.now(
                tz="Asia/Shanghai"
            ).strftime("%Y-%m-%d %H:%M:%S")
        except BrokerError as exc:
            st.warning(f"实时行情刷新失败，继续显示保存值：{exc}")
    live_quotes = st.session_state.get("saved_volatility_live_quotes", {})
    if st.session_state.get("saved_volatility_quotes_time"):
        st.caption(
            f"实时行情最近刷新（中国时间）："
            f"{st.session_state.saved_volatility_quotes_time}"
        )
    latest_decisions = {}
    for decision in store.decisions(limit=1000):
        if decision["strategy_id"] == "volatility_long_gamma":
            latest_decisions.setdefault(decision["symbol"], decision)
    rows = []
    strategy_state = load_state()
    shadow_positions = store.shadow_positions_for_strategy("volatility_long_gamma")
    for campaign in campaigns:
        issues = diagnose_campaign({**campaign, "trigger_mode": "VOLATILITY"}, [])
        shadow_quantity, shadow_cost = shadow_positions.get(campaign["code"], (0.0, 0.0))
        latest = latest_decisions.get(campaign["code"], {})
        state_key = f"volatility_long_gamma:{campaign['code']}"
        rows.append({
            **campaign, **live_quotes.get(campaign["code"], {}),
            **campaign_time_display(campaign, strategy_state.get(state_key, {})),
            "当前 Shadow 持仓": shadow_quantity, "Shadow 平均成本": shadow_cost,
            "运行状态": latest.get("decision", ""), "最新原因": latest.get("reason", ""),
            "配置状态": "阻断" if hard_campaign_issues(campaign, issues) else "正常",
            "诊断": "；".join(issues),
        })
    saved = translate_common_values(chinese_columns(pd.DataFrame(rows)))
    saved.insert(0, "操作", ":material/delete: 删除")
    saved_columns = [column for column in (
        "操作", "代码", "正股代码", "期权类型", "行权价", "到期日", "最新价", "买一价", "卖一价",
        "中间价", "买卖价差比例(%)", "价内/价外距离(%)", "未平仓量", "成交量", "隐含波动率",
        "Delta", "Gamma", "Theta", "Vega", "Rho", "合约乘数", "每批合约数", "最大批数",
        "每批最大权利金", "入场窗口（小时）", "止盈比例", "硬止损比例", "峰值回撤退出比例",
        "动态波动Z值门槛", "退出冷却小时", "入场隐含波动率", "入场Gamma/Theta效率",
        "波动率合约质量分", "波动率合约筛选原因", "最近表达复核",
        "当前 Shadow 持仓", "Shadow 平均成本", "运行状态", "最新原因", "配置状态", "诊断",
        "入场开始时间（中国）", "入场截止时间（中国）",
    ) if column in saved.columns]

    def remove_saved_volatility_campaign() -> None:
        if not admin:
            st.session_state["remove_volatility_campaign_error"] = "请先解锁管理员操作。"
            return
        click = st.session_state.get("remove_saved_volatility_campaign")
        if not click:
            return
        code = str(saved.iloc[int(click["row"])]["代码"])
        shadow_quantity, _ = store.shadow_position("volatility_long_gamma", code)
        if shadow_quantity > 0:
            st.session_state["remove_volatility_campaign_error"] = (
                f"{code} 仍有 {shadow_quantity:g} 张 Shadow 持仓，请先完成退出。"
            )
            return
        try:
            orders = broker.orders()
            positions = broker.positions()
            pending = orders[
                (orders["code"].astype(str) == code)
                & orders["order_status"].map(broker.is_cancellable_status)
            ] if not orders.empty else orders
        except BrokerError as exc:
            st.session_state["remove_volatility_campaign_error"] = (
                f"无法确认 {code} 是否存在待成交订单：{exc}"
            )
            return
        paper_rows = positions[positions["code"].astype(str) == code] if not positions.empty else positions
        paper_quantity = float(paper_rows["qty"].sum()) if not paper_rows.empty else 0
        if paper_quantity > 0:
            st.session_state["remove_volatility_campaign_error"] = (
                f"{code} 仍有 {paper_quantity:g} 张模拟盘持仓，请先平仓后再删除策略。"
            )
            return
        if not pending.empty:
            st.session_state["remove_volatility_campaign_error"] = (
                f"{code} 仍有 {len(pending)} 笔待成交订单，请先到订单管理撤单。"
            )
            return
        delete_campaign("VOLATILITY_CAMPAIGNS", code)
        store.audit(
            "VOLATILITY_CAMPAIGN_DELETED",
            {"code": code, "source": "saved_campaign_row"},
            "volatility_long_gamma",
        )
        st.session_state["removed_volatility_campaign"] = code

    st.dataframe(saved[saved_columns], hide_index=True, column_config={
        "操作": st.column_config.ButtonColumn(
            "操作", pinned=True, type="tertiary",
            on_click=remove_saved_volatility_campaign,
            key="remove_saved_volatility_campaign",
        ),
    })
    removed_campaign = st.session_state.pop("removed_volatility_campaign", None)
    if removed_campaign:
        st.success(f"{removed_campaign} 已从已保存的波动率策略中删除。")
    remove_error = st.session_state.pop("remove_volatility_campaign_error", None)
    if remove_error:
        st.error(remove_error)

    st.subheader("清空并重启标的")
    if not admin:
        st.caption("解锁管理员后才能执行清空和重启。")
    elif not campaigns:
        st.info("尚未保存波动率期权策略，无法重启。")
    else:
        restart_targets = {
            f"{item['code']} · {item.get('underlying', '')}": item for item in campaigns
        }
        selected_restart_label = st.selectbox(
            "选择要清空并重启的标的", list(restart_targets.keys()),
        )
        selected_restart_campaign = restart_targets[selected_restart_label]
        restart_reason = st.text_input(
            "重启原因", placeholder="例如：重新按新追价规则运行"
        )

        def clear_and_restart_volatility_campaign() -> None:
            if not restart_reason.strip():
                st.session_state["restart_volatility_error"] = "请先填写重启原因。"
                return
            code = str(selected_restart_campaign["code"])
            underlying_code = str(selected_restart_campaign.get("underlying", ""))
            cleared = store.clear_symbol_activity(
                "volatility_long_gamma",
                code,
                [
                    f"volatility_long_gamma:{code}",
                    f"volatility_cooldown:{underlying_code}",
                    f"volatility_cooldown_hours:{underlying_code}",
                ],
            )
            store.audit(
                "VOLATILITY_CAMPAIGN_CLEARED_AND_RESTARTED",
                {
                    "code": code, "underlying": underlying_code,
                    "reason": restart_reason, "cleared": cleared,
                },
                "volatility_long_gamma",
            )
            try:
                run_volatility_once()
            except Exception as exc:
                st.session_state["restart_volatility_error"] = (
                    f"{code} 已清空，但重启执行失败：{exc}"
                )
                return
            st.session_state["restart_volatility_success"] = (
                f"{code} 已清空并重新运行一次。"
            )

        if st.button(
            "清空并重启该标的", type="primary",
            disabled=not admin or not restart_reason.strip(),
        ):
            clear_and_restart_volatility_campaign()

    restart_success = st.session_state.pop("restart_volatility_success", None)
    if restart_success:
        st.success(restart_success)
    restart_error = st.session_state.pop("restart_volatility_error", None)
    if restart_error:
        st.error(restart_error)
else:
    st.info("尚未保存波动率期权策略。")

st.subheader("独立运行与调试")
mode = store.get_setting("strategy_execution_mode:volatility_long_gamma", "") or "INHERIT"
enabled = store.get_setting("strategy_enabled:Volatility Long Gamma", "false") == "true"
with st.container(horizontal=True):
    st.metric("执行模式", mode, border=True)
    st.metric("自动调度", "已启用" if enabled else "已关闭", border=True)
st.page_link(
    "pages/15_策略控制台.py", label="前往自动化控制中心设置波动率策略",
    icon=":material/tune:",
)
if st.button("立即运行一次波动率策略", disabled=not admin or not enabled or not campaigns):
    try:
        run_volatility_once()
        st.success("单次运行完成。")
    except Exception as exc:
        st.error(f"运行失败：{exc}")

decisions = pd.DataFrame([
    row for row in store.decisions(limit=300) if row["strategy_id"] == "volatility_long_gamma"
])
if not decisions.empty:
    def decision_detail(value: str) -> pd.Series:
        payload = json.loads(value or "{}")
        return pd.Series({
            "当前持仓": payload.get("position_quantity", 0),
            "计划下单": payload.get("planned_order_quantity", 0),
            "持仓来源": {
                "SHADOW_LEDGER": "Shadow 虚拟成交账本",
                "FUTU_ACCOUNT": "富途模拟账户",
            }.get(payload.get("position_source"), "历史执行记录"),
            "盈亏比例": payload.get("pnl_ratio"),
            "峰值回撤": payload.get("drawdown"),
            "校验提醒": "；".join(payload.get("validation_warnings", [])),
            "自动方向依据": payload.get("direction_reason", "历史记录未保存"),
            "波动Z值": (payload.get("volatility_regime") or {}).get("zscore_20d"),
            "ATR倍数": (payload.get("volatility_regime") or {}).get("atr_ratio"),
            "量比": (payload.get("volatility_regime") or {}).get("volume_ratio"),
            "短期实现波动": (payload.get("volatility_regime") or {}).get("rv10"),
            "长期实现波动": (payload.get("volatility_regime") or {}).get("rv60"),
            "入场开始（中国）": pd.to_datetime(
                payload.get("entry_start"), errors="coerce", utc=True
            ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("entry_start") else "",
            "入场截止（中国）": pd.to_datetime(
                payload.get("event_time"), errors="coerce", utc=True
            ).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if payload.get("event_time") else "",
        })

    details = decisions["payload_json"].apply(decision_detail)
    decision_display = pd.concat([decisions.drop(columns=["payload_json"]), details], axis=1)
    localized_decisions = translate_common_values(china_time_columns(decision_display).rename(columns={
        "symbol": "期权代码", "decision": "决策", "reason": "原因",
        "created_at": "时间（中国）",
    }).drop(columns=["id", "strategy_id"], errors="ignore"))
    st.dataframe(localized_decisions, hide_index=True,
        column_config={
            "盈亏比例": st.column_config.NumberColumn(format="percent"),
            "峰值回撤": st.column_config.NumberColumn(format="percent"),
        })
