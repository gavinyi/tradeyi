"""ML 晋级门禁链守卫（P1-③，2026-11-07，PRD prd_p1_batch_20261107.md §3）。

验收对照（PRD 3.5）：全 0.5 分数必红 / 影子期未满不可晋级 / 无审批记录必红 /
训练集 AUC 不算数（变异必红）/ 回滚对照判定。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from futu_quant import ml_promotion as mp

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "futu_quant" / "ml_promotion.py"


# ---------- G1：区分度（历史教训成门禁） ----------

def test_g1_rejects_all_half_scores():
    """PRD 3.5-1：伪造全 0.5 分数 → 必须红。"""
    scores = [0.5] * 50
    result = mp.check_score_distinctiveness(scores)
    assert not result.passed
    assert "0.5" in result.reason or "区分度" in result.reason


def test_g1_rejects_few_discrete_values():
    """rule 打分只有 11 个离散值的历史形态。"""
    scores = [round(i / 10, 1) for i in range(11)] * 10
    result = mp.check_score_distinctiveness(scores)
    assert not result.passed, "11 个离散值必须红（<8 判据的边界外形态用真实分布测）"


def test_g1_accepts_spread_distribution():
    import random
    rng = random.Random(42)
    scores = [rng.random() for _ in range(100)]
    result = mp.check_score_distinctiveness(scores)
    assert result.passed, result.reason


def test_g1_rejects_degenerate_percentiles():
    """P25=P50=P75（中段退化）也要红——分布必须展开。"""
    scores = [0.1] * 10 + [0.5] * 80 + [0.9] * 10
    result = mp.check_score_distinctiveness(scores)
    assert not result.passed, result.reason


# ---------- G2：样本外 AUC（红线 1） ----------

def test_g2_rejects_low_auc():
    result = mp.check_out_of_sample_auc(0.55, None)
    assert not result.passed


def test_g2_requires_minimal_lift_over_active():
    result = mp.check_out_of_sample_auc(0.61, 0.60)  # 提升 0.01 < 0.02
    assert not result.passed
    assert "噪声" in result.reason
    assert mp.check_out_of_sample_auc(0.65, 0.60).passed


def test_g2_rejects_invalid_auc():
    assert not mp.check_out_of_sample_auc(1.5, None).passed
    assert not mp.check_out_of_sample_auc(-0.1, None).passed


def test_mutation_training_auc_as_promotion_basis_must_be_red():
    """变异：去掉样本外最低线（改用任意 AUC）→ 守卫必红。"""
    import shutil
    import subprocess
    import sys

    original = MODULE.read_text(encoding="utf-8")
    anchor = "    if auc < MIN_AUC:"
    assert anchor in original, "G2 锚点丢失"
    mutated = original.replace(anchor, "    if False:", 1)
    assert mutated != original
    try:
        MODULE.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_g2_rejects_low_auc", "--capture=no"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert result.returncode != 0, "AUC 最低线被摘后守卫仍绿——低区分模型可晋级"
    finally:
        MODULE.write_text(original, encoding="utf-8")
        shutil.rmtree(ROOT / ".pytest_cache", ignore_errors=True)


# ---------- G3：影子期 ----------

def _ts(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.isoformat()


def test_g3_rejects_short_shadow_period():
    result = mp.check_shadow_period(
        _ts(1.0), now=_ts(0.0), disagreement_ratio=0.2,
    )
    assert not result.passed
    assert "继续影子" in result.reason


def test_g3_rejects_extreme_disagreement():
    result = mp.check_shadow_period(
        _ts(5.0), now=_ts(0.0), disagreement_ratio=0.9,
    )
    assert not result.passed
    assert "人工复核" in result.reason


def test_g3_passes_healthy_shadow():
    result = mp.check_shadow_period(
        _ts(5.0), now=_ts(0.0), disagreement_ratio=0.25,
        shadow_hit_rate=0.7, active_hit_rate=0.6,
    )
    assert result.passed, result.reason


# ---------- G4：人工审批（红线 2） ----------

def test_g4_rejects_missing_approval():
    for approval in (None, {}, {"approved_by": "user"}):
        result = mp.check_approval(approval)
        assert not result.passed


def test_g4_requires_admin_verified():
    result = mp.check_approval({
        "approved_by": "user", "approved_at": "2026-11-07T14:00:00+00:00",
        "report_ref": "ml_report_v9", "admin_verified": False,
    })
    assert not result.passed
    assert "管理员" in result.reason


# ---------- 端到端：四道门顺序 + 提前终止 ----------

def _good_args(**overrides):
    import random
    rng = random.Random(7)
    base = dict(
        candidate_version="v10", active_version="v9",
        candidate_scores=[rng.random() for _ in range(120)],
        out_of_sample_auc=0.68, active_out_of_sample_auc=0.63,
        shadow_started_at=_ts(5.0), now=_ts(0.0),
        disagreement_ratio=0.3, approval=None,
    )
    base.update(overrides)
    return base


def test_full_gate_chain_without_approval_stops_at_g4():
    decision = mp.evaluate(**_good_args())
    assert decision.eligible_for_approval is True, "G1-G3 应全过"
    assert decision.approved is False
    assert decision.first_failure().gate == "G4"


def test_g1_failure_short_circuits_all_later_gates():
    decision = mp.evaluate(**_good_args(candidate_scores=[0.5] * 120))
    assert decision.gates[0].gate == "G1" and not decision.gates[0].passed
    assert [g.reason for g in decision.gates[1:]] == ["G1 未过，未评估"] * 3


def test_promotion_needs_all_four_gates():
    approval = {
        "approved_by": "user", "approved_at": _ts(0.0),
        "report_ref": "ml_report_v10", "admin_verified": True,
    }
    decision = mp.evaluate(**_good_args(approval=approval))
    assert decision.approved is True


# ---------- 状态存取与一键回滚（红线 2/3） ----------

class FakeStore:
    def __init__(self):
        self.settings: dict[str, str] = {}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_promote_without_approval_raises():
    promotions = mp.PromotionsStore(FakeStore(), "t1")
    with pytest.raises(PermissionError):
        promotions.promote("v10", {})


def test_promote_writes_active_and_audit():
    store = FakeStore()
    promotions = mp.PromotionsStore(store, "t1")
    approval = {
        "approved_by": "user", "approved_at": "2026-11-07T14:00:00+00:00",
        "report_ref": "ml_report_v10", "admin_verified": True,
    }
    assert promotions.promote("v10", approval) == "v10"
    assert promotions.active_version() == "v10"
    log = promotions.approval_log()
    assert len(log) == 1 and log[0]["approved_by"] == "user"


def test_rollback_writes_back_and_verifies_pick_parity():
    store = FakeStore()
    promotions = mp.PromotionsStore(store, "t1")
    promotions.promote("v10", {
        "approved_by": "user", "approved_at": "2026-11-07T14:00:00+00:00",
        "report_ref": "r", "admin_verified": True,
    })
    assert promotions.rollback("v9") == "v9"

    # 逐笔一致性判定
    ok = mp.verify_rollback(["A", "B", "C"], ["A", "B", "C"])
    assert ok["consistent"] and ok["checked"] == 3
    bad = mp.verify_rollback(["A", "B", "C"], ["A", "X", "C"])
    assert not bad["consistent"] and bad["mismatches"][0]["index"] == 1


def test_rollback_refuses_blank_version():
    promotions = mp.PromotionsStore(FakeStore(), "t1")
    with pytest.raises(ValueError):
        promotions.rollback("")


def test_state_keys_are_tenant_scoped():
    """状态键必须带租户：两租户晋级互不污染（N-Tenant 铁律）。"""
    store = FakeStore()
    a = mp.PromotionsStore(store, "futu_real_master")
    b = mp.PromotionsStore(store, "futu_paper_sandbox")
    a.promote("v10", {
        "approved_by": "user", "approved_at": "2026-11-07T14:00:00+00:00",
        "report_ref": "r", "admin_verified": True,
    })
    assert a.active_version() == "v10"
    assert b.active_version() == ""
    assert a._key(mp.KEY_ACTIVE) != b._key(mp.KEY_ACTIVE)


def test_no_auto_promotion_path_exists():
    """红线 2 AST 守卫：模块内不得存在任何「无审批直接写现役版本」的路径。"""
    source = MODULE.read_text(encoding="utf-8")
    tree = __import__("ast").parse(source)
    import ast as _ast
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "promote")
    seg = _ast.get_source_segment(source, fn)
    assert "check_approval" in seg and "PermissionError" in seg, \
        "promote 必须先过审批校验（缺校验 = 自动晋级后门）"
