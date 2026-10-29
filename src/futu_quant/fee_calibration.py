"""P1-16：季度结单费用校准（只计算、只告警，不自动改模型或风控）。"""

from __future__ import annotations

from dataclasses import dataclass

DEVIATION_LIMIT = 0.15


@dataclass(frozen=True)
class FeeCalibration:
    statement_total: float
    estimated_total: float
    factor: float | None
    deviation: float | None
    status: str
    reason: str


def evaluate_fee_calibration(
    statement_total: float,
    estimated_total: float,
    *,
    deviation_limit: float = DEVIATION_LIMIT,
) -> FeeCalibration:
    """比较人工下载结单与同期估算费用。

    本函数不写运行配置；估算为零或输入非法时 fail-closed 返回 DEGRADED。
    """
    try:
        statement = float(statement_total)
        estimated = float(estimated_total)
        limit = float(deviation_limit)
    except (TypeError, ValueError):
        return FeeCalibration(0.0, 0.0, None, None, "DEGRADED", "费用校准输入无法解析")
    if statement < 0 or estimated <= 0 or not 0 < limit < 1:
        return FeeCalibration(
            statement, estimated, None, None, "DEGRADED",
            "费用校准缺少有效结单或同期估算基数",
        )
    factor = statement / estimated
    deviation = factor - 1.0
    if abs(deviation) > limit:
        return FeeCalibration(
            statement, estimated, factor, deviation, "DEGRADED",
            f"季度费用估算系数偏离 {deviation:+.1%}，超过 ±{limit:.0%}",
        )
    return FeeCalibration(
        statement, estimated, factor, deviation, "HEALTHY",
        f"季度费用估算系数 {factor:.4f}，偏离 {deviation:+.1%}",
    )


def record_fee_calibration(store, *, tenant_id: str, quarter: str,
                           statement_total: float, estimated_total: float) -> FeeCalibration:
    """留下租户级校准事件；永不修改费用模型、限额或熔断阈值。"""
    result = evaluate_fee_calibration(statement_total, estimated_total)
    store.operational_event(
        "fee_calibration",
        "QUARTERLY_FEE_CALIBRATION",
        result.status,
        {
            "tenant_id": str(tenant_id),
            "quarter": str(quarter),
            "statement_total": result.statement_total,
            "estimated_total": result.estimated_total,
            "factor": result.factor,
            "deviation": result.deviation,
            "auto_apply": False,
            "reason": result.reason,
        },
    )
    return result
