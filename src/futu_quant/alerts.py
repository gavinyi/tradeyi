from __future__ import annotations

import json
import hashlib
import hmac
import os
from urllib.request import Request, urlopen

from .store import TradingStore

# 严格限制推送飞书群的致命事件白名单 (少而精，杜绝告警疲劳)
FEISHU_GROUP_ALERT_WHITELIST = {
    "OPEND_DISCONNECTED",                         # OpenD 网关掉线/通信中断
    "KILL_SWITCH_ENGAGED",                        # 账户风控急停熔断触发
    "LEDGER_MISMATCH",                            # 三账对账不一致/疑似残腿或丢单
    "STRATEGY_INTERRUPTED_WITH_OPEN_OPTIONS",     # 持仓期权时网络连续失败进入紧急保护
    "PROTECTIVE_RADAR_RESTART_FAILED",            # 雷达自动重启失败
    "OPEND_AUTO_RESTART_FAILED",                  # OpenD 自动重启失败
    "OPTION_LIFECYCLE_RISK",                      # 期权临近到期/行权将产生裸空正股敞口（必须人工可见）
    # 以下三条同属「涉及真金敞口/风控门禁」的 ERROR 级事件，历史上一律 delivered=0（仅落库、
    # 从不推送）。产品口径：这类事件必须让人知道，不因「怕吵」而静默。
    "DAILY_LOSS_KILL_SWITCH",                     # 账户当日亏损触及急停线，已自动急停
    "REAL_TRADE_REJECTED",                        # 实盘下单被门禁拒绝（含超额/隔离铁律）
    "ORDER_ENV_MISMATCH",                         # 订单执行环境与券商环境不一致，已禁止改单撤单
    # 硬止损限额非法（<=0）。不修则 `usd_limit > 0` 恒假 → 止损在两层同时失效
    # （共同失败点），而这是唯一的线索 —— 不落推送等于让用户在无声中失去止损。
    "HARD_STOP_LIMIT_INVALID",
    # 开仓被限额门禁拒绝（限额缺失 / 限额非法）。这两条是**开仓拒绝**，比退出路径的
    # 回落更该让人看见：一旦 record_incident 置为 open 而首次投递恰好失败，后续每次
    # 拒绝都会被 `send()` 的 `not opened and not in whitelist` 分支提前 return ——
    # campaign 会**永久静默地被拒绝开仓**（10-22 item 36「检出但 delivered=0」同形态）。
    "VOLATILITY_STOP_LIMIT_MISSING",
    "VOLATILITY_STOP_LIMIT_INVALID",
    # 纸面账户的亏损守卫分两层，两条都要推群：
    # * PAPER_DAILY_LOSS_WARN —— **告警线**：纸面亏损不是真钱，自动急停会中断正在进行的
    #   策略验证（2026-10-22 模拟账户亏 $10,351 被全局 $10,000 线误杀，根因在此）。
    #   只告警、不拦单，但必须让人看见 —— 否则"放宽"就成了"没人知道"。
    #   作用域：**仅 `trading_env=SIMULATE` 的租户**，禁止配到实盘账户（真金无"只告警"口径）。
    # * PAPER_DAILY_LOSS_HARD_STOP —— **硬停线**（= `max_daily_loss`，$100,000）：拦单。
    "PAPER_DAILY_LOSS_WARN",
    "PAPER_DAILY_LOSS_HARD_STOP",
    # ML 模型管理（2026-10-23）。三条都是「库里有、用户看不到」的形态，必须推群：
    # * ML_MODEL_STALE        —— 模型超过新鲜度阈值未重训。评分质量退化是渐进的、
    #   不会报错，唯一线索就是年龄，不推等于没有守卫。
    # * ML_SCORING_DEGRADED  —— **本该用 ML 却回退了规则打分**（依赖缺失/文件缺失/
    #   反序列化失败）。ml_scorer 把异常吞在 INFO 级，这是唯一的对外信号。
    # * ML_ALERT_NOT_DELIVERED —— ML 守卫自己投递失败。守卫静默失效是最坏形态，
    #   必须递归可见（否则「守卫坏了」这件事本身也没人知道）。
    "ML_MODEL_STALE",
    "ML_SCORING_DEGRADED",
    "ML_ALERT_NOT_DELIVERED",
}


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
            if not opened and event_type not in FEISHU_GROUP_ALERT_WHITELIST:
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

        # 飞书群即时卡片广播 (仅对白名单内的致命事故推群，防止告警疲劳)
        feishu_delivered = False
        is_critical_or_whitelisted = (
            event_type in FEISHU_GROUP_ALERT_WHITELIST or severity.upper() == "CRITICAL"
        )
        if is_critical_or_whitelisted:
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

        # 飞书群故障恢复卡片广播 (仅对白名单内的事件发送恢复卡片)
        feishu_recovered = False
        if event_type in FEISHU_GROUP_ALERT_WHITELIST:
            try:
                from .feishu_reporter import FeishuReporter
                reporter = FeishuReporter(self.store)
                feishu_recovered = reporter.send_alert_card("INFO", event_type, message, is_recovered=True)
            except Exception:
                pass

        self.store.add_alert("INFO", f"{event_type}_RECOVERED", message, feishu_recovered)
        return True
