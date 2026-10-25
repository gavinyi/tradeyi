#!/usr/bin/env python3
"""
训练波动率标的筛选模型

用法:
    uv run python scripts/ml_train_symbol_scorer.py \
        --data data/ml_training/volatility_trades.csv \
        --model-output data/ml_models/volatility_scorer_v1.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import pandas as pd
import numpy as np


def _print_safe(*args, **kwargs) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑脚本会直接崩，`pytest -s` 也会假红。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)

try:
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import (
        classification_report, roc_auc_score, confusion_matrix,
        precision_recall_curve, roc_curve
    )
    from sklearn.ensemble import RandomForestClassifier
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    _print_safe("❌ scikit-learn未安装")
    print("请运行: uv pip install scikit-learn")
    import sys
    sys.exit(1)

try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except (ImportError, OSError) as e:
    HAS_LIGHTGBM = False
    _print_safe("⚠️  lightgbm不可用，将使用sklearn的RandomForest作为替代")
    if isinstance(e, OSError) and 'libomp' in str(e):
        print("    原因: 缺少OpenMP库（libomp.dylib）")
        print("    解决: brew install libomp 或继续使用RandomForest")


def prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """
    准备训练特征和标签
    
    Returns:
        (特征DataFrame, 标签Series, 特征列表)
    """
    _print_safe("\n🔧 准备训练特征...")
    
    # 特征列（排除ID、标签、元数据列、结果特征）
    exclude_cols = [
        'created_at', 'strategy_id', 'symbol', 'side', 'status',
        'metadata_json', 'trade_date', 'forward_days',
        'window_trades', 'window_buy_amount', 'window_sell_amount',
        'window_net_pnl', 'is_profitable',
        # 结果特征（订单执行后才知道，不应用于训练）
        'average_fill_price', 'filled_quantity', 'limit_price'
    ]
    
    feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    # 提取特征和标签
    X = df[feature_cols].copy()
    y = df['is_profitable'].copy()
    
    # 处理缺失值
    X = X.fillna(0)
    
    # 移除常量特征（方差为0）
    constant_cols = X.columns[X.std() == 0]
    if len(constant_cols) > 0:
        print(f"  移除常量特征: {list(constant_cols)}")
        X = X.drop(columns=constant_cols)
    
    feature_cols = X.columns.tolist()
    
    print(f"  特征数量: {len(feature_cols)}")
    print(f"  样本数量: {len(X)}")
    print(f"  正样本比例: {y.mean():.2%}")
    
    return X, y, feature_cols


def train_model(X_train, y_train, X_valid, y_valid):
    """训练模型"""
    _print_safe("\n🚀 开始训练模型...")
    
    if HAS_LIGHTGBM:
        print("  使用模型: LightGBM")
        
        # 计算类别权重
        pos_weight = (y_train == 0).sum() / (y_train == 1).sum()
        
        model = lgb.LGBMClassifier(
            n_estimators=200,
            max_depth=8,
            learning_rate=0.05,
            num_leaves=64,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=pos_weight,
            random_state=42,
            verbose=-1
        )
        
        model.fit(
            X_train, y_train,
            eval_set=[(X_valid, y_valid)],
            eval_metric='auc',
            callbacks=[lgb.early_stopping(20, verbose=False)]
        )
        
    else:
        print("  使用模型: RandomForest")
        
        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            min_samples_split=20,
            min_samples_leaf=10,
            class_weight='balanced',
            random_state=42,
            n_jobs=-1
        )
        
        model.fit(X_train, y_train)
    
    _print_safe("✅ 模型训练完成")
    
    return model


def evaluate_model(model, X_test, y_test, feature_names):
    """评估模型性能"""
    _print_safe("\n📊 模型评估:")
    
    # 预测
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    
    # 分类报告
    print("\n分类报告:")
    print(classification_report(y_test, y_pred, target_names=['亏损', '盈利']))
    
    # AUC
    auc = roc_auc_score(y_test, y_proba)
    print(f"AUC: {auc:.4f}")
    
    # 混淆矩阵
    cm = confusion_matrix(y_test, y_pred)
    print("\n混淆矩阵:")
    print(f"  真阴性(TN): {cm[0,0]:4d}  |  假阳性(FP): {cm[0,1]:4d}")
    print(f"  假阴性(FN): {cm[1,0]:4d}  |  真阳性(TP): {cm[1,1]:4d}")
    
    # 不同阈值下的性能
    print("\n不同阈值下的表现:")
    for threshold in [0.4, 0.5, 0.6, 0.7]:
        y_pred_th = (y_proba >= threshold).astype(int)
        precision = (y_test[y_pred_th == 1] == 1).mean() if (y_pred_th == 1).sum() > 0 else 0
        recall = (y_pred_th[y_test == 1] == 1).mean() if (y_test == 1).sum() > 0 else 0
        print(f"  阈值={threshold:.1f}: 精确率={precision:.2%}, 召回率={recall:.2%}")
    
    # 特征重要性
    if hasattr(model, 'feature_importances_'):
        importance_df = pd.DataFrame({
            'feature': feature_names,
            'importance': model.feature_importances_
        }).sort_values('importance', ascending=False)
        
        print("\nTOP 15 重要特征:")
        for idx, row in importance_df.head(15).iterrows():
            print(f"  {row['feature']:30s}: {row['importance']:.4f}")
        
        return {
            'auc': auc,
            'confusion_matrix': cm.tolist(),
            'feature_importance': importance_df.to_dict('records')
        }
    
    return {'auc': auc, 'confusion_matrix': cm.tolist()}


def save_model(model, scaler, feature_names, output_path, metrics):
    """保存模型"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    checkpoint = {
        'model': model,
        'scaler': scaler,
        'feature_names': feature_names,
        'metrics': metrics,
        'trained_at': pd.Timestamp.now().isoformat(),
        'model_type': type(model).__name__
    }
    
    with open(output_path, 'wb') as f:
        pickle.dump(checkpoint, f)
    
    _print_safe(f"\n✅ 模型已保存到: {output_path}")
    
    # 保存metrics到JSON
    metrics_path = output_path.with_suffix('.json')
    with open(metrics_path, 'w') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    
    _print_safe(f"✅ 指标已保存到: {metrics_path}")


