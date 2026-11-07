"""历史信号回测引擎 · 对账与净值层（只读，纯函数）。

职责（PRD §2 L-B + §5 报告 C）：把模拟撮合结果与实际账本（fill_ledger /
order_intents）逐笔对照，差异落原因码（`backtest_bug` 占比 >0 整体标红，
判定集中在 `ReconcileReport.has_backtest_bug` 一处并带逐行明细）；
同期回测净值 vs 实际账本净值两条线（报告 C 数据源）。

**纯函数**：不触 store、不用当前时间；账本行由调用方从只读库读出注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: L-B 差异原因码（PRD §2 L-B 枚举，报告层按此分布出表）。
REASON_SNAPSHOT_MISSING = "snapshot_missing"
REASON_SPREAD_GATE_DIFF = "spread_gate_diff"
REASON_LIMIT_VERSION_DIFF = "limit_version_diff"
REASON_SLIPPAGE = "slippage"
REASON_PARTIAL_FILL = "partial_fill"
REASON_CANCELLED = "cancelled"
REASON_BACKTEST_BUG = "backtest_bug"
#: 回测侧无价可撮（v1 撮合层在无价格依据窗口结构性不可算，2026-11-07 裁定）：
#: 历史窗口 L3 占比 100%（v2 盘口 11-06 才积累、decision_logs 不记权利金）→
#: 无模拟成交可对照——**与「对照发现 bug」本质不同**，独立分档不进
#: backtest_bug、不进对账率分子分母，报告如实标注「随 v2 积累后启用」。
REASON_NO_SIMULATED_FILL = "no_simulated_fill"

ALL_REASONS = (
    REASON_SNAPSHOT_MISSING, REASON_SPREAD_GATE_DIFF, REASON_LIMIT_VERSION_DIFF,
    REASON_SLIPPAGE, REASON_PARTIAL_FILL, REASON_CANCELLED, REASON_BACKTEST_BUG,
    REASON_NO_SIMULATED_FILL,
)


@dataclass
class ReconcileRow:
    """一笔对照结果（明细随报告输出，标红可定位到行）。"""

    created_at: str
    symbol: str
    matched: bool
    reason: str = ""          # 不一致时的原因码
    backtest_fill: float | None = None
    actual_fill: float | None = None


@dataclass
class ReconcileReport:
    rows: list[ReconcileRow] = field(default_factory=list)

    @property
    def aligned_count(self) -> int:
        return sum(1 for row in self.rows if row.matched)

    @property
    def reconcile_ratio(self) -> float:
        if not self.rows:
            return 0.0
        return round(self.aligned_count / len(self.rows), 4)

    def reason_distribution(self) -> dict[str, int]:
        distribution = {reason: 0 for reason in ALL_REASONS}
        for row in self.rows:
            if not row.matched and row.reason in distribution:
                distribution[row.reason] += 1
        return distribution

    @property
    def has_backtest_bug(self) -> bool:
        """`backtest_bug` 占比 >0 → 报告整体标红（PRD 硬门槛 2，唯一判定点）。"""
        return self.reason_distribution().get(REASON_BACKTEST_BUG, 0) > 0

    @property
    def no_price_basis_count(self) -> int:
        """回测侧无价可撮的笔数（独立档，纪律见 REASON_NO_SIMULATED_FILL 注释）。"""
        return self.reason_distribution().get(REASON_NO_SIMULATED_FILL, 0)

    def price_comparison_eligible_rows(self) -> list[ReconcileRow]:
        """可参与价格容差对照的行（排除 no_price_basis——不进对账率分子分母）。"""
        return [row for row in self.rows if row.reason != REASON_NO_SIMULATED_FILL]

    @property
    def price_reconcile_ratio(self) -> float | None:
        """价格容差对照率：**只对可对照行计算**；无可对照行返回 None
        （报告必须写「不适用」，禁止出现「对账率 100%」——Product 纪律 1）。"""
        eligible = self.price_comparison_eligible_rows()
        if not eligible:
            return None
        return round(sum(1 for row in eligible if row.matched) / len(eligible), 4)


def _actual_key(created_at: str, symbol: str) -> tuple[str, str]:
    return (created_at[:16], symbol)  # 分钟级对齐键（日志/账本时间粒度差异容忍）


def reconcile(matched_trades, actual_fills: list[dict], *,
              simulated_fill_available: bool = True) -> ReconcileReport:
    """逐笔对照：回测撮合 vs 实际成交。

    对齐键：分钟 + 标的；两侧都存在且成交价在 1 tick 容差内 → matched。
    回测有、实际无 → 判定链在实盘被某道门拦下（保守记 `backtest_bug`）。
    实际有、回测无 → 重放漏判，同样保守记 `backtest_bug`。

    **数据可得性边界（2026-11-07 Product 裁定）**：`simulated_fill_available=False`
    （该区间无任何价格依据、撮合层产不出模拟成交——历史窗口 L3 100% 形态）时，
    实际成交行落 `no_price_basis` 独立档：**不进 backtest_bug、不进对账率**，
    报告如实标注「随 v2 积累后启用」——把「算不了」与「对不上」分开，绝不造数。
    """
    report = ReconcileReport()
    actual_by_key: dict[tuple[str, str], dict] = {}
    for fill in actual_fills:
        key = _actual_key(str(fill.get("filled_at") or fill.get("created_at") or ""),
                          str(fill.get("symbol") or ""))
        actual_by_key.setdefault(key, fill)

    matched_keys: set[tuple[str, str]] = set()
    for trade in matched_trades:
        key = _actual_key(trade.created_at, trade.symbol)
        fill = actual_by_key.get(key)
        if fill is None:
            report.rows.append(ReconcileRow(
                created_at=trade.created_at, symbol=trade.symbol, matched=False,
                reason=REASON_BACKTEST_BUG, backtest_fill=trade.fill_price,
            ))
            continue
        matched_keys.add(key)
        actual_price = float(fill.get("price") or 0)
        price_ok = abs(actual_price - trade.fill_price) <= 0.011
        report.rows.append(ReconcileRow(
            created_at=trade.created_at, symbol=trade.symbol, matched=price_ok,
            reason="" if price_ok else REASON_SLIPPAGE,
            backtest_fill=trade.fill_price, actual_fill=actual_price,
        ))

    for key, fill in actual_by_key.items():
        if key in matched_keys:
            continue
        if not simulated_fill_available:
            # 回测侧无价可撮：这些实际成交无法参与价格对照——如实分档，
            # 不是「对照发现 bug」（对照根本没发生）。
            report.rows.append(ReconcileRow(
                created_at=str(fill.get("filled_at") or fill.get("created_at") or ""),
                symbol=str(fill.get("symbol") or ""), matched=False,
                reason=REASON_NO_SIMULATED_FILL,
                actual_fill=float(fill.get("price") or 0),
            ))
            continue
        # 差异分档（Reviewer Day 2 前置①）：实盘有、回测无——回放器漏判
        # （backtest_bug）；时间差/单边差异不在此列。
        report.rows.append(ReconcileRow(
            created_at=str(fill.get("filled_at") or fill.get("created_at") or ""),
            symbol=str(fill.get("symbol") or ""), matched=False,
            reason=REASON_BACKTEST_BUG, actual_fill=float(fill.get("price") or 0),
        ))
    return report


def decision_agreement(replayed_events, actual_intents: list[dict]) -> dict:
    """B1 决策结论对照（PRD §2 L-B 第一子项，不需要价格数据）。

    口径（Product 2026-11-07 三行判据，方向分开看不能只看同向率）：
    - **actual_only**（实际下了单、回放没放行）——**必须 ≈0**：说明回放判据比
      生产宽松，回测会系统性高估收益，不为 0 不得收官；
    - **replay_only**（回放放行、实际没下单）——允许存在但必须逐条归因：
      风控拒/熔断拒/限额拒/质量门变化/价格未成交/人工取消，归不了因 =
      回放器漏判；**未归因必须为 0**；
    - 三行输出：actual_only 条数 / replay_only 原因分布 / 未归因 0 条。
    replay_only 原因分布就是「为什么没成交」的答案，页 48 首屏直出。
    """
    intent_keys: set[tuple[str, str]] = set()
    for intent in actual_intents:
        created = str(intent.get("created_at") or intent.get("submitted_at") or "")
        symbol = str(intent.get("symbol") or intent.get("underlying") or "")
        if created and symbol:
            intent_keys.add((created[:16], str(symbol).upper()))

    both = 0
    replay_only_list: list[dict] = []
    for event in replayed_events:
        key = (str(event.created_at)[:16], str(event.symbol).upper())
        if key in intent_keys:
            both += 1
            continue
        # replay_only 初步归因：payload 自带触发依据在场说明判定自洽，差异落在
        # 执行层（候选被拒/意图未建/意图未成交）——归因页下钻；payload 无依据
        # 的落 unattributed（未归因必须为 0，Product 判据）。
        regime = event.payload.get("regime") or {}
        has_trigger_evidence = event.payload.get(
            "zscore_20d", regime.get("zscore_20d")) is not None
        replay_only_list.append({
            "created_at": event.created_at, "symbol": event.symbol,
            "tenant_id": event.tenant_id,
            "tentative_reason": (
                "candidate_filtered_or_intent_not_created（重放放行、执行层未建意图——"
                "候选过滤器/风控/限额/熔断在 campaign 层拦截，归因页下钻）"
                if has_trigger_evidence else "unattributed（payload 无触发依据，无法初归因）"
            ),
        })
    actual_only_list: list[dict] = []
    replay_keys = {
        (str(event.created_at)[:16], str(event.symbol).upper())
        for event in replayed_events
    }
    for key in sorted(intent_keys - replay_keys):
        actual_only_list.append({
            "created_at": key[0], "symbol": key[1],
            "note": "实际下了单、回放未放行——回放判据比生产宽松（致命方向，必须≈0）",
        })
    unattributed = [item for item in replay_only_list
                    if str(item.get("tentative_reason", "")).startswith("unattributed")]
    total = max(len(replayed_events), 1)
    return {
        "agreement_ratio": round(both / total, 4),
        "both_count": both,
        "replay_only": replay_only_list[:50],
        "replay_only_count": len(replay_only_list),
        "actual_only": actual_only_list[:50],
        "actual_only_count": len(actual_only_list),
        "unattributed_count": len(unattributed),
        "intent_total": len(intent_keys),
    }


def equity_curve(trades, *, initial_capital: float = 10_000.0,
                 fee_per_trade_fn=None) -> list[tuple[str, float]]:
    """回测净值曲线（报告 C 数据源）：按时间序累计每笔盈亏。

    v1 盈亏口径：以撮合价为开仓价，平仓价 = 序列中下一笔价格；费用逐笔
    计入（fee_per_trade_fn 注入，保持纯函数）。
    """
    curve: list[tuple[str, float]] = []
    equity = float(initial_capital)
    open_position = None
    for trade in sorted(trades, key=lambda item: item.created_at):
        if open_position is None:
            open_position = trade
            continue
        close_price = trade.fill_price
        pnl = (close_price - open_position.fill_price) * open_position.quantity * 100.0
        if fee_per_trade_fn is not None:
            pnl -= fee_per_trade_fn(open_position.fill_price, open_position.quantity)
            pnl -= fee_per_trade_fn(close_price, trade.quantity)
        equity += pnl
        curve.append((trade.created_at, round(equity, 2)))
        open_position = None
    return curve
