"""A-2（v3.2 模拟盘 A/B）：**同一批决策双算观测**（2026-10-27）。

目的：rule 臂与 v3.2 臂必须在**同一批决策、同一快照、同一分母**上比较；时间切片不算 A/B。
做法：roll 评估时把「ML 重排前」的质量分名次（rule 臂）与 ML 分数名次（v3.2 臂）一起写进
`decision_snapshots`（`strategy_id='volatility_long_ab'`，零迁移）。

六条硬条件（Chief 2026-10-27，逐条落在这里）：
1. rule 臂取**重排前**的 `volatility_quality_score`——返回值由调用方在 ML 重排**之前**冻结；
2. `ml_rank`（桶标签 HIGH/MEDIUM/LOW/NA）不参与，按 `ml_score` 自算数值名次 `ml_rank_num`；
3. ML 缺分原因**不许被吞**：由 `_apply_ml_ranking` 写 `ml_score_missing_reason`；
4. 观测写入**不得影响热路径**：`record_observation()` 内部 try/except，失败只落 DEGRADED 事件；
5. 前置断言：payload 带 `ml.coverage` / `missing_reason_counts` + `model_path_resolved` /
   `model_version`（防 `ml_scorer` 静默回退 BuiltinRuleModel 后无法自证）；
6. 分析门槛在只读脚本 `scripts/ml_ab_analyze.py`（≥30 交易日且 ≥200 批、block bootstrap、
   前向 7 日标签、缺价记 label_missing）。

只写观测：不改排序结果、不进 `submit_limit`、不改成交行为。
"""

from __future__ import annotations

import pickle
from datetime import datetime
from functools import lru_cache
from pathlib import Path

OBSERVE_BASE_KEY = "ml_ab_observe"
OBSERVATION_STRATEGY_ID = "volatility_long_ab"
SCHEMA_VERSION = "ml_ab_v1"


def observe_enabled(store, tenant_id: str) -> bool:
    """实验观测开关：`ml_ab_observe:{tenant}` = true。无租户/未注册 → 关闭（fail-closed）。"""
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return False
    from .tenant import TenantConfigKeyError, resolve_tenant_config_key

    try:
        key = resolve_tenant_config_key(tenant, OBSERVE_BASE_KEY)
    except TenantConfigKeyError:
        return False
    return str(store.get_setting(key, "") or "").strip().lower() == "true"


@lru_cache(maxsize=8)
def _model_info_cached(path: str, mtime_ns: int) -> tuple[str | None, int | None]:
    try:
        with open(path, "rb") as handle:
            data = pickle.load(handle)
    except Exception:  # noqa: BLE001 - 读不到元数据只影响自证字段，不影响观测写入
        return None, None
    if not isinstance(data, dict):
        return None, None
    version = data.get("version")
    features = data.get("feature_cols") or []
    return (
        str(version) if version is not None else None,
        len(features) if features else None,
    )


def model_metadata(model_path: str | None) -> dict:
    """读模型文件内的 version / feature_count（mtime 变化才重读）。

    **为什么要写进 payload**：`load_ml_model()` 在特征签名不符时会**静默回退**
    BuiltinRuleModel（`ml_scorer.py:806-819`），日志未必被翻到；只有把「这批用的是哪个文件、
    多少特征」写进数据，事后才能自证这批到底是不是 v3.2 算的。
    """
    path = str(model_path or "").strip()
    if not path:
        return {"model_path_resolved": None, "model_version": None, "model_feature_count": None}
    resolved = Path(path)
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        return {
            "model_path_resolved": path,
            "model_version": None,
            "model_feature_count": None,
        }
    version, feature_count = _model_info_cached(str(resolved), mtime_ns)
    return {
        "model_path_resolved": str(resolved),
        "model_version": version,
        "model_feature_count": feature_count,
    }


