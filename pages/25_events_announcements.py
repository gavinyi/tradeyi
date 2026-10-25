from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.market_information import SecFilingsClient, combined_watchlist
from futu_quant.ui import open_trading_store as TradingStore
from futu_quant.ui import china_time_columns, render_sidebar


st.set_page_config(page_title="事件与公告", page_icon="🗓️", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = TradingStore()

st.title("事件与公告")
st.caption(
    "结构化事件来自系统财报日历；公司公告来自 SEC EDGAR。资讯用于辅助判断，不直接触发自动下单。"
)


@st.cache_data(ttl="15m", max_entries=50, show_spinner=False)
def load_sec_filings(symbol: str, forms: tuple[str, ...]) -> tuple[dict, pd.DataFrame]:
    company, rows = SecFilingsClient().recent_filings(symbol, set(forms), limit=100)
    return {
        "代码": company.ticker,
        "公司": company.name,
        "交易所": company.exchange,
        "CIK": company.cik,
    }, pd.DataFrame(rows)


view = st.segmented_control(
    "资讯功能",
    ["事件日历", "SEC 公司公告", "系统资讯流"],
    default="事件日历",
    selection_mode="single",
)

watchlist = combined_watchlist(settings)
watch_symbols = tuple(watchlist["代码"].astype(str))

if view == "事件日历":
    rows = pd.DataFrame(store.earnings_events(limit=5000))
    if rows.empty:
        st.info("系统尚未保存财报事件；统一交易服务会按日刷新财报日历。")
        st.stop()
    rows["event_date"] = pd.to_datetime(rows["event_date"], errors="coerce")
    today = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).normalize()
    rows["距离天数"] = (rows["event_date"] - today).dt.days
    scope = st.pills(
        "标的范围", ["全部已缓存事件", "当前全部自选", "财报 Gamma 自选"],
        default="当前全部自选", selection_mode="single",
    )
    day_range = st.slider("日期范围（相对今天）", -14, 90, (-3, 45))
    filtered = rows[rows["距离天数"].between(day_range[0], day_range[1])]
    if scope == "当前全部自选":
        filtered = filtered[filtered["symbol"].isin(watch_symbols)]
    elif scope == "财报 Gamma 自选":
        filtered = filtered[filtered["symbol"].isin(settings.gamma_symbols)]
    session_names = {
        "BEFORE_MARKET": "盘前", "AFTER_MARKET": "盘后",
        "DURING_MARKET": "盘中", "UNKNOWN": "未知",
        "BMO": "盘前", "AMC": "盘后",
    }
    display = filtered.rename(columns={
        "symbol": "代码", "event_date": "财报日期", "session": "公布时段",
        "source": "来源", "confirmed": "双源确认", "fetched_at": "抓取时间",
    }).copy()
    if not display.empty:
        display["公布时段"] = display["公布时段"].map(session_names).fillna(display["公布时段"])
        display["双源确认"] = display["双源确认"].map({1: "是", 0: "否"}).fillna("否")
        display = china_time_columns(display.rename(columns={"抓取时间": "fetched_at"})).rename(
            columns={"fetched_at": "抓取时间（中国）"}
        )
        display = display.sort_values(["财报日期", "代码"])
    upcoming = display[display["距离天数"].between(0, 7)] if not display.empty else display
    with st.container(horizontal=True):
        st.metric("范围内事件", len(display), border=True)
        st.metric("未来 7 天", len(upcoming), border=True)
        st.metric(
            "已双源确认",
            int(display["双源确认"].eq("是").sum()) if not display.empty else 0,
            border=True,
        )
        st.metric(
            "财报策略标的",
            int(display["代码"].isin(settings.gamma_symbols).sum()) if not display.empty else 0,
            border=True,
        )
    if display.empty:
        st.info("当前范围没有事件。")
    else:
        st.dataframe(
            display, hide_index=True,
            column_config={
                "代码": st.column_config.TextColumn("代码", pinned=True),
                "财报日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "距离天数": st.column_config.NumberColumn(format="%d 天"),
            },
        )
        st.caption("未双源确认的日期只作为提示；财报事件模式仍按自身校验规则处理。")

