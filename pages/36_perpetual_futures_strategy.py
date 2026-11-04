from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import os
import time

import pandas as pd
import streamlit as st

from futu_quant.config import Settings, save_runtime_settings
from futu_quant.crypto_backtest import (
    backtest_quality_metrics,
    run_crypto_swap_managed_futures_backtest,
    trade_quality_summary,
)
from futu_quant.crypto_dashboard import localize_crypto_time_columns
from futu_quant.crypto_swap import crypto_swap_config_from_env
from futu_quant.crypto_swap_managed_futures import managed_futures_config_from_env
from futu_quant.crypto_swap_models import normalize_swap_symbol
from futu_quant.crypto_swap_service_control import start, status, stop
from futu_quant.crypto_swap_watchlist import (
    add_swap_watchlist_symbol,
    crypto_swap_radar_config_from_env,
    load_swap_radar_managed_symbols,
    remove_swap_watchlist_symbol,
    save_swap_watchlist,
)
from futu_quant.okx_broker import OkxApiError, OkxDemoBroker
from futu_quant.okx_config import OkxSettings
from futu_quant.ui import render_sidebar
from scripts.run_crypto_swap_strategy import run_once, swap_symbols_from_env


st.set_page_config(page_title="永续合约策略", page_icon="📈", layout="wide")
render_sidebar(Settings.load_cached())
st.title("永续合约策略")
st.caption("欧易 Demo USDT 永续。当前只保留 Managed Futures + 山寨币空头恢复主线配置。")

settings = OkxSettings.from_env()
service = status()
config = crypto_swap_config_from_env()
mf_config = managed_futures_config_from_env()
symbols = swap_symbols_from_env()
radar = crypto_swap_radar_config_from_env()
managed_symbols = load_swap_radar_managed_symbols()
with st.container(horizontal=True):
    st.metric("执行环境", "欧易 Demo", border=True)
    st.metric("保证金模式", "逐仓", border=True)
    st.metric("持仓模式", "净持仓", border=True)
    st.metric("杠杆", f"{config.leverage:.1f} 倍", border=True)
    st.metric("主策略引擎", mf_config.engine, border=True)
    st.metric("持续服务", service.message, border=True)
    st.metric(
        "自动雷达",
        "自动加入" if radar.enabled and radar.auto_add
        else ("仅扫描" if radar.enabled else "未启用"),
        border=True,
    )

with st.container(border=True):
    st.subheader("常用操作")
    with st.container(horizontal=True):
        if st.button("运行一次合约扫描", type="primary", icon=":material/play_arrow:"):
            try:
                with st.spinner("正在计算永续合约波动率信号……"):
                    run_once()
                st.success("本轮合约扫描完成。")
                time.sleep(.2)
                st.rerun()
            except Exception as exc:
                st.error(f"合约扫描失败：{exc}")
        if st.button(
            "启动合约服务", icon=":material/power_settings_new:",
            disabled=service.running,
        ):
            result = start(int(os.getenv("CRYPTO_SWAP_INTERVAL_SECONDS", "300")))
            st.success(f"{result.message}，PID：{result.pid}")
            st.rerun()
        if st.button(
            "停止合约服务", icon=":material/stop_circle:",
            disabled=not service.running,
        ):
            st.success(stop().message)
            st.rerun()
    if service.restart_required:
        st.warning("合约代码已更新，需停止并重新启动服务。")

