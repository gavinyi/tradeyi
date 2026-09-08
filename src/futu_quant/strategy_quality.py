from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _optional_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _grade(score: float) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "合格"
    if score >= 55:
        return "待改进"
    return "高风险"


def _clip_score(score: float) -> int:
    return int(max(0, min(100, round(score))))


def build_option_lifecycle_frame(
    option_pnl_breakdown: pd.DataFrame,
    option_quality: pd.DataFrame,
) -> pd.DataFrame:
    """Create a readable option trade lifecycle view from review outputs."""
    if option_pnl_breakdown.empty and option_quality.empty:
        return pd.DataFrame()

    pnl = option_pnl_breakdown.copy()
    quality = option_quality.copy()
    if not quality.empty and "代码" in quality.columns:
        quality = quality.drop_duplicates("代码", keep="last").set_index("代码")

    rows: list[dict[str, object]] = []
    for _, item in pnl.iterrows():
        symbol = str(item.get("代码") or "")
        quality_row = quality.loc[symbol] if not quality.empty and symbol in quality.index else {}
        remaining = _number(item.get("剩余持仓"))
        closed = _number(item.get("平仓数量"))
        net_pnl = _number(item.get("净盈亏"))
        gross_pnl = _number(item.get("毛盈亏"))
        mfe = _optional_number(getattr(quality_row, "get", lambda *_: None)("最大浮盈MFE"))
        capture = _optional_number(getattr(quality_row, "get", lambda *_: None)("浮盈捕获率"))
        current_pnl = _optional_number(getattr(quality_row, "get", lambda *_: None)("当前盈亏"))
        current_pnl_amount = _optional_number(getattr(quality_row, "get", lambda *_: None)("当前盈亏金额"))
        status = "持仓中" if remaining > 0 else "已结束"
        if remaining > 0 and closed > 0:
            status = "部分平仓"
        rows.append({
            "代码": symbol,
            "生命周期状态": status,
            "结果": item.get("结果"),
            "买入数量": item.get("买入数量"),
            "平仓数量": closed,
            "剩余持仓": remaining,
            "买入均价": item.get("买入均价"),
            "卖出均价": item.get("卖出均价"),
            "毛盈亏": gross_pnl,
            "净盈亏": net_pnl,
            "当前盈亏比例": current_pnl,
            "当前盈亏金额": current_pnl_amount,
            "最大浮盈MFE": mfe,
            "最大不利MAE": getattr(quality_row, "get", lambda *_: None)("最大不利MAE") or "暂无采集",
            "浮盈捕获率": capture,
            "首次成交": item.get("首次成交"),
            "最后成交": item.get("最后成交"),
            "最新动作": getattr(quality_row, "get", lambda *_: None)("最新动作"),
            "最新原因": getattr(quality_row, "get", lambda *_: None)("最新原因"),
        })
    if pnl.empty and not quality.empty:
        for symbol, item in quality.iterrows():
            rows.append({
                "代码": symbol,
                "生命周期状态": "观察/未成交",
                "结果": "未形成完整成交",
                "当前盈亏比例": item.get("当前盈亏"),
                "当前盈亏金额": item.get("当前盈亏金额"),
                "最大浮盈MFE": item.get("最大浮盈MFE"),
                "最大不利MAE": item.get("最大不利MAE") or "暂无采集",
                "浮盈捕获率": item.get("浮盈捕获率"),
                "最新动作": item.get("最新动作"),
                "最新原因": item.get("最新原因"),
            })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    sort_columns = ["生命周期状态"]
    ascending = [True]
    if "净盈亏" in frame.columns:
        sort_columns.append("净盈亏")
        ascending.append(True)
    return frame.sort_values(sort_columns, ascending=ascending, na_position="last").reset_index(drop=True)


