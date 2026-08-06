from __future__ import annotations

import os

import streamlit as st

from futu_quant.config import Settings, save_real_trading_settings
from futu_quant.real_trading import (
    confirmation_code,
    disable_session,
    enable_session,
    session_status,
    submitted_real_notional_since,
)
from futu_quant.risk import ny_trading_day_start_utc
from futu_quant.store import TradingStore
from futu_quant.strategy_plugins import strategy_plugins
from futu_quant.ui import render_admin_login, render_sidebar


st.set_page_config(page_title="实盘交易控制", page_icon="🔐", layout="wide")

try:
    settings = Settings.load(refresh=True)
except Exception as exc:
    settings = None
    st.error(f"当前交易配置无法加载：{exc}")
    st.caption("如果是刚刚手动修改 .env 导致，请把 FUTU_TRADING_ENV 改回 SIMULATE 后刷新。")

if settings is not None:
    render_sidebar(settings)
admin = render_admin_login()
store = TradingStore()

st.title("实盘交易控制")
st.caption("把实盘接入做成可配置能力，但默认锁定。建议长期先跑模拟盘，只有小额度验证时短时打开。")

if settings is None:
    st.stop()

active, status_reason, session = session_status(store, settings)
day_start = ny_trading_day_start_utc()
used_today = submitted_real_notional_since(store, day_start.isoformat())

with st.container(horizontal=True):
    st.metric("当前券商环境", "实盘 REAL" if settings.trading_env == "REAL" else "模拟盘 SIMULATE", border=True)
    st.metric("实盘总开关", "已开启" if settings.real_trading_enabled else "关闭", border=True)
    st.metric("实盘确认会话", "有效" if active else "无效", border=True)
    st.metric(
        "今日实盘名义金额",
        f"${used_today:,.2f}",
        delta=f"上限 ${settings.real_max_daily_notional:,.0f}",
        border=True,
    )

if settings.trading_env == "REAL":
    st.error(
        "当前已选择 REAL 实盘环境。请确认 OpenD 已登录真实账户，并且你确实只对白名单策略开放小额度交易。",
        icon=":material/warning:",
    )
else:
    st.success("当前仍处于模拟盘环境。下面的实盘配置不会产生真实订单。")

config_tab, session_tab, checklist_tab = st.tabs(["实盘配置", "短时确认会话", "上线检查清单"])

plugins = strategy_plugins()
strategy_options = [plugin.strategy_id for plugin in plugins]
strategy_labels = {plugin.strategy_id: plugin.display_name for plugin in plugins}

with config_tab:
    st.subheader("实盘基础配置")
    st.caption("保存后会写入运行时配置和 .env 镜像；不保存任何富途交易密码。")
    with st.form("real_trading_config"):
        trading_env = st.selectbox(
            "券商交易环境",
            ["SIMULATE", "REAL"],
            index=0 if settings.trading_env != "REAL" else 1,
            help="SIMULATE=模拟盘；REAL=真实账户。REAL 还需要下面所有安全条件通过。",
        )
        enabled = st.checkbox(
            "开启实盘总开关 REAL_TRADING_ENABLED",
            value=settings.real_trading_enabled,
            help="只在 FUTU_TRADING_ENV=REAL 时有意义。关闭时任何 REAL 订单都会被拒绝。",
        )
        account_id = st.text_input(
            "实盘账户 ID（REAL_ACCOUNT_ID）",
            value=str(settings.real_account_id or ""),
            help="必须和 OpenD 返回的真实账户 acc_id 一致。模拟账户继续使用 FUTU_ACC_ID。",
        )
        allowed = st.multiselect(
            "允许进入实盘的策略白名单",
            strategy_options,
            default=[item for item in settings.real_allowed_strategies if item in strategy_options],
            format_func=lambda value: strategy_labels.get(value, value),
        )
        max_order = st.number_input(
            "实盘单笔最大名义金额（美元）",
            min_value=1.0,
            max_value=10_000_000.0,
            value=float(settings.real_max_order_notional),
        )
        max_daily = st.number_input(
            "实盘每日最大名义金额（美元）",
            min_value=1.0,
            max_value=100_000_000.0,
            value=float(settings.real_max_daily_notional),
        )
        require_confirm = st.checkbox(
            "每次实盘运行都需要短时人工确认会话",
            value=settings.real_require_manual_confirm,
        )
        acknowledged = st.checkbox("我确认：REAL 会向真实账户提交订单，且 OpenD 交易密码不由本系统保存。")
        submitted = st.form_submit_button("保存实盘配置", type="primary", disabled=not admin)
        if submitted:
            if trading_env == "REAL" and not acknowledged:
                st.error("切换 REAL 前必须勾选确认。")
            else:
                try:
                    save_real_trading_settings(
                        trading_env,
                        enabled,
                        account_id,
                        allowed,
                        max_order,
                        max_daily,
                        require_confirm,
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    store.audit(
                        "REAL_TRADING_CONFIG_UPDATED",
                        {
                            "trading_env": trading_env,
                            "enabled": enabled,
                            "account_id": account_id,
                            "allowed_strategies": allowed,
                            "max_order_notional": max_order,
                            "max_daily_notional": max_daily,
                            "require_manual_confirm": require_confirm,
                        },
                    )
                    st.success("实盘配置已保存。")
                    st.rerun()

with session_tab:
    st.subheader("短时确认会话")
    if active:
        st.success(status_reason)
        st.json(session, expanded=False)
    else:
        st.warning(status_reason)
    st.info(
        "系统不会保存富途交易密码。真实下单前，请先在富途 OpenD/客户端完成交易解锁；"
        "本页只负责系统内部的短时放行。"
    )
    expected_code = confirmation_code(settings)
    with st.form("real_trading_session"):
        st.code(expected_code, language=None)
        code = st.text_input("输入上方确认码")
        minutes = st.number_input("会话有效分钟数", 1, 240, 30)
        enable = st.form_submit_button("开启实盘确认会话", type="primary", disabled=not admin)
        if enable:
            ok, message = enable_session(store, settings, code, int(minutes))
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
    if st.button("立即关闭实盘确认会话", disabled=not admin, icon=":material/lock:"):
        disable_session(store)
        st.success("实盘确认会话已关闭。")
        st.rerun()

with checklist_tab:
    st.subheader("上线前检查清单")
    checks = [
        ("当前环境为 REAL", settings.trading_env == "REAL"),
        ("实盘总开关开启", settings.real_trading_enabled),
        ("配置了 REAL_ACCOUNT_ID", bool(settings.real_account_id)),
        ("至少配置一个实盘策略白名单", bool(settings.real_allowed_strategies)),
        ("单笔额度小于等于日额度", settings.real_max_order_notional <= settings.real_max_daily_notional),
        ("短时确认会话有效", active or not settings.real_require_manual_confirm),
        ("策略下单总开关开启", settings.enable_paper_orders),
        ("OpenD 地址已配置", bool(settings.host and settings.port)),
    ]
    for label, passed in checks:
        if passed:
            st.success(label, icon=":material/check_circle:")
        else:
            st.error(label, icon=":material/cancel:")
    st.caption(
        f"当前执行模式：{store.get_setting('execution_mode', os.getenv('EXECUTION_MODE', 'PAPER')).upper()}。"
        "只有策略执行模式实际为 REAL 时，才会进入实盘闸门。"
    )
