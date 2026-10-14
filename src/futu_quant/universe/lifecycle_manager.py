"""
模块 5: 生命周期晋级准入、迟滞缓冲与秒踢仲裁器 (Universe Lifecycle Manager)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS, save_volatility_symbols
from futu_quant.store import TradingStore
from .recall_scanner import INDUSTRY_MAPPING

logger = logging.getLogger(__name__)


class LifecycleManager:
    """标的晋级准入、迟滞防抖与秒踢仲裁器"""

    def __init__(self, store: TradingStore | None = None):
        self.store = store or TradingStore()

    def evaluate_promotions(
        self,
        diverse_top_candidates: list[str],
        candidate_scores: dict[str, float] | None = None,
        duration_days: int = 21,
        max_capacity: int = 10,
        over_extended_symbols: set[str] | None = None,
    ) -> list[str]:
        """
        实施准入硬门槛、自适应容量扩充 (5~10只)、动量耗竭防接盘与迟滞防抖:
        1. 检查动量耗竭 (Over-extended): 短期涨幅过大一票否决，绝不高位接盘;
        2. 快轨/慢轨双轨达标，或者得分 >= 0.75;
        3. 自适应容量: 优质标的充沛时外援池弹性扩充至最多 10 只;
        4. 池满时启用迟滞防抖 (超出最低现任至少 10%)。
        """
        scores = candidate_scores or {}
        exhausted = over_extended_symbols or set()
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=duration_days)).isoformat()

        current_active_rows = self.store.active_promoted_symbols()
        current_active = [r["symbol"] for r in current_active_rows]
        promoted: list[str] = list(current_active)

        for candidate in diverse_top_candidates:
            if candidate in promoted:
                continue

            # 1. 动量耗竭防接盘门禁 (一票否决)
            if candidate in exhausted:
                logger.warning("标的 %s 近期涨幅过大进入动量耗竭期，一票否决禁止高位接盘", candidate)
                self.store.record_universe_lifecycle_event(
                    event_type="PROMOTION_REJECTED_OVER_EXTENDED",
                    symbol=candidate,
                    details={"reason": "近期涨幅过大进入耗竭期，防追尾一票否决"},
                )
                continue

            # 2. 检查沙盒准入硬门槛
            incubator_profile = self.store.incubator_symbol(candidate)
            eligible = bool(incubator_profile and incubator_profile.get("promotion_eligible", 0))
            score = float(scores.get(candidate, (incubator_profile or {}).get("latest_ml_score", 0.50)))

            if not eligible and score < 0.75:
                logger.debug("标的 %s 未满足沙盒或打分准入门槛 (score=%.2f, eligible=%s)", candidate, score, eligible)
                continue

            track = (incubator_profile or {}).get("track_mode", "STANDARD")

            # 3. 自适应外援容量: 允许动态扩充至最多 10 只
            if len(promoted) < max_capacity:
                industry = INDUSTRY_MAPPING.get(candidate, "OTHER")
                self.store.promote_universe_symbol(candidate, score, expires, industry)
                self.store.record_universe_lifecycle_event(
                    event_type="PROMOTION",
                    symbol=candidate,
                    details={
                        "score": score,
                        "expires_at": expires,
                        "industry": industry,
                        "track_mode": track,
                        "reason": f"通过沙盒孵化({track})与多样性打散晋级",
                    },
                )
                promoted.append(candidate)
                logger.info("标的 %s 成功晋级外援实盘池 [模式=%s, Score=%.2f, 到期=%s]", candidate, track, score, expires)
            else:
                # 4. 迟滞防抖: 需高出现任最低分 10%
                active_scores = {r["symbol"]: float(r.get("promotion_score", 0.0)) for r in current_active_rows}
                lowest_current = min(active_scores.values()) if active_scores else 0.0
                if score >= lowest_current * 1.10 and score >= 0.80:
                    # 替换最低分现任
                    lowest_sym = min(active_scores, key=active_scores.get)
                    self.store.evict_promoted_symbol(lowest_sym, f"被更高分候选 {candidate} 优胜劣汰替换")
                    promoted.remove(lowest_sym)
                    
                    industry = INDUSTRY_MAPPING.get(candidate, "OTHER")
                    self.store.promote_universe_symbol(candidate, score, expires, industry)
                    promoted.append(candidate)
                    self.store.record_universe_lifecycle_event(
                        event_type="PROMOTION",
                        symbol=candidate,
                        details={
                            "score": score,
                            "replaced_symbol": lowest_sym,
                            "track_mode": track,
                            "reason": "超出最低现任标的10%完成迟滞晋级替换",
                        },
                    )

        return promoted

    def evaluate_evictions(
        self,
        hard_stop_symbols: set[str] | None = None,
        wide_spread_symbols: set[str] | None = None,
    ) -> list[str]:
        """
        清退与应急秒踢 (Demotion & Fast Eviction):
        1. 授权到期且动量衰竭;
        2. 连续 2 次触发硬止损;
        3. 买卖价差恶化超过 10%。
        底仓 13 只标的绝对不在此列。
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        active_promoted = self.store.active_promoted_symbols()
        evicted: list[str] = []

        hard_stops = hard_stop_symbols or set()
        wide_spreads = wide_spread_symbols or set()

        for item in active_promoted:
            symbol = item["symbol"]
            reason = ""

            # 1. 应急红线: 连续止损
            if symbol in hard_stops:
                reason = "触发连续硬止损红线，即刻秒踢隔离"
            # 2. 应急红线: 价差骤降
            elif symbol in wide_spreads:
                reason = "期权买卖价差恶化超过10%，流动性丧失秒踢"
            # 3. 授权到期
            elif str(item.get("expires_at", "")) < now_iso:
                reason = "授权周期到期自然清退"

            if reason:
                self.store.evict_promoted_symbol(symbol, reason)
                self.store.record_universe_lifecycle_event(
                    event_type="FAST_EVICTION" if "秒踢" in reason else "DEMOTION",
                    symbol=symbol,
                    details={"reason": reason},
                )
                evicted.append(symbol)
                logger.warning("外援标的 %s 已被安全移出交易池: %s", symbol, reason)

        return evicted

    def sync_to_runtime_universe(self) -> tuple[str, ...]:
        """
        合并公式: 永久核心底仓 (13只) ∪ 当前有效晋级外援 (3~5只)
        原子回写至 SQLite runtime_config 与环境变量，实现全系统秒级热生效。
        """
        active_promoted = [r["symbol"] for r in self.store.active_promoted_symbols()]
        merged = tuple(dict.fromkeys(list(DEFAULT_VOLATILITY_SYMBOLS) + active_promoted))

        save_volatility_symbols(merged)
        self.store.record_universe_lifecycle_event(
            event_type="SYNC_RUNTIME",
            symbol="ALL",
            details={
                "core_count": len(DEFAULT_VOLATILITY_SYMBOLS),
                "satellite_count": len(active_promoted),
                "total_symbols": list(merged),
            },
        )
        logger.info("已同步最新交易池到运行时: 核心 %d 只 + 外援 %d 只 = 共 %d 只", len(DEFAULT_VOLATILITY_SYMBOLS), len(active_promoted), len(merged))
        return merged