def _float_or_none(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric:  # NaN
        return None
    return numeric


def _rank_by_score(rows: list[dict], score_key: str) -> dict[str, int | None]:
    """按分数降序、code 升序（确定性平局键）算数值名次；缺分（None/NaN）不参与排名。

    不用 `ml_rank` 桶标签：HIGH/MEDIUM/LOW/NA 无法区分同桶内次序，做 IC/五档会退化。
    """
    scored = []
    for row in rows:
        numeric = _float_or_none(row.get(score_key))
        if numeric is None:
            continue
        scored.append((numeric, str(row.get("code") or "")))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ranks: dict[str, int | None] = {
        str(row.get("code") or ""): None for row in rows
    }
    for position, (_value, code) in enumerate(scored, start=1):
        ranks[code] = position
    return ranks


def build_payload(
    *,
    tenant_id: str,
    env: str,
    strategy_id: str,
    underlying: str,
    observed_at: datetime,
    rule_rows: list[dict],
    ml_rows: list[dict],
    model_path: str | None,
    rule_selected_code: str | None,
    ml_selected_code: str | None,
    regime: dict | None = None,
) -> dict:
    """把同一批候选的两臂分数/名次组装成快照 payload（纯函数，便于守卫测试）。

    `rule_rows` 与 `ml_rows` 必须是**同一批候选**（同一 `eligible` 帧，行数与 code 集合相等）：
    这里会断言并抛 `ValueError`——静默接受不同分母会让整份 A/B 结论作废。
    """
    rule_codes = {str(row.get("code") or "") for row in rule_rows}
    ml_codes = {str(row.get("code") or "") for row in ml_rows}
    if rule_codes != ml_codes or len(rule_rows) != len(ml_rows):
        raise ValueError(
            "两臂分母不一致：rule 与 ML 必须覆盖同一批候选（同批同分母是 A/B 有效性的前提）"
        )
    rule_ranks = _rank_by_score(rule_rows, "rule_score")
    ml_ranks = _rank_by_score(ml_rows, "ml_score")
    ml_by_code = {str(row.get("code") or ""): row for row in ml_rows}

    candidates: list[dict] = []
    scored = 0
    missing_reasons: dict[str, int] = {}
    for row in rule_rows:
        code = str(row.get("code") or "")
        ml_row = ml_by_code.get(code) or {}
        ml_score = _float_or_none(ml_row.get("ml_score"))
        reason = ml_row.get("ml_score_missing_reason")
        if ml_score is not None:
            scored += 1
        if reason:
            key = str(reason)[:120]
            missing_reasons[key] = missing_reasons.get(key, 0) + 1
        candidates.append(
            {
                "code": code,
                "rule_score": _float_or_none(row.get("rule_score")),
                "rule_rank": rule_ranks.get(code),
                "ml_score": ml_score,
                "ml_rank_num": ml_ranks.get(code),
                "ml_adjusted_score": _float_or_none(ml_row.get("ml_adjusted_score")),
                "ml_score_missing_reason": reason,
            }
        )
    total = len(candidates)
    metadata = model_metadata(model_path)
    return {
        "schema": SCHEMA_VERSION,
        "batch_id": (
            f"{tenant_id}:{str(underlying or '').upper()}:"
            f"{observed_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        ),
        "tenant_id": tenant_id,
        "env": env,
        "strategy_id": strategy_id,
        "underlying": str(underlying or "").upper(),
        "observed_at": observed_at.isoformat(),
        "regime": dict(regime or {}),
        "ml": {
            "enabled": True,
            **metadata,
            "coverage": (scored / total) if total else 0.0,
            "scored_count": scored,
            "candidate_count": total,
            "missing_reason_counts": missing_reasons,
        },
        "rule_selected_code": rule_selected_code,
        "ml_selected_code": ml_selected_code,
        "candidates": candidates,
    }


def record_observation(store, payload: dict) -> bool:
    """写观测快照；**失败绝不影响热路径**（条件 4）。

    观测失败 → 一条 DEGRADED 事件后继续，不抛回调用方：观测数据缺失是可接受的，
    因为观测写失败而中断 roll/成交是不可接受的。
    """
    try:
        store.decision_snapshot(
            OBSERVATION_STRATEGY_ID, str(payload.get("underlying") or ""), payload,
        )
    except Exception as exc:  # noqa: BLE001 - 观测写入失败不得打断决策/成交
        try:
            store.operational_event(
                "volatility_roll", "ML_AB_OBSERVE_FAILED", "DEGRADED",
                {"error": str(exc)[:300], "batch_id": payload.get("batch_id")},
            )
        except Exception:  # noqa: BLE001 - 连事件都写不进去时只能放弃，不能让主流程崩
            # silent-ok: 观测链路自身的最后一级兜底，主流程（决策/成交）必须继续
            pass
        return False
    return True
