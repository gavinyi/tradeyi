"""限额地图 · 实盘/模拟盘全部门槛只读汇总页（backlog P2-2，2026-11-07）。

产品口径：让用户一眼看清「每一道金额门：键名 / 现值 / 在哪改 / 是否租户独立」，
杜绝「配置了但找不到在哪生效」的透明度缺口。

数据两层：
- **动态层**：limit_profile.resolve_limit_chain 按当前权益实时派生（单笔/容量/
  日累计/日亏四道派生门，与下单门禁同源同值——预检不得比门禁更宽）；
- **登记层**：限额地图20261107.md 的 13+4 道门清单（键/来源/UI 位置/租户
  独立标记），含「共用表已租户化（迁移 25）」的更正说明。

**只读铁律**：本页零配置写入，只展示——任何修改走各自 UI 位置（页内给链接）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from futu_quant.config import Settings
from futu_quant.limit_profile import LimitProfile, resolve_limit_chain
from futu_quant.ui import (
    get_current_tenant,
    open_trading_store,
    render_sidebar,
)

st.set_page_config(page_title="限额地图", page_icon="🗺️", layout="wide")
settings = Settings.load()
render_sidebar(settings)
store = open_trading_store()
tenant = get_current_tenant(settings)
tenant_id = str(tenant.get("tenant_id") or "")

st.title("限额地图")
st.caption(
    "实盘/模拟盘生效的每一道金额门：键名、现值、在哪改、是否租户独立。"
    "本页只读——修改请前往对应页面（下方给出入口），杜绝「配了但找不到在哪生效」。"
)

# ---------- 动态层：派生门（与下单门禁同源） ----------

st.header(f"① 派生门（实时按权益计算 · 当前租户：{tenant_id or '未识别'}）")
equity = None
try:
    snapshot = store.latest_equity_snapshot(tenant_id=tenant_id) if hasattr(
        store, "latest_equity_snapshot") else None
    if snapshot:
        equity = float(snapshot.get("equity") or 0) or None
except Exception:  # noqa: BLE001 - 快照读不到时动态层如实显示「无权益快照」
    equity = None

if not equity:
    st.info("当前租户无可用权益快照——派生门需权益才能计算，请先看登记层（②）。")
else:
    try:
        chain = resolve_limit_chain(store, tenant_id, equity, LimitProfile())
        rows = [
            {"门": "单笔（派生）", "现值": f"${chain.per_order:,.0f}",
             "说明": "权益×比例，受 floor/hard_cap 钳制",
             "钳制": chain.clamped.get("per_order") or "—"},
            {"门": "日累计开仓", "现值": f"${chain.daily_entries:,.0f}/日",
             "说明": "同上派生口径", "钳制": chain.clamped.get("daily_entries") or "—"},
            {"门": "日亏急停", "现值": f"${chain.daily_loss:,.0f}",
             "说明": "亏损预算派生口径", "钳制": chain.clamped.get("daily_loss") or "—"},
            {"门": "容量线", "现值": f"${chain.capacity:,.0f}",
             "说明": "权益×20%，再受日亏预算双重钳制（预检与门禁同源）",
             "钳制": chain.clamped.get("capacity_line") or "—"},
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if chain.clamped:
            st.warning(f"⚠️ 有钳制生效：{json.dumps(chain.clamped, ensure_ascii=False)}", icon=":material/warning:")
        st.caption(f"权益来源：最近快照 ${equity:,.0f}（租户 {tenant_id}）")
    except ValueError as exc:
        st.error(f"派生门计算失败（如实展示，不静默）：{exc}")

# ---------- 登记层：全部 13+4 道 ----------

st.header("② 登记层 · 全部门槛清单（实盘 13 道 + 模拟盘 4 道）")

REAL_GATES = [
    {"门": "单笔（派生）", "键/来源": "limit_profile:{tenant}.per_order_ratio", "UI 位置": "统一配置中心→风控边界", "租户独立": "✅"},
    {"门": "容量线", "键/来源": "同上 capacity_ratio + min(loss_limit)", "UI 位置": "统一配置中心→风控边界", "租户独立": "✅"},
    {"门": "日累计开仓", "键/来源": "daily_entries_ratio", "UI 位置": "统一配置中心→风控边界", "租户独立": "✅"},
    {"门": "日亏急停", "键/来源": "run_service 派生（resolve_daily_loss_limit）", "UI 位置": "无直接 UI（派生）", "租户独立": "✅"},
    {"门": "微资本单笔门", "键/来源": "ENV_SETTINGS.REAL_MAX_ORDER_NOTIONAL", "UI 位置": "风险与限额页（7）", "租户独立": "❌ 全局（只作用于实盘）"},
    {"门": "微资本单日门", "键/来源": "REAL_MAX_DAILY_NOTIONAL", "UI 位置": "风险与限额页（7）", "租户独立": "❌ 全局"},
    {"门": "回撤预警/熔断/恢复", "键/来源": "risk_drawdown_warn/stop/resume_pct:{tenant}", "UI 位置": "统一配置中心→④风控边界", "租户独立": "✅"},
    {"门": "熔断模式", "键/来源": "risk_breaker_mode:{tenant}", "UI 位置": "统一配置中心（需管理员+确认）", "租户独立": "✅"},
    {"门": "压力损失预算", "键/来源": "strategy_risk_budgets.{tenant} 行（迁移 25 已租户化）", "UI 位置": "策略中心页 27 / 页 15", "租户独立": "✅（原共用表已消除）"},
    {"门": "风控引擎敞口", "键/来源": "RISK_MAX_GROSS_EXPOSURE 等", "UI 位置": "风险与限额页（7）", "租户独立": "❌ 全局"},
    {"门": "日内回撤急停", "键/来源": "RISK_MAX_DRAWDOWN_PCT", "UI 位置": "风险与限额页（7）", "租户独立": "❌ 全局"},
    {"门": "订单执行", "键/来源": "ORDER_TIMEOUT_SECONDS 等", "UI 位置": "风险与限额页（7）执行 tab", "租户独立": "❌ 全局"},
    {"门": "三账对账熔断", "键/来源": "自动（MISMATCH→锁 SELL）", "UI 位置": "无 UI（自动）", "租户独立": "各自独立"},
]

PAPER_GATES = [
    {"门": "单笔/容量线/日亏", "值": "派生（同键不同租户值）", "说明": "与实盘同键不同租户值"},
    {"门": "压力损失预算", "值": "模拟净值×0.20（paper 行独立）", "说明": "迁移 25 后 paper 行独立，页 15 可调"},
    {"门": "risk_limits:{tenant}", "值": "max_entry/daily/daily_loss 独立租户键", "说明": "遗留，部分被派生替代"},
    {"门": "雷达批预算", "值": "VOLATILITY_RADAR_BUDGETS:{tenant}", "说明": "独立"},
    {"门": "日亏预警", "值": "paper_daily_loss_warn", "说明": "独立"},
]

st.subheader("实盘（futu_real_master）")
st.dataframe(pd.DataFrame(REAL_GATES), hide_index=True, width="stretch")

st.subheader("模拟盘（futu_paper_sandbox）")
st.dataframe(pd.DataFrame(PAPER_GATES), hide_index=True, width="stretch")

# ---------- 「在哪改」入口 ----------

st.header("③ 修改入口汇总")
entry_cols = st.columns(3)
with entry_cols[0]:
    st.page_link("pages/43_unified_config_center.py", label="统一配置中心（风控边界/熔断）",
                 icon=":material/tune:")
with entry_cols[1]:
    st.page_link("pages/7_risk_and_limits.py", label="风险与限额（全局 RISK_*/REAL_*）",
                 icon=":material/shield:")
with entry_cols[2]:
    st.page_link("pages/44_strategy_center.py", label="策略中心（预算表，租户独立）",
                 icon=":material/savings:")

st.caption(
    "共用表说明：strategy_risk_budgets 已于迁移 25（v2026.11.07-2）租户化——"
    "real/paper 各行独立，改一租户不再联动另一租户。"
)
