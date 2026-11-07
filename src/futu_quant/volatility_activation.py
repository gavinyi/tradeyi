"""波动率雷达激活证据：scanner 唯一生产，campaign 只消费。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


KEY_PREFIX = "volatility_activation_evidence"
DEFAULT_MAX_AGE_SECONDS = 900.0


def evidence_key(symbol: str) -> str:
    return f"{KEY_PREFIX}:{str(symbol).upper()}"


def build_activation_evidence(
    symbol: str,
    regime: dict[str, Any],
    profile: dict[str, Any],
    *,
    observed_at: datetime,
    trace_id: str,
) -> dict[str, Any]:
    """构造不可变的当轮激活事实；阈值和结果必须来自同一次 scanner 计算。"""
    return {
        "schema_version": 1,
        "trace_id": trace_id,
        "symbol": str(symbol).upper(),
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "active": bool(regime.get("active", False)),
        "reason": str(regime.get("reason", "")),
        "profile": dict(profile),
        "metrics": {
            key: regime.get(key)
            for key in (
                "intraday_return", "zscore_20d", "atr_ratio", "volume_ratio",
                "direction_score", "trigger_confirmed", "volume_confirmed",
                "direction_gate", "entry_zscore_target", "minimum_move_target",
            )
        },
        "checks": list(regime.get("checks") or []),
    }


def save_activation_evidence(store, evidence: dict[str, Any]) -> None:
    store.set_setting(
        evidence_key(str(evidence["symbol"])),
        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )


# 按 trace_id 存档（回测 L-A 对齐前置，2026-11-07）：单槽键 `evidence_key` 是
# 覆盖型——每标的只留最近一次，历史 BUY 无法对到当次证据。存档键按 trace_id
# 一条一存，**只在 active 触发轮写入**（量小且恰是重放需要的），不覆盖不清理。
ARCHIVE_KEY_PREFIX = "volatility_activation_evidence_archive"


def archive_key(trace_id: str) -> str:
    return f"{ARCHIVE_KEY_PREFIX}:{trace_id}"


def save_activation_evidence_archive(store, evidence: dict[str, Any]) -> None:
    """把当轮证据按 trace_id 存档，供回放按 `payload.activation_trace_id` 复验。"""
    store.set_setting(
        archive_key(str(evidence["trace_id"])),
        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )


def load_activation_evidence_archive(store, trace_id: str) -> dict[str, Any] | None:
    raw = store.get_setting(archive_key(str(trace_id)), "")
    if not raw:
        return None
    try:
        evidence = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return evidence if isinstance(evidence, dict) else None


def load_activation_evidence(
    store,
    symbol: str,
    *,
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> tuple[dict[str, Any] | None, str]:
    """读取并校验 scanner 证据；缺失、损坏、过期均 fail-closed。"""
    raw = store.get_setting(evidence_key(symbol), "")
    if not raw:
        return None, "缺少 scanner 激活证据"
    try:
        evidence = json.loads(raw)
        observed = datetime.fromisoformat(str(evidence["observed_at"]))
        if observed.tzinfo is None:
            raise ValueError("observed_at 无时区")
        age = (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"scanner 激活证据无效：{exc}"
    if age < -5:
        return None, f"scanner 激活证据时钟回拨 {age:.1f}s"
    if age > max_age_seconds:
        return None, f"scanner 激活证据已过期 {age:.1f}s > {max_age_seconds:.0f}s"
    if str(evidence.get("symbol", "")).upper() != str(symbol).upper():
        return None, "scanner 激活证据标的不匹配"
    return evidence, "scanner 激活证据有效"
