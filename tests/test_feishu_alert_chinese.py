from __future__ import annotations

from unittest.mock import MagicMock

from futu_quant.feishu_reporter import FeishuReporter, human_event_type


def test_human_event_type_coverage():
    assert human_event_type("ML_MODEL_STALE") == "ML 模型需重训（模型过旧）"
    assert human_event_type("STRATEGY_ERROR") == "策略执行异常"
    assert human_event_type("OPEND_UNAVAILABLE") == "富途 OpenD 网关暂不可用"
    assert human_event_type("LEDGER_MISMATCH") == "三账持仓对账异常"
    assert human_event_type("PROCESS_TERMINATED") == "服务进程异常退出"
    assert human_event_type("RECONCILIATION_POSITION_MISMATCH") == "对账差异（position_mismatch）"
    assert human_event_type("CUSTOM_UNKNOWN") == "CUSTOM_UNKNOWN"


def test_send_alert_card_uses_chinese_title(monkeypatch):
    mock_store = MagicMock()
    mock_store.get_setting.return_value = ""
    reporter = FeishuReporter(store=mock_store)
    reporter.chat_id = "test_chat"
    reporter.group_chat_id = None
    reporter.webhook_url = None

    captured_cmds = []

    def fake_run(cmd):
        captured_cmds.append(cmd)
        return {"ok": True}

    monkeypatch.setattr("futu_quant.feishu_reporter._run_lark_json", fake_run)

    # 1. 验证 ERROR 级别卡片标题包含中文
    reporter.send_alert_card("ERROR", "ML_MODEL_STALE", "模型过旧")
    assert len(captured_cmds) == 1
    markdown_text = captured_cmds[0][captured_cmds[0].index("--markdown") + 1]
    assert "【系统错误】ML 模型需重训（模型过旧）" in markdown_text
    assert "ML 模型需重训（模型过旧） (`ML_MODEL_STALE`)" in markdown_text

    # 2. 验证恢复卡片标题包含中文
    reporter.send_alert_card("INFO", "STRATEGY_ERROR", "策略恢复正常", is_recovered=True)
    markdown_text2 = captured_cmds[1][captured_cmds[1].index("--markdown") + 1]
    assert "【故障恢复】策略执行恢复正常 已恢复正常" in markdown_text2
    assert "策略执行恢复正常 (`STRATEGY_ERROR`)" in markdown_text2
