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

ALL_REASONS = (
    REASON_SNAPSHOT_MISSING, REASON_SPREAD_GATE_DIFF, REASON_LIMIT_VERSION_DIFF,
    REASON_SLIPPAGE, REASON_PARTIAL_FILL, REASON_CANCELLED, REASON_BACKTEST_BUG,
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


def _actual_key(created_at: str, symbol: str) -> tuple[str, str]:
    return (created_at[:16], symbol)  # 分钟级对齐键（日志/账本时间粒度差异容忍）


def reconcile(matched_trades, actual_fills: list[dict]) -> ReconcileReport:
    """逐笔对照：回测撮合 vs 实际成交。

    对齐键：分钟 + 标的；两侧都存在且成交价在 1 tick 容差内 → matched。
    回测有、实际无 → 判定链在实盘被某道门拦下（保守记 `backtest_bug`）。
    实际有、回测无 → 重放漏判，同样保守记 `backtest_bug`。
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
        # 差异分档（Reviewer Day 2 前置①）：实盘有、回测无——回放器漏判
        # （backtest_bug）；时间差/单边差异不在此列。
        report.rows.append(ReconcileRow(
            created_at=str(fill.get("filled_at") or fill.get("created_at") or ""),
            symbol=str(fill.get("symbol") or ""), matched=False,
            reason=REASON_BACKTEST_BUG, actual_fill=float(fill.get("price") or 0),
        ))
    return report


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