def main():
    parser = argparse.ArgumentParser(description='训练波动率标的筛选模型')
    parser.add_argument('--data', required=True, help='训练数据CSV路径')
    parser.add_argument('--model-output', required=True, help='模型输出路径')
    parser.add_argument('--test-size', type=float, default=0.2, help='测试集比例')
    parser.add_argument('--random-state', type=int, default=42, help='随机种子')
    
    args = parser.parse_args()
    
    print("=" * 60)
    _print_safe("🎯 波动率标的筛选模型训练")
    print("=" * 60)
    
    # 1. 加载数据
    _print_safe(f"\n📂 加载数据: {args.data}")
    df = pd.read_csv(args.data)
    print(f"  样本数: {len(df)}")
    print(f"  时间范围: {df['trade_date'].min()} 至 {df['trade_date'].max()}")
    
    # 2. 准备特征
    X, y, feature_names = prepare_features(df)
    
    # 3. 划分数据集（按时间划分）
    _print_safe("\n✂️  划分数据集...")
    df_sorted = df.sort_values('created_at').reset_index(drop=True)
    split_idx = int(len(df_sorted) * (1 - args.test_size))
    
    train_indices = df_sorted.index[:split_idx]
    test_indices = df_sorted.index[split_idx:]
    
    X_train, X_test = X.loc[train_indices], X.loc[test_indices]
    y_train, y_test = y.loc[train_indices], y.loc[test_indices]
    
    # 进一步划分验证集
    X_train, X_valid, y_train, y_valid = train_test_split(
        X_train, y_train, test_size=0.15, random_state=args.random_state
    )
    
    print(f"  训练集: {len(X_train)} 样本")
    print(f"  验证集: {len(X_valid)} 样本")
    print(f"  测试集: {len(X_test)} 样本")
    
    # 4. 特征标准化
    _print_safe("\n🔄 特征标准化...")
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_valid_scaled = scaler.transform(X_valid)
    X_test_scaled = scaler.transform(X_test)
    
    # 5. 训练模型
    model = train_model(X_train_scaled, y_train, X_valid_scaled, y_valid)
    
    # 6. 评估模型
    metrics = evaluate_model(model, X_test_scaled, y_test, feature_names)
    
    # 7. 保存模型
    save_model(model, scaler, feature_names, args.model_output, metrics)
    
    print("\n" + "=" * 60)
    _print_safe("✅ 训练完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
