"""限额地图 UI 化守卫（backlog P2-2，2026-11-07）。

口径：只读汇总页——零配置写入、登记层 13+4 道门完整、派生层与下单门禁同源
（resolve_limit_chain）、修改入口链接齐、共用表租户化说明在场。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "49_limit_map.py"


def _source() -> str:
    text = PAGE.read_text(encoding="utf-8")
    assert text, "页 49 为空"
    return text


def test_page_readonly_no_config_write():
    """只读铁律：页面不得出现任何配置写入调用。"""
    source = _source()
    for banned in ("set_setting", "set_tenant_setting", "mutate_runtime_config",
                   "save_", "upsert_", "set_strategy_enabled", "set_strategy_entries_paused"):
        assert banned not in source, f"只读页出现写入调用：{banned}"


def test_page_covers_all_registered_gates():
    """登记层完整性：实盘 13 道 + 模拟盘 5 行（含遗留键说明）齐全。"""
    source = _source()
    for gate in ("单笔（派生）", "容量线", "日累计开仓", "日亏急停",
                 "微资本单笔门", "REAL_MAX_ORDER_NOTIONAL", "REAL_MAX_DAILY_NOTIONAL",
                 "回撤预警/熔断/恢复", "risk_drawdown_warn", "熔断模式", "risk_breaker_mode",
                 "压力损失预算", "strategy_risk_budgets", "风控引擎敞口",
                 "RISK_MAX_GROSS_EXPOSURE", "日内回撤急停", "订单执行",
                 "三账对账熔断", "雷达批预算", "VOLATILITY_RADAR_BUDGETS",
                 "日亏预警"):
        assert gate in source, f"登记层缺门槛：{gate}"


def test_page_derived_layer_uses_same_source_as_gate():
    """派生层必须与下单门禁同源（resolve_limit_chain），不得自算口径。"""
    source = _source()
    assert "resolve_limit_chain" in source, "派生层未复用门禁同源接口"
    assert "LimitProfile()" in source


def test_page_has_edit_entry_links():
    """修改入口：三个 UI 位置链接齐（配置中心/风险限额/策略中心）。"""
    source = _source()
    for link in ("pages/43_unified_config_center.py", "pages/7_risk_and_limits.py",
                 "pages/44_strategy_center.py"):
        assert link in source, f"缺修改入口链接：{link}"


def test_page_notes_migration25_shared_table_eliminated():
    """共用表租户化说明必须在场（消除「改一行两租户联动」的旧认知）。"""
    source = _source()
    assert "迁移 25" in source and "租户化" in source


def test_page_uses_readonly_store_entry():
    """store 必须经 open_trading_store 统一入口（迁移门禁守卫口径）。"""
    source = _source()
    assert "open_trading_store" in source
    assert "TradingStore(" not in source.replace("open_trading_store", "")
