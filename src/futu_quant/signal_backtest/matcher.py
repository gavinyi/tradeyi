"""历史信号回测引擎 · 模拟撮合层（只读，纯函数）。

职责：把一条信号触发变成一笔「如果当时下单」的模拟成交——撮合价按数据
可得性分层（PRD §4）：L1 v2 盘口快照 > L2 记录权利金/IV > L3 拒绝编造。
每笔输出带 `price_tier`，L3 占比超限的结论由报告层自动降级。

**纯函数**：不触 store、不取实时行情、不用当前时间——行情/价格全部由
调用方注入（从只读库读出的历史数据）。
"""
from __future__ import annotations

from dataclasses import dataclass

OPTION_MULTIPLIER = 100.0
#: 默认滑点：成交价 +1 tick（PRD §3.4，保守估算；敏感性对比可传 0）。
DEFAULT_SLIPPAGE_TICKS = 1
TICK_SIZE = 0.01


@dataclass(frozen=True)
class PriceQuote:
    """一笔历史行情（由调用方从 v2 frames / option_history 读出注入）。"""

    bid_price: float | None
    ask_price: float | None
    recorded_premium: float | None = None  # L2：决策/账本中记录的权利金
    tier: str = "L3"  # "L1" / "L2" / "L3"


@dataclass(frozen=True)
class MatchedTrade:
    code: str
    symbol: str
    created_at: str
    fill_price: float
    price_tier: str
    side: str = "BUY"
    quantity: int = 1
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS
    premium_notional: float = 0.0

    def __post_init__(self) -> None:
        if self.fill_price <= 0:
            raise ValueError(f"撮合价必须为正，得到 {self.fill_price}")


def match_trade(
    signal, quote: PriceQuote, *, quantity: int = 1,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS,
) -> MatchedTrade:
    """按数据分层撮合一条信号。

    - L1：ask + slippage（真实盘口，最可信）；
    - L2：记录权利金 + slippage（单边近似，报告需标注）；
    - L3：无任何价格依据 → 抛 ValueError（由调用方决定跳过并计入 L3 占比，
      **绝不编造价格**——PRD「L3 仅示意」的落点是标注占比，不是默许造数）。
    """
    slippage = max(0, int(slippage_ticks)) * TICK_SIZE
    if quote.tier == "L1" and quote.ask_price and quote.ask_price > 0:
        fill = float(quote.ask_price) + slippage
    elif quote.tier == "L2" and quote.recorded_premium and quote.recorded_premium > 0:
        fill = float(quote.recorded_premium) + slippage
    else:
        raise ValueError("无可用历史价格（L3）：拒绝编造撮合价")
    return MatchedTrade(
        code=str(getattr(signal, "code", "") or getattr(signal, "symbol", "")),
        symbol=str(getattr(signal, "symbol", "")),
        created_at=str(getattr(signal, "created_at", "")),
        quantity=int(quantity),
        fill_price=round(fill, 4),
        price_tier=quote.tier,
        slippage_ticks=int(slippage_ticks),
        premium_notional=round(fill * int(quantity) * OPTION_MULTIPLIER, 2),
    )


def tier_shares(trades: list[MatchedTrade]) -> dict[str, float]:
    """三层占比（报告必带；L3 >50% 的结论由报告层降级）。"""
    total = len(trades)
    if not total:
        return {"L1": 0.0, "L2": 0.0, "L3": 0.0}
    counts = {"L1": 0, "L2": 0, "L3": 0}
    for trade in trades:
        counts[trade.price_tier] = counts.get(trade.price_tier, 0) + 1
    return {tier: round(count / total, 4) for tier, count in counts.items()}


def fee_per_trade(fill_price: float, quantity: int) -> float:
    """手续费估算（PRD §3.4 沿用既有保守口径；期权按张计）。"""
    return round(max(1.0, fill_price * OPTION_MULTIPLIER * 0.0065) + 1.1 * quantity, 2)
