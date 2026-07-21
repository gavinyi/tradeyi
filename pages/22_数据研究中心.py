from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from futu_quant.backtest import run_backtest
from futu_quant.backtest_metrics import calculate_backtest_metrics
from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings, normalize_us_symbol
from futu_quant.market_data import (
    AlpacaMarketDataProvider,
    FutuMarketDataProvider,
    YahooMarketDataProvider,
)
from futu_quant.market_data_store import (
    CanonicalMarketDataStore,
    MarketDataRouter,
    reconcile_sources,
)
from futu_quant.qlib_adapter import QlibResearchAdapter
from futu_quant.strategies import STRATEGIES
from futu_quant.ui import chinese_columns, render_admin_login, render_sidebar


st.set_page_config(page_title="数据研究中心", page_icon="🔬", layout="wide")
settings = Settings.load()
render_sidebar(settings)
admin = render_admin_login()
warehouse = CanonicalMarketDataStore()

st.title("数据研究中心")
st.caption(
    "第三方行情只用于历史补全、质量校验和研究；实时交易价格、账户和订单仍只以富途模拟盘为准。"
)


@st.cache_data(ttl="30s", max_entries=4, show_spinner=False)
def load_catalog() -> pd.DataFrame:
    return CanonicalMarketDataStore().catalog()


def providers():
    broker = FutuPaperBroker(Settings.load())
    return {
        "富途（主历史源）": FutuMarketDataProvider(broker),
        "Alpaca（IEX 研究补全）": AlpacaMarketDataProvider.from_env(),
        "Yahoo（三级研究备用）": YahooMarketDataProvider(),
    }


view = st.segmented_control(
    "研究功能",
    ["数据源状态", "数据目录", "数据质量", "回测 V2"],
    default="数据源状态",
    selection_mode="single",
)

if view == "数据源状态":
    source_rows = []
    for label, provider in providers().items():
        status = provider.health()
        source_rows.append(
            {
                "数据源": label,
                "已配置": "是" if status.configured else "否",
                "当前可用": "是" if status.available else "否",
                "用途": "仅研究" if status.research_only else "交易",
                "说明": status.message,
            }
        )
    qlib = QlibResearchAdapter().status()
    source_rows.append(
        {
            "数据源": "Qlib 研究引擎",
            "已配置": "是" if qlib.available else "否",
            "当前可用": "是" if qlib.available else "否",
            "用途": "仅研究",
            "说明": qlib.message,
        }
    )
    st.subheader("数据源状态")
    st.dataframe(pd.DataFrame(source_rows), hide_index=True, width="stretch")
    st.info("Alpaca 免费股票数据是 IEX 口径；不能直接替换富途全市场成交量或期权盘口。")

elif view == "数据目录":
    st.subheader("数据目录")
    catalog = load_catalog()
    if catalog.empty:
        st.info("尚未建立规范历史数据。请在下方按标的和日期显式执行第一次补全。")
    else:
        with st.container(horizontal=True):
            st.metric("数据分区", len(catalog), border=True)
            st.metric("规范行情行数", int(catalog["row_count"].sum()), border=True)
            st.metric("覆盖标的", int(catalog["symbol"].nunique()), border=True)
            st.metric("A级分区", int(catalog["quality_grade"].eq("A").sum()), border=True)
        display = catalog.rename(
            columns={
                "symbol": "代码", "source": "来源", "adjustment": "复权口径",
                "start": "开始日期", "end": "结束日期", "row_count": "行数",
                "dataset_version": "数据版本", "content_hash": "内容哈希",
                "path": "本地路径", "quality_grade": "质量等级", "updated_at": "更新时间",
            }
        )
        st.dataframe(display, hide_index=True, width="stretch")

    with st.form("market_data_backfill", border=True):
        st.markdown("**历史数据补全**")
        symbol_input = st.text_input("美股代码", value="SPY", help="支持 SPY 或 US.SPY")
        source_choice = st.selectbox(
            "数据源顺序",
            ["自动：富途 → Alpaca → Yahoo", "仅富途", "仅 Alpaca", "仅 Yahoo"],
        )
        end_date = st.date_input("结束日期", value=date.today())
        start_date = st.date_input("开始日期", value=end_date - timedelta(days=365 * 3))
        submitted = st.form_submit_button(
            "开始补全并保存", type="primary", disabled=not admin,
            icon=":material/download:",
        )
    if submitted:
        try:
            code = normalize_us_symbol(symbol_input)
            choices = providers()
            if source_choice == "仅富途":
                selected = [choices["富途（主历史源）"]]
            elif source_choice == "仅 Alpaca":
                selected = [choices["Alpaca（IEX 研究补全）"]]
            elif source_choice == "仅 Yahoo":
                selected = [choices["Yahoo（三级研究备用）"]]
            else:
                selected = list(choices.values())
            with st.spinner("正在获取并规范化历史行情……"):
                routed = MarketDataRouter(selected).get_bars(code, start_date, end_date)
                manifest = warehouse.write_bars(routed.frame, routed.source)
            st.cache_data.clear()
            st.success(
                f"已保存 {manifest.row_count} 行；实际来源 {routed.source}；"
                f"数据版本 {manifest.dataset_version}。"
            )
            st.dataframe(
                pd.DataFrame(
                    [{"数据源": row.provider, "结果": row.status, "说明": row.message}
                     for row in routed.attempts]
                ),
                hide_index=True,
                width="stretch",
            )
        except Exception as exc:
            st.error(f"补全失败：{exc}")

