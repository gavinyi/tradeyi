"""ML 模型新鲜度与降级可观测性（ML 管理第一步：让「断了」可见）。

背景（2026-10-23 事故复盘）：

1. 曾有一个「月度重训」脚本**只刷元数据、从不训练**（已于 2026-10-25 P1-05 删除），
   它把 ``volatility_scorer_v3.json`` 的 ``trained_at`` 写成当前时间 —— 于是生产上出现
   「json 显示 10-14、pkl 实际停在 09-09」的假象。**任何以该 json 的 ``trained_at``
   作为年龄依据的守卫都会永远显示绿色**，等于没有守卫。
2. ``ml_scorer.load_ml_model`` 在 ``pickle.load`` 失败（例如缺 scikit-learn）时
   **吞掉异常**并返回内置规则打分器，且只记 INFO —— 于是「ML 静默降级 6 周」
   没有任何告警，也没有心跳。

本模块**只检测、只上报，不改变任何打分行为**：

* 年龄来源优先级：**pkl 内的 ``trained_at`` → pkl 文件 mtime**。
  **绝不**把 ``volatility_scorer_v3.json`` 的 ``trained_at`` 当作年龄依据；该值只作为
  ``metadata_json_trained_at`` 单独展示，并在页面上标注「元数据刷新时间，非训练时间」。
* 打分方式判定：实例化 ``MLSymbolScorer``，看 ``model`` 是不是 ``BuiltinRuleModel``。
  这能把「显式禁用」（配置选择）与「静默降级」（故障）区分开 —— 前者不该告警，
  后者必须让人看见。
* 所有函数 **fail-safe**：任何异常都只体现在返回值的 ``reason`` 里，绝不抛出，
  更不允许让调用方（交易主循环）因此中断。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODEL_DIR = Path("data/ml_models")
DEFAULT_MODEL_NAME = "volatility_scorer_v3.pkl"

# 新鲜度分档（天）。**与 PRD §三/§四 同一套阈值**：<30 绿 / 30-34 橙 / ≥35 红。
# 页面与告警必须同源，否则「页面说新鲜、告警说必须重训」会互相打脸。
DEFAULT_WARN_DAYS = 30
DEFAULT_STALE_DAYS = 35

FRESHNESS_OK = "OK"
FRESHNESS_WARN = "WARN"
FRESHNESS_STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"

FRESHNESS_LABELS = {
    FRESHNESS_OK: "新鲜",
    FRESHNESS_WARN: "偏旧",
    FRESHNESS_STALE: "过旧",
    FRESHNESS_UNKNOWN: "未知",
}

SCORING_ML = "ml"
SCORING_RULE_FALLBACK = "rule_fallback"
SCORING_DISABLED = "disabled"

SCORING_LABELS = {
    SCORING_ML: "ML 打分",
    SCORING_RULE_FALLBACK: "⚠️ 规则降级",
    SCORING_DISABLED: "已禁用",
}

# 需要推群的告警类型（与 alerts.FEISHU_GROUP_ALERT_WHITELIST 保持一致）
EVENT_STALE = "ML_MODEL_STALE"
EVENT_DEGRADED = "ML_SCORING_DEGRADED"
EVENT_DISABLED = "ML_SCORING_DISABLED"
EVENT_NOT_DELIVERED = "ML_ALERT_NOT_DELIVERED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(raw: Any) -> datetime | None:
    """把 pkl 里可能是 str / datetime / None 的 ``trained_at`` 归一成带时区的 datetime。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def default_model_path() -> Path:
    """生产模型路径。租户化（``volatility_scorer_v3.{tenant_id}.pkl``）在配置区落地后接入。"""
    return Path(os.getenv("ML_MODEL_PATH", str(MODEL_DIR / DEFAULT_MODEL_NAME)))


