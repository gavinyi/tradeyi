"""熔断 ack（P2-①，PRD data/qa/prd_p2_batch_20261107.md §1）· 红区。

语义定死（PRD 1.1）：**ack 只表示「人已看过报警，并决定继续」，不等于解除熔断、
重置回撤、放宽阈值。** ack 是知情留痕，不是风控放松。

三要素（PRD 1.3，缺一不可）：理由必填（≥10 字）/ 管理员密码校验 / 二次确认
（确认文案必须含「ack 不改变任何限额与阈值」）。

硬性禁止（PRD 1.4）：
- 禁批量 ack（本模块只暴露单对象接口，不存在批量入口）；
- 禁 ack 时顺带改配置（本模块零 runtime_config / limit_profile 写入，AST 守卫钉死）；
- 禁永久豁免：ack 只对当次触发有效——ack 记录按 incident_key 版本化，
  同类条件再次触发（新 incident）须重新 ack；
- 禁删/改 ack 记录（append-only）；
- 跨租户隔离：ack 记录键带租户，paper 的 ack 不影响 real。

与手工 ack（Ops 在飞书/脚本里 `alerts.recover`）的**零行为差异**：本模块只追加
审批记录 + 恢复告警（`alerts.recover`），不写任何其他状态。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .admin_auth import configured as admin_configured, verify as verify_admin

#: ack 记录键（append-only，按租户 + incident 分区）。
ACK_LOG_KEY = "breaker_ack:{tenant}:log"
#: 理由最小长度（PRD 1.3-A1）。
MIN_REASON_CHARS = 10
#: 二次确认文案必须包含的关键句（PRD 1.3-A3，页面守卫也钉）。
CONFIRM_ACK_PHRASE = "ack 不改变任何限额与阈值"


@dataclass(frozen=True)
class AckRequest:
    """一次 ack 请求（单对象：tenant + incident_key + 事故版本）。"""

    tenant_id: str
    incident_key: str
    incident_version: str      # 触发时的 incident 版本/时间戳——「当次」的锚
    reason: str
    admin_password: str
    confirmed: bool            # 二次确认勾选
    actor: str = "streamlit_admin"

    def validate(self) -> list[str]:
        """三要素校验（返回问题列表，空 = 可 ack）。"""
        problems: list[str] = []
        reason = str(self.reason or "").strip()
        if len(reason) < MIN_REASON_CHARS:
            problems.append(f"理由必填且至少 {MIN_REASON_CHARS} 字（当前 {len(reason)} 字）")
        if not self.confirmed:
            problems.append("未完成二次确认")
        if not admin_configured():
            problems.append("管理员密码未配置（ADMIN_PASSWORD_HASH）")
        elif not verify_admin(str(self.admin_password or "")):
            problems.append("管理员密码校验未通过")
        return problems


class BreakerAckLog:
    """append-only ack 记录（键租户隔离；禁删除/覆盖——只提供 append 与查询）。"""

    def __init__(self, store, tenant_id: str):
        self._store = store
        self._tenant = str(tenant_id or "default")

    def _key(self) -> str:
        return ACK_LOG_KEY.format(tenant=self._tenant)

    def entries(self) -> list[dict]:
        raw = self._store.get_setting(self._key(), "")
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def has_ack_for(self, incident_key: str, incident_version: str) -> bool:
        """「当次有效」判据：同 key 且同版本才算已 ack；新版本须重新 ack（禁永久豁免）。"""
        return any(
            str(entry.get("incident_key")) == str(incident_key)
            and str(entry.get("incident_version")) == str(incident_version)
            for entry in self.entries()
        )

    def append(self, entry: dict) -> dict:
        log = self.entries()
        log.append(dict(entry))
        self._store.set_setting(
            self._key(), json.dumps(log, ensure_ascii=False, sort_keys=True),
        )
        return entry


def ack_breaker(store, alerts, request: AckRequest, *,
                incident_open: bool, context: dict | None = None) -> dict:
    """执行一次 ack（三要素校验 → 追加审计 → recover 告警）。

    **零行为差异**：与手工 ack 相同的动作 = `alerts.recover(incident_key, ...)`，
    此外只落 ack 审计记录。**不写任何 runtime_config / limit_profile 值。**

    incident_open=False 时拒绝：ack 针对的是「还开着的触发」，事故已恢复就无需 ack。
    """
    problems = request.validate()
    if problems:
        raise PermissionError("；".join(problems))
    if not incident_open:
        raise PermissionError("incident 已不在 OPEN 状态，无需 ack")

    entry = {
        "actor": str(request.actor),
        "tenant_id": str(request.tenant_id),
        "incident_key": str(request.incident_key),
        "incident_version": str(request.incident_version),
        "reason": str(request.reason).strip(),
        "context": dict(context or {}),
        "acked_at": alerts.store.now() if hasattr(alerts.store, "now") else "",
    }
    BreakerAckLog(store, request.tenant_id).append(entry)
    alerts.recover(
        str(request.incident_key),
        f"已人工确认（ack）：{entry['reason']}",
        incident_key=str(request.incident_key),
    )
    return entry
