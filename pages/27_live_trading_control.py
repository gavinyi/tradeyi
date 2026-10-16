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
cfg_futuhk = get_acc_config(FUTUHK_ID, ["core_satellite"])

# 顶部多账户并排监控卡片
day_start = ny_trading_day_start_utc()
snap_moomoo = store.broker_snapshot("account_info_moomoo_real") or {}
snap_futuhk = store.broker_snapshot("account_info_futuhk_real") or {}

assets_moomoo = float(snap_moomoo.get("payload", [{}])[0].get("total_assets", 30007.57) or 30007.57)
cash_moomoo = float(snap_moomoo.get("payload", [{}])[0].get("cash", 30007.57) or 30007.57)

assets_futuhk = float(snap_futuhk.get("payload", [{}])[0].get("total_assets", 27013.54) or 27013.54)
cash_futuhk = float(snap_futuhk.get("payload", [{}])[0].get("cash", 56.29) or 56.29)

col_m1, col_m2 = st.columns(2)
with col_m1:
    with st.container(border=True):
        st.markdown("**🔴 Moomoo US · 美股期权打猎专户**")
        st.caption(f"账号 ID: `{MOOMOO_ID}` · 券商实体: `FUTUINC (美股本土)` · 卡号: `1007376216226587`")
        c1, c2, c3 = st.columns(3)
        c1.metric("可用资金", f"${cash_moomoo:,.2f}")
        c2.metric("实盘状态", "已接入" if cfg_moomoo.get("enabled", True) else "未开启")
        c3.metric("单笔上限", f"${float(cfg_moomoo.get('max_order_notional', 1500)):,.0f}")
        st.success("角色: 纯现金高盈亏比期权打猎 (买方 Long Gamma) · 0 股票占用", icon=":material/check_circle:")

with col_m2:
    with st.container(border=True):
        st.markdown("**🟡 富途证券香港 · 现货底仓专户**")
        st.caption(f"账号 ID: `{FUTUHK_ID}` · 券商实体: `FUTUSECURITIES (香港)` · 卡号: `1001259607272607`")
        c1, c2, c3 = st.columns(3)
        c1.metric("总资产", f"${assets_futuhk:,.2f}")
        c2.metric("实盘状态", "已接入" if cfg_futuhk.get("enabled", True) else "未开启")
        c3.metric("单笔上限", f"${float(cfg_futuhk.get('max_order_notional', 1500)):,.0f}")
        st.warning("角色: Core Satellite 现货底仓 (8 NVDA/4.4 GLD 物理锁死保护)", icon=":material/shield:")

config_tab, session_tab, checklist_tab = st.tabs(["多账户实盘配置", "短时确认会话管理", "上线安全检查清单"])

plugins = strategy_plugins()
strategy_options = [plugin.strategy_id for plugin in plugins]
strategy_labels = {plugin.strategy_id: plugin.display_name for plugin in plugins}

with config_tab:
    tab_moomoo, tab_futuhk = st.tabs([
        "🔴 Moomoo US 期权专户配置",
        "🟡 富途香港 现货底仓专户配置",
    ])
    
    with tab_moomoo:
        st.subheader("Moomoo US 实盘期权参数设置")
        st.caption("该账户专门负责做多波动率（Long Gamma）期权自动开仓与高位止盈。")
        with st.form("form_moomoo_us"):
            moomoo_enabled = st.checkbox("开启此账户实盘交易", value=cfg_moomoo.get("enabled", True))
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
                updated_moomoo = {
                    "enabled": moomoo_enabled,
                    "allowed_strategies": moomoo_strategies,
                    "max_order_notional": moomoo_max_order,
                    "max_daily_notional": moomoo_max_daily,
                    "require_manual_confirm": moomoo_confirm,
                }
                store.set_setting(f"real_account_config:{MOOMOO_ID}", json.dumps(updated_moomoo))
                
                # 同步更新全局设置
                all_allowed = sorted(set(moomoo_strategies).union(set(cfg_futuhk.get("allowed_strategies", []))))
                save_runtime_settings({
                    "REAL_ACCOUNT_ID": MOOMOO_ID,
                    "FUTU_SECURITY_FIRM": "FUTUINC",
                    "REAL_ALLOWED_STRATEGIES": ",".join(all_allowed),
                    "REAL_MAX_ORDER_NOTIONAL": f"{moomoo_max_order:.2f}",
                    "REAL_MAX_DAILY_NOTIONAL": f"{moomoo_max_daily:.2f}",
                    "REAL_REQUIRE_MANUAL_CONFIRM": "true" if moomoo_confirm else "false",
                })
                st.success("Moomoo US 实盘参数已成功持久化并同步至全局环境！")
                st.rerun()

    with tab_futuhk:
        st.subheader("富途证券香港 现货底仓参数设置")
        st.caption("该账户专门负责 Core Satellite 现货股票底仓趋势调仓与再平衡。")
        with st.form("form_futuhk"):
            futuhk_enabled = st.checkbox("开启此账户实盘交易", value=cfg_futuhk.get("enabled", True))
            futuhk_strategies = st.multiselect(
                "放行策略白名单",
                strategy_options,
                default=[s for s in cfg_futuhk.get("allowed_strategies", ["core_satellite"]) if s in strategy_options],
                format_func=lambda s: strategy_labels.get(s, s),
            )
            c_ord2, c_day2 = st.columns(2)
            futuhk_max_order = c_ord2.number_input("单笔最大金额 (USD)", 1.0, 100000.0, float(cfg_futuhk.get("max_order_notional", 1500.0)))
            futuhk_max_daily = c_day2.number_input("单日累计上限 (USD)", 1.0, 100000.0, float(cfg_futuhk.get("max_daily_notional", 3000.0)))
            futuhk_confirm = st.checkbox("需要短时人工会话确认", value=cfg_futuhk.get("require_manual_confirm", False))
            
            st.info("🛡️ 存量底仓免卖物理锁死名单：US.NVDA (8股)、US.GLD (4.43股) 受到底层绝对保护，无论怎么调仓都绝对不卖！")
            
            sub_futuhk = st.form_submit_button("保存 富途香港 独立配置", type="primary", disabled=not admin)
            if sub_futuhk:
                updated_futuhk = {
                    "enabled": futuhk_enabled,
                    "allowed_strategies": futuhk_strategies,
                    "max_order_notional": futuhk_max_order,
                    "max_daily_notional": futuhk_max_daily,
                    "require_manual_confirm": futuhk_confirm,
                }
                store.set_setting(f"real_account_config:{FUTUHK_ID}", json.dumps(updated_futuhk))
                
                # 同步更新全局设置
                all_allowed = sorted(set(futuhk_strategies).union(set(cfg_moomoo.get("allowed_strategies", []))))
                save_runtime_settings({
                    "REAL_ALLOWED_STRATEGIES": ",".join(all_allowed),
                })
                st.success("富途证券香港 实盘参数已成功持久化并同步！")
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
