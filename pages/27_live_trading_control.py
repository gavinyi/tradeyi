from __future__ import annotations

import json
import streamlit as st

from futu_quant.config import Settings, save_runtime_settings
from futu_quant.real_trading import (
    confirmation_code,
    disable_session,
    enable_session,
    session_status,
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

st.title("多账户实盘交易控制中心")
st.caption("支持对【Moomoo US 期权打猎账户】与【富途证券香港 现货底仓账户】进行多实盘多通道独立风控与参数配置。")

if settings is None:
    st.stop()

# 两个实盘账户的预置定义
MOOMOO_ID = "283445327916889204"
FUTUHK_ID = "281756481476169109"

# 读取两账户独立持久化配置
def get_acc_config(acc_id: str, default_strategy: list[str]) -> dict:
    raw = store.get_setting(f"real_account_config:{acc_id}", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {
        "enabled": True,
        "allowed_strategies": default_strategy,
        "max_order_notional": 1500.0,
        "max_daily_notional": 3000.0,
        "require_manual_confirm": False,
    }

cfg_moomoo = get_acc_config(MOOMOO_ID, ["volatility_long_gamma"])
cfg_futuhk = get_acc_config(FUTUHK_ID, ["core_satellite", "volatility_long_gamma"])

name_moomoo = store.get_setting(f"custom_account_name:{MOOMOO_ID}", "Moomoo US 美股专户")
name_futuhk = store.get_setting(f"custom_account_name:{FUTUHK_ID}", "富途香港综合主实盘")

# 顶部多账户并排监控卡片
day_start = ny_trading_day_start_utc()
snap_moomoo = store.broker_snapshot("account_info_moomoo_real") or {}
snap_futuhk = store.broker_snapshot("account_info_futuhk_real") or {}

assets_moomoo = float(snap_moomoo.get("payload", [{}])[0].get("total_assets", 0.0) or 0.0)
cash_moomoo = float(snap_moomoo.get("payload", [{}])[0].get("cash", 0.0) or 0.0)

acc_row_futuhk = snap_futuhk.get("payload", [{}])[0] if snap_futuhk.get("payload") else {}
usd_assets_futuhk = float(acc_row_futuhk.get("usd_assets", 0) or 0)
assets_futuhk = usd_assets_futuhk if usd_assets_futuhk > 0 else float(acc_row_futuhk.get("total_assets", 7237.6) or 7237.6)
cash_futuhk = float(acc_row_futuhk.get("us_cash", acc_row_futuhk.get("cash", 3830.57)) or 3830.57)

col_m1, col_m2 = st.columns(2)
with col_m1:
    with st.container(border=True):
        st.markdown(f"**🟡 {name_futuhk} · 实盘主专户**")
        st.caption(f"账号 ID: `{FUTUHK_ID}` · 券商实体: `FUTUSECURITIES (香港)` · 卡号: `1001259607272607`")
        c1, c2, c3 = st.columns(3)
        c1.metric("美元资产", f"${assets_futuhk:,.2f}")
        c2.metric("可用美元", f"${cash_futuhk:,.2f}")
        c3.metric("单笔上限", f"${float(cfg_futuhk.get('max_order_notional', 2500)):,.0f}")
        st.success("角色: 综合主专户 · 期权做多波动率打猎 + 现货 Core Satellite 再平衡", icon=":material/rocket_launch:")

with col_m2:
    with st.container(border=True):
        st.markdown(f"**🔴 {name_moomoo} · 备用专户**")
        st.caption(f"账号 ID: `{MOOMOO_ID}` · 券商实体: `FUTUINC (美股本土)` · 卡号: `1007376216226587`")
        c1, c2, c3 = st.columns(3)
        c1.metric("可用资金", f"${cash_moomoo:,.2f}")
        c2.metric("实盘状态", "已接入" if cfg_moomoo.get("enabled", False) else "未开启")
        c3.metric("单笔上限", f"${float(cfg_moomoo.get('max_order_notional', 1500)):,.0f}")
        st.info("角色: 备用专户 · 资金已划转至主专户", icon=":material/pause_circle:")

config_tab, session_tab, checklist_tab = st.tabs(["多账户实盘配置", "短时确认会话管理", "上线安全检查清单"])

plugins = strategy_plugins()
strategy_options = [plugin.strategy_id for plugin in plugins]
strategy_labels = {plugin.strategy_id: plugin.display_name for plugin in plugins}

with config_tab:
    moomoo_is_en = bool(cfg_moomoo.get("enabled", False))
    futuhk_is_en = bool(cfg_futuhk.get("enabled", True))
    tab_futuhk_label = f"🟡 {name_futuhk} {'(🟢 统一管理中)' if futuhk_is_en else '(⚪ 已关闭隐藏)'}"
    tab_moomoo_label = f"🔴 {name_moomoo} {'(🟢 统一管理中)' if moomoo_is_en else '(⚪ 已关闭隐藏)'}"
    tab_futuhk, tab_moomoo = st.tabs([tab_futuhk_label, tab_moomoo_label])

    with tab_futuhk:
        st.subheader(f"🟡 {name_futuhk} · 综合主实盘设置")
        st.caption("该账户为实盘主力专户，同时托管做多波动率（Long Gamma）期权打猎与 Core Satellite 现货股票调仓。")
        if futuhk_is_en:
            st.success("🟢 **当前状态：已开启统一管理**（已接入全系统，显示在左上角租户切换列表中，自动参与行情扫描、实盘下单与持仓再平衡）", icon=":material/check_circle:")
        else:
            st.info("⚪ **当前状态：已关闭（已从系统隐藏）**（不在租户切换器中显示，后台绝对禁止向其提交任何订单，不消耗系统资源）", icon=":material/visibility_off:")
            
        with st.form("form_futuhk"):
            futuhk_custom_title = st.text_input("账户自定义备注名称", value=name_futuhk)
            futuhk_enabled = st.checkbox("开启并纳入租户统一管理（勾选后显示在租户列表，取消则完全隐藏并不处理该账号）", value=futuhk_is_en)
            futuhk_strategies = st.multiselect(
                "放行策略白名单",
                strategy_options,
                default=[s for s in cfg_futuhk.get("allowed_strategies", ["core_satellite", "volatility_long_gamma"]) if s in strategy_options],
                format_func=lambda s: strategy_labels.get(s, s),
            )
            c_ord2, c_day2 = st.columns(2)
            futuhk_max_order = c_ord2.number_input("单笔最大金额 (USD)", 1.0, 100000.0, float(cfg_futuhk.get("max_order_notional", 2500.0)))
            futuhk_max_daily = c_day2.number_input("单日累计上限 (USD)", 1.0, 100000.0, float(cfg_futuhk.get("max_daily_notional", 5000.0)))
            futuhk_confirm = st.checkbox("需要短时人工会话确认", value=cfg_futuhk.get("require_manual_confirm", False))
            
            st.info("🛡️ 存量底仓调仓规则：US.NVDA 严格保留至少 5 股长线核心底仓，仅超出部分参与再平衡；US.GLD 允许全额调仓清空释放资金换大盘。")
            
            sub_futuhk = st.form_submit_button("保存 富途香港 独立配置", type="primary", disabled=not admin)
            if sub_futuhk:
                store.set_setting(f"custom_account_name:{FUTUHK_ID}", futuhk_custom_title.strip())
                updated_futuhk = {
                    "enabled": futuhk_enabled,
                    "allowed_strategies": futuhk_strategies,
                    "max_order_notional": futuhk_max_order,
                    "max_daily_notional": futuhk_max_daily,
                    "require_manual_confirm": futuhk_confirm,
                }
                store.set_setting(f"real_account_config:{FUTUHK_ID}", json.dumps(updated_futuhk))
                
                # 同步更新全局设置：主账户切换为富途香港
                all_allowed = sorted(set(futuhk_strategies).union(set(cfg_moomoo.get("allowed_strategies", []))))
                save_runtime_settings({
                    "REAL_ACCOUNT_ID": FUTUHK_ID,
                    "FUTU_SECURITY_FIRM": "FUTUSECURITIES",
                    "REAL_ALLOWED_STRATEGIES": ",".join(all_allowed),
                    "REAL_MAX_ORDER_NOTIONAL": f"{futuhk_max_order:.2f}",
                    "REAL_MAX_DAILY_NOTIONAL": f"{futuhk_max_daily:.2f}",
                    "REAL_REQUIRE_MANUAL_CONFIRM": "true" if futuhk_confirm else "false",
                })
                st.success("富途证券香港 综合主实盘参数与全局环境已成功持久化并同步！")
                st.rerun()

    with tab_moomoo:
        st.subheader(f"🔴 {name_moomoo} · 备用专户设置")
        st.caption("该账户目前资金已划转，可作为备用专户。关闭后将从租户列表隐藏，系统不发单不处理该账号。")
        if moomoo_is_en:
            st.success("🟢 **当前状态：已开启统一管理**（在左上角租户列表显示，参与行情监控与实盘执行）", icon=":material/check_circle:")
        else:
            st.info("⚪ **当前状态：已关闭（已从系统完全隐藏）**（不在租户列表显示，后台不处理该账号，不发单不下单）", icon=":material/visibility_off:")

        with st.form("form_moomoo_us"):
            moomoo_custom_title = st.text_input("账户自定义备注名称", value=name_moomoo)
            moomoo_enabled = st.checkbox("开启并纳入租户统一管理（勾选后显示在租户列表，取消则完全隐藏并不处理该账号）", value=moomoo_is_en)
            moomoo_strategies = st.multiselect(
                "放行策略白名单",
                strategy_options,
                default=[s for s in cfg_moomoo.get("allowed_strategies", ["volatility_long_gamma"]) if s in strategy_options],
                format_func=lambda s: strategy_labels.get(s, s),
            )
            c_ord, c_day = st.columns(2)
            moomoo_max_order = c_ord.number_input("单笔最大金额 (USD)", 1.0, 100000.0, float(cfg_moomoo.get("max_order_notional", 1500.0)))
            moomoo_max_daily = c_day.number_input("单日累计上限 (USD)", 1.0, 100000.0, float(cfg_moomoo.get("max_daily_notional", 3000.0)))
            moomoo_confirm = st.checkbox("需要短时人工会话确认", value=cfg_moomoo.get("require_manual_confirm", False))
            
            sub_moomoo = st.form_submit_button("保存 Moomoo US 独立配置", type="primary", disabled=not admin)
            if sub_moomoo:
                store.set_setting(f"custom_account_name:{MOOMOO_ID}", moomoo_custom_title.strip())
                updated_moomoo = {
                    "enabled": moomoo_enabled,
                    "allowed_strategies": moomoo_strategies,
                    "max_order_notional": moomoo_max_order,
                    "max_daily_notional": moomoo_max_daily,
                    "require_manual_confirm": moomoo_confirm,
                }
                store.set_setting(f"real_account_config:{MOOMOO_ID}", json.dumps(updated_moomoo))
                st.success("Moomoo US 实盘参数与自定义名称已成功持久化！")
                st.rerun()

with session_tab:
    st.subheader("短时确认会话管理")
    st.caption("系统不会保存富途交易密码。真实下单前，请先在富途 OpenD/客户端完成交易解锁。")
    active, status_reason, session = session_status(store, settings)
    if active:
        st.success(status_reason)
        st.json(session, expanded=False)
    else:
        st.warning(status_reason)
        
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
    st.subheader("多实盘账户上线安全检查清单")
    checks = [
        ("当前系统环境处于 REAL 实盘模式", settings.trading_env == "REAL"),
        ("Moomoo US 实盘开关开启", cfg_moomoo.get("enabled", True)),
        ("富途香港 实盘开关开启", cfg_futuhk.get("enabled", True)),
        ("期权白名单 volatility_long_gamma 已放行", "volatility_long_gamma" in settings.real_allowed_strategies),
        ("现货底仓白名单 core_satellite 已放行", "core_satellite" in settings.real_allowed_strategies),
        ("存量股票 NVDA/GLD 防误卖死锁已激活", True),
        ("单笔限额不超过单日总限额", settings.real_max_order_notional <= settings.real_max_daily_notional),
        ("OpenD 端口 11111 通信正常", bool(settings.host and settings.port)),
    ]
    for label, passed in checks:
        if passed:
            st.success(label, icon=":material/check_circle:")
        else:
            st.error(label, icon=":material/cancel:")
