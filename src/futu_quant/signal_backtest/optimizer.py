"""历史信号回测引擎 · 参数寻优层（只读，纯函数）。

PRD 包2（data/qa/prd_p1_batch_20261107.md §2）三条硬约束的实现落点：
1. **参数空间事前声明并落盘**：`declare_parameter_space` 生成 N 组组合并冻结
   （`frozen=True`）；寻优只能消费声明过的空间，禁止事后追加/裁剪——那是挑赢家。
2. **样本内寻优 → 样本外验证**：`split_window` 按时间把窗口切成前 70% 训练段 /
   后 30% 验证段；`evaluate_grid` 只在训练段选 Top，`verify_out_of_sample` 在
   验证段重跑同一组参数。缺样本外列的报告 `is_deliverable=False`（PRD 2.3）。
3. **第一输出是过拟合风险**：`decay_verdict` 按衰减率分档（<30% 可关注 /
   30–60% 存疑 / >60% 判为过拟合不建议采用）；`random_pick_baseline` 给出
   「纯随机挑一组」的期望表现，与「N 组里挑最好」并列——让挑赢家效应自曝。

**纯函数**：不触 store、不写任何配置；组合评估器由调用方注入（复用
replay/matcher/ledger 既有纯函数）。确定性：同输入同输出，无随机源。
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime

#: 样本外衰减分档（PRD 2.2 表格，唯一判定点）。
DECAY_OK = 0.30
DECAY_SUSPECT = 0.60

#: 最小样本笔数（PRD 2.3：样本 <30 的组不得进入推荐位）。
MIN_SAMPLE_TRADES = 30


@dataclass(frozen=True)
class ParameterSpace:
    """事前声明的参数空间（冻结：声明后不可增删取值，杜绝事后裁剪）。"""

    name: str
    axes: dict[str, tuple]
    combinations: tuple[dict[str, object], ...]
    declared_at: str

    @property
    def size(self) -> int:
        return len(self.combinations)


def declare_parameter_space(
    axes: dict[str, list | tuple], *, declared_at: str, name: str = "grid",
) -> ParameterSpace:
    """把参数轴的笛卡尔积一次性展开并冻结（PRD 2.2 第 1 条）。

    declared_at 必须由调用方显式传入（来自数据行/CLI 参数，禁止 datetime.now）
    ——确定性约束与 replay 同源。
    """
    if not axes:
        raise ValueError("参数空间不能为空：至少声明一个参数轴")
    clean = {}
    for axis, values in axes.items():
        values = tuple(values)
        if not values:
            raise ValueError(f"参数轴 {axis} 取值集为空")
        clean[str(axis)] = values
    keys = sorted(clean)
    combos = tuple(
        dict(zip(keys, values))
        for values in itertools.product(*(clean[key] for key in keys))
    )
    return ParameterSpace(
        name=str(name), axes=clean, combinations=combos, declared_at=str(declared_at),
    )


def space_declaration_json(space: ParameterSpace) -> str:
    """参数空间声明落盘形态（PRD 2.2：事前声明并落盘，禁止事后变更）。"""
    return json.dumps({
        "name": space.name,
        "declared_at": space.declared_at,
        "axes": {k: list(v) for k, v in space.axes.items()},
        "size": space.size,
        "combinations": [dict(c) for c in space.combinations],
    }, ensure_ascii=False, sort_keys=True, indent=2)


def split_window(
    window_start: str, window_end: str, *, train_ratio: float = 0.7,
) -> tuple[str, str, str, str]:
    """按时间把窗口切成（训练段，验证段）两个子窗口（PRD 2.2 第 2 条）。"""
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio 必须在 (0,1)，得到 {train_ratio}")
    start = datetime.fromisoformat(window_start)
    end = datetime.fromisoformat(window_end)
    if end <= start:
        raise ValueError(f"窗口非法：{window_start} -> {window_end}")
    split_point = start + (end - start) * train_ratio
    iso = "%Y-%m-%dT%H:%M:%S+00:00"
    return (
        window_start, split_point.strftime(iso),
        split_point.strftime(iso), window_end,
    )


@dataclass(frozen=True)
class ArmResult:
    """一组参数在一段窗口上的评估结果（由注入的评估器产出）。"""

    params: dict[str, object]
    trades: int
    pnl: float
    extra: dict = field(default_factory=dict)


@dataclass
class OptimizerReport:
    """寻优报告（PRD 2.2 第 3 条：第一屏必须是衰减结论）。"""

    space: ParameterSpace
    train_window: tuple[str, str]
    verify_window: tuple[str, str]
    train_results: list[ArmResult] = field(default_factory=list)
    out_of_sample: list[ArmResult] = field(default_factory=list)
    random_baseline_pnl: float | None = None

    # ---- 第一屏结论 ----

    @property
    def top_train(self) -> ArmResult | None:
        """样本内最优组（pnl 降序取第一；同分取组合字典序，保证确定性）。"""
        if not self.train_results:
            return None
        return sorted(
            self.train_results,
            key=lambda item: (-item.pnl, json.dumps(item.params, sort_keys=True)),
        )[0]

    @property
    def top_out_of_sample(self) -> ArmResult | None:
        top_params = self.top_train.params if self.top_train else None
        if top_params is None:
            return None
        for arm in self.out_of_sample:
            if arm.params == top_params:
                return arm
        return None

    @property
    def decay_ratio(self) -> float | None:
        """样本外相对样本内的表现衰减率（pnl 基准，两段都非空才有值）。"""
        top, oos = self.top_train, self.top_out_of_sample
        if top is None or oos is None or top.pnl <= 0:
            return None
        return max(0.0, 1.0 - oos.pnl / top.pnl)

    def decay_verdict(self) -> str:
        """PRD 2.2 衰减分档：<30% 可关注 / 30–60% 存疑 / >60% 判过拟合。"""
        decay = self.decay_ratio
        if decay is None:
            return "样本不足或样本内 pnl 非正，无法计算衰减——不作推荐"
        if decay < DECAY_OK:
            return f"衰减 {decay:.0%} <30%：可关注（仍需人工审批，不构成准入结论）"
        if decay <= DECAY_SUSPECT:
            return f"衰减 {decay:.0%} 处于 30–60%：存疑，需更长样本"
        return f"衰减 {decay:.0%} >60%：判为过拟合，不建议采用"

    @property
    def is_deliverable(self) -> bool:
        """PRD 2.3：缺样本外列的寻优报告视为未交付。"""
        return bool(self.out_of_sample) and self.top_train is not None

    def recommendation(self) -> dict:
        """推荐位（PRD 2.3：样本 <30 的组不得进入推荐位）。"""
        top, oos = self.top_train, self.top_out_of_sample
        if top is None or not self.is_deliverable:
            return {"eligible": False, "reason": "缺样本外验证或无可用组合"}
        if top.trades < MIN_SAMPLE_TRADES or (oos and oos.trades < MIN_SAMPLE_TRADES):
            return {"eligible": False, "reason": "样本不足（<30 笔），不得进入推荐位"}
        return {
            "eligible": True,
            "params": dict(top.params),
            "train_pnl": top.pnl, "train_trades": top.trades,
            "oos_pnl": oos.pnl if oos else None,
            "oos_trades": oos.trades if oos else None,
            "decay_ratio": self.decay_ratio,
            "verdict": self.decay_verdict(),
            "random_baseline_pnl": self.random_baseline_pnl,
            "note": "回测结论不得用于放宽风控/质量门槛（v1 红线）；改参数必须人工审批",
        }


def random_pick_baseline(
    space: ParameterSpace, train_results: list[ArmResult],
) -> float:
    """多重比较基准（PRD 2.2 第 4 条）：「纯随机挑一组」的期望 pnl。

    即全组合 pnl 均值——与「N 组里挑最好」（top_train）并列展示，
    让用户看见挑赢家效应本身就会把数字抬上去。
    """
    if not train_results:
        return 0.0
    return sum(arm.pnl for arm in train_results) / len(train_results)


def evaluate_grid(
    space: ParameterSpace, window_start: str, window_end: str, evaluate_arm,
) -> list[ArmResult]:
    """对声明空间逐组评估（评估器注入：复用 replay/matcher/ledger 纯函数）。

    evaluate_arm(params, window_start, window_end) -> ArmResult。
    禁止评估声明空间之外的参数（PRD 2.2 第 1 条的执行点）。
    """
    allowed = {json.dumps(c, sort_keys=True) for c in space.combinations}
    results: list[ArmResult] = []
    for combo in space.combinations:
        key = json.dumps(combo, sort_keys=True)
        if key not in allowed:  # 双保险：冻结空间外的组合直接拒绝
            raise ValueError(f"组合不在声明空间内：{key}")
        arm = evaluate_arm(dict(combo), window_start, window_end)
        if not isinstance(arm, ArmResult):
            raise TypeError("evaluate_arm 必须返回 ArmResult")
        if dict(arm.params) != dict(combo):
            raise ValueError("evaluate_arm 返回的参数与声明组合不一致（串组）")
        results.append(arm)
    return results
