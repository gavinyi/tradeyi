"""`PORTFOLIO_STRESS` 告警：租户化 incident + 双阈值滞回（P1-8）。

两个要解决的问题（2026-10-23 夜间实测）：

1. **无租户隔离**：`incident_key` 全局硬编码，多个租户的组合压力告警互相顶掉，
   飞书卡片看不出是实盘还是模拟盘。
2. **无滞回 → flapping**：单一句阈值（默认 10%）时，损失率在 9.99% / 10.02%
   之间震荡，每 15 分钟一轮「告警→恢复→告警」，一晚 24 次，告警被刷成噪音。

本模块把「阈值解析、事件键、滞回状态机」集中到一处：
- 阈值：`portfolio_stress_alert_pct:{tenant}` / `portfolio_stress_recover_pct:{tenant}`，
  回落旧无前缀键 / 环境变量 / 默认值；**非法值 → 落 ERROR 并用安全默认值**，绝不静默；
- 事件键：`PORTFOLIO_STRESS_BREACH:{tenant}`；
- 滞回：告警态下必须跌破恢复线才消警，缓冲带内静默维持；**状态持久化**，
  进程重启不会重复告警也不会漏恢复。
"""

from __future__ import annotations

import os

from .store import TradingStore
from .tenant import TenantConfigKeyError, resolve_tenant_config_key

LEGACY_INCIDENT_KEY = "PORTFOLIO_STRESS_BREACH"
ALERT_PCT_BASE = "portfolio_stress_alert_pct"
RECOVER_PCT_BASE = "portfolio_stress_recover_pct"
STATE_BASE = "portfolio_stress_state"
LEGACY_CLOSED_BASE = "portfolio_stress_legacy_incident_closed"

DEFAULT_BREACH = 0.10
DEFAULT_RECOVER = 0.08

STATE_ALERTING = "ALERTING"
STATE_OK = "OK"


def _tenant_key(store: TradingStore, base_key: str, tenant_id: str | None) -> str | None:
    """租户键；租户为空/未注册时返回 None（由调用方回落旧键，并留痕）。"""
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return None
    try:
        return resolve_tenant_config_key(tenant, base_key)
    except TenantConfigKeyError:
        # 未注册租户只能回落全局键 —— 两个未注册租户会共享同一份滞回状态。
        # 生产走的是注册过的 profile，实际不会触发；但**不能静默**，否则将来真触发时
        # 会表现为"两个租户的告警状态互相顶掉"而无从排查。
        try:
            store.operational_event(
                "portfolio_stress", "STRESS_STATE_TENANT_FALLBACK", "WARNING",
                {"tenant_id": tenant, "base_key": base_key,
                 "message": "租户未注册：状态键回落全局，可能与其它未注册租户共享"},
            )
        except Exception:  # noqa: BLE001 - 留痕失败不该让告警主流程崩
            pass
        return None


def _read_float(
    store: TradingStore, base_key: str, tenant_id: str | None, default: float,
    env_name: str | None = None,
) -> tuple[float, str]:
    """读一个阈值：租户键 → 旧无前缀键 → env → 默认。返回 (值, 来源)。"""
    key = _tenant_key(store, base_key, tenant_id)
    if key:
        raw = str(store.get_setting(key, "") or "").strip()
        if raw:
            try:
                return float(raw), f"tenant:{tenant_id}"
            except ValueError:
                return float("nan"), f"tenant:{tenant_id}"
    raw = str(store.get_setting(base_key, "") or "").strip()
    if raw:
        try:
            return float(raw), "legacy"
        except ValueError:
            return float("nan"), "legacy"
    if env_name and os.getenv(env_name):
        try:
            return float(os.getenv(env_name, "")), "env"
        except ValueError:
            return float("nan"), "env"
    return default, "default"


