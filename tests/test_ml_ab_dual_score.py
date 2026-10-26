"""A-2（v3.2 模拟盘 A/B）：同一批决策双算观测的守卫（2026-10-27）。

核心不变量：
1. rule 臂取 **ML 重排之前** 的原始质量分（源码顺序守卫）——顺序错了整份 A/B 无意义；
2. 两臂必须**同批同分母**（build_payload 里 code 集合/行数不一致直接抛错）；
3. `ml_rank` 是桶标签不能当名次：数值名次按 `ml_score` 自算，缺分不参与排名；
4. ML 缺分原因不许被吞（`ml_score_missing_reason` 进 payload）；
5. payload 必须带 `model_path_resolved` / `model_version`（防静默回退 BuiltinRuleModel 后无法自证）；
6. 观测写入失败**不得影响热路径**（只落 DEGRADED 事件、不抛）。
"""

from __future__ import annotations

import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from futu_quant.ml_ab_observe import (  # noqa: E402
    OBSERVATION_STRATEGY_ID,
    build_payload,
    model_metadata,
    observe_enabled,
    record_observation,
)
from futu_quant import volatility_roll  # noqa: E402  实现落在 src/futu_quant，不是 scripts/

TENANT = "futu_paper_sandbox"
NOW = datetime(2026, 10, 28, 14, 35, 12, tzinfo=timezone.utc)


class _FakeStore:
    def __init__(self, settings: dict | None = None, fail_snapshot: bool = False) -> None:
        self.settings = dict(settings or {})
        self.fail_snapshot = fail_snapshot
        self.snapshots: list[tuple] = []
        self.events: list[tuple] = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def decision_snapshot(self, strategy_id, symbol, payload):
        if self.fail_snapshot:
            raise RuntimeError("db locked")
        self.snapshots.append((strategy_id, symbol, payload))

    def operational_event(self, component, event_type, status, payload=None):
        self.events.append((component, event_type, status, payload))


def _rows_with_ml():
    rule_rows = [
        {"code": "US.AMD261030P520000", "rule_score": 62.5},
        {"code": "US.AMD261030P530000", "rule_score": 58.0},
        {"code": "US.AMD261030P540000", "rule_score": 55.0},
    ]
    ml_rows = [
        {
            "code": "US.AMD261030P520000", "ml_score": 0.612,
            "ml_adjusted_score": 68.1, "ml_score_missing_reason": None,
        },
        {
            "code": "US.AMD261030P530000", "ml_score": 0.740,
            "ml_adjusted_score": 70.2, "ml_score_missing_reason": None,
        },
        {
            "code": "US.AMD261030P540000", "ml_score": None,
            "ml_adjusted_score": 55.0,
            "ml_score_missing_reason": "缺少模型所需特征 ['moneyness']",
        },
    ]
    return rule_rows, ml_rows


def _payload(rule_rows, ml_rows, model_path=None):
    return build_payload(
        tenant_id=TENANT,
        env="SIMULATE",
        strategy_id="volatility_long_gamma",
        underlying="US.AMD",
        observed_at=NOW,
        rule_rows=rule_rows,
        ml_rows=ml_rows,
        model_path=model_path,
        rule_selected_code="US.AMD261030P520000",
        ml_selected_code="US.AMD261030P530000",
        regime={"risk_tier": "常规", "candidate_count": 5, "eligible_count": 3},
    )


# ---------------------------------------------------------------------------
# 1. 同批同分母 + 数值名次 + 缺分原因
# ---------------------------------------------------------------------------


def test_payload_keeps_two_arms_on_same_batch():
    rule_rows, ml_rows = _rows_with_ml()
    payload = _payload(rule_rows, ml_rows)

    assert payload["schema"] == "ml_ab_v1"
    assert payload["tenant_id"] == TENANT
    assert len(payload["candidates"]) == 3
    by_code = {item["code"]: item for item in payload["candidates"]}
    # rule 臂名次按质量分：62.5 > 58.0 > 55.0
    assert by_code["US.AMD261030P520000"]["rule_rank"] == 1
    assert by_code["US.AMD261030P540000"]["rule_rank"] == 3
    # ML 臂名次**按 ml_score 自算**（不是 HIGH/MEDIUM 桶标签）
    assert by_code["US.AMD261030P530000"]["ml_rank_num"] == 1
    assert by_code["US.AMD261030P520000"]["ml_rank_num"] == 2
    # 缺分：不参与排名、原因落库、覆盖率不掺水
    missing = by_code["US.AMD261030P540000"]
    assert missing["ml_score"] is None
    assert missing["ml_rank_num"] is None
    assert "moneyness" in missing["ml_score_missing_reason"]
    assert payload["ml"]["scored_count"] == 2
    assert payload["ml"]["coverage"] == pytest.approx(2 / 3)
    assert payload["ml"]["missing_reason_counts"], "缺失原因分布没落库"