def read_model_metadata(model_path: Path) -> dict | None:
    """读 pkl 内的元数据字典。**反序列化失败时返回 None，绝不抛出**。

    注意：这一步需要 scikit-learn 才能 unpickle sklearn 模型；缺依赖时返回 None，
    由 `model_age_days` 回落到文件 mtime —— 这本身就是「依赖缺失」的判据。
    """
    try:
        with open(model_path, "rb") as handle:
            payload = pickle.load(handle)
    except Exception as exc:  # noqa: BLE001 - fail-safe：任何失败都只是「读不到」
        logger.info("ML 模型元数据读取失败（将回落文件 mtime）: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def model_age_days(model_path: Path, now: datetime | None = None) -> tuple[float | None, str]:
    """返回 ``(年龄天数, 年龄来源)``。

    来源优先级：``pkl.trained_at`` → ``pkl.mtime`` → ``unknown``。
    **刻意不读 ``volatility_scorer_v3.json``** —— 那是元数据刷新时间，不是训练时间。
    """
    now = now or _now()
    reference = Path(model_path)
    if not reference.exists():
        return None, "unknown"

    metadata = read_model_metadata(reference)
    trained_at = _as_datetime((metadata or {}).get("trained_at"))
    if trained_at is not None:
        return (now - trained_at).total_seconds() / 86400.0, "pkl.trained_at"

    try:
        mtime = datetime.fromtimestamp(reference.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None, "unknown"
    return (now - mtime).total_seconds() / 86400.0, "pkl.mtime"


def classify_freshness(
    age_days: float | None,
    warn_days: int = DEFAULT_WARN_DAYS,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> str:
    """按年龄分档。阈值非法（warn >= stale）时抛 ValueError —— 配置错误不该静默。"""
    if warn_days >= stale_days:
        raise ValueError(f"新鲜度阈值非法：warn_days={warn_days} 必须小于 stale_days={stale_days}")
    if age_days is None:
        return FRESHNESS_UNKNOWN
    if age_days >= stale_days:
        return FRESHNESS_STALE
    if age_days >= warn_days:
        return FRESHNESS_WARN
    return FRESHNESS_OK


def _explicitly_disabled(enabled: bool | None) -> bool:
    if enabled is False:
        return True
    if enabled is None:
        return os.getenv("ML_ENABLED", "true").strip().lower() != "true"
    return False


def detect_scoring_mode(model_path: Path | None = None, enabled: bool | None = None) -> dict:
    """判定当前实际生效的打分方式，并把「为什么不是 ML」的原因一并给出。

    三种状态必须区分：

    * ``disabled``       —— 配置显式关掉 ML（``ML_ENABLED=false``），是选择，不是故障
    * ``rule_fallback``  —— 本该用 ML 但没能用上（依赖缺失/文件缺失/反序列化失败），是故障
    * ``ml``             —— 真在用 ML 模型
    """
    result: dict[str, Any] = {
        "mode": SCORING_ML,
        "label": SCORING_LABELS[SCORING_ML],
        "reason": "",
        "degradation_cause": "",
        "sklearn_available": True,
        "model_type": None,
        "model_loaded": False,
    }
    try:
        result["sklearn_available"] = importlib.util.find_spec("sklearn") is not None
    except (ImportError, ValueError):
        result["sklearn_available"] = False

    path = Path(model_path) if model_path is not None else default_model_path()
    result["model_path"] = str(path)

    if _explicitly_disabled(enabled):
        result["mode"] = SCORING_DISABLED
        result["label"] = SCORING_LABELS[SCORING_DISABLED]
        result["reason"] = "ML_ENABLED=false，ML 评分被显式关闭（配置选择，非故障）"
        return result

    try:
        from .ml_scorer import BuiltinRuleModel
        from .ml_symbol_scorer import MLSymbolScorer
    except Exception as exc:  # noqa: BLE001 - 导入失败也要给出可读原因
        result["mode"] = SCORING_RULE_FALLBACK
        result["label"] = SCORING_LABELS[SCORING_RULE_FALLBACK]
        result["reason"] = f"评分器模块导入失败，已回退规则打分：{exc}"
        result["degradation_cause"] = "import_failed"
        return result

    try:
        scorer = MLSymbolScorer(model_path=str(path), enabled=enabled)
    except Exception as exc:  # noqa: BLE001
        result["mode"] = SCORING_RULE_FALLBACK
        result["label"] = SCORING_LABELS[SCORING_RULE_FALLBACK]
        result["reason"] = f"评分器初始化失败，已回退规则打分：{exc}"
        result["degradation_cause"] = "init_failed"
        return result

    result["model_type"] = type(scorer.model).__name__ if scorer.model is not None else None

    if not scorer.enabled:
        # 走到这里说明不是显式关闭，那就是初始化时自己关掉了
        result["mode"] = SCORING_RULE_FALLBACK
        result["label"] = SCORING_LABELS[SCORING_RULE_FALLBACK]
        result["reason"] = "评分器初始化后处于关闭状态，已回退规则打分"
        result["degradation_cause"] = "scorer_disabled_after_init"
        return result

    if isinstance(scorer.model, BuiltinRuleModel):
        result["mode"] = SCORING_RULE_FALLBACK
        result["label"] = SCORING_LABELS[SCORING_RULE_FALLBACK]
        result["reason"] = "ML 模型未能加载，已回退内置规则打分（不拦单，但评分质量下降）"
        # PRD §四 的五种降级原因之一：缺依赖必须**明确**（此前记成 "unknown"，
        # 页面只能显示"未知"，运维无从下手）。
        if not result["sklearn_available"]:
            result["degradation_cause"] = "sklearn_missing"
        elif not path.exists():
            result["degradation_cause"] = "model_file_missing"
        else:
            result["degradation_cause"] = "model_unpickle_failed"
        return result

    result["mode"] = SCORING_ML
    result["label"] = SCORING_LABELS[SCORING_ML]
    result["model_loaded"] = True
    result["reason"] = "ML 模型已加载并生效"
    return result


def ml_model_health(
    model_path: Path | None = None,
    enabled: bool | None = None,
    tenant_id: str | None = None,
    now: datetime | None = None,
    warn_days: int = DEFAULT_WARN_DAYS,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict:
    """汇总模型健康状态。**永不抛出** —— 调用方是交易主循环，不能因检测失败而中断。"""
    now = now or _now()
    path = Path(model_path) if model_path is not None else default_model_path()
    health: dict[str, Any] = {
        "tenant_id": tenant_id or "",
        "model_path": str(path),
        "model_exists": path.exists(),
        "trained_at": None,
        "age_days": None,
        "age_source": "unknown",
        "freshness": FRESHNESS_UNKNOWN,
        "freshness_label": FRESHNESS_LABELS[FRESHNESS_UNKNOWN],
        "metadata_json_trained_at": None,
        "scoring_mode": SCORING_ML,
        "scoring_label": SCORING_LABELS[SCORING_ML],
        "scoring_reason": "",
        "degradation_cause": "",
        "sklearn_available": True,
        "feature_cols_count": None,
        "version": None,
        "checked_at": now.isoformat(),
    }

    try:
        age_days, age_source = model_age_days(path, now)
        health["age_days"] = age_days
        health["age_source"] = age_source
        health["freshness"] = classify_freshness(age_days, warn_days, stale_days)
        health["freshness_label"] = FRESHNESS_LABELS[health["freshness"]]
    except Exception as exc:  # noqa: BLE001
        health["freshness"] = FRESHNESS_UNKNOWN
        health["freshness_label"] = FRESHNESS_LABELS[FRESHNESS_UNKNOWN]
        health["scoring_reason"] = f"新鲜度检测异常：{exc}"

    try:
        metadata = read_model_metadata(path) or {}
        trained_at = _as_datetime(metadata.get("trained_at"))
        if trained_at is not None:
            health["trained_at"] = trained_at.isoformat()
        health["feature_cols_count"] = len(metadata.get("feature_cols") or [])
        health["version"] = metadata.get("version")
    except Exception:  # noqa: BLE001
        pass

    # 元数据 json 的 trained_at 单独展示 —— 它是「元数据刷新时间」，不是训练时间
    try:
        json_path = path.with_suffix(".json")
        if json_path.exists():
            import json

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            parsed = _as_datetime(payload.get("trained_at"))
            if parsed is not None:
                health["metadata_json_trained_at"] = parsed.isoformat()
    except Exception:  # noqa: BLE001
        pass

    try:
        scoring = detect_scoring_mode(path, enabled)
        health["scoring_mode"] = scoring["mode"]
        health["scoring_label"] = scoring["label"]
        health["scoring_reason"] = scoring["reason"] or health.get("scoring_reason", "")
        health["degradation_cause"] = scoring["degradation_cause"]
        health["sklearn_available"] = scoring["sklearn_available"]
    except Exception as exc:  # noqa: BLE001
        health["scoring_reason"] = f"打分方式检测异常：{exc}"

    return health


def _newest_alert_for(store, event_type: str, limit: int = 50) -> dict | None:
    """取最近一条该事件类型的告警，用于投递校验。"""
    try:
        for row in store.alerts(limit=limit):
            if row.get("event_type") == event_type:
                return row
    except Exception:  # noqa: BLE001
        return None
    return None


def _verify_delivered(store, event_type: str) -> bool | None:
    """校验最近一条告警是否真的投递出去。

    Returns:
        True  —— 已投递；False —— 明确未投递；None —— 没有对应告警行（被去重跳过）
    """
    row = _newest_alert_for(store, event_type)
    if row is None:
        return None
    return bool(row.get("delivered"))


def check_ml_model_health(store, alerts, tenant_id: str | None = None, **kwargs) -> dict:
    """检测并按需上报告警。**永不抛出**。

    上报走 ``alerts.send``（白名单 + 飞书卡片 + ``delivered`` 落库），随后**回读校验
    投递结果** —— 只写库不推送正是 P1-1 查出的「库里有、用户看不到」形态，这里不复演。
    """
    report = {"tenant_id": tenant_id or "", "health": {}, "alerts": []}
    try:
        health = ml_model_health(tenant_id=tenant_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        report["health"] = {"freshness": FRESHNESS_UNKNOWN, "scoring_mode": SCORING_ML}
        report["error"] = f"ML 健康检测失败：{exc}"
        return report
    report["health"] = health

    def _send(severity: str, event_type: str, message: str) -> None:
        entry = {"event_type": event_type, "severity": severity, "message": message}
        try:
            alerts.send(severity, event_type, message, incident_key=event_type)
            entry["delivered"] = _verify_delivered(store, event_type)
        except Exception as exc:  # noqa: BLE001
            entry["delivered"] = False
            entry["error"] = str(exc)
        report["alerts"].append(entry)

    if health["freshness"] in (FRESHNESS_WARN, FRESHNESS_STALE):
        severity = "ERROR" if health["freshness"] == FRESHNESS_STALE else "WARNING"
        _send(
            severity,
            EVENT_STALE,
            f"ML 模型已 {health['age_days']:.0f} 天未重训（{health['age_source']}），"
            f"状态 {health['freshness_label']}；模型 {health['model_path']}",
        )

    if health["scoring_mode"] == SCORING_RULE_FALLBACK:
        _send(
            "ERROR",
            EVENT_DEGRADED,
            f"ML 评分未生效，已回退规则打分：{health['scoring_reason']}；"
            f"原因 {health['degradation_cause'] or '未知'}",
        )
    elif health["scoring_mode"] == SCORING_DISABLED:
        _send(
            "WARNING",
            EVENT_DISABLED,
            f"ML 评分处于关闭状态（{health['scoring_reason']}）",
        )

    # 投递校验：本模块刚发出去的事件若明确未投递，必须升级成一条独立告警
    for entry in report["alerts"]:
        if entry.get("delivered") is False:
            _send(
                "ERROR",
                EVENT_NOT_DELIVERED,
                f"ML 告警 {entry['event_type']} 未能投递（delivered=0），"
                "请检查飞书 webhook 与告警白名单",
            )
            break

    return report


def freshness_summary_line(health: dict) -> str:
    """给页面/日志用的一行摘要。"""
    age = health.get("age_days")
    age_text = f"{age:.0f} 天" if isinstance(age, (int, float)) else "未知"
    return (
        f"{health.get('scoring_label', '未知')} · 模型年龄 {age_text}"
        f"（来源 {health.get('age_source', 'unknown')}）· {health.get('freshness_label', '未知')}"
    )
