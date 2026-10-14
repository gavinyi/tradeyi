"""
测试飞书群实时告警与故障恢复链路 (Feishu Real-time Alert Test Suite)
严格隔离：单元测试必须 Mock 真实网络请求，禁止污染生产飞书群。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from futu_quant.alerts import AlertManager
from futu_quant.store import TradingStore


def test_alert_manager_dispatches_feishu_card():
    db_path = Path("data/test_alert_feishu.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        alerts = AlertManager(store=store)

        with patch("futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True) as mock_send:
            # 1. 触发一次 CRITICAL 告警
            alerts.send("CRITICAL", "OPEND_DISCONNECTED", "OpenD 网关掉线演练测试", incident_key="TEST_INCIDENT")
            alert_rows = store.alerts()
            assert len(alert_rows) == 1
            assert alert_rows[0]["severity"] == "CRITICAL"
            assert alert_rows[0]["event_type"] == "OPEND_DISCONNECTED"
            assert mock_send.call_count == 1

            # 2. 验证防刷屏：立即再次发送相同告警，不重复记录，也不重复推送
            alerts.send("CRITICAL", "OPEND_DISCONNECTED", "OpenD 网关掉线演练测试", incident_key="TEST_INCIDENT")
            assert len(store.alerts()) == 1
            assert mock_send.call_count == 1

            # 3. 触发故障恢复
            recovered = alerts.recover("OPEND_DISCONNECTED", "OpenD 连接恢复正常", incident_key="TEST_INCIDENT")
            assert recovered is True

            # 验证产生了 RECOVERED 记录 (id 倒序排列，第一条为最新)
            alert_rows2 = store.alerts()
            assert len(alert_rows2) == 2
            assert alert_rows2[0]["event_type"] == "OPEND_DISCONNECTED_RECOVERED"
            assert mock_send.call_count == 2
    finally:
        db_path.unlink(missing_ok=True)
