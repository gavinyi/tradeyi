#!/usr/bin/env python3
"""
训练波动率标的筛选模型 V3 - 增强版
新增: 支持更多特征、特征选择、模型对比

用法:
    uv run python scripts/ml_train_symbol_scorer_v3.py \
        --data data/ml_training/volatility_trades_v3.csv \
        --model-output data/ml_models/volatility_scorer_v3.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import pandas as pd
import numpy as np

try:
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import (
        classification_report, roc_auc_score, confusion_matrix,
        precision_recall_curve, roc_curve
    )
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.feature_selection import SelectKBest, f_classif
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    print("❌ scikit-learn未安装")
    print("请运行: uv pip install scikit-learn")
    import sys
    sys.exit(1)

try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except (ImportError, OSError) as e:
    HAS_LIGHTGBM = False
    print("⚠️  lightgbm不可用，将使用sklearn的RandomForest作为替代")
    if isinstance(e, OSError) and 'libomp' in str(e):
        print("    原因: 缺少OpenMP库（libomp.dylib）")
        print("    解决: brew install libomp 或继续使用RandomForest")


def prepare_features(df: pd.DataFrame, feature_selection: bool = True) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """
    准备训练特征和标签
    
    Args:
        df: 原始数据
        feature_selection: 是否进行特征选择
        
    Returns:
        (特征DataFrame, 标签Series, 特征列表)
    """
    print("\n🔧 准备训练特征...")
    
    # 特征列（排除ID、标签、元数据列、结果特征）
    exclude_cols = [
        'created_at', 'strategy_id', 'symbol', 'side', 'status',
        'metadata_json', 'trade_date', 'forward_days',
        'window_trades', 'window_buy_amount', 'window_sell_amount',
        'window_net_pnl', 'is_profitable',
        # 结果特征（订单执行后才知道，不应用于训练）
        'average_fill_price', 'filled_quantity', 'limit_price', 'quantity',
        # 标的符号（改用underlying_symbol的编码版本）
        'underlying_symbol'
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
    
    # 特征选择（可选）
    if feature_selection and len(X.columns) > 15:
        print(f"\n  执行特征选择（从{len(X.columns)}个特征中选择）...")
        
        # 使用方差分析选择Top-K特征
        selector = SelectKBest(f_classif, k=min(20, len(X.columns)))
        X_selected = selector.fit_transform(X, y)
        
        # 获取选中的特征
        selected_mask = selector.get_support()
        selected_features = X.columns[selected_mask].tolist()
        
        # 计算特征重要性分数
        scores = selector.scores_[selected_mask]
        feature_scores = sorted(zip(selected_features, scores), key=lambda x: x[1], reverse=True)
        
        print(f"  选择了 {len(selected_features)} 个特征:")
        for feat, score in feature_scores[:10]:
            print(f"    - {feat}: {score:.2f}")
        
        X = pd.DataFrame(X_selected, columns=selected_features, index=X.index)
    
    feature_cols = X.columns.tolist()
    
    print(f"\n  最终特征数量: {len(feature_cols)}")
    print(f"  样本数量: {len(X)}")
    print(f"  正样本比例: {y.mean():.2%}")
    
    return X, y, feature_cols


def train_model(X_train, y_train, X_valid, y_valid, model_type='auto'):
    """
    训练模型
    
    Args:
        model_type: 'lgb', 'rf', 或 'auto'（自动选择）
    """
    print(f"\n🚀 开始训练模型 (type={model_type})...")
    
    # 自动选择模型
    if model_type == 'auto':
        model_type = 'lgb' if HAS_LIGHTGBM else 'rf'
    
    if model_type == 'lgb' and HAS_LIGHTGBM:
        print("  使用模型: LightGBM")
        
        # 计算类别权重
        pos_weight = (y_train == 0).sum() / (y_train == 1).sum()
        
        model = lgb.LGBMClassifier(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=pos_weight,
            random_state=42,
            verbose=-1
        )
        
        # 训练
        model.fit(
            X_train, y_train,
            eval_set=[(X_valid, y_valid)],
            eval_metric='auc',
            callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)]
        )
        
        best_iteration = model.best_iteration_
        print(f"  最佳迭代: {best_iteration}")
        
    else:
        print("  使用模型: Random Forest")
        
        # 计算类别权重
        class_weight = {
            0: 1.0,
            1: (y_train == 0).sum() / (y_train == 1).sum()
        }
        
        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=10,
            min_samples_split=10,
            min_samples_leaf=5,
            max_features='sqrt',
            class_weight=class_weight,
            random_state=42,
            n_jobs=-1
        )
        
        model.fit(X_train, y_train)
    
    return model


def evaluate_model(model, X_test, y_test, feature_cols):
    """评估模型性能"""
    print("\n📊 模型评估:")
    
    # 预测
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    
    # 计算指标
    auc = roc_auc_score(y_test, y_proba)
    print(f"\n  🎯 AUC: {auc:.4f}")
    
    # 混淆矩阵
    cm = confusion_matrix(y_test, y_pred)
    print("\n  混淆矩阵:")
    print(f"              预测亏损  预测盈利")
    print(f"    实际亏损      {cm[0,0]:4d}      {cm[0,1]:4d}")
    print(f"    实际盈利      {cm[1,0]:4d}      {cm[1,1]:4d}")
    
    # 分类报告
    print("\n  详细指标:")
    report = classification_report(y_test, y_pred, target_names=['亏损', '盈利'])
    print(report)
    
    # 特征重要性
    print("\n  特征重要性 TOP 10:")
    if hasattr(model, 'feature_importances_'):
        importances = model.feature_importances_
        feature_importance = sorted(
            zip(feature_cols, importances),
            key=lambda x: x[1],
            reverse=True
        )
        
        for feat, imp in feature_importance[:10]:
            print(f"    {feat:25s}: {imp:.4f} ({imp*100:.1f}%)")
    
    # 不同阈值的表现
    print("\n  不同预测阈值的性能:")
    print("  阈值   精确率  召回率  F1-Score")
    for threshold in [0.3, 0.4, 0.5, 0.6, 0.7]:
        y_pred_thresh = (y_proba >= threshold).astype(int)
        if y_pred_thresh.sum() > 0:
            precision = (y_pred_thresh & y_test).sum() / y_pred_thresh.sum()
            recall = (y_pred_thresh & y_test).sum() / y_test.sum()
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            print(f"  {threshold:.1f}    {precision:.1%}   {recall:.1%}   {f1:.3f}")
    
    return auc, y_proba


def save_model(model, feature_cols, metrics, output_path):
    """保存模型和元数据"""
    print(f"\n💾 保存模型到: {output_path}")
    
    # 保存模型
    with open(output_path, 'wb') as f:
        pickle.dump({
            'model': model,
            'feature_cols': feature_cols,
            'version': 'v3',
            'trained_at': pd.Timestamp.now().isoformat()
        }, f)
    
    # 保存指标
    metrics_path = output_path.with_suffix('.json')
    with open(metrics_path, 'w') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    
    print(f"  模型文件: {output_path}")
    print(f"  指标文件: {metrics_path}")


def main():
    parser = argparse.ArgumentParser(description='训练波动率标的筛选模型 V3')
    parser.add_argument('--data', required=True, help='训练数据CSV文件')
    parser.add_argument('--model-output', required=True, help='模型输出路径')
    parser.add_argument('--test-size', type=float, default=0.2, help='测试集比例')
    parser.add_argument('--model-type', choices=['lgb', 'rf', 'auto'], default='auto',
                        help='模型类型: lgb/rf/auto')
    parser.add_argument('--no-feature-selection', action='store_true',
                        help='禁用特征选择')
    
    args = parser.parse_args()
    
    # 检查输入文件
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"❌ 数据文件不存在: {data_path}")
        return
    
    # 创建输出目录
    output_path = Path(args.model_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("🤖 波动率标的筛选模型训练 V3")
    print("=" * 60)
    print(f"数据文件: {data_path}")
    print(f"模型输出: {output_path}")
    print(f"测试集比例: {args.test_size}")
    
    # 1. 加载数据
    print("\n📂 加载数据...")
    df = pd.read_csv(data_path)
    print(f"  原始数据: {len(df)} 条记录, {len(df.columns)} 列")
    
    # 2. 准备特征
    X, y, feature_cols = prepare_features(
        df, 
        feature_selection=not args.no_feature_selection
    )
    
    # 3. 划分数据集
    print("\n📊 划分数据集...")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=42, stratify=y
    )
    
    # 进一步划分验证集
    X_train, X_valid, y_train, y_valid = train_test_split(
        X_train, y_train, test_size=0.15, random_state=42, stratify=y_train
    )
    
    print(f"  训练集: {len(X_train)} 条")
    print(f"  验证集: {len(X_valid)} 条")
    print(f"  测试集: {len(X_test)} 条")
    
    # 4. 训练模型
    model = train_model(X_train, y_train, X_valid, y_valid, args.model_type)
    
    # 5. 评估模型
    auc, y_proba = evaluate_model(model, X_test, y_test, feature_cols)
    
    # 6. 保存模型
    metrics = {
        'auc': float(auc),
        'test_size': len(X_test),
        'train_size': len(X_train),
        'n_features': len(feature_cols),
        'feature_cols': feature_cols,
        'model_type': args.model_type,
        'positive_rate': float(y.mean())
    }
    
    save_model(model, feature_cols, metrics, output_path)
    
    print("\n" + "=" * 60)
    print("✅ 训练完成!")
    print("=" * 60)
    
    # 使用建议
    print("\n💡 使用建议:")
    print(f"  - 当前AUC: {auc:.4f}")
    if auc < 0.55:
        print("  - ⚠️  模型预测能力较弱，建议:")
        print("    1. 收集更多历史数据（目标1000+样本）")
        print("    2. 添加市场数据特征（真实VIX、SPY等）")
        print("    3. 工程化更多期权特征")
    elif auc < 0.65:
        print("  - ✅ 模型有一定预测能力，可用于初筛")
        print("  - 建议配合其他规则使用，ML权重不超过40%")
    else:
        print("  - 🎉 模型表现良好！")
        print("  - 可作为主要筛选依据，ML权重50-70%")
    
    print(f"\n  推荐阈值: 0.5 (精确率优先)")
    print(f"  模型文件: {output_path}")


if __name__ == '__main__':
    main()