def resolve_portfolio_stress_thresholds(
    store: TradingStore, tenant_id: str | None,
) -> tuple[float, float]:
    """返回 (告警阈值 breach, 恢复阈值 recover)。

    判据：`0 < recover < breach <= 1.0`。非法（含解析失败）→ 落 ERROR 事件并用
    `0.10 / 0.08` 安全默认值；**不静默**，否则「配置写错了」会表现为「压力告警再也不响」。
    """
    breach, breach_source = _read_float(
        store, ALERT_PCT_BASE, tenant_id, DEFAULT_BREACH, "PORTFOLIO_STRESS_ALERT_PCT",
    )
    recover, recover_source = _read_float(
        store, RECOVER_PCT_BASE, tenant_id, DEFAULT_RECOVER, "PORTFOLIO_STRESS_RECOVER_PCT",
    )
    valid = (
        breach == breach  # 非 NaN
        and recover == recover
        and 0.0 < recover < breach <= 1.0
    )
    if not valid:
        store.operational_event(
            "portfolio_stress", "PORTFOLIO_STRESS_THRESHOLD_INVALID", "ERROR",
            {
                "tenant_id": str(tenant_id or ""),
                "breach": breach if breach == breach else "NaN",
                "recover": recover if recover == recover else "NaN",
                "breach_source": breach_source,
                "recover_source": recover_source,
                "fallback": {"breach": DEFAULT_BREACH, "recover": DEFAULT_RECOVER},
                "rule": "必须满足 0 < recover < breach <= 1.0",
            },
        )
        return DEFAULT_BREACH, DEFAULT_RECOVER
    return breach, recover


def portfolio_stress_incident_key(tenant_id: str | None) -> str:
    """租户级 incident 键；租户为空时用旧全局键（单向兼容，不做反向迁移）。"""
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return LEGACY_INCIDENT_KEY
    return resolve_tenant_config_key(tenant, LEGACY_INCIDENT_KEY)


def read_hysteresis_state(store: TradingStore, tenant_id: str | None) -> str:
    key = _tenant_key(store, STATE_BASE, tenant_id) or STATE_BASE
    return str(store.get_setting(key, "") or "").strip().upper()


def write_hysteresis_state(store: TradingStore, tenant_id: str | None, state: str) -> None:
    key = _tenant_key(store, STATE_BASE, tenant_id) or STATE_BASE
    store.set_setting(key, state)


def next_stress_action(
    current_ratio: float, state: str, breach: float, recover: float,
) -> str:
    """滞回状态机：返回 ``alert`` / ``recover`` / ``none``。

    - 未告警 且 ≥ breach → ``alert``（**只在首次越过阈值时发一次**）；
    - 已告警 且 < recover  → ``recover``；
    - 其余（含 [recover, breach) 缓冲带、以及 ALERTING 期间持续破线）→ ``none``：
      **不重发告警，也不误消警**。

    语义钉死（Reviewer 记录项）：持续破线期间每 15 分钟重发一条"百分比不同"的告警
    会被当成新事故，`record_incident` 的 count 也会每轮 +1。一条 incident 只发一次，
    恢复时发一次 recover —— 这才是一对事件。
    """
    state_upper = str(state or "").strip().upper()
    if float(current_ratio) >= float(breach):
        return "none" if state_upper == STATE_ALERTING else "alert"
    if state_upper == STATE_ALERTING and float(current_ratio) < float(recover):
        return "recover"
    return "none"


def legacy_incident_closed(store: TradingStore, tenant_id: str | None) -> bool:
    key = _tenant_key(store, LEGACY_CLOSED_BASE, tenant_id) or LEGACY_CLOSED_BASE
    return str(store.get_setting(key, "") or "").strip().lower() == "true"


def mark_legacy_incident_closed(store: TradingStore, tenant_id: str | None) -> None:
    key = _tenant_key(store, LEGACY_CLOSED_BASE, tenant_id) or LEGACY_CLOSED_BASE
    store.set_setting(key, "true")
