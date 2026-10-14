"""
测试标的池双轨赛马 (快慢双轨)、弹性扩容与动量耗竭防接盘机制
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from futu_quant.store import TradingStore
from futu_quant.universe.diversity_filter import DiversityFilter
from futu_quant.universe.incubator_engine import IncubatorEngine
from futu_quant.universe.lifecycle_manager import LifecycleManager


def test_fast_track_qualifies_in_3_days():
    db_path = Path("data/test_fast_track.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        engine = IncubatorEngine(store=store)

        # 录入一只只观察了 3 天，但 2 次放量突破、收益正、价差小、得分 0.85 的超级黑马
        sym = "US.PLTR"
        engine.record_simulated_signal(sym, z_score=2.5, volume_ratio=2.1, spread_pct=0.018)
        engine.update_simulated_trade(
            sym, is_win=True, pnl_usd=450.0, return_ratio=0.35,
            slippage_pct=0.015, volume_retention=0.75,
        )

        # 模拟第 3 天评估
        store.upsert_incubator_metrics(sym, {
            "symbol": sym,
            "observation_days": 3,
            "signal_triggers": 2,
            "simulated_wins": 1,
            "simulated_losses": 0,
            "simulated_pnl_usd": 450.0,
            "simulated_max_dd": 0.05,
            "avg_slippage_pct": 0.018,
            "volume_retention_ratio": 0.75,
            "latest_ml_score": 0.86,
            "promotion_eligible": 0,
        })

        eligible = engine.evaluate_daily_metrics(sym, current_days=3)
        assert eligible is True

        profile = store.incubator_symbol(sym)
        assert profile is not None
        assert profile["promotion_eligible"] == 1
        assert profile["track_mode"] == "FAST_TRACK"
    finally:
        db_path.unlink(missing_ok=True)


def test_adaptive_capacity_expands_to_10_satellites():
    diversity = DiversityFilter(max_satellites=5)

    # 1. 弱势市场/少高分标的：维持 5 只基准
    weak_df = pd.DataFrame([
        {"symbol": f"US.SYM{i}", "ml_score": 0.55} for i in range(12)
    ])
    assert diversity.resolve_capacity(weak_df) == 5

    # 2. 爆发牛市/大量优质标的 (>= 8 只打分 >= 0.75)：自适应扩容至 10 只
    hot_df = pd.DataFrame([
        {"symbol": f"US.HOT{i}", "ml_score": 0.85} for i in range(9)
    ])
    assert diversity.resolve_capacity(hot_df) == 10

    # 传入分散行业字典，验证能自适应容纳更多外援席位
    industry_map = {f"US.HOT{i}": f"IND_{i}" for i in range(9)}
    selected = diversity.select_diverse_portfolio(hot_df, industry_map=industry_map, adaptive_capacity=True)
    assert len(selected) > 5


def test_over_extended_momentum_exhaustion_blocks_promotion():
    db_path = Path("data/test_exhaustion.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        manager = LifecycleManager(store=store)

        # 模拟候选标的在沙盒达标但短线涨幅过大进入耗竭期
        sym = "US.CHASING_PEAK"
        store.upsert_incubator_metrics(sym, {
            "symbol": sym,
            "observation_days": 10,
            "signal_triggers": 3,
            "simulated_pnl_usd": 800.0,
            "simulated_max_dd": 0.10,
            "avg_slippage_pct": 0.02,
            "volume_retention_ratio": 0.70,
            "latest_ml_score": 0.90,
            "promotion_eligible": 1,
        })

        # 传入耗竭标的黑名单 (5日涨幅过大)
        over_extended = {sym}
        promoted = manager.evaluate_promotions(
            [sym],
            candidate_scores={sym: 0.90},
            over_extended_symbols=over_extended,
        )

        # 坚决不予晋级，防高位接盘
        assert sym not in promoted

        # 检查是否记录了审计事件
        events = [e for e in store.universe_lifecycle_events(5) if e["event_type"] == "PROMOTION_REJECTED_OVER_EXTENDED"]
        assert len(events) == 1
        assert events[0]["symbol"] == sym
    finally:
        db_path.unlink(missing_ok=True)