elif view == "SEC 公司公告":
    if not watch_symbols:
        st.info("请先添加自选标的。")
        st.stop()
    if not os.getenv("SEC_USER_AGENT"):
        st.info(
            "当前使用低频本地标识访问 SEC。建议在 .env 配置 "
            "SEC_USER_AGENT=你的名称 your_email@example.com，便于遵守 SEC 自动访问规范。"
        )
    with st.form("sec_filing_search", border=True):
        symbol = st.selectbox("公司代码", watch_symbols)
        forms = st.multiselect(
            "公告类型",
            ["8-K", "10-Q", "10-K", "6-K", "20-F", "40-F", "S-1", "DEF 14A", "4"],
            default=["8-K", "10-Q", "10-K"],
            help="8-K 为重大事项，10-Q/10-K 为季报/年报，Form 4 为内部人交易。",
        )
        submitted = st.form_submit_button(
            "读取 SEC 公告", type="primary", icon=":material/article:"
        )
    if submitted:
        try:
            with st.spinner("正在读取 SEC EDGAR 公司目录与公告……"):
                company, filings = load_sec_filings(symbol, tuple(forms))
            st.session_state.sec_company = company
            st.session_state.sec_filings = filings
            st.session_state.sec_symbol = symbol
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
    if st.session_state.get("sec_symbol") == symbol:
        company = st.session_state.get("sec_company", {})
        filings = st.session_state.get("sec_filings", pd.DataFrame())
        if company:
            with st.container(horizontal=True):
                st.metric("公司", company.get("公司", "—"), border=True)
                st.metric("交易所", company.get("交易所", "—"), border=True)
                st.metric("CIK", company.get("CIK", "—"), border=True)
                st.metric("匹配公告", len(filings), border=True)
        if isinstance(filings, pd.DataFrame) and not filings.empty:
            st.dataframe(
                filings, hide_index=True,
                column_config={
                    "表格类型": st.column_config.TextColumn("表格类型", pinned=True),
                    "提交日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                    "报告日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                    "公告链接": st.column_config.LinkColumn("打开 SEC 原文", display_text="打开原文"),
                },
            )
        elif submitted:
            st.info("该公司当前没有匹配所选类型的近期公告。")

elif view == "系统资讯流":
    rows = pd.DataFrame(store.operational_events(limit=1000))
    if rows.empty:
        st.info("尚无系统资讯事件。")
        st.stop()
    status_filter = st.pills(
        "状态", ["全部", "HEALTHY", "DEGRADED", "ERROR", "RECOVERED"],
        default="全部", selection_mode="single",
    )
    if status_filter != "全部":
        rows = rows[rows["status"].astype(str) == status_filter]

    def readable_payload(value: str) -> str:
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return str(value)
        if not payload:
            return "—"
        return "；".join(f"{key}：{item}" for key, item in payload.items())

    rows["详情"] = rows["payload_json"].map(readable_payload)
    rows = china_time_columns(rows).rename(columns={
        "component": "组件", "event_type": "事件", "status": "状态",
        "created_at": "时间（中国）",
    })
    visible = [column for column in ["时间（中国）", "组件", "事件", "状态", "详情"] if column in rows]
    with st.container(horizontal=True):
        st.metric("显示事件", len(rows), border=True)
        st.metric("异常或降级", int(rows["状态"].isin(["ERROR", "DEGRADED"]).sum()), border=True)
        st.metric("已恢复", int(rows["状态"].eq("RECOVERED").sum()), border=True)
    st.dataframe(rows[visible], hide_index=True)
    st.caption("系统资讯流用于解释数据源、日历、雷达和执行服务发生了什么，不等同于财经新闻。")
