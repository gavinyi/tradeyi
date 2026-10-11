"""
每日盘后标的 ML 精排、多样性打散与外援晋级轮换入口脚本
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.universe.diversity_filter import DiversityFilter
from futu_quant.universe.incubator_engine import IncubatorEngine
from futu_quant.universe.lifecycle_manager import LifecycleManager
from futu_quant.universe.ranking_pipeline import RankingPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily ML universe ranking and promotion")
    parser.add_argument("--top-satellites", type=int, default=5, help="Max satellite promotions")
    args = parser.parse_args()

    settings = Settings.load()
    broker = FutuPaperBroker(settings)
    store = TradingStore()

    incubator = IncubatorEngine(store=store)
    ranking = RankingPipeline(store=store)
    diversity = DiversityFilter(max_satellites=args.top_satellites)
    lifecycle = LifecycleManager(store=store)

    # 1. 获取所有候选标的
    candidates = store.candidate_symbols(status="ACTIVE")
    if not candidates:
        print("当前暂无候选标的，执行一次基础召回...")
        from futu_quant.universe.recall_scanner import RecallScanner
        scanner = RecallScanner(broker=broker, store=store)
        week_start = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        candidates = [{"symbol": c.symbol} for c in scanner.run_weekly_recall(week_start)]

    candidate_symbols = [c["symbol"] for c in candidates]

    # 2. 模拟盘后指标自评
    for s in candidate_symbols:
        incubator.evaluate_daily_metrics(s)

    # 3. 运行 ML 截面打分
    ranked_df = ranking.rank_candidates(candidate_symbols, broker=broker)
    print("\n=== 候选标的 ML 精排积分榜 (Top 10) ===")
    print(ranked_df.head(10).to_string(index=False))

    # 4. 执行行业配额与 MMR 打散
    diverse_top = diversity.select_diverse_portfolio(ranked_df)
    print(f"\n多样性打散选出标的 ({len(diverse_top)} 只):", diverse_top)

    # 5. 迟滞缓冲评估晋级与退出
    candidate_scores = dict(zip(ranked_df["symbol"], ranked_df["ml_score"]))
    promoted = lifecycle.evaluate_promotions(diverse_top, candidate_scores=candidate_scores)
    evicted = lifecycle.evaluate_evictions()

    # 6. 同步至运行时标的池
    active_pool = lifecycle.sync_to_runtime_universe()
    print(f"\n=== 最新生效实盘交易池 ({len(active_pool)} 只) ===")
    print("总标的列表:", active_pool)
    print(f"其中包含晋级外援 ({len(promoted)} 只):", promoted)
    if evicted:
        print(f"本次清退标的 ({len(evicted)} 只):", evicted)


if __name__ == "__main__":
    main()
