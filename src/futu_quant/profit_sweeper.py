"""
期权超额利润自动沉淀至 SPY 底仓系统 (Profit Sweeper)
"""
from __future__ import annotations

import logging
from futu_quant.option_pnl import build_option_pnl_ledger, option_pnl_summary
from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)

PROFIT_SWEEPER_TOTAL_KEY = "profit_sweeper_total_swept_usd"
PROFIT_SWEEPER_PENDING_KEY = "profit_sweeper_pending_usd"


class ProfitSweeper:
    """期权利润锁箱与底仓沉淀器"""

    def __init__(self, store: TradingStore | None = None):
        self.store = store or TradingStore()

    def evaluate_unsettled_profit(self) -> dict[str, float]:
        """统计当前尚未沉淀为 SPY 底仓的期权超额利润。"""
        ledger, _, _ = build_option_pnl_ledger(self.store)
        summary = option_pnl_summary(ledger)
        total_realized = float(summary.get("realized_pnl", 0.0) or 0.0)

        already_swept = float(self.store.get_setting(PROFIT_SWEEPER_TOTAL_KEY, "0.0") or 0.0)
        unsettled = max(0.0, total_realized - already_swept)

        # 50% 沉淀到底仓，50% 留作期权复利资金
        to_core = round(unsettled * 0.50, 2)
        to_options = round(unsettled * 0.50, 2)

        return {
            "total_realized_option_pnl": total_realized,
            "already_swept_usd": already_swept,
            "unsettled_profit_usd": unsettled,
            "reinvest_to_spy_usd": to_core,
            "retained_in_options_usd": to_options,
        }

    def sweep_profit_to_spy(self, min_threshold_usd: float = 1000.0) -> dict[str, float] | None:
        """
        当新增未沉淀利润达到门槛 (默认 $1,000 美元) 时，
        自动将 50% 利润标记并沉淀到 Core Satellite 的 SPY 核心底仓。
        """
        stats = self.evaluate_unsettled_profit()
        unsettled = stats["unsettled_profit_usd"]
        to_spy = stats["reinvest_to_spy_usd"]

        if unsettled < min_threshold_usd or to_spy <= 0:
            return None

        # 更新已沉淀总额
        new_total_swept = stats["already_swept_usd"] + to_spy
        self.store.set_setting(PROFIT_SWEEPER_TOTAL_KEY, str(new_total_swept))

        # 累计记入 Core 专用额外购买力
        current_core_bonus = float(self.store.get_setting("core_satellite_profit_bonus_usd", "0.0") or 0.0)
        self.store.set_setting("core_satellite_profit_bonus_usd", str(current_core_bonus + to_spy))

        self.store.record_universe_lifecycle_event(
            event_type="PROFIT_SWEEPER_SETTLED",
            symbol="US.SPY",
            details={
                "unsettled_profit": unsettled,
                "swept_to_spy": to_spy,
                "retained_in_options": stats["retained_in_options_usd"],
                "total_swept_cumulative": new_total_swept,
                "core_bonus_cumulative": current_core_bonus + to_spy,
                "note": "期权大额利润 50% 自动以攻转守铸造为 SPY 现货底仓",
            },
        )
        logger.info("期权利润锁箱触发: 已沉淀 $%.2f 美元至 SPY 底仓 (累计沉淀 $%.2f)", to_spy, new_total_swept)
        return stats