def build_trade_quality_scores(
    option_lifecycle: pd.DataFrame,
    orders: pd.DataFrame,
    blockers: pd.DataFrame,
    runtime_health: pd.DataFrame,
) -> pd.DataFrame:
    """Score execution quality by practical trading dimensions."""
    rows: list[dict[str, object]] = []
    if not option_lifecycle.empty:
        for _, row in option_lifecycle.iterrows():
            symbol = str(row.get("代码") or "")
            net_pnl = _number(row.get("净盈亏"))
            capture = _optional_number(row.get("浮盈捕获率"))
            current_pnl = _optional_number(row.get("当前盈亏比例"))
            remaining = _number(row.get("剩余持仓"))
            status = str(row.get("生命周期状态") or "")

            entry_score = 72
            if net_pnl > 0:
                entry_score += 8
            elif net_pnl < -300:
                entry_score -= 22
            elif net_pnl < 0:
                entry_score -= 10

            exit_score = 75
            if capture is not None:
                if capture < 0.35:
                    exit_score -= 30
                elif capture < 0.55:
                    exit_score -= 16
                elif capture >= 0.75:
                    exit_score += 10
            if remaining > 0 and current_pnl is not None and current_pnl <= -0.25:
                exit_score -= 25
            if "持仓" in status and net_pnl < -300:
                exit_score -= 15

            execution_score = 80
            matched_orders = orders[orders["代码"].astype(str).eq(symbol)] if not orders.empty and "代码" in orders else pd.DataFrame()
            if not matched_orders.empty:
                filled = pd.to_numeric(matched_orders.get("已成交", 0), errors="coerce").fillna(0)
                planned = pd.to_numeric(matched_orders.get("数量", 0), errors="coerce").fillna(0)
                if planned.sum() > 0 and filled.sum() <= 0:
                    execution_score -= 35
                elif planned.sum() > filled.sum():
                    execution_score -= 12

            risk_score = 82
            if net_pnl <= -500:
                risk_score -= 35
            elif net_pnl <= -300:
                risk_score -= 20
            if remaining > 0 and current_pnl is not None and current_pnl <= -0.25:
                risk_score -= 30

            total = _clip_score(entry_score * 0.25 + exit_score * 0.30 + execution_score * 0.20 + risk_score * 0.25)
            weak = min(
                [
                    ("入场质量", _clip_score(entry_score)),
                    ("退出质量", _clip_score(exit_score)),
                    ("执行质量", _clip_score(execution_score)),
                    ("风控质量", _clip_score(risk_score)),
                ],
                key=lambda item: item[1],
            )
            rows.append({
                "对象": symbol,
                "类型": "波动率期权",
                "总评分": total,
                "等级": _grade(total),
                "入场质量": _clip_score(entry_score),
                "退出质量": _clip_score(exit_score),
                "执行质量": _clip_score(execution_score),
                "风控质量": _clip_score(risk_score),
                "主要短板": weak[0],
                "建议": _quality_suggestion(weak[0], net_pnl, capture, remaining),
            })

    if not blockers.empty:
        for _, row in blockers.head(5).iterrows():
            count = int(_number(row.get("次数")))
            score = _clip_score(82 - min(45, count * 6))
            rows.append({
                "对象": row.get("未下单原因"),
                "类型": "阻塞来源",
                "总评分": score,
                "等级": _grade(score),
                "入场质量": score,
                "退出质量": None,
                "执行质量": None,
                "风控质量": None,
                "主要短板": "信号转订单",
                "建议": f"优先检查该阻塞原因的阈值和数据源，出现 {count} 次。",
            })

    if not runtime_health.empty:
        for _, row in runtime_health.iterrows():
            degraded = int(_number(row.get("退化/超时")))
            errors = int(_number(row.get("异常次数")))
            if degraded <= 0 and errors <= 0:
                continue
            score = _clip_score(85 - degraded * 8 - errors * 15)
            rows.append({
                "对象": row.get("任务"),
                "类型": "运行健康",
                "总评分": score,
                "等级": _grade(score),
                "入场质量": None,
                "退出质量": None,
                "执行质量": score,
                "风控质量": score,
                "主要短板": "系统稳定性",
                "建议": row.get("问题摘要") or "检查服务日志和数据源连接。",
            })

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values(["总评分", "对象"], ascending=[True, True]).reset_index(drop=True)


def _quality_suggestion(weak: str, net_pnl: float, capture: float | None, remaining: float) -> str:
    if weak == "退出质量":
        if capture is not None and capture < 0.55:
            return "重点复核移动止盈、阶梯锁盈和可成交价，避免冲高回落后才退出。"
        if remaining > 0:
            return "仍有持仓，确认是否需要触发持仓管理或手动接管。"
        return "检查退出原因是否与峰值盈利和最短持有时间一致。"
    if weak == "风控质量":
        if net_pnl <= -300:
            return "核对硬止损、临近到期退出、未接管持仓和富途实际可卖数量。"
        return "检查该标的预算、连续亏损降级和冷却规则。"
    if weak == "执行质量":
        return "检查盘口价差、追价次数、订单状态和成交回报是否及时入账。"
    return "复核入场质量分、合约成本、方向确认和模型预期收益。"


