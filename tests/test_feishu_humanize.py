from __future__ import annotations

from futu_quant.feishu_humanize import humanize_alert


def test_humanize_alert_structured_four_elements():
    title, lines = humanize_alert(
        "DAILY_LOSS_KILL_SWITCH",
        "CRITICAL",
        "账户日内浮亏达到 -5000 美元",
        is_recovered=False,
        host="192.168.1.50",
        port=8501,
    )
    assert "🚨 【紧急风控】日内亏损熔断急停" in title
    content = "\n".join(lines)
    assert "🔴 **告警级别**: **紧急处理 (CRITICAL)**" in content
    assert "📌 **业务事实**: 账户日内浮亏达到 -5000 美元" in content
    assert "⚡ **资金与系统影响**: 今日账户浮动亏损触及日限额红线" in content
    assert "👉 **交易员指引**: 建议前往【风控中心】" in content
    assert "🔗 **控制台直达**: [点击打开交易控制台](http://192.168.1.50:8501)" in content


def test_humanize_alert_recovery_status():
    title, lines = humanize_alert(
        "STRATEGY_ERROR",
        "INFO",
        "option_combo_monitor 已恢复正常",
        is_recovered=True,
    )
    assert "🟢 【故障自愈】策略运行异常已恢复正常" in title
    content = "\n".join(lines)
    assert "🟢 **当前状态**: **已恢复正常 (NORMAL)**" in content
    assert "📌 **业务事实**: option_combo_monitor 已恢复正常" in content


def test_humanize_alert_fallback_for_unknown():
    title, lines = humanize_alert(
        "CUSTOM_UNREGISTERED_EVENT",
        "WARNING",
        "某个未登记的事件产生",
    )
    assert "🟡 【风险预警】CUSTOM_UNREGISTERED_EVENT" in title
    content = "\n".join(lines)
    assert "📌 **业务事实**: 某个未登记的事件产生" in content


def test_humanize_alert_reconciliation_prefix():
    title, lines = humanize_alert(
        "RECONCILIATION_POSITION_DIFF",
        "ERROR",
        "发现持仓差异",
    )
    assert "⚠️ 【系统异常】对账差异（position_diff）" in title
    content = "\n".join(lines)
    assert "⚡ **资金与系统影响**" in content
