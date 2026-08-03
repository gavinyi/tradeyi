from __future__ import annotations

import json

import pandas as pd
import streamlit as st
import os
import time
from pandas.api.types import is_datetime64_any_dtype
from zoneinfo import ZoneInfo

from .admin_auth import (
    configured as admin_configured, login_allowed, record_login, verify as verify_admin,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")
US_EASTERN_TZ = ZoneInfo("America/New_York")
UTC_TIME_COLUMNS = {
    "created_at", "updated_at", "fetched_at", "approved_at", "last_broker_update",
    "last_reprice_at", "intent_created_at", "open_time", "close_time",
}
MARKET_TIME_COLUMNS = {
    "time_key", "create_time", "updated_time", "update_time", "expire_time",
}


MOBILE_RESPONSIVE_CSS = """
<style>
@media (max-width: 768px) {
  [data-testid="stMainBlockContainer"],
  .block-container {
    max-width: 100% !important;
    padding: 0.75rem 0.75rem 5rem !important;
  }

  [data-testid="stSidebar"][aria-expanded="true"],
  [data-testid="stSidebar"]:not([aria-expanded]) {
    width: min(88vw, 22rem) !important;
    min-width: min(88vw, 22rem) !important;
    max-width: min(88vw, 22rem) !important;
  }

  [data-testid="stSidebar"][aria-expanded="false"] {
    width: 0 !important;
    min-width: 0 !important;
    max-width: 0 !important;
    margin-left: 0 !important;
    overflow: hidden !important;
  }

  [data-testid="stSidebarCollapsedControl"] {
    left: 0.5rem !important;
    top: 0.5rem !important;
    z-index: 999999 !important;
  }

  h1 {
    font-size: 1.65rem !important;
    line-height: 1.2 !important;
  }

  h2 {
    font-size: 1.35rem !important;
  }

  h3 {
    font-size: 1.15rem !important;
  }

  [data-testid="stHorizontalBlock"] {
    flex-wrap: wrap !important;
    gap: 0.5rem !important;
  }

  [data-testid="stHorizontalBlock"] > div {
    flex: 1 1 100% !important;
    width: 100% !important;
    min-width: 0 !important;
  }

  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
  [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    flex: 1 1 100% !important;
    width: 100% !important;
    min-width: 0 !important;
  }

  [data-testid="stMetric"] {
    width: 100% !important;
    min-width: 0 !important;
  }

  [data-testid="stMetricValue"] {
    font-size: 1.35rem !important;
    overflow-wrap: anywhere !important;
  }

  [data-testid="stButton"] > button,
  [data-testid="stFormSubmitButton"] > button,
  [data-testid="stDownloadButton"] > button,
  [data-testid="stPageLink"] a {
    width: 100% !important;
    min-height: 44px !important;
    white-space: normal !important;
  }

  [data-testid="stTextInput"] input,
  [data-testid="stNumberInput"] input,
  [data-testid="stSelectbox"] > div,
  [data-testid="stMultiSelect"] > div {
    min-height: 44px !important;
  }

  [data-testid="stSegmentedControl"] [role="radiogroup"] {
    flex-wrap: wrap !important;
  }

  [data-testid="stSegmentedControl"] [role="radio"] {
    min-height: 44px !important;
  }

  [data-testid="stDataFrame"],
  [data-testid="stTable"] {
    max-width: 100% !important;
    overflow-x: auto !important;
    overscroll-behavior-inline: contain;
  }

  [data-baseweb="tab-list"] {
    overflow-x: auto !important;
    overflow-y: hidden !important;
    flex-wrap: nowrap !important;
    scrollbar-width: thin;
  }

  [data-baseweb="tab"] {
    flex: 0 0 auto !important;
    min-height: 44px !important;
    white-space: nowrap !important;
  }

  [data-testid="stExpander"] summary {
    min-height: 44px !important;
  }

  [role="dialog"] {
    width: calc(100vw - 1rem) !important;
    max-width: calc(100vw - 1rem) !important;
    max-height: calc(100vh - 1rem) !important;
  }

  [data-testid="stAlert"] {
    overflow-wrap: anywhere !important;
  }

  code, pre {
    white-space: pre-wrap !important;
    overflow-wrap: anywhere !important;
  }
}

@media (max-width: 480px) {
  [data-testid="stMainBlockContainer"],
  .block-container {
    padding-inline: 0.5rem !important;
  }

  [data-testid="stMetric"] {
    padding: 0.75rem !important;
  }
}

@media (pointer: coarse) {
  button, a, input, summary, [role="tab"] {
    touch-action: manipulation;
  }
}
</style>
"""


def china_time_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert display timestamps to timezone-naive China Standard Time values."""
    result = frame.copy()
    for column in result.columns:
        if column in UTC_TIME_COLUMNS:
            if is_datetime64_any_dtype(result[column]):
                values = pd.to_datetime(result[column], errors="coerce")
                if values.dt.tz is None:
                    continue
                values = values.dt.tz_convert("UTC")
            else:
                values = pd.to_datetime(result[column], errors="coerce", utc=True)
        elif column in MARKET_TIME_COLUMNS:
            if is_datetime64_any_dtype(result[column]):
                values = pd.to_datetime(result[column], errors="coerce")
                if values.dt.tz is None:
                    continue
            else:
                values = pd.to_datetime(result[column], errors="coerce")
            if values.dt.tz is None:
                values = values.dt.tz_localize(US_EASTERN_TZ, ambiguous="NaT", nonexistent="shift_forward")
            else:
                values = values.dt.tz_convert(US_EASTERN_TZ)
        else:
            continue
        converted = values.dt.tz_convert(CHINA_TZ).dt.tz_localize(None)
        result[column] = converted.where(values.notna(), result[column])
    return result


def render_sidebar(settings=None) -> None:
    """Render shared environment information below the app navigation."""
    # All application pages call this shared entry point. The media-query layer
    # keeps desktop density while making trading controls usable on narrow screens.
    st.markdown(MOBILE_RESPONSIVE_CSS, unsafe_allow_html=True)
    if settings is not None:
        st.sidebar.divider()
        if getattr(settings, "trading_env", "SIMULATE") == "REAL":
            st.sidebar.error("REAL 实盘 · US")
        else:
            st.sidebar.success("SIMULATE 模拟盘 · US")
        st.sidebar.caption(f"OpenD: {settings.host}:{settings.port}")
        st.sidebar.warning(
            "策略下单已启用" if settings.enable_paper_orders else "策略下单关闭（仅观察）"
        )


def render_admin_login() -> bool:
    timeout = max(60, int(os.getenv("ADMIN_SESSION_TIMEOUT_SECONDS", "1800")))
    if st.session_state.get("admin_authenticated", False):
        last = float(st.session_state.get("admin_last_active", 0))
        if time.time() - last <= timeout:
            st.session_state.admin_last_active = time.time()
            return True
        st.session_state.admin_authenticated = False
        st.warning("管理员会话已超时，请重新登录。")
    if not admin_configured():
        st.warning("尚未配置 ADMIN_PASSWORD_HASH，服务控制和高风险修改已锁定。")
        return False
    with st.form("admin_login", border=True):
        password = st.text_input("管理员密码", type="password")
        submitted = st.form_submit_button("解锁管理操作")
        if submitted:
            client_key = str(st.context.headers.get("X-Forwarded-For", "local"))
            allowed, remaining = login_allowed(client_key)
            if not allowed:
                st.error(f"错误次数过多，请在 {remaining} 秒后重试。")
            elif verify_admin(password):
                record_login(client_key, True)
                st.session_state.admin_authenticated = True
                st.session_state.admin_last_active = time.time()
                st.rerun()
            else:
                record_login(client_key, False)
                st.error("管理员密码错误。")
    return False


COLUMN_NAMES = {
    # Common identity and time fields
    "code": "代码", "name": "名称", "stock_name": "证券名称", "date": "日期",
    "time_key": "时间（中国）", "create_time": "创建时间（中国）", "updated_time": "更新时间（中国）",
    "update_time": "行情更新时间（中国）", "listing_date": "上市日期", "strike_time": "到期日",
    "created_at": "创建时间（中国）", "updated_at": "更新时间（中国）",
    "fetched_at": "抓取时间（中国）", "approved_at": "批准时间（中国）",
    "strategy_id": "策略", "max_net_debit": "最大净借记",
    "min_net_credit": "最小净贷记", "assembly_timeout_seconds": "装配超时（秒）",
    "naked_timeout_seconds": "裸腿保护时限（秒）", "auto_rollback": "自动回滚",
    "leg_order": "腿序号", "role": "腿角色", "symbol": "合约代码",
    "limit_price": "限价", "close_intent_id": "平仓意图ID",
    "close_attempts": "平仓尝试次数",
    "intent_key": "交易意图键", "broker_order_id": "券商订单号",
    "event_type": "事件", "message": "说明",
    "parent_combo_id": "来源组合", "requires_parent_closed": "等待旧组合平仓",
    "bid": "买一价", "ask": "卖一价", "mid": "中间价",
    "executable_open_price": "立即成交参考价",
    "mid_net_contribution": "中间价净贡献",
    "executable_net_contribution": "立即成交净贡献",
    "open_interest": "未平仓量", "volume_capacity": "成交量容量",
    "oi_capacity": "未平仓量容量", "package_spread": "组合盘口成本",
    # Account
    "acc_id": "账户ID", "trd_env": "交易环境", "acc_type": "账户类型",
    "sim_acc_type": "模拟账户类型", "uni_card_num": "统一账户卡号",
    "currency": "币种", "cash": "现金", "total_assets": "总资产",
    "market_val": "证券市值", "power": "购买力", "max_power_short": "最大做空购买力",
    "securities_assets": "证券资产", "fund_assets": "基金资产", "bond_assets": "债券资产",
    "long_mv": "多头市值", "short_mv": "空头市值", "pending_asset": "待交收资产",
    "interest_charged_amount": "已计利息", "max_withdrawal": "最大可提金额",
    "net_cash_power": "净现金购买力", "available_funds": "可用资金",
    "maintenance_margin": "维持保证金", "unrealized_pl": "未实现盈亏",
    "realized_pl": "已实现盈亏", "risk_level": "风险等级", "risk_status": "风险状态",
    "initial_margin": "初始保证金", "margin_call_margin": "追缴保证金",
    "frozen_cash": "冻结现金", "avl_withdrawal_cash": "可提取现金",
    "is_pdt": "是否日内交易账户", "pdt_seq": "日内交易序号",
    "beginning_dtbp": "期初日内购买力", "remaining_dtbp": "剩余日内购买力",
    "dt_call_amount": "日内追缴金额", "dt_status": "日内交易状态",
    "crypto_mv": "数字资产市值", "exposure_level": "风险敞口等级",
    "exposure_limit": "风险敞口上限", "used_limit": "已用额度",
    "remaining_limit": "剩余额度",
    # Position and orders
    "qty": "数量", "quantity": "数量", "can_sell_qty": "可卖数量", "cost_price": "成本价",
    "price": "价格", "nominal_price": "名义价格", "market": "市场",
    "position_side": "持仓方向", "pl_ratio": "盈亏比例", "pl_val": "盈亏金额",
    "position_market": "持仓市场", "cost_price_valid": "成本价是否有效",
    "average_cost": "平均成本", "diluted_cost": "摊薄成本",
    "pl_ratio_valid": "盈亏比例是否有效", "pl_ratio_avg_cost": "按平均成本盈亏比例",
    "pl_val_valid": "盈亏金额是否有效", "today_trd_val": "今日交易金额",
    "today_sell_val": "今日卖出金额", "position_id": "持仓ID", "combo_id": "组合ID",
    "strategy_type": "策略类型", "position_type": "持仓类型", "jp_acc_type": "日本账户类型",
    "today_buy_qty": "今日买入数量", "today_sell_qty": "今日卖出数量",
    "today_buy_val": "今日买入金额", "today_pl_val": "今日盈亏",
    "trd_side": "交易方向", "side": "方向", "order_type": "订单类型",
    "order_status": "订单状态", "order_id": "订单ID", "dealt_qty": "已成交数量",
    "dealt_avg_price": "成交均价", "last_err_msg": "最后错误信息", "remark": "备注",
    "last_broker_update": "券商最近更新时间（中国）",
    "time_in_force": "有效期", "fill_outside_rth": "是否允许盘前盘后",
    "session": "交易时段", "aux_price": "触发价格", "trail_type": "跟踪类型",
    "trail_value": "跟踪值", "trail_spread": "跟踪价差", "commission": "佣金",
    "order_market": "订单市场", "expire_time": "订单到期时间", "amount": "订单金额",
    "combo_legs": "组合腿信息",
    "asset_type": "资产类型", "result": "处理结果",
    "notional": "估算名义金额",
    "cash_after": "交易后现金", "pnl": "本次盈亏", "reason": "原因",
    # Security directory and OHLCV
    "stock_type": "证券类型", "lot_size": "每手数量", "stock_id": "证券ID",
    "open": "开盘价", "close": "收盘价", "high": "最高价", "low": "最低价",
    "open_price": "开盘价", "close_price": "收盘价", "high_price": "最高价",
    "low_price": "最低价", "last_price": "最新价", "prev_close_price": "昨收价",
    "volume": "成交量", "turnover": "成交额", "turnover_rate": "换手率",
    "change_rate": "涨跌幅", "amplitude": "振幅", "suspension": "是否停牌",
    "pe_ratio": "市盈率",
    # Options
    "option_type": "期权类型", "strike_price": "行权价", "option_strike_price": "行权价",
    "underlying_price": "正股价格",
    "stock_owner": "正股代码", "expiration_cycle": "到期周期",
    "option_standard_type": "合约标准类型", "option_settlement_mode": "结算方式",
    "bid_price": "买一价", "ask_price": "卖一价", "bid_vol": "买一量", "ask_vol": "卖一量",
    "mid_price": "中间价", "spread_pct": "买卖价差比例(%)",
    "moneyness_pct": "价内/价外距离(%)",
    "estimated_profit_probability": "模型盈利概率",
    "breakeven_price": "盈亏平衡价", "probability_confidence": "概率置信度",
    "model_expected_return": "模型期望收益",
    "expectancy_model_version": "期望收益模型",
    "expectancy_empirical_samples": "逐笔校准样本",
    "iv_history_percentile": "IV历史百分位",
    "iv_percentile_status": "IV百分位来源",
    "iv_history_samples": "IV历史样本数",
    "structure_expected_return": "结构期望收益",
    "structure_max_loss": "结构最大亏损",
    "gamma_theta_efficiency": "Gamma/Theta 效率",
    "option_open_interest": "未平仓量", "option_net_open_interest": "净未平仓量",
    "option_implied_volatility": "隐含波动率", "option_delta": "Delta",
    "option_gamma": "Gamma", "option_theta": "Theta", "option_vega": "Vega",
    "option_rho": "Rho", "option_contract_multiplier": "合约乘数",
    "option_contract_size": "合约规模", "option_expiry_date_distance": "剩余天数",
    "underlying": "正股代码", "expiration": "到期日", "strike": "行权价",
    "contract_multiplier": "合约乘数", "max_premium": "最大权利金", "enabled": "是否启用",
    "event_date": "活动开始日期", "event_session": "触发时段",
    "contracts_per_stage": "每批合约数", "max_stages": "最大批数",
    "max_premium_per_stage": "每批最大权利金", "entry_hours": "入场窗口（小时）",
    "profit_target": "止盈比例", "stop_loss": "硬止损比例",
    "trailing_stop": "峰值价格回撤退出比例", "trigger_mode": "触发模式",
    "profit_protection_start": "盈利保护启动阈值",
    "profit_protection_drawdown": "盈利回吐阈值",
    "minimum_locked_profit": "最低锁定盈利",
    "profit_protection_start_normal": "常规启动阈值",
    "near_expiry_protection_days": "临近到期保护天数",
    "profit_protection_start_near_expiry": "临近到期启动阈值",
    "profit_protection_giveback_near_expiry": "临近到期盈利回吐",
    "minimum_locked_profit_near_expiry": "临近到期锁定盈利",
    "profit_protection_giveback_normal": "常规盈利回吐",
    "minimum_locked_profit_normal": "常规锁定盈利",
    "profit_protection_start_dynamic": "动态波动启动阈值",
    "profit_protection_giveback_dynamic": "动态波动盈利回吐",
    "minimum_locked_profit_dynamic": "动态波动锁定盈利",
    "profit_protection_start_strong": "强波动启动阈值",
    "profit_protection_giveback_strong": "强波动盈利回吐",
    "minimum_locked_profit_strong": "强波动锁定盈利",
    "partial_profit_enabled": "启用分批止盈",
    "partial_profit_tier1": "第一档止盈阈值",
    "partial_profit_tier1_fraction": "第一档卖出比例",
    "partial_profit_tier2": "第二档止盈阈值",
    "partial_profit_tier2_fraction": "第二档卖出比例",
    "cooldown_take_profit_hours": "止盈后冷却小时",
    "cooldown_profit_protection_hours": "盈利保护后冷却小时",
    "cooldown_stop_loss_hours": "硬止损后冷却小时",
    "cooldown_expiry_hours": "到期风险退出后冷却小时",
    "cooldown_decay_hours": "IV/Gamma衰减后冷却小时",
    "cooldown_contraction_hours": "波动收缩后冷却小时",
    "minimum_underlying_move": "自动方向最低当日涨跌幅",
    "entry_zscore": "动态波动Z值门槛", "cooldown_hours": "退出冷却小时",
    "volatility_eligible": "波动率合约合格", "volatility_quality_score": "波动率合约质量分",
    "volatility_filter_reason": "波动率合约筛选原因",
    "last_expression_review": "最近表达复核", "days_to_expiry": "剩余天数",
    "iv_rv": "IV/RV", "entry_iv": "入场隐含波动率",
    "IV历史样本数": "IV历史样本数", "IV百分位可用": "IV百分位可用",
    "entry_gamma_theta_efficiency": "入场Gamma/Theta效率",
    "structure": "期权结构", "structure_label": "期权结构",
    "opportunity_score": "财报机会评分", "minimum_score": "最低交易评分",
    "expected_move_edge": "历史相对隐含波动优势", "trade_allowed": "允许自动交易",
    "score_reason": "评分依据", "structure_net_debit": "组合净权利金",
    "max_hold_hours": "财报后最长持有小时", "legs": "组合腿",
    "event_source": "事件来源", "event_confirmed": "事件已确认",
    "intent_id": "交易意图ID", "previous_status": "原状态", "new_status": "新状态",
    "source": "来源",
    "filled_quantity": "已成交数量", "average_fill_price": "成交均价",
    "lot_id": "批次ID", "open_time": "开仓时间（中国）",
    "open_time_source": "开仓时间来源", "close_time": "平仓时间（中国）",
    "close_time_source": "平仓时间来源", "open_quantity": "开仓数量",
    "closed_quantity": "平仓数量", "remaining_quantity": "剩余数量",
    "avg_close_price": "平仓均价", "entry_tier": "入场层级",
    "effective_premium_budget": "实际权利金预算", "order_amount": "订单金额",
    "filled_amount": "成交金额", "return_ratio": "收益率",
    "details_json": "状态详情", "severity": "风险等级", "action": "建议动作",
    "intrinsic_value": "每份内在价值", "projected_cash_need": "预计资金需求",
    "projected_share_need": "预计正股变化",
    "close_days_before": "提前交易日", "close_window_minutes": "收盘前窗口（分钟）",
    "actor": "配置人",
    "sample_count": "样本数", "good_samples": "可用样本数",
    "trading_days": "覆盖交易日", "first_time": "首条时间",
    "last_time": "末条时间", "quality_status": "数据质量",
    "quality_reason": "质量说明",
    "capital_ratio": "资金配额", "max_loss_ratio": "最大损失预算",
    "option_premium_ratio": "期权权利金配额", "capital_used": "已用资金",
    "loss_used": "估算风险占用", "option_premium_used": "已用期权权利金",
    "capital_limit": "资金上限", "loss_limit": "损失上限",
    "option_premium_limit": "期权权利金上限",
    "capital_utilization": "资金使用率", "loss_utilization": "损失预算使用率",
    "scenario": "压力场景", "price_shock": "价格冲击",
    "iv_change_points": "IV变化（百分点）", "days_elapsed": "时间流逝（天）",
    "liquidity_haircut": "流动性折价", "stock_pnl": "正股估算损益",
    "option_pnl": "期权估算损益", "liquidity_cost": "流动性成本",
    "estimated_pnl": "组合估算损益", "stressed_equity": "压力后权益",
    "equity_loss_ratio": "权益损失比例", "breach": "突破警戒线",
    "asset_class": "资产类别", "realized_pnl": "已实现盈亏",
    "unrealized_pnl": "未实现盈亏", "slippage_cost": "不利滑点成本",
    "total_pnl": "总盈亏", "delta_exposure": "Delta金额敞口",
    "gamma_exposure": "Gamma敞口", "vega_exposure": "Vega敞口",
    "theta_exposure": "Theta敞口", "closed_trades": "已平仓交易",
    "winning_trades": "盈利交易", "win_rate": "胜率", "market_value": "持仓市值",
    "version_hash": "完整版本指纹", "code_version": "代码版本",
    "data_version": "数据版本", "metrics_json": "指标",
    "parameters_json": "参数", "universe_json": "标的池", "is_active": "当前运行版本",
    "promoted_at": "启用时间（中国）",
    # Backtest
    "equity": "账户权益", "stock_value": "股票市值", "position": "持仓数量",
    "signal": "策略信号", "risk_state": "风险状态", "target_weight": "目标权重",
}

VALUE_NAMES = {
    "order_status": {
        "NONE": "未知", "WAITING_SUBMIT": "待提交", "SUBMITTING": "提交中",
        "SUBMITTED": "已提交，等待成交", "FILLED_PART": "部分成交",
        "FILLED_ALL": "全部成交", "CANCELLING_PART": "部分撤单中",
        "CANCELLING_ALL": "全部撤单中", "CANCELLED_PART": "部分成交，余单已撤",
        "CANCELLED_ALL": "全部已撤", "FAILED": "下单失败", "DISABLED": "已失效",
        "DELETED": "已删除", "FILL_CANCELLED": "成交已撤销",
    },
    "order_type": {
        "NORMAL": "限价单", "MARKET": "市价单", "ABSOLUTE_LIMIT": "竞价限价单",
        "AUCTION": "竞价市价单", "AUCTION_LIMIT": "竞价限价单",
        "SPECIAL_LIMIT": "特别限价单", "SPECIAL_LIMIT_ALL": "特别限价全成单",
        "STOP": "止损市价单", "STOP_LIMIT": "止损限价单",
        "MARKET_IF_TOUCHED": "触及市价单", "LIMIT_IF_TOUCHED": "触及限价单",
        "TRAILING_STOP": "跟踪止损市价单", "TRAILING_STOP_LIMIT": "跟踪止损限价单",
    },
    "trd_side": {"BUY": "买入", "SELL": "卖出", "BUY_BACK": "买入平仓", "SELL_SHORT": "卖出开空"},
    "time_in_force": {"DAY": "当日有效", "GTC": "撤销前有效", "GTD": "指定日期前有效"},
    "session": {"NONE": "常规时段", "RTH": "常规时段", "ETH": "盘前盘后", "ALL": "全部时段", "OVERNIGHT": "夜盘"},
    "trd_env": {"SIMULATE": "模拟盘", "REAL": "实盘"},
    "option_type": {"CALL": "认购 Call", "PUT": "认沽 Put"},
    "action": {
        "MONITOR": "继续监测", "MANUAL_REVIEW": "到期前人工复核",
        "ACCEPT_EXERCISE": "接受行权或指派", "AUTO_CLOSE": "到期日前自动平仓",
    },
    "stock_type": {"STOCK": "股票", "ETF": "ETF", "DRVT": "衍生品"},
}

for prefix, label in {
    "hk": "港币", "hkd": "港币", "us": "美元", "usd": "美元", "cn": "人民币",
    "cnh": "离岸人民币", "jp": "日元", "jpy": "日元", "sg": "新加坡元",
    "sgd": "新加坡元", "au": "澳元", "aud": "澳元", "ca": "加元",
    "cad": "加元", "my": "马来西亚林吉特", "myr": "马来西亚林吉特",
}.items():
    COLUMN_NAMES.setdefault(f"{prefix}_cash", f"{label}现金")
    COLUMN_NAMES.setdefault(f"{prefix}_avl_withdrawal_cash", f"{label}可提现金")
    COLUMN_NAMES.setdefault(f"{prefix}_net_cash_power", f"{label}净现金购买力")
    COLUMN_NAMES.setdefault(f"{prefix}_assets", f"{label}资产")


def chinese_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a display-only copy with stable Chinese column labels."""
    result = china_time_columns(frame)
    for column, mapping in VALUE_NAMES.items():
        if column in result.columns:
            def translate(value: object) -> object:
                normalized = str(value).upper().split(".")[-1]
                normalized = normalized.replace("ORDERSTATUS_", "").replace("ORDERTYPE_", "")
                return mapping.get(normalized, value)

            result[column] = result[column].map(translate)
    result = result.rename(columns={column: COLUMN_NAMES.get(column, column) for column in result.columns})
    if not result.columns.duplicated().any():
        return result
    merged: dict[str, pd.Series] = {}
    for index, column in enumerate(result.columns):
        values = result.iloc[:, index]
        if column in merged:
            merged[column] = merged[column].combine_first(values)
        else:
            merged[column] = values
    return pd.DataFrame(merged, index=result.index)


STRATEGY_LABELS = {
    "core_satellite": "Core Satellite 核心-卫星", "earnings_gamma": "财报 Gamma",
    "earnings_gamma_manual": "财报 Gamma 手动单", "volatility_long_gamma": "波动率期权",
    "trading_service": "统一交易服务", "shadow_matching": "Shadow 虚拟撮合",
    "order_reconciliation": "订单对账", "ledger_reconciliation": "三账对账",
    "system_health": "系统健康检查", "earnings_calendar": "财报日历",
    "daily_report": "每日运行报告", "correlation_risk": "相关性风险",
    "order_push": "订单推送通道",
    "option_combo_monitor": "期权组合执行监控",
    "volatility_scanner": "波动率雷达",
    "page_compatibility": "页面兼容性巡检", "paper_e2e": "模拟交易端到端验收",
    "observability": "可观测指标采集", "automatic_recovery": "自动故障恢复",
    "idempotency_audit": "订单幂等审计", "operations_report": "每日运营报告",
    "dynamic_allocation": "动态资金调度",
    "long_term_validation": "长期策略验证",
    "SMA Cross": "均线交叉策略", "RSI Mean Reversion": "RSI 均值回归策略",
    "20 Day Breakout": "20日突破策略",
}

STATUS_LABELS = {
    "HEALTHY": "正常", "RUNNING": "正在执行", "ERROR": "执行异常",
    "BLOCKED": "已阻断", "STARTING": "正在启动", "STOPPED": "已停止",
    "REJECTED": "风控拒绝", "SHADOW": "Shadow 待撮合", "SHADOW_PART": "Shadow 部分成交",
    "SHADOW_FILLED": "Shadow 已成交", "SHADOW_EXPIRED": "Shadow 已失效", "SUBMITTED": "已提交富途模拟盘",
    "FILLED": "已全部成交", "CANCELLED": "已撤单", "FAILED": "执行失败",
    "DRAFT": "草稿", "EXECUTING": "正在分腿开仓", "OPEN": "已完整开仓",
    "CLOSING": "正在组合平仓", "ROLLBACK_PENDING": "正在失败回滚",
    "CLOSED": "已平仓", "WORKING": "待成交", "CLOSE_WORKING": "平仓待成交",
    "CLOSE_FAILED": "平仓失败",
    "WAITING_EXIT_LIQUIDITY": "等待可退出盘口",
    "PRE_TRADE_BLOCKED": "开仓前已阻断",
}

DECISION_LABELS = {"BUY": "买入", "SELL": "卖出", "HOLD": "观察", "BLOCKED": "阻断", "CLOSED": "已结束"}

REASON_LABELS = {
    "hard stop loss": "触发硬止损", "profit target": "达到止盈目标",
    "trailing stop": "触发峰值回撤退出", "24-hour time exit": "触发事件后24小时退出",
    "outside entry window": "尚未进入允许入场的时间窗口",
    "initial entry": "首次入场", "event passed without a position": "入场窗口已结束且当前无持仓",
    "waiting; losing positions are never averaged down": "继续观察，亏损持仓禁止摊低成本",
    "post-event monitoring; adding is disabled": "事件后监控中，禁止继续加仓",
    "pyramid winner after directional confirmation": "方向持续确认，仅对盈利持仓加仓",
    "weekly Friday schedule": "按周调仓：每周五执行",
}

EVENT_LABELS = {
    "PROCESS_START": "进程启动", "PROCESS_STOP": "进程停止",
    "STARTUP_RECOVERY": "启动恢复检查", "DAILY_SUMMARY": "24小时运行汇总",
    "NASDAQ_SOURCE_FAILED": "Nasdaq 财报源异常", "FUTU_SOURCE_FAILED": "富途财报源异常",
    "APPROVAL_ENFORCEMENT_CHANGED": "上线审批强制规则变更",
    "EARNINGS_CALENDAR_REFRESHED": "财报日历已刷新",
    "EXECUTION_MODE_CHANGED": "全局执行模式变更",
    "EXPEDITED_PAPER_APPROVAL": "策略快速批准进入模拟盘",
    "GAMMA_SHADOW_DEBUG_ENABLED": "财报 Gamma 调试模式已启用",
    "GAMMA_VALIDATION_WARNING": "财报 Gamma 配置校验提醒",
    "HEALTH_CHECK": "系统健康检查",
    "MIGRATION_DUPLICATE_CLEANED": "迁移时重复配置已清理",
    "PRE_TRADE_CHECK_FAILED": "下单前检查未通过",
    "RISK_REJECTED": "订单被风控拒绝",
    "SERVICE_START": "统一交易服务已启动", "SERVICE_STOP": "统一交易服务已停止",
    "SERVICE_RESTART": "统一交易服务已重启",
    "SHADOW_FILL": "Shadow 虚拟订单成交", "SHADOW_ORDER": "Shadow 虚拟订单已创建",
    "STRATEGIES_PAUSE_CHANGED": "全部策略暂停状态变更",
    "STRATEGY_APPROVED": "策略上线审批已通过",
    "STRATEGY_BUDGETS_CHANGED": "策略预算已变更",
    "STRATEGY_SWITCHES_CHANGED": "策略启用状态已变更",
    "STRATEGY_EXECUTION_MODES_CHANGED": "策略执行模式已变更",
    "VIRTUAL_ALLOCATION_CHANGED": "虚拟持仓归属已变更",
    "VIRTUAL_POSITION_ENFORCEMENT_CHANGED": "虚拟持仓强校验规则变更",
    "VOLATILITY_SHADOW_ENABLED": "波动率期权 Shadow 模式已启用",
    "VOLATILITY_STRATEGY_MIGRATED": "波动率策略配置迁移完成",
    "VOLATILITY_SIGNAL_DETECTED": "发现动态波动信号",
    "VOLATILITY_SIGNAL_CONFIRMED": "波动率雷达信号已确认",
    "VOLATILITY_SHADOW_CAMPAIGN_AUTO_CREATED": "波动率自动策略已生成",
    "VOLATILITY_AUTO_CAMPAIGN_CREATED": "波动率自动策略已生成",
    "VOLATILITY_SYMBOL_AUTO_ADDED": "波动率标的已自动加入",
    "VOLATILITY_EXPIRED_CAMPAIGNS_CLEANED": "过期/失效波动率策略已清理",
    "OPTION_MARKET_DATA_BLOCKED": "期权行情/盘口阻断开仓",
    "OPTION_MARKET_DATA_WAITING": "期权行情/盘口等待退出",
    "OPTION_EXIT_WAITING_FOR_BID": "期权平仓等待买一价",
    "OPTION_QUOTE_DEGRADED_ACCEPTED": "期权行情降级通过",
    "TASK_TIMEOUT": "调度任务执行超时",
    "COMBO_NAKED_EXPOSURE": "期权组合保护腿缺失",
    "COMBO_CLOSE_FAILED": "期权组合平仓失败",
    "PROTECTED_COMBO_SHORT_ALLOWED": "受保护的期权组合短腿已放行",
    "EXECUTION_STARTED": "组合开始分腿开仓", "OPENED": "组合完整开仓",
    "CLOSE_REQUESTED": "组合平仓请求", "ROLLBACK_STARTED": "组合失败回滚",
    "ASSEMBLY_TIMEOUT": "组合装配超时", "LEG_OPEN_SUBMITTED": "组合开仓腿已提交",
    "LEG_CLOSE_SUBMITTED": "组合平仓腿已提交", "LEG_SUBMIT_FAILED": "组合腿提交失败",
    "PACKAGE_REPRICED": "组合智能限价调整", "PACKAGE_REPRICE_FAILED": "组合调价失败",
    "ROLL_DRAFT_CREATED": "滚仓草稿已创建",
    "VALIDATION_AUTO_DOWNGRADE": "验证系统自动降级为 Shadow",
    "STORAGE_CAPACITY": "存储容量检查",
    "STORAGE_CAPACITY_WARNING": "磁盘空间预警",
}

PAYLOAD_LABELS = {
    "accounts": "模拟账户数", "orders_reconciled": "已对账订单数", "ledger_mismatches": "持仓差异数",
    "orders": "订单意图数", "filled": "有成交订单数", "rejected": "风控拒单数",
    "errors": "错误告警数", "shadow_orders": "Shadow 订单数", "service_pid": "服务进程号",
    "position_quantity": "当前持仓", "planned_order_quantity": "计划下单数量",
    "position_source": "持仓来源", "pnl_ratio": "盈亏比例", "drawdown": "峰值价格回撤",
    "profit_giveback": "盈利回吐比例",
    "partial_exit_tier": "分批止盈档位",
    "profit_protection_profile": "盈利保护档案",
    "trigger_mode": "触发模式", "validation_warnings": "校验提醒",
    "direction_reason": "自动方向依据", "entry_start": "入场开始时间", "event_time": "入场截止/事件时间",
    "direction_metrics": "方向指标",
    "actor": "操作来源", "source": "来源", "result": "处理结果", "message": "说明",
    "intent_id": "订单意图编号", "broker_order_id": "券商订单编号",
    "quantity": "数量", "price": "价格", "error": "错误信息", "age": "等待秒数",
    "strategy_id": "策略", "symbol": "代码", "side": "交易方向",
    "enabled": "是否启用", "paused": "是否暂停", "mode": "执行模式",
    "execution_mode": "执行模式", "changes": "变更内容", "checks": "检查结果",
    "warnings": "提醒", "mismatches": "差异", "approved_by": "批准人",
    "database": "运行数据库", "opend": "富途 OpenD", "quote": "行情接口",
    "quote_latency_seconds": "行情延迟（秒）", "market_data": "行情数据状态",
    "cleanup": "历史数据清理", "audit_events": "操作审计记录",
    "alerts": "告警记录", "equity_snapshots": "账户权益快照",
    "market_metrics": "行情指标记录", "decision_logs": "策略决策记录",
    "decision_snapshots": "决策快照", "reconciliation_reports": "对账报告",
    "pid": "进程号", "uptime_seconds": "运行时长（秒）", "delivered": "是否已通知",
    "opportunity_score": "财报机会评分", "minimum_score": "最低交易评分",
    "expected_move_edge": "历史相对隐含波动优势", "score_reason": "评分依据",
    "structure": "期权结构", "legs": "组合腿",
    "market_session": "期权市场状态", "option_quote_time": "期权报价时间",
    "market_data_status": "行情/盘口处理状态",
    "underlying_quote_age_seconds": "正股行情延迟（秒）",
    "option_quote_age_seconds": "期权行情延迟（秒）",
    "book_ok": "盘口是否可用",
    "book_reason": "盘口检查结果",
    "spread_pct": "盘口价差比例",
    "quote_attempts": "行情刷新尝试",
    "volatility_regime": "动态波动状态", "regime": "动态波动状态",
}


def translate_reason(value: object) -> str:
    text = str(value or "")
    return REASON_LABELS.get(text, text)


def readable_payload(value: object) -> str:
    try:
        payload = json.loads(str(value or "{}")) if not isinstance(value, dict) else value
    except json.JSONDecodeError:
        return str(value or "")
    if not isinstance(payload, dict):
        return str(payload)
    parts = []
    for key, item in payload.items():
        label = PAYLOAD_LABELS.get(key, COLUMN_NAMES.get(key, key))
        if isinstance(item, bool):
            shown = "是" if item else "否"
        elif key == "direction_metrics" and isinstance(item, dict):
            shown = "、".join((
                f"日内 {float(item.get('intraday_return', 0)):.2%}",
                f"5日动量 {float(item.get('momentum_5d', 0)):.2%}",
                f"20日趋势 {float(item.get('trend_20d', 0)):.2%}",
                f"方向分 {float(item.get('direction_score', 0)):g}",
            ))
        elif key == "position_source":
            shown = {"SHADOW_LEDGER": "Shadow 虚拟成交账本", "FUTU_ACCOUNT": "富途模拟账户"}.get(str(item), str(item))
        elif key == "trigger_mode":
            shown = {"VOLATILITY": "波动率行情", "EARNINGS": "财报事件"}.get(str(item), str(item))
        elif key in {"entry_start", "event_time"} and item:
            timestamp = pd.to_datetime(item, errors="coerce", utc=True)
            shown = timestamp.tz_convert(CHINA_TZ).strftime("%Y-%m-%d %H:%M") if not pd.isna(timestamp) else str(item)
        elif key in {"strategy_id"}:
            shown = STRATEGY_LABELS.get(str(item), str(item))
        elif key == "side":
            shown = {"BUY": "买入", "SELL": "卖出"}.get(str(item).upper(), str(item))
        elif key in {"mode", "execution_mode"}:
            shown = {"PAPER": "富途模拟盘", "SHADOW": "Shadow 观察模式"}.get(str(item).upper(), str(item))
        elif key == "partial_exit_tier":
            shown = {"tier1": "第一档止盈", "tier2": "第二档止盈"}.get(str(item), str(item))
        elif isinstance(item, list):
            shown = "；".join(translate_reason(entry) for entry in item) if item else "无"
        elif isinstance(item, dict):
            shown = readable_payload(item)
        elif key in {"pnl_ratio", "drawdown", "profit_giveback"} and item is not None:
            shown = f"{float(item):.2%}"
        else:
            shown = {
                "OK": "正常", "HEALTHY": "正常", "SESSION_GAP": "当前非连续行情时段",
                "RUNNING": "运行中", "STOPPED": "已停止", "PAPER": "富途模拟盘",
                "SHADOW": "Shadow 观察模式", "TRUE": "是", "FALSE": "否",
                "WAITING_EXIT_LIQUIDITY": "等待可退出盘口",
                "PRE_TRADE_BLOCKED": "开仓前已阻断",
            }.get(str(item).upper(), str(item))
        parts.append(f"{label}：{shown}")
    return "；".join(parts)


def translate_common_values(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in ("strategy_id", "策略", "策略/服务", "component", "组件"):
        if column in result:
            result[column] = result[column].map(
                lambda value: "系统" if pd.isna(value) else STRATEGY_LABELS.get(str(value), value)
            )
    for column in ("status", "状态", "执行状态"):
        if column in result:
            result[column] = result[column].map(lambda value: STATUS_LABELS.get(str(value).upper(), value))
    for column in ("decision", "决策", "判断"):
        if column in result:
            result[column] = result[column].map(lambda value: DECISION_LABELS.get(str(value).upper(), value))
    for column in ("reason", "原因", "最新原因"):
        if column in result:
            result[column] = result[column].map(translate_reason)
    for column in ("event_type", "事件"):
        if column in result:
            result[column] = result[column].map(lambda value: EVENT_LABELS.get(str(value), value))
    return result