def test_payload_rejects_mismatched_denominators():
    rule_rows, ml_rows = _rows_with_ml()
    with pytest.raises(ValueError, match="同分母"):
        _payload(rule_rows, ml_rows[:2])
    with pytest.raises(ValueError, match="同分母"):
        _payload(rule_rows, [{**row, "code": "US.OTHER"} for row in ml_rows])


def test_payload_carries_model_self_proof(tmp_path):
    """model_path_resolved / model_version 必须进 payload（防静默回退规则模型）。"""
    model = tmp_path / "candidate.pkl"
    with open(model, "wb") as handle:
        pickle.dump({"version": "v3.2", "feature_cols": ["a"] * 19}, handle)
    rule_rows, ml_rows = _rows_with_ml()
    payload = _payload(rule_rows, ml_rows, model_path=str(model))
    assert payload["ml"]["model_path_resolved"] == str(model)
    assert payload["ml"]["model_version"] == "v3.2"
    assert payload["ml"]["model_feature_count"] == 19


def test_model_metadata_missing_file_is_soft():
    info = model_metadata("does/not/exist.pkl")
    assert info["model_path_resolved"]
    assert info["model_version"] is None


# ---------------------------------------------------------------------------
# 2. 观测开关 fail-closed（只对显式开启的租户写）
# ---------------------------------------------------------------------------


def test_observe_disabled_by_default_and_fail_closed():
    assert observe_enabled(_FakeStore(), TENANT) is False
    assert observe_enabled(_FakeStore(), "") is False


def test_observe_only_for_flagged_tenant():
    store = _FakeStore({f"ml_ab_observe:{TENANT}": "true"})
    assert observe_enabled(store, TENANT) is True
    other = "futu_real_master"
    assert observe_enabled(store, other) is False, "实验观测不许写到其他/实盘租户"


# ---------------------------------------------------------------------------
# 3. 写入失败不得影响热路径
# ---------------------------------------------------------------------------


def test_record_failure_is_isolated_and_logged():
    store = _FakeStore(fail_snapshot=True)
    rule_rows, ml_rows = _rows_with_ml()
    payload = _payload(rule_rows, ml_rows)

    assert record_observation(store, payload) is False
    assert store.snapshots == []
    events = [event for event in store.events if event[1] == "ML_AB_OBSERVE_FAILED"]
    assert events and events[0][2] == "DEGRADED"


def test_record_success_writes_observation_row():
    store = _FakeStore()
    rule_rows, ml_rows = _rows_with_ml()
    assert record_observation(store, _payload(rule_rows, ml_rows)) is True
    strategy_id, symbol, written = store.snapshots[0]
    assert strategy_id == OBSERVATION_STRATEGY_ID
    assert symbol == "US.AMD"
    assert written["schema"] == "ml_ab_v1"


# ---------------------------------------------------------------------------
# 4. 源码顺序守卫：rule 臂必须在 ML 重排之前冻结；缺分原因必须写
# ---------------------------------------------------------------------------


def test_rule_arm_is_frozen_before_ml_ranking():
    source = Path(volatility_roll.__file__).read_text(encoding="utf-8")
    rule_pos = source.index("_rule_arm_rows = [")
    rank_pos = source.index("eligible = _apply_ml_ranking(")
    assert rule_pos < rank_pos, "rule 臂在 ML 重排之后才取 → 两臂不同源，A/B 无效"

    apply_source = source[source.index("def _apply_ml_ranking("):]
    assert "ml_score_missing_reason" in apply_source, "ML 缺分原因又被 except 吞掉了"
    assert "result[\"ml_score_missing_reason\"]" in apply_source
