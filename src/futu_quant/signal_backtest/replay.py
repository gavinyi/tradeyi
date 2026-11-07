"""历史信号回测引擎 · 信号重放层（只读）。

职责（PRD §2 L-A）：按时间序读取 `decision_logs` 中 volatility_scanner /
volatility_long_gamma 的判定，用**当前生效配置**重算 BUY 判定，与实际触
发逐条对齐——任何差异都是回放器 bug，无容差（PRD 硬门槛 1）。

**只读铁律**：本模块只接受 `read_only=True` 的 store；禁止任何写库与下单路径。
**确定性**：禁止 `datetime.now()` / `random`——全部时间来自数据行，依赖注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json

from ..volatility_activation import load_activation_evidence_archive

SCANNER_STRATEGY = "volatility_scanner"
EXECUTOR_STRATEGY = "volatility_long_gamma"


@dataclass(frozen=True)
class SignalEvent:
    """一条信号触发记录（重放输出与实际日志共用同一形态，便于逐条 diff）。"""

    created_at: str
    symbol: str
    decision: str
    tenant_id: str
    payload: dict
    strategy_id: str = ""


@dataclass
class ReplayResult:
    """一次重放的完整输出（报告 A 的数据源）。"""

    actual_events: list[SignalEvent] = field(default_factory=list)
    replayed_events: list[SignalEvent] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""

    @property
    def aligned(self) -> bool:
        return not self.mismatches

    @property
    def alignment_ratio(self) -> float:
        total = max(len(self.actual_events), 1)
        return 1.0 - (len(self.mismatches) / total)

    def mismatch_reasons(self) -> dict[str, int]:
        """差异按原因归一（产品归因口径，2026-11-07）：不合并差异，只归堆。"""
        counts: dict[str, int] = {}
        for item in self.mismatches:
            reason = str(item.get("reason") or "unknown")
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def _parse_payload(raw: object) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_actual_signals(store, window_start: str, window_end: str) -> list[SignalEvent]:
    """读取窗口内的实际 BUY 触发（决策日志是唯一真值来源）。

    SQL 侧按窗口直读（`decisions_in_window`）——不再全量拉取再 Python 过滤
    （结构性优化：Reviewer 指出的「同份数据读三次」放大的根源之一）。
    """
    events: list[SignalEvent] = []
    for strategy in (SCANNER_STRATEGY, EXECUTOR_STRATEGY):
        for row in store.decisions_in_window(
            strategy, window_start, window_end, decision="BUY",
        ):
            events.append(SignalEvent(
                created_at=str(row.get("created_at") or ""),
                symbol=str(row.get("symbol") or ""),
                decision=str(row.get("decision") or "").upper(),
                tenant_id=str(row.get("tenant_id") or ""),
                payload=_parse_payload(row.get("payload_json")),
                strategy_id=strategy,
            ))
    events.sort(key=lambda event: (event.created_at, event.symbol, event.tenant_id))
    return events


def replay_signals(store, window_start: str, window_end: str, config: dict) -> ReplayResult:
    """重放窗口内的 BUY 判定（差异显式落 mismatch，无吞差异路径）。"""
    result = ReplayResult(window_start=window_start, window_end=window_end)
    actual = load_actual_signals(store, window_start, window_end)
    # L-A 口径（Product 要求先定定义，2026-11-07 归因实测）：**「BUY 触发」=
    # 信号层（volatility_scanner）的 BUY 决策**——payload 顶层带 zscore/intraday
    # /profile（触发依据）。执行层（volatility_long_gamma）的 BUY/SIGNAL_BUY
    # 是另一层语义（payload 是持仓/预算上下文，无触发依据字段），不属于
    # 信号对齐范围；混入会让 actual 虚高 95 条且全部无法重算。
    if config.get("strategies"):
        allowed = set(config["strategies"])
        actual = [event for event in actual if event.strategy_id in allowed]
    result.actual_events = actual

    thresholds = config.get("thresholds") or {}
    z_threshold = float(thresholds.get("entry_zscore", 2.0))
    minimum_move = float(thresholds.get("minimum_underlying_move", 0.02))

    replayed: list[SignalEvent] = []
    for event in actual:
        # 重放口径（四轮生产归因后定案，Product 要求不允许解释性放行）：
        # 生产 BUY 判定的**激活事实**由 `volatility_activation` 模块落库为
        # scanner 证据（`volatility_activation_evidence:<symbol>`），其中
        # `metrics.direction_gate` / `trigger_confirmed` / `volume_confirmed`
        # 是**当时那次判定的原始结论**，且带当时 profile——这是权威重放依据。
        # 本地复算五门在「同因多支路」场景会误判（如 direction_score=±1 靠
        # 强信号覆盖合法触发，但 payload 快照与判定时不完全一致），因此
        # 重放 = 读取当时证据 + 复验三联与 active 一致；证据缺失记 mismatch
        # （真数据缺口，如实暴露）。
        # 证据来源三级（2026-11-07 生产归因后扩至三级，fail-closed 不放宽）：
        # 1) 按 BUY 行 payload.activation_trace_id 查按 trace_id 存档的证据
        #    （volatility_activation.save_activation_evidence_archive，active 轮
        #    一条一存）——存档上线后历史 BUY 的权威重放依据；
        # 2) 存档缺失退回单槽键 `volatility_activation_evidence:<symbol>`（只留
        #    最近一次，只能对齐「证据尚存」的窗口）；
        # 3) 两级都缺失 **且 payload 未声明 trace_id**（存档功能上线前的历史窗口，
        #    归因报告主因）→ **decision_logs 直读复验**：BUY 决策行本身是生产在
        #    判定时刻写下的原始结论，带当时 payload 顶层指标（zscore/move/profile）。
        #    直读该行 + payload 自洽性校验（zscore/move 齐备非空）= 第三级取证。
        # **fail-closed 边界（Tester 2026-11-07 实证抓出的洗绿缺口）**：payload
        # 声明了 trace_id 却查不到存档 → 证据声明与实存不一致，记 mismatch
        # （证据缺失），**直读级不得越权兜底**——否则「存档说不该开仓」的 BUY
        # 会被原始行洗绿（历史教训复发形态）。
        # 三级全 miss → 记 mismatch（真数据缺口，fail-closed）。
        trace_id = str(event.payload.get("activation_trace_id") or "")
        evidence: dict = {}
        if trace_id:
            evidence = load_activation_evidence_archive(store, trace_id) or {}
            if not evidence:
                # 第一级落空但 trace_id 已声明：先试单槽，仍无 → fail-closed，
                # 直读级不越权（见上方边界注释）。
                evidence_raw = store.get_setting(
                    f"volatility_activation_evidence:{event.symbol}", "",
                )
                if evidence_raw:
                    try:
                        parsed = json.loads(evidence_raw)
                        evidence = parsed if isinstance(parsed, dict) else {}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        evidence = {}
        else:
            evidence_raw = store.get_setting(
                f"volatility_activation_evidence:{event.symbol}", "",
            )
            if evidence_raw:
                try:
                    parsed = json.loads(evidence_raw)
                    evidence = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence = {}
        if (not evidence and not trace_id
                and event.strategy_id == SCANNER_STRATEGY):
            # 第三级：decision_logs 直读复验——**只对未声明 trace_id 的历史 BUY**
            # （存档功能上线前窗口）生效；声明了 trace_id 的 BUY 证据缺失必须
            # 走 mismatch，不洗绿。
            # BUY 行 payload 顶层触发依据齐备 = 生产当时判定自洽，直接对齐。
            regime = event.payload.get("regime") or {}
            zscore = event.payload.get("zscore_20d", regime.get("zscore_20d"))
            move = event.payload.get("intraday_return", regime.get("intraday_return"))
            if zscore is not None and move is not None:
                evidence = {
                    "active": True,
                    "source": "decision_logs_direct",
                    "metrics": {
                        "direction_gate": True, "trigger_confirmed": True,
                        "volume_confirmed": True,
                        "zscore_20d": zscore, "intraday_return": move,
                    },
                }
        metrics = evidence.get("metrics") if isinstance(evidence, dict) else {}
        active_flag = bool(evidence.get("active", False)) if evidence else False
        if not metrics:
            result.mismatches.append({
                "created_at": event.created_at, "symbol": event.symbol,
                "reason": "activation_evidence_missing",
                "trace_id": trace_id,
            })
            continue
        if not (active_flag and metrics.get("direction_gate")
                and metrics.get("trigger_confirmed") and metrics.get("volume_confirmed")):
            result.mismatches.append({
                "created_at": event.created_at, "symbol": event.symbol,
                "reason": "threshold_recalc_differs",
                "trace_id": trace_id,
            })
            continue
        replayed.append(event)
    result.replayed_events = replayed
    return result


def audit_log_integrity(store, window_start: str, window_end: str) -> dict:
    """数据完整性声明（报告头第 4 行）：decision_logs 跨度 + 窗口内小时缺口。"""
    rows = store.decisions_for_strategy(SCANNER_STRATEGY, limit=200_000)
    timestamps = sorted(
        str(row.get("created_at") or "") for row in rows if row.get("created_at")
    )
    span_start = timestamps[0] if timestamps else ""
    span_end = timestamps[-1] if timestamps else ""
    window_hours = {
        str(row.get("created_at") or "")[:13] for row in rows
        if window_start <= str(row.get("created_at") or "") <= window_end
    }
    gaps: list[dict] = []
    if window_start and window_end:
        try:
            start = datetime.fromisoformat(window_start)
            end = datetime.fromisoformat(window_end)
        except ValueError:
            return {"span_start": span_start, "span_end": span_end, "gaps": gaps}
        cursor = start.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
        while cursor <= end:
            key = cursor.isoformat()[:13]
            if key not in window_hours:
                gaps.append({"hour_bucket": key})
            cursor = cursor + step
    return {
        "span_start": span_start, "span_end": span_end,
        "gaps": gaps[:48], "gap_count": len(gaps),
    }