elif view == "数据质量":
    st.subheader("数据质量")
    catalog = load_catalog()
    symbols = sorted(catalog["symbol"].astype(str).unique()) if not catalog.empty else []
    if not symbols:
        st.info("先在“数据目录”完成至少一个标的的历史数据补全。")
    else:
        symbol = st.selectbox("检查标的", symbols)
        frame = warehouse.read_bars(symbol, include_all_sources=True)
        duplicated = int(frame.duplicated(["symbol", "timestamp", "adjustment"]).sum())
        quarantined = int(frame["quality_status"].astype(str).eq("QUARANTINED").sum())
        expected_days = pd.bdate_range(frame["session_date"].min(), frame["session_date"].max())
        observed = set(frame["session_date"].astype(str))
        missing_weekdays = sum(day.strftime("%Y-%m-%d") not in observed for day in expected_days)
        with st.container(horizontal=True):
            st.metric("行情行数", len(frame), border=True)
            st.metric("重复行", duplicated, border=True)
            st.metric("隔离行", quarantined, border=True)
            st.metric("缺失工作日（未扣美股假日）", missing_weekdays, border=True)
        sources = sorted(frame["source"].astype(str).unique())
        if len(sources) >= 2:
            primary_name = st.selectbox("主来源", sources, index=0)
            secondary_name = st.selectbox("对照来源", sources, index=1)
            result = reconcile_sources(
                frame[frame["source"] == primary_name],
                frame[frame["source"] == secondary_name],
            )
            if result.quality_status == "GOOD":
                st.success(result.reason)
            else:
                st.warning(result.reason)
        else:
            st.caption("当前只有一个来源；补充第二来源后可进行同交易日价格交叉校验。")
        st.dataframe(chinese_columns(frame.tail(500)), hide_index=True, width="stretch")

elif view == "回测 V2":
    st.subheader("回测 V2")
    catalog = load_catalog()
    symbols = sorted(catalog["symbol"].astype(str).unique()) if not catalog.empty else []
    if not symbols:
        st.info("请先在“数据目录”补全需要回测的标的。")
    else:
        with st.form("canonical_backtest_v2", border=True):
            symbol = st.selectbox("回测标的", symbols)
            strategy = st.selectbox("基础策略", list(STRATEGIES))
            initial_cash = st.number_input("初始资金（美元）", 1_000.0, 100_000_000.0, 100_000.0)
            quantity = st.number_input("每次交易股数", 1, 100_000, 100)
            commission = st.number_input("每笔手续费（美元）", 0.0, 1000.0, 1.0)
            slippage = st.number_input("滑点（基点）", 0.0, 1000.0, 5.0)
            run = st.form_submit_button("运行回测 V2", type="primary", icon=":material/query_stats:")
        if run:
            try:
                bars = warehouse.read_bars(symbol)
                input_bars = bars.rename(columns={"timestamp": "time_key"})
                result = run_backtest(
                    input_bars, strategy, float(initial_cash), int(quantity),
                    float(commission), float(slippage),
                )
                benchmark_bars = warehouse.read_bars("US.SPY")
                benchmark = benchmark_bars["close"] if not benchmark_bars.empty else None
                metrics = calculate_backtest_metrics(
                    result.equity, result.trades, float(initial_cash), benchmark,
                )
                with st.container(horizontal=True):
                    st.metric("总收益率", f"{metrics['total_return']:.2%}", border=True)
                    st.metric("年化收益率", f"{metrics['cagr']:.2%}", border=True)
                    st.metric("最大回撤", f"{metrics['max_drawdown']:.2%}", border=True)
                    st.metric("夏普比率", f"{metrics['sharpe']:.2f}", border=True)
                    st.metric("索提诺比率", f"{metrics['sortino']:.2f}", border=True)
                    st.metric("换手率", f"{metrics['turnover']:.2f}", border=True)
                st.line_chart(result.equity.set_index("date")[["equity"]])
                st.dataframe(
                    pd.DataFrame(
                        [{
                            "基准收益率": metrics["benchmark_return"],
                            "超额收益率": metrics["excess_return"],
                            "Alpha": metrics["alpha"], "Beta": metrics["beta"],
                            "盈利因子": metrics["profit_factor"],
                            "每笔期望值": metrics["expectancy"],
                            "最大回撤持续交易日": int(metrics["max_drawdown_duration"]),
                        }]
                    ),
                    hide_index=True,
                    width="stretch",
                )
                st.dataframe(chinese_columns(result.trades), hide_index=True, width="stretch")
            except ValueError as exc:
                st.error(str(exc))
