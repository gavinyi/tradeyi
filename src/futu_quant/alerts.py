from __future__ import annotations

import json
import hashlib
import hmac
import os
from urllib.request import Request, urlopen

from .store import TradingStore


class AlertManager:
    """Persists every alert and optionally mirrors it to a generic JSON webhook."""

    def __init__(self, store: TradingStore):
        self.store = store
        self.webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()

    def send(
        self, severity: str, event_type: str, message: str, incident_key: str | None = None,
    ) -> None:
        if severity.upper() in {"ERROR", "CRITICAL"}:
            opened, _ = self.store.record_incident(incident_key or event_type, severity, message)
            if not opened:
                return
        dedupe_seconds = max(0, int(os.getenv("ALERT_DEDUPE_SECONDS", "900")))
        if dedupe_seconds and self.store.alert_recently_exists(
            severity, event_type, message, dedupe_seconds
        ):
            return
        delivered = False
        if self.webhook:
            try:
                payload = json.dumps({
                    "severity": severity, "event_type": event_type, "message": message,
                }).encode()
                headers = {"Content-Type": "application/json"}
                secret = os.getenv("ALERT_WEBHOOK_SECRET", "").encode()
                if secret:
                    headers["X-Quant-Signature-SHA256"] = hmac.new(
                        secret, payload, hashlib.sha256
                    ).hexdigest()
                request = Request(self.webhook, payload, headers)
                with urlopen(request, timeout=5) as response:  # noqa: S310
                    delivered = 200 <= response.status < 300
            except Exception:
                delivered = False

        # 飞书群即时卡片广播 (支持群 Chat ID 与自定义 Webhook)
        feishu_delivered = False
        try:
            from .feishu_reporter import FeishuReporter
            reporter = FeishuReporter(self.store)
            feishu_delivered = reporter.send_alert_card(severity, event_type, message, is_recovered=False)
        except Exception:
            pass

        self.store.add_alert(severity, event_type, message, delivered or feishu_delivered)

    def recover(
        self, event_type: str, message: str = "故障已恢复", incident_key: str | None = None,
    ) -> bool:
        if not self.store.recover_incident(incident_key or event_type):
            return False

        # 飞书群故障恢复卡片广播
        feishu_recovered = False
        try:
            from .feishu_reporter import FeishuReporter
            reporter = FeishuReporter(self.store)
            feishu_recovered = reporter.send_alert_card("INFO", event_type, message, is_recovered=True)
        except Exception:
            pass

        self.store.add_alert("INFO", f"{event_type}_RECOVERED", message, feishu_recovered)
        return True
