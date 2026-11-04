from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from futu_quant.config import Settings, save_runtime_settings
from futu_quant.crypto_backtest import (
    backtest_quality_metrics,
    run_crypto_volatility_backtest,
    run_crypto_volatility_parameter_grid,
    trade_quality_summary,
)
from futu_quant.crypto_dashboard import localize_crypto_time_columns
from futu_quant.crypto_models import normalize_crypto_symbol
from futu_quant.crypto_protections import crypto_protection_config_from_env
from futu_quant.crypto_research import (
    crypto_lookahead_audit,
    crypto_monte_carlo,
    crypto_walk_forward,
)
from futu_quant.crypto_volatility import (
    crypto_volatility_config_from_env,
    decide_crypto_volatility,
)
from futu_quant.crypto_watchlist import (
    add_crypto_watchlist_symbol,
    crypto_radar_config_from_env,
    load_crypto_radar_managed_symbols,
    load_crypto_watchlist,
    normalize_crypto_symbols,
    remove_crypto_watchlist_symbol,
    pin_crypto_watchlist_symbol,
)
from futu_quant.okx_broker import OkxApiError, OkxDemoBroker
from futu_quant.okx_config import (
    OKX_API_BASE_URLS,
    OkxSettings,
    normalize_okx_base_url,
)
from futu_quant.secret_store import keychain_available, secret_status, set_secret
from futu_quant.ui import render_sidebar


st.set_page_config(page_title="币策略配置与回测", page_icon="📈", layout="wide")
render_sidebar(Settings.load_cached())
st.title("币策略配置与回测")
st.caption("现货波动率扩张策略。默认 Shadow，只使用已完成的 1 小时 K 线。")

config_tab, backtest_tab, connection_tab = st.tabs(["配置中心", "回测验证", "连接诊断"])

