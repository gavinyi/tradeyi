"""ML 模型晋级门禁链（P1-③，PRD data/qa/prd_p1_batch_20261107.md §3）。

四道门（缺一不可，顺序执行，前一门不过不进后一门）：
- G1 打分有效性：分数分布有区分度——不同取值数量 ≥ MIN_DISTINCT_VALUES 且
  分位数展开（P25/P50/P75 三点不全相等）。**一个「分数没区分度」的模型
  （历史事故：ml_score 全 0.5），AUC 再好看也不许晋级。**
- G2 样本外 AUC：只认时间切分的样本外 AUC（训练集/验证集 AUC 一律不算数），
  达标且相对现役模型有最小提升（MIN_AUC_LIFT，防噪声级提升）。
- G3 影子期：候选模型 shadow 模式并行至少 SHADOW_DAYS_MIN 天，分歧率与
  分歧部分事后表现达标。
- G4 人工审批：管理员审批记录（谁、何时、依据哪个报告版本）。**不得自动晋级。**

三条红线（PRD 3.3）：
1. 训练集/验证集 AUC 不作为晋级依据（evaluate 只接受 out_of_sample_auc）；
2. 不自动晋级（promote 必须带 approval 记录，缺记录即拒绝）；
3. 一键回滚：active_model 键写回旧版本号；rollback 后选品逐笔一致由
   「分数决定选品」的确定性保证（同分数输入同排序输出），verify_rollback
   提供逐笔对照判定。

状态全部走显式 runtime_config 键（`ml_promotion:{tenant}:*`），判定为纯函数，
store 交互集中在 PromotionsStore 薄壳（可注入测试替身）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---- 门禁阈值（PRD §3，不改任何现有阈值；本包全部为新键） ----
MIN_DISTINCT_VALUES = 8          # G1：分数至少有这么多个不同取值
MIN_AUC = 0.60                   # G2：样本外 AUC 最低线
MIN_AUC_LIFT = 0.02              # G2：相对现役模型的最小提升（防噪声）
MIN_SHADOW_DAYS = 3              # G3：影子期最短天数
MAX_DISAGREEMENT_RATIO = 0.50    # G3：影子期分歧率上限（高于此说明两模型几乎不相关，需人工看）
SHADOW_DAYS_MIN = MIN_SHADOW_DAYS

# ---- 状态键（显式键，不散落） ----
KEY_ACTIVE = "ml_promotion:{tenant}:active_model"        # 现役模型版本号
KEY_STATE = "ml_promotion:{tenant}:gate_state"           # 四道门状态 JSON
KEY_AUDIT = "ml_promotion:{tenant}:approval_log"         # 审批记录 JSON 列表


@dataclass
class GateResult:
    gate: str            # "G1"/"G2"/"G3"/"G4"
    passed: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"gate": self.gate, "passed": self.passed, "reason": self.reason}


@dataclass
class PromotionDecision:
    candidate_version: str
    active_version: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def eligible_for_approval(self) -> bool:
        """G1-G3 全过才允许进入 G4 人工审批（审批按钮未过门禁时禁用）。"""
        return all(gate.passed for gate in self.gates if gate.gate != "G4")

    @property
    def approved(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def first_failure(self) -> GateResult | None:
        for gate in self.gates:
            if not gate.passed:
                return gate
        return None

    def as_dict(self) -> dict:
        return {
            "candidate_version": self.candidate_version,
            "active_version": self.active_version,
            "gates": [gate.as_dict() for gate in self.gates],
            "eligible_for_approval": self.eligible_for_approval,
            "approved": self.approved,
        }


# ---- G1：打分有效性（专防 ml_score 全 0.5 复发） ----

def check_score_distinctiveness(scores: list[float]) -> GateResult:
    values = [float(v) for v in scores if v is not None]
    if len(values) < MIN_DISTINCT_VALUES:
        return GateResult("G1", False,
                          f"分数样本不足或无区分度：仅 {len(values)} 个取值（<{MIN_DISTINCT_VALUES}）")
    distinct = {round(v, 6) for v in values}
    if len(distinct) < MIN_DISTINCT_VALUES:
        return GateResult("G1", False,
                          f"分数无区分度：{len(distinct)} 个离散取值（<{MIN_DISTINCT_VALUES}）"
                          "——历史事故形态（ml_score 全 0.5）")
    ordered = sorted(values)
    def _pct(p: float) -> float:
        index = min(len(ordered) - 1, max(0, int(p * (len(ordered) - 1))))
        return ordered[index]
    p25, p50, p75 = _pct(0.25), _pct(0.50), _pct(0.75)
    if p25 == p50 == p75:
        return GateResult("G1", False,
                          f"分位数未展开：P25=P50=P75={p50}——分布退化，对照组失效")
    return GateResult("G1", True,
                      f"{len(distinct)} 个离散取值，P25={p25} P50={p50} P75={p75}")


# ---- G2：样本外 AUC（训练集 AUC 不算数——红线 1 的执行点） ----

def check_out_of_sample_auc(
    out_of_sample_auc: float, active_auc: float | None,
) -> GateResult:
    """只接受时间切分的样本外 AUC；参数名就是契约（传训练集 AUC 进来是调用方的错）。"""
    auc = float(out_of_sample_auc)
    if not 0.0 <= auc <= 1.0:
        return GateResult("G2", False, f"样本外 AUC 非法：{auc}")
    if auc < MIN_AUC:
        return GateResult("G2", False, f"样本外 AUC {auc:.3f} < {MIN_AUC:.2f}")
    if active_auc is not None:
        lift = auc - float(active_auc)
        if lift < MIN_AUC_LIFT:
            return GateResult("G2", False,
                              f"相对现役提升 {lift:.3f} < {MIN_AUC_LIFT:.2f}（噪声级提升不晋级）")
        return GateResult("G2", True, f"样本外 AUC {auc:.3f}，较现役 +{lift:.3f}")
    return GateResult("G2", True, f"样本外 AUC {auc:.3f}（无现役基线，跳过提升门槛）")


# ---- G3：影子期 ----

def check_shadow_period(
    shadow_started_at: str, *, now: str,
    disagreement_ratio: float, shadow_hit_rate: float | None = None,
    active_hit_rate: float | None = None,
) -> GateResult:
    started = datetime.fromisoformat(str(shadow_started_at).replace("Z", "+00:00"))
    moment = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    days = (moment - started).total_seconds() / 86400.0
    if days < SHADOW_DAYS_MIN:
        return GateResult("G3", False,
                          f"影子期 {days:.1f} 天 < {SHADOW_DAYS_MIN} 天——继续影子，不得晋级")
    ratio = float(disagreement_ratio)
    if not 0.0 <= ratio <= 1.0:
        return GateResult("G3", False, f"分歧率非法：{ratio}")
    if ratio > MAX_DISAGREEMENT_RATIO:
        return GateResult("G3", False,
                          f"分歧率 {ratio:.0%} > {MAX_DISAGREEMENT_RATIO:.0%}——两模型行为几乎不相关，需人工复核")
    if shadow_hit_rate is not None and active_hit_rate is not None:
        if float(shadow_hit_rate) < float(active_hit_rate):
            return GateResult("G3", False,
                              f"分歧部分事后表现差于现役（{shadow_hit_rate:.2f} < {active_hit_rate:.2f}）")
    return GateResult("G3", True, f"影子期 {days:.1f} 天，分歧率 {ratio:.0%}")


# ---- G4：人工审批（红线 2 的执行点） ----

def check_approval(approval: dict | None) -> GateResult:
    """审批记录四要素：approved_by / approved_at / report_ref / admin_verified。"""
    if not isinstance(approval, dict) or not approval:
        return GateResult("G4", False, "无审批记录——不得自动晋级（红线 2）")
    required = ("approved_by", "approved_at", "report_ref", "admin_verified")
    missing = [key for key in required if not approval.get(key)]
    if missing:
        return GateResult("G4", False, f"审批记录缺字段：{', '.join(missing)}")
    if not approval.get("admin_verified"):
        return GateResult("G4", False, "管理员校验未通过（admin_verified=false）")
    return GateResult("G4", True,
                      f"{approval.get('approved_by')} 于 {str(approval.get('approved_at'))[:19]} 批准（报告 {approval.get('report_ref')}）")


def evaluate(
    candidate_version: str,
    active_version: str,
    *,
    candidate_scores: list[float],
    out_of_sample_auc: float,
    active_out_of_sample_auc: float | None,
    shadow_started_at: str,
    now: str,
    disagreement_ratio: float,
    shadow_hit_rate: float | None = None,
    active_hit_rate: float | None = None,
    approval: dict | None = None,
) -> PromotionDecision:
    """四道门顺序评估：G1 不过不进 G2，以此类推（前一门的失败即终态）。"""
    decision = PromotionDecision(
        candidate_version=str(candidate_version), active_version=str(active_version),
    )
    g1 = check_score_distinctiveness(candidate_scores)
    decision.gates.append(g1)
    if not g1.passed:
        decision.gates.append(GateResult("G2", False, "G1 未过，未评估"))
        decision.gates.append(GateResult("G3", False, "G1 未过，未评估"))
        decision.gates.append(GateResult("G4", False, "G1 未过，未评估"))
        return decision
    g2 = check_out_of_sample_auc(out_of_sample_auc, active_out_of_sample_auc)
    decision.gates.append(g2)
    if not g2.passed:
        decision.gates.append(GateResult("G3", False, "G2 未过，未评估"))
        decision.gates.append(GateResult("G4", False, "G2 未过，未评估"))
        return decision
    g3 = check_shadow_period(
        shadow_started_at, now=now, disagreement_ratio=disagreement_ratio,
        shadow_hit_rate=shadow_hit_rate, active_hit_rate=active_hit_rate,
    )
    decision.gates.append(g3)
    if not g3.passed:
        decision.gates.append(GateResult("G4", False, "G3 未过，未评估"))
        return decision
    decision.gates.append(check_approval(approval))
    return decision


# ---- 状态存取薄壳（store 交互集中于此，可注入测试替身） ----

class PromotionsStore:
    def __init__(self, store, tenant_id: str):
        self._store = store
        self._tenant = str(tenant_id or "default")

    def _key(self, template: str) -> str:
        return template.format(tenant=self._tenant)

    def active_version(self) -> str:
        return str(self._store.get_setting(self._key(KEY_ACTIVE), "") or "")

    def gate_state(self) -> dict:
        raw = self._store.get_setting(self._key(KEY_STATE), "")
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def save_gate_state(self, decision: PromotionDecision) -> None:
        self._store.set_setting(
            self._key(KEY_STATE),
            json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True),
        )

    def approval_log(self) -> list[dict]:
        raw = self._store.get_setting(self._key(KEY_AUDIT), "")
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def record_approval(self, approval: dict) -> None:
        """G4 通过后追加审批日志（谁/何时/依据哪个报告，PRD 3.2-G4）。"""
        log = self.approval_log()
        log.append(dict(approval))
        self._store.set_setting(
            self._key(KEY_AUDIT),
            json.dumps(log, ensure_ascii=False, sort_keys=True),
        )

    def promote(self, candidate_version: str, approval: dict) -> str:
        """晋级：写现役版本号 + 审批日志。无审批记录直接拒绝（红线 2）。"""
        gate = check_approval(approval)
        if not gate.passed:
            raise PermissionError(f"晋级被拒绝：{gate.reason}")
        self.record_approval(approval)
        self._store.set_setting(self._key(KEY_ACTIVE), str(candidate_version))
        return self.active_version()

    def rollback(self, previous_version: str) -> str:
        """一键回滚：现役版本号写回旧值。旧模型文件由 ml_freshness/版本目录保留。"""
        if not str(previous_version).strip():
            raise ValueError("回滚目标版本号为空——拒绝盲回滚")
        self._store.set_setting(self._key(KEY_ACTIVE), str(previous_version))
        return self.active_version()


def verify_rollback(picks_before_active: list[str], picks_after_rollback: list[str]) -> dict:
    """红线 3 的对照判定：回滚后选品逐笔一致（不是「大概一致」）。"""
    if len(picks_before_active) != len(picks_after_rollback):
        return {"consistent": False,
                "reason": f"笔数不一致：{len(picks_before_active)} vs {len(picks_after_rollback)}"}
    mismatches = [
        {"index": i, "before": b, "after": a}
        for i, (b, a) in enumerate(zip(picks_before_active, picks_after_rollback))
        if str(b) != str(a)
    ]
    return {"consistent": not mismatches, "mismatches": mismatches[:20],
            "checked": len(picks_before_active)}