def build_action_items(
    quality_scores: pd.DataFrame,
    option_lifecycle: pd.DataFrame,
    runtime_health: pd.DataFrame,
    blockers: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if not option_lifecycle.empty:
        for _, row in option_lifecycle.iterrows():
            remaining = _number(row.get("剩余持仓"))
            net_pnl = _number(row.get("净盈亏"))
            current_pnl = _optional_number(row.get("当前盈亏比例"))
            capture = _optional_number(row.get("浮盈捕获率"))
            symbol = row.get("代码")
            if remaining > 0 and current_pnl is not None and current_pnl <= -0.25:
                rows.append({
                    "优先级": "高",
                    "对象": symbol,
                    "问题": "未平持仓亏损超过 25%",
                    "处理入口": "账户总览 / 波动率策略总览 / 订单管理",
                    "建议动作": "先确认富途可卖数量，再触发自动退出或手动平仓。",
                })
            if net_pnl <= -300:
                rows.append({
                    "优先级": "高",
                    "对象": symbol,
                    "问题": "单合约净亏损超过 $300",
                    "处理入口": "策略复盘与执行质量",
                    "建议动作": "复核入场、退出和风控评分，必要时降低同标的预算。",
                })
            if capture is not None and capture < 0.45:
                rows.append({
                    "优先级": "中",
                    "对象": symbol,
                    "问题": "浮盈捕获率偏低",
                    "处理入口": "波动率期权质量面板",
                    "建议动作": "检查阶梯锁盈、移动止盈和追价退出是否生效。",
                })
    if not runtime_health.empty:
        bad = runtime_health[
            pd.to_numeric(runtime_health.get("异常次数", 0), errors="coerce").fillna(0).gt(0)
            | pd.to_numeric(runtime_health.get("退化/超时", 0), errors="coerce").fillna(0).gt(0)
        ]
        for _, row in bad.head(5).iterrows():
            rows.append({
                "优先级": "高" if _number(row.get("异常次数")) > 0 else "中",
                "对象": row.get("任务"),
                "问题": row.get("问题摘要") or row.get("最近状态"),
                "处理入口": "系统监控",
                "建议动作": "先处理运行层异常，再判断策略是否真的没有机会。",
            })
    if not blockers.empty:
        for _, row in blockers.head(3).iterrows():
            rows.append({
                "优先级": "中",
                "对象": row.get("未下单原因"),
                "问题": f"阻塞出现 {int(_number(row.get('次数')))} 次",
                "处理入口": "波动率策略总览 / 交易配置",
                "建议动作": "确认是市场条件不满足，还是参数、行情或成本门槛过严。",
            })
    if not rows and not quality_scores.empty:
        worst = quality_scores.iloc[0]
        rows.append({
            "优先级": "低",
            "对象": worst.get("对象"),
            "问题": f"当前最低评分维度：{worst.get('主要短板')}",
            "处理入口": "策略复盘与执行质量",
            "建议动作": worst.get("建议"),
        })
    return pd.DataFrame(rows)


def build_evidence_alignment(store, strategies: tuple[str, ...] = ("volatility_long_gamma", "core_satellite")) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    try:
        experiments = store.strategy_experiments(limit=500)
    except Exception:
        experiments = []
    try:
        validation_runs = store.validation_runs(limit=500)
    except Exception:
        validation_runs = []
    try:
        evidence = store.acceptance_evidence()
    except Exception:
        evidence = []

    for strategy in strategies:
        scoped_experiments = [row for row in experiments if row.get("strategy_id") == strategy]
        scoped_validation = [row for row in validation_runs if row.get("strategy_id") == strategy]
        scoped_evidence = [row for row in evidence if row.get("strategy_id") == strategy]
        latest_exp = scoped_experiments[0] if scoped_experiments else {}
        latest_val = scoped_validation[0] if scoped_validation else {}
        latest_ev = scoped_evidence[0] if scoped_evidence else {}
        metrics = _json(latest_exp.get("metrics_json")) or _json(latest_val.get("metrics_json"))
        rows.append({
            "策略": strategy,
            "活跃实验": "是" if latest_exp.get("is_active") else "否",
            "实验状态": latest_exp.get("status", "无"),
            "最近验证": latest_val.get("created_at", ""),
            "验证结果": (
                "通过" if latest_val and bool(latest_val.get("passed"))
                else ("未通过" if latest_val else "无")
            ),
            "最近证据": latest_ev.get("created_at", ""),
            "证据结果": (
                "通过" if latest_ev and bool(latest_ev.get("passed"))
                else ("未通过" if latest_ev else "无")
            ),
            "关键指标": "；".join(f"{key}={value}" for key, value in list(metrics.items())[:5]) if metrics else "",
            "结论": _evidence_conclusion(latest_exp, latest_val, latest_ev),
        })
    return pd.DataFrame(rows)


def _evidence_conclusion(experiment: dict, validation: dict, evidence: dict) -> str:
    if validation and not bool(validation.get("passed")):
        return "最近回测/前测未通过，参数调整需要优先补证据。"
    if evidence and not bool(evidence.get("passed")):
        return "审批/证据未通过，先补齐验收材料。"
    if experiment and experiment.get("is_active"):
        return "有活跃实验版本，可对照当前交易表现。"
    if validation or evidence:
        return "已有验证记录，但缺少活跃实验版本。"
    return "缺少回测/前测证据，暂时只能做运行复盘。"
