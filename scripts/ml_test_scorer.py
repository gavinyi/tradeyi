#!/usr/bin/env python3
"""
快速测试ML标的筛选功能（v3 版本）

用法:
    uv run python scripts/ml_test_scorer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加src到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from futu_quant.ml_symbol_scorer import MLSymbolScorer
from futu_quant.store import TradingStore


def test_scorer():
    """测试ML评分器 v3"""
    
    print("=" * 60)
    print("🧪 测试ML标的评分器 (v3)")
    print("=" * 60)
    
    # 1. 检查模型文件
    model_path = Path("data/ml_models/volatility_scorer_v3.pkl")
    
    if not model_path.exists():
        print("\n❌ 模型文件不存在:", model_path)
        print("\n请先运行以下命令训练模型:")
        print("  uv run python scripts/ml_export_training_data_v3.py --output data/ml_training/volatility_trades_v3.csv --start-date 2026-07-01")
        print("  uv run python scripts/ml_train_symbol_scorer_v3.py --data data/ml_training/volatility_trades_v3.csv --model-output data/ml_models/volatility_scorer_v3.pkl")
        return
    
    print(f"\n✅ 找到模型文件: {model_path}")
    print(f"   文件大小: {model_path.stat().st_size / 1024:.1f} KB")
    
    # 2. 初始化存储
    print("\n🔧 初始化组件...")
    try:
        store = TradingStore("data/trading.db")
        print("  ✅ 数据库初始化成功")
    except Exception as e:
        print(f"  ❌ 初始化失败: {e}")
        return
    
    # 3. 加载评分器（默认读取环境变量 ML_*）
    print("\n📊 加载ML评分器...")
    try:
        scorer = MLSymbolScorer()
        if not scorer.enabled:
            print("  ❌ 评分器未启用（检查 ML_ENABLED / 模型文件）")
            return
        print("  ✅ 评分器加载成功")
        print(f"  特征数量: {len(scorer.feature_cols)}")
        print(f"  预测阈值: {scorer.threshold}")
    except Exception as e:
        print(f"  ❌ 加载失败: {e}")
        return
    
    # 4. 测试单个期权标的评分
    print("\n🎯 测试单个期权标的评分...")
    test_symbols = [
        "US.BRKB260828C530000",
        "US.KO260828C91000",
        "US.NVDA260831P222500",
        "US.AAPL260831C320000",
        "US.COIN260904P177500",
    ]
    
    for symbol in test_symbols:
        try:
            score = scorer.score_symbol(symbol, None, store)
            rank = "HIGH ⭐" if score >= 0.7 else "MEDIUM ●" if score >= 0.5 else "LOW ▼"
            print(f"  {symbol:28s}: {score:.3f} [{rank}]")
        except Exception as e:
            print(f"  {symbol:28s}: 评分失败 - {e}")
    
    # 5. 测试批量排序
    print("\n📋 测试批量排序...")
    try:
        ranked = scorer.rank_symbols(test_symbols, None, store)
        print("\n排序结果:")
        print(ranked.to_string(index=False))
    except Exception as e:
        print(f"  ❌ 排序失败: {e}")
    
    # 6. 测试过滤
    print("\n🔍 测试标的过滤（min_score=0.5）...")
    try:
        filtered = scorer.filter_symbols(test_symbols, None, store, min_score=0.5)
        print(f"  原始标的: {len(test_symbols)} 个")
        print(f"  过滤后: {len(filtered)} 个")
        print(f"  合格标的: {filtered}")
    except Exception as e:
        print(f"  ❌ 过滤失败: {e}")
    
    print("\n" + "=" * 60)
    print("✅ 测试完成！")
    print("=" * 60)
    
    print("\n💡 下一步:")
    print("  1. 检查评分是否合理（高分标的历史表现应该更好）")
    print("  2. 波动率策略已自动集成ML重排序（volatility_roll._apply_ml_ranking）")
    print("  3. 观察策略日志中的 ml_score / ml_rank 字段")


def check_data_quality():
    """检查训练数据质量"""
    
    print("\n📊 检查训练数据质量...")
    
    csv_path = Path("data/ml_training/volatility_trades_v3.csv")
    
    if not csv_path.exists():
        print(f"  ⚠️  训练数据不存在: {csv_path}")
        print("     请先运行: uv run python scripts/ml_export_training_data_v3.py")
        return
    
    import pandas as pd
    
    df = pd.read_csv(csv_path)
    
    print(f"  样本数量: {len(df)}")
    print(f"  特征数量: {len(df.columns)}")
    print(f"  时间范围: {df['trade_date'].min()} 至 {df['trade_date'].max()}")
    
    if 'is_profitable' in df.columns:
        pos_ratio = df['is_profitable'].mean()
        print(f"  正样本比例: {pos_ratio:.1%}")
        
        if pos_ratio < 0.2 or pos_ratio > 0.8:
            print("  ⚠️  样本不平衡！考虑调整forward_days参数")
    
    if len(df) < 300:
        print("  ⚠️  样本数量较少，建议至少500+条")
    else:
        print("  ✅ 样本数量充足")


if __name__ == "__main__":
    # 先检查数据
    check_data_quality()
    
    print("\n")
    
    # 再测试评分器
    test_scorer()