config_tab, backtest_tab, catalog_tab, account_tab = st.tabs([
    "策略与自选配置", "合约回测", "查找合约", "账户初始化",
])
with config_tab:
    st.subheader("当前合约自选")
    for symbol in symbols:
        name_col, remove_col = st.columns([5, 1], vertical_alignment="center")
        source = "雷达托管" if symbol in managed_symbols else "固定自选"
        name_col.markdown(f"**{symbol}** · {source}")
        if remove_col.button(
            "移除", key=f"remove_swap_{symbol}", icon=":material/delete:",
            disabled=len(symbols) <= 1,
        ):
            remove_swap_watchlist_symbol(symbol)
            st.success(f"{symbol} 已移除。")
            st.rerun()
    with st.form("crypto_swap_config", border=True):
        symbols_text = st.text_area(
            "合约自选（逗号或换行分隔）", value="\n".join(symbols),
            help="仅允许 USDT 永续，例如 BTC-USDT-SWAP。",
        )
        mode = st.segmented_control(
            "执行模式", ["SHADOW", "OKX_DEMO"],
            default=os.getenv("CRYPTO_SWAP_EXECUTION_MODE", "SHADOW"),
        )
        st.info("当前合约主策略固定为 Managed Futures + 山寨币空头恢复；旧波动突破/均值回归参数已从配置页移除。")
        entry_enabled = st.checkbox(
            "允许合约策略新开仓",
            value=config.entry_enabled,
            help=(
                "关闭后只管理已有仓位和平仓，不会新增合约仓位。"
                "打开后由山寨币空头恢复模型决定是否开空。"
            ),
        )
        st.markdown("**新主线：Managed Futures + 山寨币空头恢复**")
        alt_short_enabled = st.checkbox(
            "启用山寨币空头恢复",
            value=mf_config.alt_short_enabled,
            help="只做空弱势山寨币；BTC/ETH 默认排除。亏损补仓必须重新通过弱势评分，不机械补。",
        )
        allow_major_short = st.checkbox(
            "允许 BTC/ETH 做空",
            value=mf_config.allow_major_short,
            help="默认关闭。你当前思路是不做空 BTC/ETH 这类正统币。",
        )
        excluded_bases_text = st.text_input(
            "空头排除币种",
            value=",".join(mf_config.excluded_bases),
            help="例如 BTC,ETH。这里配置的是基础币种，不是完整合约代码。",
        )
        mf_score_threshold = st.number_input(
            "空头弱势评分门槛",
            0.0, 100.0, mf_config.score_threshold, step=1.0,
            help="评分越高越严格。综合 1H 动量、相对 BTC 弱势、4H/日线空头结构、量能、资金费率。",
        )
        mf_relative_weakness = st.number_input(
            "相对 BTC 弱势门槛",
            -1.0, 1.0, mf_config.relative_weakness_threshold,
            step=.005, format="%.3f",
            help="币种近 72H 收益 - BTC 近 72H 收益。默认 ≤ -1.5% 才认为明显弱于 BTC。",
        )
        mf_min_volume_ratio = st.number_input(
            "Managed Futures 最低量比",
            0.0, 20.0, mf_config.min_volume_ratio, step=.05,
        )
        mf_max_negative_funding = st.number_input(
            "空头最大不利资金费率",
            -0.05, 0.05, mf_config.max_negative_funding_for_short,
            step=.0001, format="%.5f",
            help="做空时如果资金费率过度为负，说明空头付费太贵，会阻止开仓/补仓。",
        )
        initial_margin_fraction = st.number_input(
            "首仓保证金比例",
            .01, 1.0, mf_config.initial_margin_fraction,
            step=.05,
            help="首仓 = 单标的保证金预算 × 该比例。用于小批量试探。",
        )
        add_loss_steps_text = st.text_input(
            "补仓亏损阶梯",
            value=",".join(f"{value:.3f}" for value in mf_config.recovery_add_loss_steps),
            help="例如 0.03,0.06,0.10，代表亏损达到 3%、6%、10% 时允许考虑下一层补仓。",
        )
        add_margin_multipliers_text = st.text_input(
            "补仓金额倍数",
            value=",".join(f"{value:.2f}" for value in mf_config.recovery_add_margin_multipliers),
            help="相对首仓金额的倍数，例如 1.0,1.2,1.5。不建议翻倍马丁。",
        )
        max_recovery_layers = st.number_input(
            "最大补仓层数", 0, 10, mf_config.max_recovery_layers,
        )
        recovery_take_profit = st.number_input(
            "恢复转正止盈",
            0.0, .50, mf_config.recovery_take_profit,
            step=.001, format="%.3f",
        )
        max_position_loss = st.number_input(
            "单笔恢复硬止损",
            .01, .80, mf_config.max_position_loss,
            step=.01,
            help="这是反马丁兜底，避免山寨币暴拉时无限补仓。",
        )
        trend_invalidation_score = st.number_input(
            "空头趋势失效评分",
            0.0, 100.0, mf_config.trend_invalidation_score, step=1.0,
            help="已有空仓持有超过最短持有后，评分低于该值会退出。",
        )
        benchmark_symbol = st.text_input(
            "相对弱势基准合约",
            value=mf_config.benchmark_symbol,
            help="默认 BTC-USDT-SWAP。",
        )
        st.markdown("**预算与执行风控**")
        leverage = st.number_input("杠杆倍数", 1.0, 3.0, config.leverage, step=.5)
        margin_budget = st.number_input(
            "单标的保证金预算（USDT）", 10.0, 100_000.0,
            config.margin_budget, step=50.0,
        )
        short_profit_start = st.number_input(
            "做空盈利保护启动", .01, 2.0,
            config.short_profit_protection_start, step=.01,
        )
        short_profit_giveback = st.number_input(
            "做空允许盈利回吐", .01, .99,
            config.short_profit_giveback, step=.01,
        )
        interval = st.number_input(
            "扫描间隔（秒）", 60, 86_400,
            int(os.getenv("CRYPTO_SWAP_INTERVAL_SECONDS", "300")), step=60,
        )
        max_positions = st.number_input(
            "最大同时持仓", 1, 20,
            int(os.getenv("CRYPTO_SWAP_MAX_OPEN_POSITIONS", "3")),
        )
        total_margin = st.number_input(
            "最大总保证金（USDT）", 10.0, 1_000_000.0,
            float(os.getenv("CRYPTO_SWAP_MAX_TOTAL_MARGIN", "1500")), step=100.0,
        )
        partial_1 = st.number_input("第一档止盈", .01, 2.0, config.partial_profit_tier1, step=.01)
        partial_1_fraction = st.number_input("第一档减仓比例", .01, 1.0, config.partial_profit_tier1_fraction, step=.05)
        partial_2 = st.number_input("第二档止盈", .02, 3.0, config.partial_profit_tier2, step=.01)
        partial_2_fraction = st.number_input("第二档减仓比例", .01, 1.0, config.partial_profit_tier2_fraction, step=.05)
        min_hold = st.number_input("最短持有（小时）", 0.0, 240.0, config.min_hold_hours, step=.5)
        funding_exit = st.number_input("资金费率恶化退出阈值", .00001, .05, config.funding_exit_rate, step=.0001, format="%.5f")
        liquidation_buffer = st.number_input(
            "强平缓冲退出阈值", .01, .90,
            config.liquidation_buffer_exit, step=.01, format="%.2f",
            help="OKX Demo 返回强平价时，标记价距强平价低于该比例会立即退出。",
        )
        st.markdown("**自动雷达与候选生命周期**")
        radar_enabled = st.checkbox("启用合约自动雷达", value=radar.enabled)
        radar_auto_add = st.checkbox(
            "满足条件后自动加入策略自选", value=radar.auto_add,
            disabled=not radar_enabled,
        )
        radar_universe = st.text_area(
            "雷达候选池（逗号或换行分隔）",
            value="\n".join(radar.candidates),
            help="只扫描此候选池，不扫描欧易全部市场；固定自选不会被自动移除。",
        )
        radar_per_cycle = st.number_input(
            "每轮最多自动加入数量", 1, 50, radar.max_symbols_per_cycle,
            help="开启全量扫描时，会先扫描整个候选池，再按评分择优加入，最多加入这里设置的数量。",
        )
        radar_full_scan = st.checkbox(
            "每轮全量扫描候选池",
            value=radar.full_scan_enabled,
            help=(
                "候选池较小时建议开启。候选池很大或遇到 OKX 频率限制时关闭，"
                "系统会按游标分批扫描。"
            ),
        )
        radar_min_score = st.number_input(
            "自动加入最低评分", 0.0, 100.0, radar.min_auto_add_score,
            step=1.0,
            help="只有候选评分达到该门槛，且策略入场信号、资金费率、盘口价差都通过时才会自动加入。",
        )
        radar_max_managed = st.number_input(
            "雷达最多托管标的数", 1, 50, radar.max_managed_symbols,
        )
        radar_exit_cycles = st.number_input(
            "连续无信号多少轮后自动移除", 2, 10_000,
            radar.exit_after_inactive_cycles,
            help="有持仓或待成交订单时绝不会自动移除。",
        )
        max_funding = st.number_input(
            "最大资金费率绝对值", .00001, .05,
            radar.max_abs_funding_rate, step=.0001, format="%.5f",
        )
        max_spread = st.number_input(
            "最大盘口价差比例", .00001, .05,
            radar.max_spread_ratio, step=.0001, format="%.5f",
        )
        submitted = st.form_submit_button("保存合约配置", type="primary")
    if submitted:
        try:
            normalized = tuple(dict.fromkeys(
                normalize_swap_symbol(item)
                for item in symbols_text.replace(",", "\n").splitlines()
                if item.strip()
            ))
            if not normalized:
                raise ValueError("至少保留一个永续合约")
            radar_symbols = tuple(dict.fromkeys(
                normalize_swap_symbol(item)
                for item in radar_universe.replace(",", "\n").splitlines()
                if item.strip()
            ))
            if not radar_symbols:
                raise ValueError("合约雷达候选池不能为空")
            save_swap_watchlist(normalized, managed_symbols)
            save_runtime_settings({
                "CRYPTO_SWAP_EXECUTION_MODE": mode or "SHADOW",
                "CRYPTO_SWAP_ENGINE": "MANAGED_FUTURES",
                "CRYPTO_SWAP_ENTRY_ENABLED": str(bool(entry_enabled)).lower(),
                "CRYPTO_SWAP_ALT_SHORT_ENABLED": str(bool(alt_short_enabled)).lower(),
                "CRYPTO_SWAP_ALLOW_MAJOR_SHORT": str(bool(allow_major_short)).lower(),
                "CRYPTO_SWAP_ALT_SHORT_EXCLUDED_BASES": ",".join(
                    item.strip().upper()
                    for item in excluded_bases_text.replace("，", ",").split(",")
                    if item.strip()
                ),
                "CRYPTO_SWAP_MF_SCORE_THRESHOLD": str(float(mf_score_threshold)),
                "CRYPTO_SWAP_MF_RELATIVE_WEAKNESS": str(float(mf_relative_weakness)),
                "CRYPTO_SWAP_MF_MIN_VOLUME_RATIO": str(float(mf_min_volume_ratio)),
                "CRYPTO_SWAP_MF_MAX_NEGATIVE_FUNDING_SHORT": str(
                    float(mf_max_negative_funding)
                ),
                "CRYPTO_SWAP_ALT_SHORT_INITIAL_MARGIN_FRACTION": str(
                    float(initial_margin_fraction)
                ),
                "CRYPTO_SWAP_ALT_SHORT_ADD_LOSS_STEPS": ",".join(
                    item.strip()
                    for item in add_loss_steps_text.replace("，", ",").split(",")
                    if item.strip()
                ),
                "CRYPTO_SWAP_ALT_SHORT_ADD_MARGIN_MULTIPLIERS": ",".join(
                    item.strip()
                    for item in add_margin_multipliers_text.replace("，", ",").split(",")
                    if item.strip()
                ),
                "CRYPTO_SWAP_ALT_SHORT_MAX_LAYERS": str(int(max_recovery_layers)),
                "CRYPTO_SWAP_ALT_SHORT_RECOVERY_TAKE_PROFIT": str(float(recovery_take_profit)),
                "CRYPTO_SWAP_ALT_SHORT_MAX_POSITION_LOSS": str(float(max_position_loss)),
                "CRYPTO_SWAP_MF_TREND_INVALIDATION_SCORE": str(
                    float(trend_invalidation_score)
                ),
                "CRYPTO_SWAP_MF_BENCHMARK_SYMBOL": normalize_swap_symbol(benchmark_symbol),
                "CRYPTO_SWAP_LEVERAGE": str(float(leverage)),
                "CRYPTO_SWAP_MARGIN_BUDGET": str(float(margin_budget)),
                "CRYPTO_SWAP_SHORT_PROFIT_PROTECTION_START": str(float(short_profit_start)),
                "CRYPTO_SWAP_SHORT_PROFIT_GIVEBACK": str(float(short_profit_giveback)),
                "CRYPTO_SWAP_INTERVAL_SECONDS": str(int(interval)),
                "CRYPTO_SWAP_MAX_OPEN_POSITIONS": str(int(max_positions)),
                "CRYPTO_SWAP_MAX_TOTAL_MARGIN": str(float(total_margin)),
                "CRYPTO_SWAP_MAX_MARGIN_PER_POSITION": str(float(margin_budget)),
                "CRYPTO_SWAP_MAX_LEVERAGE": str(float(leverage)),
                "CRYPTO_SWAP_PARTIAL_PROFIT_TIER1": str(float(partial_1)),
                "CRYPTO_SWAP_PARTIAL_PROFIT_TIER1_FRACTION": str(float(partial_1_fraction)),
                "CRYPTO_SWAP_PARTIAL_PROFIT_TIER2": str(float(partial_2)),
                "CRYPTO_SWAP_PARTIAL_PROFIT_TIER2_FRACTION": str(float(partial_2_fraction)),
                "CRYPTO_SWAP_MIN_HOLD_HOURS": str(float(min_hold)),
                "CRYPTO_SWAP_FUNDING_EXIT_RATE": str(float(funding_exit)),
                "CRYPTO_SWAP_LIQUIDATION_BUFFER_EXIT": str(float(liquidation_buffer)),
                "CRYPTO_SWAP_RADAR_ENABLED": str(bool(radar_enabled)).lower(),
                "CRYPTO_SWAP_RADAR_AUTO_ADD": str(bool(radar_auto_add)).lower(),
                "CRYPTO_SWAP_RADAR_UNIVERSE": ",".join(radar_symbols),
                "CRYPTO_SWAP_RADAR_MAX_PER_CYCLE": str(int(radar_per_cycle)),
                "CRYPTO_SWAP_RADAR_MAX_MANAGED": str(int(radar_max_managed)),
                "CRYPTO_SWAP_RADAR_EXIT_CYCLES": str(int(radar_exit_cycles)),
                "CRYPTO_SWAP_MAX_ABS_FUNDING_RATE": str(float(max_funding)),
                "CRYPTO_SWAP_MAX_SPREAD_RATIO": str(float(max_spread)),
                "CRYPTO_SWAP_RADAR_FULL_SCAN_ENABLED": str(bool(radar_full_scan)).lower(),
                "CRYPTO_SWAP_RADAR_MIN_AUTO_ADD_SCORE": str(float(radar_min_score)),
            })
            st.success("合约配置已保存。持续服务会在下一轮重新读取。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

with backtest_tab:
    symbol = st.selectbox("回测合约", symbols)
    initial_cash = st.number_input("初始资金（USDT）", 100.0, 10_000_000.0, 10_000.0, key="swap_bt_cash")
    fee_rate = st.number_input("单边手续费率", 0.0, .02, .0005, format="%.4f", key="swap_bt_fee")
    slippage = st.number_input("滑点（基点）", 0.0, 500.0, 5.0, key="swap_bt_slip")
    funding = st.number_input("假设资金费率/8小时", -.05, .05, 0.0, format="%.5f", key="swap_bt_funding")
    backtest_entry_enabled = st.checkbox(
        "回测时启用新开仓",
        value=config.entry_enabled,
        key="swap_bt_entry_enabled",
        help="用于验证合约入场逻辑。即使正式配置关闭新开仓，也可以在回测中临时打开。",
    )
    backtest_bars = st.number_input(
        "回测K线根数", 100, 1500, 720, step=100,
        key="swap_bt_bars",
        help="超过 300 根时自动使用 OKX 历史K线分页，样本更稳定但读取更慢。",
    )
    if st.button("读取欧易历史并回测合约", type="primary", icon=":material/query_stats:"):
        try:
            broker = OkxDemoBroker(settings)
            bars = (
                broker.historical_candles(symbol, limit=int(backtest_bars))
                if int(backtest_bars) > 300
                else broker.candles(symbol, limit=int(backtest_bars))
            )
            active_config = replace(
                crypto_swap_config_from_env(),
                entry_enabled=bool(backtest_entry_enabled),
            )
            active_mf_config = managed_futures_config_from_env()
            higher_bars = (
                broker.historical_candles(
                    symbol,
                    bar=active_config.higher_timeframe_bar,
                    limit=min(1000, max(active_config.higher_timeframe_min_bars + 50, int(backtest_bars) // 4 + 50)),
                )
                if active_config.higher_timeframe_enabled else None
            )
            daily_bars = broker.historical_candles(symbol, bar="1D", limit=260)
            benchmark_bars = (
                broker.historical_candles(active_mf_config.benchmark_symbol, limit=int(backtest_bars))
                if symbol != active_mf_config.benchmark_symbol else bars
            )
            result = run_crypto_swap_managed_futures_backtest(
                symbol,
                bars,
                float(initial_cash),
                active_config,
                active_mf_config,
                float(fee_rate), float(slippage), float(funding),
                higher_timeframe_bars=higher_bars,
                daily_bars=daily_bars,
                benchmark_bars=benchmark_bars,
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
            equity = localize_crypto_time_columns(result.equity).rename(
                columns={"date": "时间", "equity": "权益", "cash": "现金", "position": "合约张数", "close": "收盘价", "signal": "信号"}
            )
            trades = localize_crypto_time_columns(result.trades).rename(
                columns={
                    "timestamp": "成交时间", "signal_timestamp": "信号时间",
                    "side": "方向", "price": "成交价", "quantity": "张数",
                    "notional": "名义金额", "fee": "手续费",
                    "entry_price": "开仓价", "entry_timestamp": "开仓时间",
                    "entry_type": "入场类型", "pnl": "平仓盈亏",
                    "pnl_ratio": "平仓收益率", "mfe": "最大浮盈",
                    "mae": "最大浮亏", "holding_bars": "持有K线",
                    "recovery_layer": "恢复层数",
                }
            )
            st.line_chart(equity, x="时间", y="权益")
            st.dataframe(
                trades,
                hide_index=True,
                column_config={
                    "平仓收益率": st.column_config.NumberColumn(format="percent"),
                    "最大浮盈": st.column_config.NumberColumn(format="percent"),
                    "最大浮亏": st.column_config.NumberColumn(format="percent"),
                    "平仓盈亏": st.column_config.NumberColumn(format="%.2f"),
                    "名义金额": st.column_config.NumberColumn(format="%.2f"),
                    "手续费": st.column_config.NumberColumn(format="%.4f"),
                },
            )
            quality = trade_quality_summary(result.trades)
            if not quality.empty:
                st.subheader("按方向拆分交易质量")
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
        except (OkxApiError, ValueError) as exc:
            st.error(str(exc))
    st.divider()
    st.subheader("批量回测摘要")
    batch_swaps = st.text_area(
        "批量合约（逗号或换行分隔）",
        value="\n".join(symbols),
        key="crypto_swap_batch_backtest_symbols",
    )
    if st.button("批量读取并回测合约", icon=":material/table_chart:"):
        rows = []
        broker = OkxDemoBroker(settings)
        for raw_symbol in batch_swaps.replace(",", "\n").splitlines():
            if not raw_symbol.strip():
                continue
            normalized = raw_symbol.strip().upper()
            try:
                bars = (
                    broker.historical_candles(normalized, limit=int(backtest_bars))
                    if int(backtest_bars) > 300
                    else broker.candles(normalized, limit=int(backtest_bars))
                )
                active_config = replace(
                    crypto_swap_config_from_env(),
                    entry_enabled=bool(backtest_entry_enabled),
                )
                active_mf_config = managed_futures_config_from_env()
                higher_bars = (
                    broker.historical_candles(
                        normalized,
                        bar=active_config.higher_timeframe_bar,
                        limit=min(1000, max(active_config.higher_timeframe_min_bars + 50, int(backtest_bars) // 4 + 50)),
                    )
                    if active_config.higher_timeframe_enabled else None
                )
                daily_bars = broker.historical_candles(normalized, bar="1D", limit=260)
                benchmark_bars = (
                    broker.historical_candles(
                        active_mf_config.benchmark_symbol,
                        limit=int(backtest_bars),
                    )
                    if normalized != active_mf_config.benchmark_symbol else bars
                )
                result = run_crypto_swap_managed_futures_backtest(
                    normalized,
                    bars,
                    float(initial_cash),
                    active_config,
                    active_mf_config,
                    float(fee_rate), float(slippage), float(funding),
                    higher_timeframe_bars=higher_bars,
                    daily_bars=daily_bars,
                    benchmark_bars=benchmark_bars,
                )
                rows.append({
                    "合约": normalized,
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
                rows.append({"合约": normalized, "错误": str(exc)})
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

with catalog_tab:
    if st.button("加载欧易 USDT 永续目录", icon=":material/sync:"):
        try:
            st.session_state.swap_catalog = [
                item for item in OkxDemoBroker(settings).swap_instruments()
                if item.state == "live"
            ]
        except OkxApiError as exc:
            st.error(str(exc))
    catalog = st.session_state.get("swap_catalog", [])
    query = st.text_input("合约代码搜索", placeholder="BTC 或 BTC-USDT-SWAP").upper()
    matches = [
        item.inst_id for item in catalog
        if not query or query in item.inst_id
    ][:30]
    if matches:
        selected_swap = st.selectbox("搜索结果", matches)
        if st.button(
            "加入合约自选", type="primary", icon=":material/add_circle:",
        ):
            add_swap_watchlist_symbol(selected_swap)
            st.success(f"{selected_swap} 已加入合约自选。")
            st.rerun()
    elif catalog:
        st.info("没有匹配的 USDT 永续合约。")
    else:
        st.info("点击按钮后按需加载合约目录。")

with account_tab:
    st.warning("初始化会修改欧易 Demo 账户的持仓模式和所选合约杠杆。")
    if st.button(
        "初始化净持仓与逐仓杠杆", type="primary",
        icon=":material/admin_panel_settings:",
        disabled=not settings.credentials_configured,
    ):
        broker = OkxDemoBroker(settings)
        try:
            broker.set_position_mode("net_mode")
            for symbol in symbols:
                broker.set_swap_leverage(symbol, Decimal(str(config.leverage)))
            st.success(f"已设置净持仓，并为 {len(symbols)} 个合约设置逐仓杠杆。")
        except OkxApiError as exc:
            st.error(str(exc))