with config_tab:
    st.subheader("币策略自选标的")
    st.caption("自动策略只交易这里的标的；雷达候选池与策略自选分开管理。")
    watchlist = load_crypto_watchlist()
    managed_watchlist = set(load_crypto_radar_managed_symbols())
    if watchlist:
        for symbol in watchlist:
            name_col, pin_col, action_col = st.columns(
                [5, 2, 1], vertical_alignment="center"
            )
            source = "雷达托管" if symbol in managed_watchlist else "固定自选"
            name_col.markdown(f"**{symbol}** · {source}")
            if symbol in managed_watchlist and pin_col.button(
                "转为固定", key=f"pin_crypto_watchlist_{symbol}",
                icon=":material/push_pin:",
            ):
                pin_crypto_watchlist_symbol(symbol)
                st.success(f"{symbol} 已转为固定自选，不会被雷达自动移除。")
                st.rerun()
            if action_col.button(
                "移除", key=f"remove_crypto_watchlist_{symbol}", icon=":material/delete:",
            ):
                remove_crypto_watchlist_symbol(symbol)
                st.success(f"{symbol} 已从币策略自选移除。")
                st.rerun()
    else:
        st.info("币策略自选为空。请在“查找并添加”中加入标的，或启用自动雷达。")

    with st.expander("查找并添加欧易现货", expanded=not bool(watchlist)):
        st.caption("现货目录按需加载；支持输入 BTC、BTC-USDT 等代码精确查找。")
        if st.button("加载 / 刷新欧易现货目录", icon=":material/sync:"):
            try:
                settings = OkxSettings.from_env()
                st.session_state.crypto_spot_catalog = [
                    item for item in OkxDemoBroker(settings).instruments()
                    if item.state == "live" and item.quote_currency == "USDT"
                ]
                st.success(
                    f"已加载 {len(st.session_state.crypto_spot_catalog)} 个 USDT 现货标的。"
                )
            except (OkxApiError, ValueError) as exc:
                st.error(f"欧易现货目录加载失败：{exc}")
        catalog = st.session_state.get("crypto_spot_catalog", [])
        query = st.text_input(
            "代码搜索", placeholder="例如 BTC 或 BTC-USDT",
            key="crypto_spot_search",
        ).strip().upper().replace("/", "-")
        if catalog:
            matches = [
                item for item in catalog
                if not query
                or item.inst_id == query
                or item.base_currency == query
                or query in item.inst_id
            ][:30]
            if matches:
                selected_symbol = st.selectbox(
                    "搜索结果",
                    [item.inst_id for item in matches],
                    key="crypto_spot_search_result",
                )
                add_col, radar_col = st.columns(2)
                if add_col.button(
                    "加入策略自选", type="primary", icon=":material/add_circle:",
                ):
                    add_crypto_watchlist_symbol(selected_symbol)
                    st.success(f"{selected_symbol} 已加入币策略自选。")
                    st.rerun()
                if radar_col.button("加入雷达候选池", icon=":material/radar:"):
                    radar_symbols = normalize_crypto_symbols((
                        *crypto_radar_config_from_env().candidates, selected_symbol,
                    ))
                    save_runtime_settings({
                        "CRYPTO_RADAR_UNIVERSE": ",".join(radar_symbols)
                    })
                    st.success(f"{selected_symbol} 已加入自动雷达候选池。")
                    st.rerun()
            else:
                st.warning("现货目录中没有找到匹配代码。")
        else:
            st.info("点击上方按钮加载目录后即可搜索和添加。")

    radar_settings = crypto_radar_config_from_env()
    with st.expander("自动雷达与候选池", expanded=False):
        st.caption(
            "雷达只扫描候选池中尚未加入策略自选的标的；满足完整买入条件后，"
            "自动加入自选并交给正常策略执行。"
        )
        with st.form("crypto_radar_config", border=True):
            radar_enabled = st.toggle("启用自动雷达", value=radar_settings.enabled)
            radar_auto_add = st.toggle(
                "满足条件后自动加入策略自选", value=radar_settings.auto_add,
            )
            radar_candidates = st.text_area(
                "雷达候选池（逗号或换行分隔）",
                value="\n".join(radar_settings.candidates),
                help="这里只是待观察池，不会直接下单；例如 SOL-USDT、XRP-USDT。",
            )
            radar_limit = st.number_input(
                "每轮最多自动加入数量", 1, 50,
                radar_settings.max_symbols_per_cycle,
                help="开启全量扫描时，会先扫描整个候选池，再按评分择优加入，最多加入这里设置的数量。",
            )
            radar_full_scan = st.toggle(
                "每轮全量扫描候选池",
                value=radar_settings.full_scan_enabled,
                help="候选池较小时建议开启；候选池很大或遇到欧易限频时关闭。",
            )
            radar_min_score = st.number_input(
                "自动加入最低评分", 0.0, 100.0,
                radar_settings.min_auto_add_score,
                step=1.0,
                help="只有买入信号、盘口价差和候选评分都通过时才会自动加入策略自选。",
            )
            max_managed = st.number_input(
                "雷达最多托管标的数", 1, 100,
                radar_settings.max_managed_symbols,
                help="只限制雷达自动加入的标的，不限制手工固定自选。",
            )
            exit_cycles = st.number_input(
                "连续无信号多少轮后自动退出", 2, 10_000,
                radar_settings.exit_after_inactive_cycles,
                help=(
                    "仅在没有持仓、没有待成交订单且无买入信号时累计；"
                    "达到轮数后从策略自选移除，但仍保留在雷达候选池。"
                ),
            )
            save_radar = st.form_submit_button("保存自动雷达配置", type="primary")
        if save_radar:
            try:
                candidates = normalize_crypto_symbols(
                    item for item in radar_candidates.replace(",", "\n").splitlines()
                    if item.strip()
                )
                save_runtime_settings({
                    "CRYPTO_RADAR_ENABLED": str(bool(radar_enabled)).lower(),
                    "CRYPTO_RADAR_AUTO_ADD": str(bool(radar_auto_add)).lower(),
                    "CRYPTO_RADAR_UNIVERSE": ",".join(candidates),
                    "CRYPTO_RADAR_MAX_SYMBOLS_PER_CYCLE": str(int(radar_limit)),
                    "CRYPTO_RADAR_MAX_MANAGED_SYMBOLS": str(int(max_managed)),
                    "CRYPTO_RADAR_EXIT_INACTIVE_CYCLES": str(int(exit_cycles)),
                    "CRYPTO_RADAR_FULL_SCAN_ENABLED": str(bool(radar_full_scan)).lower(),
                    "CRYPTO_RADAR_MIN_AUTO_ADD_SCORE": str(float(radar_min_score)),
                })
                st.success("自动雷达配置已保存，下一轮服务扫描生效。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("API 连接")
    st.caption("密钥保存到 macOS 系统钥匙串，页面不会回显；非敏感配置保存到运行配置。")
    statuses = secret_status(["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"])
    with st.container(horizontal=True):
        for item in statuses:
            st.metric(
                item["name"].replace("OKX_", ""),
                "已配置" if item["configured"] else "未配置",
                border=True,
            )
    if not keychain_available():
        st.error("当前系统钥匙串不可用，页面已禁止保存 API 密钥，请继续使用受保护的环境变量。")
    with st.form("okx_api_config", border=True):
        api_key = st.text_input(
            "Demo API Key", type="password",
            placeholder="留空表示保持现有值", autocomplete="off",
        )
        secret_key = st.text_input(
            "Demo Secret Key", type="password",
            placeholder="留空表示保持现有值", autocomplete="off",
        )
        passphrase = st.text_input(
            "Demo Passphrase", type="password",
            placeholder="留空表示保持现有值", autocomplete="off",
        )
        configured_base_url = normalize_okx_base_url(
            os.getenv("OKX_BASE_URL", "https://openapi.okx.com")
        )
        base_url = st.selectbox(
            "欧易 API 域名",
            list(OKX_API_BASE_URLS),
            index=(
                list(OKX_API_BASE_URLS).index(configured_base_url)
                if configured_base_url in OKX_API_BASE_URLS else 0
            ),
            help=(
                "普通国际版使用 openapi.okx.com；美国/澳大利亚账户使用 us.okx.com；"
                "欧洲账户使用 eea.okx.com。交易环境始终锁定为 Demo。"
            ),
        )
        timeout_seconds = st.number_input(
            "请求超时（秒）", 2.0, 60.0,
            float(os.getenv("OKX_TIMEOUT_SECONDS", "10")), step=1.0,
        )
        retry_attempts = st.number_input(
            "网络失败重试次数", 0, 5,
            int(os.getenv("OKX_RETRY_ATTEMPTS", "2")),
            help="握手超时、DNS 或临时网络失败时自动重试；不改变下单安全锁。",
        )
        retry_backoff = st.number_input(
            "重试退避秒数", 0.0, 10.0,
            float(os.getenv("OKX_RETRY_BACKOFF_SECONDS", "0.8")),
            step=0.2,
        )
        save_api = st.form_submit_button(
            "保存 API 连接配置", type="primary", disabled=not keychain_available(),
        )
    if save_api:
        try:
            for name, value in {
                "OKX_API_KEY": api_key,
                "OKX_SECRET_KEY": secret_key,
                "OKX_PASSPHRASE": passphrase,
            }.items():
                if value.strip():
                    set_secret(name, value.strip())
            save_runtime_settings(
                {
                    "OKX_TRADING_MODE": "DEMO",
                    "OKX_BASE_URL": base_url,
                    "OKX_TIMEOUT_SECONDS": str(float(timeout_seconds)),
                    "OKX_RETRY_ATTEMPTS": str(int(retry_attempts)),
                    "OKX_RETRY_BACKOFF_SECONDS": str(float(retry_backoff)),
                }
            )
            st.success("欧易 Demo API 配置已保存。留空的密钥字段保持原值。")
            st.rerun()
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("运行模式")
    with st.form("crypto_strategy_config", border=True):
        mode = st.segmented_control(
            "执行模式", ["SHADOW", "OKX_DEMO"],
            default=os.getenv("CRYPTO_EXECUTION_MODE", "SHADOW"),
            help="OKX_DEMO 仍是欧易模拟盘，但会向模拟账户提交订单。",
        )
        enable_orders = st.toggle(
            "允许提交欧易模拟订单",
            value=os.getenv("OKX_ENABLE_DEMO_ORDERS", "false").lower() == "true",
            help="只有执行模式为 OKX_DEMO 且本开关开启时才会提交模拟订单。",
        )
        interval = st.number_input(
            "自动扫描间隔（秒）", 60, 86_400,
            int(os.getenv("CRYPTO_INTERVAL_SECONDS", "300")), step=60,
        )
        budget = st.number_input(
            "单标的策略预算（USDT）", 10.0, 1_000_000.0,
            float(os.getenv("CRYPTO_VOLATILITY_BUDGET", "1000")), step=100.0,
        )
        max_order = st.number_input(
            "单笔最大金额（USDT）", 10.0, 100_000.0,
            float(os.getenv("CRYPTO_MAX_ORDER_NOTIONAL", "500")), step=50.0,
        )
        submitted = st.form_submit_button("保存运行配置", type="primary")
    if submitted:
        try:
            save_runtime_settings(
                {
                    "CRYPTO_EXECUTION_MODE": mode or "SHADOW",
                    "CRYPTO_VOLATILITY_BUDGET": str(float(budget)),
                    "CRYPTO_MAX_ORDER_NOTIONAL": str(float(max_order)),
                    "OKX_MAX_ORDER_NOTIONAL": str(float(max_order)),
                    "OKX_ENABLE_DEMO_ORDERS": str(bool(enable_orders)).lower(),
                    "OKX_TRADING_MODE": "DEMO",
                    "CRYPTO_INTERVAL_SECONDS": str(int(interval)),
                }
            )
            st.success("币策略运行配置已保存。重新运行服务后生效。")
        except ValueError as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("现货趋势参与策略参数")
    st.caption("仅保留当前启用的趋势参与、高周期确认、仓位与退出风控参数；旧突破/回调参数已从页面移除。")
    with st.form("crypto_volatility_parameters", border=True):
        st.markdown("**趋势状态过滤**")
        regime_filter = st.toggle(
            "启用市场状态分层",
            value=os.getenv("CRYPTO_REGIME_FILTER_ENABLED", "true").lower() == "true",
            help="启用后，只在趋势向上状态参与现货趋势。",
        )
        trend_window = st.number_input(
            "趋势状态窗口", 24, 1000,
            int(os.getenv("CRYPTO_TREND_WINDOW", "120")),
            help="用于判断大周期趋势，近似成熟框架里的多周期过滤。",
        )
        min_trend_strength = st.number_input(
            "最低趋势强度", 0.0, 1.0,
            float(os.getenv("CRYPTO_MIN_TREND_STRENGTH", "0.005")),
            step=0.001, format="%.3f",
        )
        max_chase_atr = st.number_input(
            "最大追价距离（ATR倍数）", 1.0, 50.0,
            float(os.getenv("CRYPTO_MAX_CHASE_ATR", "8.0")),
            step=0.5,
            help="价格离均线太远时不追，降低趋势末端入场。",
        )
        st.markdown("**趋势参与入场**")
        trend_participation_enabled = st.toggle(
            "启用趋势参与入场",
            value=os.getenv("CRYPTO_TREND_PARTICIPATION_ENABLED", "true").lower() == "true",
            help="强趋势中不等待明显回调/突破，用小仓位参与趋势。",
        )
        trend_participation_min_24h_return = st.number_input(
            "趋势参与 24h 最低动量", 0.0, 1.0,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_24H_RETURN", "0.030")),
            step=0.005, format="%.3f",
        )
        trend_participation_min_72h_return = st.number_input(
            "趋势参与 72h 最低动量", 0.0, 1.0,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_72H_RETURN", "0.045")),
            step=0.005, format="%.3f",
        )
        trend_participation_max_rsi = st.number_input(
            "趋势参与 RSI 上限", 30.0, 95.0,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_MAX_RSI", "75")),
            step=1.0,
        )
        trend_participation_max_bollinger = st.number_input(
            "趋势参与布林位置上限", 0.10, 1.50,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_MAX_BOLLINGER", "1.10")),
            step=0.05, format="%.2f",
        )
        trend_participation_min_volume_ratio = st.number_input(
            "趋势参与最低量比", 0.0, 20.0,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_MIN_VOLUME_RATIO", "0.70")),
            step=0.05, format="%.2f",
        )
        trend_participation_position_scale = st.number_input(
            "趋势参与仓位系数", 0.01, 1.0,
            float(os.getenv("CRYPTO_TREND_PARTICIPATION_POSITION_SCALE", "0.45")),
            step=0.05, format="%.2f",
            help="趋势参与不是强突破，默认只使用普通仓位的一部分。",
        )
        st.markdown("**高周期确认**")
        higher_timeframe_enabled = st.toggle(
            "启用高周期趋势确认",
            value=os.getenv("CRYPTO_HIGHER_TIMEFRAME_ENABLED", "true").lower() == "true",
            help="1H 触发入场后，再用 4H 等高周期确认大方向。已有持仓退出不受影响。",
        )
        higher_timeframe_bar = st.selectbox(
            "高周期K线",
            ["2H", "4H", "6H", "12H", "1D"],
            index=["2H", "4H", "6H", "12H", "1D"].index(
                os.getenv("CRYPTO_HIGHER_TIMEFRAME_BAR", "4H")
                if os.getenv("CRYPTO_HIGHER_TIMEFRAME_BAR", "4H") in {"2H", "4H", "6H", "12H", "1D"}
                else "4H"
            ),
        )
        higher_timeframe_min_bars = st.number_input(
            "高周期最少K线数", 24, 300,
            int(os.getenv("CRYPTO_HIGHER_TIMEFRAME_MIN_BARS", "80")),
            help="样本不足时不开新仓。",
        )
        higher_timeframe_min_trend_return = st.number_input(
            "高周期最低趋势收益", -1.0, 1.0,
            float(os.getenv("CRYPTO_HIGHER_TIMEFRAME_MIN_TREND_RETURN", "0")),
            step=0.005, format="%.3f",
        )
        target_volatility = st.number_input(
            "目标年化波动率", 0.01, 10.0,
            float(os.getenv("CRYPTO_VOL_TARGET_VOLATILITY", "0.80")),
            step=0.05, format="%.2f",
        )
        hard_stop = st.number_input(
            "硬止损比例", 0.01, 0.50,
            float(os.getenv("CRYPTO_VOL_HARD_STOP", "0.08")),
            step=0.01, format="%.2f",
        )
        profit_protection_start = st.number_input(
            "盈利保护启动比例", 0.01, 2.0,
            float(os.getenv("CRYPTO_VOL_PROFIT_PROTECTION_START", "0.10")),
            step=0.01, format="%.2f",
            help="持仓峰值盈利达到该比例后，开始计算回吐保护线。",
        )
        profit_giveback = st.number_input(
            "允许盈利回吐比例", 0.01, 0.99,
            float(os.getenv("CRYPTO_VOL_PROFIT_GIVEBACK", "0.30")),
            step=0.01, format="%.2f",
            help="例如 0.30 表示最多允许从峰值盈利回吐 30%。",
        )
        minimum_locked_profit = st.number_input(
            "最低锁定盈利比例", 0.0, 1.99,
            float(os.getenv("CRYPTO_VOL_MINIMUM_LOCKED_PROFIT", "0.04")),
            step=0.01, format="%.2f",
            help="盈利保护启用后，保护线不会低于该值。",
        )
        tactical_profit_protection_start = st.number_input(
            "早期浮盈保护启动", 0.001, 1.0,
            float(os.getenv("CRYPTO_VOL_TACTICAL_PROFIT_PROTECTION_START", "0.015")),
            step=0.001, format="%.3f",
            help="适合短线波动，达到小幅峰值盈利后开始保护。",
        )
        tactical_profit_giveback = st.number_input(
            "早期允许回吐比例", 0.01, 0.99,
            float(os.getenv("CRYPTO_VOL_TACTICAL_PROFIT_GIVEBACK", "0.55")),
            step=0.01, format="%.2f",
        )
        tactical_minimum_locked_profit = st.number_input(
            "早期最低锁定盈利", 0.0, 0.99,
            float(os.getenv("CRYPTO_VOL_TACTICAL_MINIMUM_LOCKED_PROFIT", "0.002")),
            step=0.001, format="%.3f",
        )
        st.markdown("**弱动量时间退出**")
        weak_momentum_exit_enabled = st.toggle(
            "启用弱动量时间退出",
            value=os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_ENABLED", "true").lower() == "true",
            help="持仓一段时间后仍没有形成足够浮盈，且当前收益不佳，则主动退出。",
        )
        weak_momentum_exit_hours = st.number_input(
            "弱动量观察小时数", 0.0, 240.0,
            float(os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_HOURS", "12")),
            step=1.0,
        )
        weak_momentum_exit_max_pnl = st.number_input(
            "弱动量退出当前收益阈值", -0.50, 0.50,
            float(os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_MAX_PNL", "0")),
            step=0.001, format="%.3f",
            help="例如 0 表示持有超过观察期后，当前收益不大于 0% 且峰值浮盈不足时退出。",
        )
        weak_momentum_exit_min_peak_pnl = st.number_input(
            "弱动量最低峰值浮盈", 0.0, 0.50,
            float(os.getenv("CRYPTO_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL", "0.008")),
            step=0.001, format="%.3f",
            help="峰值浮盈低于该值，说明入场后没有给出足够空间。",
        )
        partial_tier1 = st.number_input(
            "第一档止盈", 0.01, 5.0,
            float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER1", "0.025")), step=0.005,
            format="%.3f",
        )
        partial_tier1_fraction = st.number_input(
            "第一档卖出比例", 0.01, 1.0,
            float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER1_FRACTION", "0.50")), step=0.05,
        )
        partial_tier2 = st.number_input(
            "第二档止盈", 0.02, 5.0,
            float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER2", "0.050")), step=0.005,
            format="%.3f",
        )
        partial_tier2_fraction = st.number_input(
            "第二档卖出比例", 0.01, 1.0,
            float(os.getenv("CRYPTO_PARTIAL_PROFIT_TIER2_FRACTION", "0.25")), step=0.05,
        )
        max_hold_hours = st.number_input(
            "最长持有（小时）", 1.0, 10_000.0,
            float(os.getenv("CRYPTO_MAX_HOLD_HOURS", "120")), step=12.0,
        )
        contraction_ratio = st.number_input(
            "波动收缩退出倍数", 0.01, 1.0,
            float(os.getenv("CRYPTO_CONTRACTION_RATIO", "0.75")), step=0.05,
        )
        contraction_confirmations = st.number_input(
            "波动收缩连续确认次数", 1, 100,
            int(os.getenv("CRYPTO_CONTRACTION_CONFIRMATIONS", "3")),
        )
        cooldown_hours = st.number_input(
            "退出冷却（小时）", 0.0, 720.0,
            float(os.getenv("CRYPTO_COOLDOWN_HOURS", "6")), step=1.0,
        )
        max_open_positions = st.number_input(
            "最大同时持仓数", 1, 100,
            int(os.getenv("CRYPTO_MAX_OPEN_POSITIONS", "3")),
        )
        max_total_exposure = st.number_input(
            "组合最大敞口（USDT）", 10.0, 100_000_000.0,
            float(os.getenv("CRYPTO_MAX_TOTAL_EXPOSURE", "3000")), step=100.0,
        )
        max_portfolio_drawdown = st.number_input(
            "组合最大回撤熔断", 0.01, 0.99,
            float(os.getenv("CRYPTO_MAX_PORTFOLIO_DRAWDOWN", "0.15")), step=0.01,
        )
        save_parameters = st.form_submit_button("保存策略参数", type="primary")
    if save_parameters:
        try:
            values = {
                "CRYPTO_REGIME_FILTER_ENABLED": str(bool(regime_filter)).lower(),
                "CRYPTO_TREND_WINDOW": str(int(trend_window)),
                "CRYPTO_MIN_TREND_STRENGTH": str(float(min_trend_strength)),
                "CRYPTO_MAX_CHASE_ATR": str(float(max_chase_atr)),
                "CRYPTO_TREND_PARTICIPATION_ENABLED": str(bool(trend_participation_enabled)).lower(),
                "CRYPTO_TREND_PARTICIPATION_MIN_24H_RETURN": str(float(trend_participation_min_24h_return)),
                "CRYPTO_TREND_PARTICIPATION_MIN_72H_RETURN": str(float(trend_participation_min_72h_return)),
                "CRYPTO_TREND_PARTICIPATION_MAX_RSI": str(float(trend_participation_max_rsi)),
                "CRYPTO_TREND_PARTICIPATION_MAX_BOLLINGER": str(float(trend_participation_max_bollinger)),
                "CRYPTO_TREND_PARTICIPATION_MIN_VOLUME_RATIO": str(float(trend_participation_min_volume_ratio)),
                "CRYPTO_TREND_PARTICIPATION_POSITION_SCALE": str(float(trend_participation_position_scale)),
                "CRYPTO_HIGHER_TIMEFRAME_ENABLED": str(bool(higher_timeframe_enabled)).lower(),
                "CRYPTO_HIGHER_TIMEFRAME_BAR": str(higher_timeframe_bar),
                "CRYPTO_HIGHER_TIMEFRAME_MIN_BARS": str(int(higher_timeframe_min_bars)),
                "CRYPTO_HIGHER_TIMEFRAME_MIN_TREND_RETURN": str(float(higher_timeframe_min_trend_return)),
                "CRYPTO_VOL_TARGET_VOLATILITY": str(float(target_volatility)),
                "CRYPTO_VOL_HARD_STOP": str(float(hard_stop)),
                "CRYPTO_VOL_PROFIT_PROTECTION_START": str(float(profit_protection_start)),
                "CRYPTO_VOL_PROFIT_GIVEBACK": str(float(profit_giveback)),
                "CRYPTO_VOL_MINIMUM_LOCKED_PROFIT": str(float(minimum_locked_profit)),
                "CRYPTO_VOL_TACTICAL_PROFIT_PROTECTION_START": str(float(tactical_profit_protection_start)),
                "CRYPTO_VOL_TACTICAL_PROFIT_GIVEBACK": str(float(tactical_profit_giveback)),
                "CRYPTO_VOL_TACTICAL_MINIMUM_LOCKED_PROFIT": str(float(tactical_minimum_locked_profit)),
                "CRYPTO_WEAK_MOMENTUM_EXIT_ENABLED": str(bool(weak_momentum_exit_enabled)).lower(),
                "CRYPTO_WEAK_MOMENTUM_EXIT_HOURS": str(float(weak_momentum_exit_hours)),
                "CRYPTO_WEAK_MOMENTUM_EXIT_MAX_PNL": str(float(weak_momentum_exit_max_pnl)),
                "CRYPTO_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL": str(float(weak_momentum_exit_min_peak_pnl)),
                "CRYPTO_PARTIAL_PROFIT_TIER1": str(float(partial_tier1)),
                "CRYPTO_PARTIAL_PROFIT_TIER1_FRACTION": str(float(partial_tier1_fraction)),
                "CRYPTO_PARTIAL_PROFIT_TIER2": str(float(partial_tier2)),
                "CRYPTO_PARTIAL_PROFIT_TIER2_FRACTION": str(float(partial_tier2_fraction)),
                "CRYPTO_MAX_HOLD_HOURS": str(float(max_hold_hours)),
                "CRYPTO_CONTRACTION_RATIO": str(float(contraction_ratio)),
                "CRYPTO_CONTRACTION_CONFIRMATIONS": str(int(contraction_confirmations)),
                "CRYPTO_COOLDOWN_HOURS": str(float(cooldown_hours)),
                "CRYPTO_MAX_OPEN_POSITIONS": str(int(max_open_positions)),
                "CRYPTO_MAX_TOTAL_EXPOSURE": str(float(max_total_exposure)),
                "CRYPTO_MAX_PORTFOLIO_DRAWDOWN": str(float(max_portfolio_drawdown)),
            }
            previous = {key: os.environ.get(key) for key in values}
            os.environ.update(values)
            crypto_volatility_config_from_env()
            crypto_protection_config_from_env()
            save_runtime_settings(values)
            st.success("波动率策略参数已保存，回测与自动服务将使用同一配置。")
        except ValueError as exc:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            st.error(str(exc))

with backtest_tab:
    symbol = st.text_input("回测标的", "BTC-USDT", key="crypto_backtest_symbol")
    initial_cash = st.number_input("初始资金（USDT）", 100.0, 10_000_000.0, 10_000.0)
    fee_rate = st.number_input("单边手续费率", 0.0, 0.02, 0.001, format="%.4f")
    slippage = st.number_input("滑点（基点）", 0.0, 500.0, 5.0)
    backtest_bars = st.number_input(
        "回测K线根数", 100, 1500, 720, step=100,
        help="超过 300 根时自动使用 OKX 历史K线分页，样本更稳定但读取更慢。",
    )
    if st.button("读取欧易历史并回测", type="primary", icon=":material/query_stats:"):
        try:
            broker = OkxDemoBroker(OkxSettings.from_env())
            normalized_symbol = normalize_crypto_symbol(symbol)
            bars = (
                broker.historical_candles(normalized_symbol, limit=int(backtest_bars))
                if int(backtest_bars) > 300
                else broker.candles(normalized_symbol, limit=int(backtest_bars))
            )
            result = run_crypto_volatility_backtest(
                bars, float(initial_cash),
                crypto_volatility_config_from_env(),
                float(fee_rate), float(slippage),
            )
            with st.container(horizontal=True):
                st.metric("总收益率", f"{result.metrics['total_return']:.2%}", border=True)
                st.metric("相对持有", f"{result.metrics['excess_return']:.2%}", border=True)
                st.metric("最大回撤", f"{result.metrics['max_drawdown']:.2%}", border=True)
                st.metric("交易次数", int(result.metrics["trade_count"]), border=True)
                st.metric("胜率", f"{result.metrics['win_rate']:.2%}", border=True)
                st.metric("盈利因子", f"{result.metrics['profit_factor']:.2f}", border=True)
                st.metric("期末权益", f"{result.metrics['final_equity']:,.2f}", border=True)
            quality_metrics = backtest_quality_metrics(result)
            with st.container(horizontal=True):
                st.metric("验收状态", str(quality_metrics["验收状态"]), border=True)
                st.metric("质量评分", f"{float(quality_metrics['质量评分']):.1f}", border=True)
                st.metric("盈亏比", f"{float(quality_metrics['盈亏比']):.2f}", border=True)
                st.metric(
                    "浮盈兑现率",
                    f"{float(quality_metrics['浮盈兑现率']):.2%}",
                    border=True,
                )
            if quality_metrics["主要问题"] != "无明显阻塞":
                st.warning(f"主要问题：{quality_metrics['主要问题']}")
            equity_view = localize_crypto_time_columns(result.equity).rename(
                columns={
                    "date": "时间", "equity": "权益", "cash": "现金",
                    "position": "持仓数量", "close": "收盘价", "signal": "信号",
                }
            )
            trade_view = localize_crypto_time_columns(result.trades).rename(
                columns={
                    "timestamp": "成交时间", "signal_timestamp": "信号时间",
                    "side": "方向", "price": "成交价", "quantity": "数量",
                    "notional": "成交金额", "fee": "手续费",
                    "entry_price": "开仓价", "entry_timestamp": "开仓时间",
                    "entry_type": "入场类型", "exit_type": "退出类型",
                    "pnl": "平仓盈亏", "pnl_ratio": "平仓收益率",
                    "mfe": "最大浮盈", "mae": "最大浮亏",
                    "holding_bars": "持有K线",
                }
            )
            st.line_chart(equity_view, x="时间", y="权益")
            st.dataframe(
                trade_view,
                hide_index=True,
                column_config={
                    "平仓收益率": st.column_config.NumberColumn(format="percent"),
                    "最大浮盈": st.column_config.NumberColumn(format="percent"),
                    "最大浮亏": st.column_config.NumberColumn(format="percent"),
                    "平仓盈亏": st.column_config.NumberColumn(format="%.2f"),
                    "成交金额": st.column_config.NumberColumn(format="%.2f"),
                    "手续费": st.column_config.NumberColumn(format="%.4f"),
                },
            )
            quality = trade_quality_summary(result.trades)
            if not quality.empty:
                st.subheader("按入场类型拆分")
                st.dataframe(
                    quality,
                    hide_index=True,
                    column_config={
                        "胜率": st.column_config.NumberColumn(format="percent"),
                        "平均收益率": st.column_config.NumberColumn(format="percent"),
                        "平均最大浮盈": st.column_config.NumberColumn(format="percent"),
                        "平均最大浮亏": st.column_config.NumberColumn(format="percent"),
                        "总盈亏": st.column_config.NumberColumn(format="%.2f"),
                        "平均盈亏": st.column_config.NumberColumn(format="%.2f"),
                    },
                )
            latest = decide_crypto_volatility(bars)
            st.info(f"最新信号：{latest.action}，{latest.reason}")
            st.dataframe(latest.checks, hide_index=True)
            st.subheader("成熟度验证")
            lookahead = crypto_lookahead_audit(result)
            st.metric(
                "前视偏差检查",
                "通过" if lookahead["passed"] else "失败",
                help=lookahead["reason"],
            )
            monte_carlo = crypto_monte_carlo(result.equity, simulations=300)
            st.dataframe(
                pd.DataFrame([monte_carlo]),
                hide_index=True,
                column_config={
                    "收益中位数": st.column_config.NumberColumn(format="percent"),
                    "收益5%分位": st.column_config.NumberColumn(format="percent"),
                    "最大回撤95%压力": st.column_config.NumberColumn(format="percent"),
                    "亏损概率": st.column_config.NumberColumn(format="percent"),
                },
            )
            try:
                validation = crypto_walk_forward(
                    bars, float(initial_cash), crypto_volatility_config_from_env(),
                )
                st.write(
                    f"Walk-forward：{'通过' if validation['passed'] else '未通过'}"
                )
                st.dataframe(validation["windows"], hide_index=True)
            except ValueError as exc:
                st.warning(str(exc))
        except (OkxApiError, ValueError) as exc:
            st.error(str(exc))
    st.divider()
    st.subheader("单标的参数热力表")
    st.caption("使用当前回测标的已拉取的 1 小时 K 线，在本地枚举回调参数，不额外扫全市场。")
    min_closed_for_grid = st.number_input(
        "最少已平仓笔数",
        1, 20, 2,
        key="crypto_grid_min_closed",
        help="少于该数量的组合标记为样本不足，不参与推荐排序。",
    )
    show_insufficient_grid = st.checkbox(
        "显示样本不足组合",
        value=False,
        key="crypto_grid_show_insufficient",
    )
    if st.button("运行现货参数热力表", icon=":material/grid_view:"):
        try:
            broker = OkxDemoBroker(OkxSettings.from_env())
            normalized_symbol = normalize_crypto_symbol(symbol)
            bars = (
                broker.historical_candles(normalized_symbol, limit=int(backtest_bars))
                if int(backtest_bars) > 300
                else broker.candles(normalized_symbol, limit=int(backtest_bars))
            )
            grid = run_crypto_volatility_parameter_grid(
                bars, float(initial_cash),
                crypto_volatility_config_from_env(),
                float(fee_rate), float(slippage),
                min_closed_trades=int(min_closed_for_grid),
            )
            visible_grid = (
                grid if show_insufficient_grid
                else grid[grid["可验收"].astype(bool)].copy()
            )
            if visible_grid.empty:
                st.warning("没有达到最少已平仓笔数的参数组合。请勾选“显示样本不足组合”查看原因。")
            else:
                st.dataframe(
                    visible_grid,
                    hide_index=True,
                    column_config={
                        "可验收": st.column_config.CheckboxColumn(),
                        "质量评分": st.column_config.NumberColumn(format="%.1f"),
                        "回调深度": st.column_config.NumberColumn(format="percent"),
                        "布林位置上限": st.column_config.NumberColumn(format="percent"),
                        "总收益率": st.column_config.NumberColumn(format="percent"),
                        "持有收益": st.column_config.NumberColumn(format="percent"),
                        "相对持有": st.column_config.NumberColumn(format="percent"),
                        "最大回撤": st.column_config.NumberColumn(format="percent"),
                        "胜率": st.column_config.NumberColumn(format="percent"),
                        "平均最大浮盈": st.column_config.NumberColumn(format="percent"),
                        "平均最大浮亏": st.column_config.NumberColumn(format="percent"),
                        "平均平仓收益率": st.column_config.NumberColumn(format="percent"),
                        "浮盈兑现率": st.column_config.NumberColumn(format="percent"),
                        "单笔期望": st.column_config.NumberColumn(format="%.2f"),
                        "盈利因子": st.column_config.NumberColumn(format="%.2f"),
                        "盈亏比": st.column_config.NumberColumn(format="%.2f"),
                    },
                )
            insufficient = grid[~grid["可验收"].astype(bool)].copy()
            if not insufficient.empty:
                with st.expander(f"样本不足 / 无交易组合（{len(insufficient)}）"):
                    st.dataframe(
                        insufficient,
                        hide_index=True,
                        column_config={
                            "回调深度": st.column_config.NumberColumn(format="percent"),
                            "布林位置上限": st.column_config.NumberColumn(format="percent"),
                            "总收益率": st.column_config.NumberColumn(format="percent"),
                            "相对持有": st.column_config.NumberColumn(format="percent"),
                            "最大回撤": st.column_config.NumberColumn(format="percent"),
                            "胜率": st.column_config.NumberColumn(format="percent"),
                            "质量评分": st.column_config.NumberColumn(format="%.1f"),
                        },
                    )
        except (OkxApiError, ValueError) as exc:
            st.error(str(exc))
    st.divider()
    st.subheader("批量回测摘要")
    batch_symbols = st.text_area(
        "批量标的（逗号或换行分隔）",
        value="\n".join(load_crypto_watchlist() or ["BTC-USDT", "ETH-USDT"]),
        key="crypto_batch_backtest_symbols",
    )
    if st.button("批量读取并回测现货", icon=":material/table_chart:"):
        rows = []
        broker = OkxDemoBroker(OkxSettings.from_env())
        for raw_symbol in batch_symbols.replace(",", "\n").splitlines():
            if not raw_symbol.strip():
                continue
            try:
                normalized = normalize_crypto_symbol(raw_symbol)
                bars = (
                    broker.historical_candles(normalized, limit=int(backtest_bars))
                    if int(backtest_bars) > 300
                    else broker.candles(normalized, limit=int(backtest_bars))
                )
                result = run_crypto_volatility_backtest(
                    bars, float(initial_cash), crypto_volatility_config_from_env(),
                    float(fee_rate), float(slippage),
                )
                rows.append({
                    "标的": normalized,
                    "总收益率": result.metrics["total_return"],
                    "持有收益": result.metrics["benchmark_return"],
                    "相对持有": result.metrics["excess_return"],
                    "最大回撤": result.metrics["max_drawdown"],
                    "交易次数": int(result.metrics["trade_count"]),
                    "已平仓": int(result.metrics["closed_trades"]),
                    "胜率": result.metrics["win_rate"],
                    "盈利因子": result.metrics["profit_factor"],
                    "质量评分": backtest_quality_metrics(result)["质量评分"],
                    "验收状态": backtest_quality_metrics(result)["验收状态"],
                    "主要问题": backtest_quality_metrics(result)["主要问题"],
                })
            except (OkxApiError, ValueError) as exc:
                rows.append({"标的": raw_symbol.strip().upper(), "错误": str(exc)})
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                column_config={
                    "总收益率": st.column_config.NumberColumn(format="percent"),
                    "持有收益": st.column_config.NumberColumn(format="percent"),
                    "相对持有": st.column_config.NumberColumn(format="percent"),
                    "最大回撤": st.column_config.NumberColumn(format="percent"),
                    "胜率": st.column_config.NumberColumn(format="percent"),
                    "盈利因子": st.column_config.NumberColumn(format="%.2f"),
                    "质量评分": st.column_config.NumberColumn(format="%.1f"),
                },
            )

with connection_tab:
    settings = OkxSettings.from_env()
    with st.container(horizontal=True):
        st.metric("环境", "OKX DEMO", border=True)
        st.metric(
            "凭证状态", "完整" if settings.credentials_configured else "缺失",
            border=True,
        )
        st.metric("模拟下单", "开启" if settings.enable_demo_orders else "关闭", border=True)
    st.warning("只允许使用欧易 Demo Trading API Key，不要填写真实交易 API Key。")
    st.caption(
        "如果看到 SSL handshake timed out，表示网络到 OKX API 握手超时。"
        "系统会按配置自动重试；仍失败时可切换 API 域名或调大超时/重试次数。"
    )
    if st.button("测试公开行情连接", icon=":material/network_check:"):
        try:
            ticker = OkxDemoBroker(settings).ticker("BTC-USDT")
            st.success(f"公开行情连接正常，BTC-USDT 最新价：{ticker.get('last', '未知')}")
        except OkxApiError as exc:
            st.error(str(exc))
    if st.button("测试模拟账户连接", icon=":material/account_balance:"):
        try:
            balance = OkxDemoBroker(settings).account_balance()
            st.success(f"模拟账户连接正常，返回 {len(balance)} 组账户数据。")
        except OkxApiError as exc:
            st.error(str(exc))
