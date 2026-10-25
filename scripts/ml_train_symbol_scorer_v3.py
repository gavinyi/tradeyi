#!/usr/bin/env python3
"""
训练波动率标的筛选模型 V3 - 增强版
新增: 支持更多特征、特征选择、模型对比

切分口径（2026-10-25 起默认）：**按 `trade_date` 时间序 / walk-forward 切分**，
同日样本绝不跨集，并有反泄漏断言（训练集最晚日 < 验证集最早日 < 测试集最早日）。
`--split-mode random` 仅保留用于与旧口径对比复现（会泄漏、指标不可作 forward 证据）。

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

from futu_quant.ml_scorer import gamma_to_theta_ratio, moneyness_ratio


def _print_safe(message) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑重训会直接崩，`pytest -s` 也会假红。
    与 `ml_export_training_data_v3._print_safe` 同一口径（backlog #10 全仓 sweep 的一部分）。
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(str(message).encode("ascii", "replace").decode("ascii"))


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
    _print_safe("❌ scikit-learn未安装")
    _print_safe("请运行: uv pip install scikit-learn")
    import sys
    sys.exit(1)

try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except (ImportError, OSError) as e:
    HAS_LIGHTGBM = False
    _print_safe("⚠️  lightgbm不可用，将使用sklearn的RandomForest作为替代")
    if isinstance(e, OSError) and 'libomp' in str(e):
        _print_safe("    原因: 缺少OpenMP库（libomp.dylib）")
        _print_safe("    解决: brew install libomp 或继续使用RandomForest")


def prepare_features(df: pd.DataFrame, feature_selection: bool = True) -> tuple[pd.DataFrame, pd.Series, list[str], pd.Series]:
    """
    准备训练特征、标签和样本收益凸性权重
    
    Args:
        df: 原始数据
        feature_selection: 是否进行特征选择
        
    Returns:
        (特征DataFrame, 标签Series, 特征列表, 样本权重Series)
    """
    _print_safe("\n🔧 准备训练特征与凸性收益加权...")
    
    # 特征列（排除ID、标签、元数据列、结果特征）
    exclude_cols = [
        'created_at', 'strategy_id', 'symbol', 'side', 'status',
        'metadata_json', 'trade_date', 'forward_days',
        'window_trades', 'window_buy_amount', 'window_sell_amount',
        'window_net_pnl', 'is_profitable',
        # 结果特征（订单执行后才知道，不应用于训练）
        'average_fill_price', 'filled_quantity', 'limit_price', 'quantity',
        # 标的符号（改用underlying_symbol的编码版本）
        'underlying_symbol',
        # v3.2 原始行情列：只用于派生 moneyness/gamma_to_theta，不作为特征直投
        'underlying_price', 'option_gamma', 'option_theta',
        # 「只观测」列与缺失原因：覆盖率不足时只留观测，绝不入训
        'symbol_momentum_20d_observation', 'symbol_momentum_missing_reason',
        # P0-A 新增的缺失原因/滞后审计列（字符串/审计值，绝不能当特征喂给模型）
        'moneyness_missing_reason', 'gamma_to_theta_missing_reason', 'moneyness_lag_minutes',
    ]

    feature_cols = [col for col in df.columns if col not in exclude_cols]
    # 任何 `*_missing_reason` / `*_observation` / `*_lag_minutes` 列一律不入训
    # （后缀规则兜住未来新增列；`*_lag_minutes` 不在 INFERENCE_FEATURE_KEYS，
    #  一旦混进训练集，训出的模型会被 load_ml_model fail-closed 拒载；
    #  `*_observation` 若不排除，会悄悄"多学一列"且不报错）
    meta_suffixes = ('_missing_reason', '_observation', '_lag_minutes')
    dropped_meta = [col for col in feature_cols if col.endswith(meta_suffixes)]
    if dropped_meta:
        _print_safe(f"  排除元数据列（不入训）: {dropped_meta}")
        feature_cols = [col for col in feature_cols if col not in dropped_meta]
    
    # 提取特征和标签
    X = df[feature_cols].copy()
    y = df['is_profitable'].copy()
    
    # 样本收益对数加权：彻底解决期权 Long Gamma 场景下的胜率陷阱
    # 使得捕捉到大肉单的样本权重显著提升，强迫模型重视非对称爆发性标的
    pnl = df['window_net_pnl'].fillna(0).abs() if 'window_net_pnl' in df.columns else pd.Series(0.0, index=df.index)
    sample_weights = pd.Series(1.0 + np.log1p(pnl.values * 10.0), index=df.index)

    # 特征工程 v3.2（凸性与无量纲化）：口径与推理侧共用 `futu_quant.ml_scorer` 的
    # `moneyness_ratio` / `gamma_to_theta_ratio`，且**只认真实行情**。
    # 以前的两个"特征"都是伪的：
    #   * `strike/median(同标的strike)-1` → 该行权价在同批候选里的相对位置，
    #     随当天候选池漂移，同一合约换一天值就变，推理侧不可能复现；
    #   * `1/(dte·vix)` → 与 gamma/theta 没有任何关系，名不符实。
    # 缺列就报错拒绝，绝不静默换定义（否则训练出的重要性没有意义）。
    required_market = ('underlying_price', 'option_gamma', 'option_theta')
    missing_market = [column for column in required_market if column not in df.columns]
    if missing_market:
        raise RuntimeError(
            f"训练数据缺少 v3.2 必需的真实行情列 {missing_market}：拒绝用伪特征兜底"
            "（strike/median(strike)、dte·vix 都不是 moneyness/gamma_to_theta）。"
            "请先用 scripts/ml_export_training_data_v3.py 重新导出（会 join option_history）。"
        )

    moneyness_values = [
        moneyness_ratio(spot, strike)
        for spot, strike in zip(df['underlying_price'], df['strike_price'])
    ]
    if any(value is None for value in moneyness_values):
        raise RuntimeError("moneyness 出现空值（spot/strike 无效）：拒绝静默填 0")
    gamma_theta_values = [
        gamma_to_theta_ratio(gamma, theta)
        for gamma, theta in zip(df['option_gamma'], df['option_theta'])
    ]
    if any(value is None for value in gamma_theta_values):
        raise RuntimeError("gamma_to_theta 出现空值（gamma/theta 无效）：拒绝静默填 0")

    X['moneyness'] = [float(value) for value in moneyness_values]
    X['gamma_to_theta'] = [float(value) for value in gamma_theta_values]
    X = X.drop(columns=['strike_price', *required_market], errors='ignore')
    
    # 处理缺失值
    X = X.fillna(0)
    
    # 移除常量特征（方差为0）
    constant_cols = X.columns[X.std() == 0]
    if len(constant_cols) > 0:
        _print_safe(f"  移除常量特征: {list(constant_cols)}")
        X = X.drop(columns=constant_cols)
    
    # 特征选择（可选）
    if feature_selection and len(X.columns) > 15:
        _print_safe(f"\n  执行特征选择（从{len(X.columns)}个特征中选择）...")
        
        # 使用方差分析选择Top-K特征
        selector = SelectKBest(f_classif, k=min(20, len(X.columns)))
        X_selected = selector.fit_transform(X, y)
        
        # 获取选中的特征
        selected_mask = selector.get_support()
        selected_features = X.columns[selected_mask].tolist()
        
        # 计算特征重要性分数
        scores = selector.scores_[selected_mask]
        feature_scores = sorted(zip(selected_features, scores), key=lambda x: x[1], reverse=True)
        
        _print_safe(f"  选择了 {len(selected_features)} 个特征:")
        for feat, score in feature_scores[:10]:
            _print_safe(f"    - {feat}: {score:.2f}")
        
        X = pd.DataFrame(X_selected, columns=selected_features, index=X.index)
    
    feature_cols = X.columns.tolist()
    
    _print_safe(f"\n  最终特征数量: {len(feature_cols)}")
    _print_safe(f"  样本数量: {len(X)}")
    _print_safe(f"  正样本比例: {y.mean():.2%}")
    _print_safe(f"  样本权重均值: {sample_weights.mean():.2f} (最大权重: {sample_weights.max():.2f})")
    
    return X, y, feature_cols, sample_weights


def train_model(X_train, y_train, X_valid, y_valid, model_type='auto', sample_weight_train=None):
    """
    训练模型
    
    Args:
        model_type: 'lgb', 'rf', 或 'auto'（自动选择）
        sample_weight_train: 样本收益凸性权重
    """
    _print_safe(f"\n🚀 开始训练模型 (type={model_type})...")
    
    # 自动选择模型
    if model_type == 'auto':
        model_type = 'lgb' if HAS_LIGHTGBM else 'rf'
    
    if model_type == 'lgb' and HAS_LIGHTGBM:
        _print_safe("  使用模型: LightGBM")
        
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
            n_jobs=1,
            verbose=-1
        )
        
        # 训练
        model.fit(
            X_train, y_train,
            sample_weight=sample_weight_train,
            eval_set=[(X_valid, y_valid)],
            eval_metric='auc',
            callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)]
        )
        
        best_iteration = model.best_iteration_
        _print_safe(f"  最佳迭代: {best_iteration}")
        
    else:
        _print_safe("  使用模型: Random Forest")
        
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
            n_jobs=1
        )
        
        if sample_weight_train is not None:
            model.fit(X_train, y_train, sample_weight=sample_weight_train)
        else:
            model.fit(X_train, y_train)
    
    return model


def evaluate_model(model, X_test, y_test, feature_cols):
    """评估模型性能"""
    _print_safe("\n📊 模型评估:")
    
    # 预测
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    
    # 计算指标
    auc = roc_auc_score(y_test, y_proba)
    _print_safe(f"\n  🎯 AUC: {auc:.4f}")
    
    # 混淆矩阵
    cm = confusion_matrix(y_test, y_pred)
    _print_safe("\n  混淆矩阵:")
    _print_safe(f"              预测亏损  预测盈利")
    _print_safe(f"    实际亏损      {cm[0,0]:4d}      {cm[0,1]:4d}")
    _print_safe(f"    实际盈利      {cm[1,0]:4d}      {cm[1,1]:4d}")
    
    # 分类报告
    _print_safe("\n  详细指标:")
    report = classification_report(y_test, y_pred, target_names=['亏损', '盈利'])
    _print_safe(report)
    
    # 特征重要性
    _print_safe("\n  特征重要性 TOP 10:")
    if hasattr(model, 'feature_importances_'):
        importances = model.feature_importances_
        feature_importance = sorted(
            zip(feature_cols, importances),
            key=lambda x: x[1],
            reverse=True
        )
        
        for feat, imp in feature_importance[:10]:
            _print_safe(f"    {feat:25s}: {imp:.4f} ({imp*100:.1f}%)")
    
    # 不同阈值的表现
    _print_safe("\n  不同预测阈值的性能:")
    _print_safe("  阈值   精确率  召回率  F1-Score")
    for threshold in [0.3, 0.4, 0.5, 0.6, 0.7]:
        y_pred_thresh = (y_proba >= threshold).astype(int)
        if y_pred_thresh.sum() > 0:
            precision = (y_pred_thresh & y_test).sum() / y_pred_thresh.sum()
            recall = (y_pred_thresh & y_test).sum() / y_test.sum()
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            _print_safe(f"  {threshold:.1f}    {precision:.1%}   {recall:.1%}   {f1:.3f}")
    
    return auc, y_proba


def save_model(model, feature_cols, metrics, output_path):
    """保存模型和元数据"""
    _print_safe(f"\n💾 保存模型到: {output_path}")
    
    # 保存模型
    with open(output_path, 'wb') as f:
        pickle.dump({
            'model': model,
            'feature_cols': feature_cols,
            'version': 'v3.2',
            'trained_at': pd.Timestamp.now().isoformat()
        }, f)
    
    # 保存指标
    metrics_path = output_path.with_suffix('.json')
    with open(metrics_path, 'w') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    
    _print_safe(f"  模型文件: {output_path}")
    _print_safe(f"  指标文件: {metrics_path}")


def assert_no_temporal_leakage(
    dates: pd.Series, train_idx, valid_idx, test_idx,
) -> dict:
    """反泄漏断言：训练集最大交易日 < 验证集最小交易日 ≤ … < 测试集最小交易日。

    **必须抛错**而不是打印警告：随机切分曾让同一天的样本同时进训练/测试，
    模型提前见过答案，切模拟盘测出来的 forward 证据不可用（2026-10-25 裁定的根因）。
    """
    def _span(indices) -> tuple[str, str]:
        subset = dates.iloc[np.asarray(indices, dtype=int)]
        if subset.empty:
            raise RuntimeError("时序切分出现空集：无法做反泄漏断言")
        return str(subset.min()), str(subset.max())

    train_span = _span(train_idx)
    valid_span = _span(valid_idx)
    test_span = _span(test_idx)
    if not train_span[1] < valid_span[0]:
        raise RuntimeError(
            f"时序泄漏：训练集最晚交易日 {train_span[1]} 不早于验证集最早交易日 "
            f"{valid_span[0]}（同日样本跨集 = 模型提前见过答案）"
        )
    if not valid_span[1] < test_span[0]:
        raise RuntimeError(
            f"时序泄漏：验证集最晚交易日 {valid_span[1]} 不早于测试集最早交易日 "
            f"{test_span[0]}（同日样本跨集 = 模型提前见过答案）"
        )
    return {"train": train_span, "valid": valid_span, "test": test_span}


def time_series_split_indices(
    df: pd.DataFrame, *, test_size: float = 0.2, valid_size: float = 0.15,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按 `trade_date` **时间序**切分（walk-forward）：最后一段做测试集，倒数第二段做验证集。

    与随机切分的区别：同一天的样本**绝不跨集**。`test_size`/`valid_size` 是
    **交易日**比例（不是行数比例），保证每段至少 1 个交易日。
    """
    if "trade_date" not in df.columns:
        raise RuntimeError(
            "训练数据缺少 trade_date：无法按时序切分（拒绝退回随机切分）"
        )
    dates = df["trade_date"].astype(str).reset_index(drop=True)
    unique_dates = np.array(sorted(dates.unique()))
    if len(unique_dates) < 6:
        raise RuntimeError(
            f"可用交易日仅 {len(unique_dates)} 个，不足以做时序切分（至少 6 个）"
        )
    n_dates = len(unique_dates)
    n_test = max(1, int(round(n_dates * test_size)))
    n_valid = max(1, int(round(n_dates * valid_size)))
    if n_test + n_valid >= n_dates:
        raise RuntimeError(
            f"时序切分参数不合理：{n_dates} 个交易日里测试 {n_test} + 验证 {n_valid} "
            "会吃掉全部训练集"
        )
    train_dates = set(unique_dates[: n_dates - n_test - n_valid])
    valid_dates = set(unique_dates[n_dates - n_test - n_valid : n_dates - n_test])
    test_dates = set(unique_dates[n_dates - n_test :])

    positions = np.arange(len(df))
    date_values = dates.to_numpy()
    train_idx = positions[np.isin(date_values, list(train_dates))]
    valid_idx = positions[np.isin(date_values, list(valid_dates))]
    test_idx = positions[np.isin(date_values, list(test_dates))]
    assert_no_temporal_leakage(dates, train_idx, valid_idx, test_idx)

    labels = df["is_profitable"].to_numpy()[train_idx] if "is_profitable" in df else None
    if labels is not None and len(set(labels.tolist())) < 2:
        raise RuntimeError(
            "时序切分的训练集只有单一标签：无法训练（样本不足以做 walk-forward）"
        )
    _print_safe(
        f"  时序切分（同日不跨集）：训练 {min(train_dates)}~{max(train_dates)}"
        f"（{len(train_dates)} 日 / {len(train_idx)} 笔），"
        f"验证 {min(valid_dates)}~{max(valid_dates)}（{len(valid_dates)} 日 / {len(valid_idx)} 笔），"
        f"测试 {min(test_dates)}~{max(test_dates)}（{len(test_dates)} 日 / {len(test_idx)} 笔）"
    )
    return train_idx, valid_idx, test_idx


def main():
    parser = argparse.ArgumentParser(description='训练波动率标的筛选模型 V3')
    parser.add_argument('--data', required=True, help='训练数据CSV文件')
    parser.add_argument('--model-output', required=True, help='模型输出路径')
    parser.add_argument('--test-size', type=float, default=0.2, help='测试集比例')
    parser.add_argument('--valid-size', type=float, default=0.15, help='验证集比例（时序模式下按交易日比例）')
    parser.add_argument(
        '--split-mode', choices=['timeseries', 'random'], default='timeseries',
        help=(
            '切分方式：timeseries=按 trade_date 时间序/walk-forward（默认，'
            '同日样本绝不跨集）；random=旧随机切分（仅用于对比复现，会泄漏）'
        ),
    )
    parser.add_argument('--model-type', choices=['lgb', 'rf', 'auto'], default='auto',
                        help='模型类型: lgb/rf/auto')
    parser.add_argument('--no-feature-selection', action='store_true',
                        help='禁用特征选择')
    
    args = parser.parse_args()
    
    # 检查输入文件
    data_path = Path(args.data)
    if not data_path.exists():
        _print_safe(f"❌ 数据文件不存在: {data_path}")
        return
    
    # 创建输出目录
    output_path = Path(args.model_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    _print_safe("=" * 60)
    _print_safe("🤖 波动率标的筛选模型训练 V3")
    _print_safe("=" * 60)
    _print_safe(f"数据文件: {data_path}")
    _print_safe(f"模型输出: {output_path}")
    _print_safe(f"测试集比例: {args.test_size}")
    
    # 1. 加载数据
    _print_safe("\n📂 加载数据...")
    df = pd.read_csv(data_path)
    _print_safe(f"  原始数据: {len(df)} 条记录, {len(df.columns)} 列")
    
    # 2. 准备特征与凸性收益加权
    X, y, feature_cols, sample_weights = prepare_features(
        df, 
        feature_selection=not args.no_feature_selection
    )
    
    # 3. 划分数据集（默认时间序；同日样本绝不跨集，防泄漏）
    _print_safe("\n📊 划分数据集...")
    split_span: dict | None = None
    if args.split_mode == "timeseries":
        train_idx, valid_idx, test_idx = time_series_split_indices(
            df, test_size=args.test_size, valid_size=args.valid_size,
        )
        split_span = assert_no_temporal_leakage(
            df["trade_date"].astype(str).reset_index(drop=True),
            train_idx, valid_idx, test_idx,
        )
    else:
        _print_safe("  ⚠️  随机切分模式（仅用于与旧口径对比；同日样本会跨集 → 指标不可作为 forward 证据）")
        indices = np.arange(len(X))
        train_idx, test_idx = train_test_split(
            indices, test_size=args.test_size, random_state=42, stratify=y
        )

        # 进一步划分验证集
        train_idx, valid_idx = train_test_split(
            train_idx, test_size=args.valid_size, random_state=42,
            stratify=y.iloc[train_idx]
        )
    
    X_train, y_train, sw_train = X.iloc[train_idx], y.iloc[train_idx], sample_weights.iloc[train_idx]
    X_valid, y_valid, sw_valid = X.iloc[valid_idx], y.iloc[valid_idx], sample_weights.iloc[valid_idx]
    X_test, y_test, sw_test = X.iloc[test_idx], y.iloc[test_idx], sample_weights.iloc[test_idx]
    
    _print_safe(f"  训练集: {len(X_train)} 条")
    _print_safe(f"  验证集: {len(X_valid)} 条")
    _print_safe(f"  测试集: {len(X_test)} 条")
    
    # 4. 训练模型（传入样本凸性收益加权）
    model = train_model(X_train, y_train, X_valid, y_valid, args.model_type, sample_weight_train=sw_train)
    
    # 5. 评估模型
    auc, y_proba = evaluate_model(model, X_test, y_test, feature_cols)

    # Rank IC：预测分与测试集实际盈亏的 Spearman 相关（与 AUC 一起报，防单指标误判）
    pnl_test = (
        df['window_net_pnl'].iloc[test_idx].fillna(0.0).to_numpy()
        if 'window_net_pnl' in df.columns else np.zeros(len(test_idx))
    )
    rank_ic = float(pd.Series(y_proba).corr(pd.Series(pnl_test), method='spearman'))
    if np.isnan(rank_ic):
        rank_ic = 0.0
    _print_safe(f"\n  🎯 Rank IC（预测分 vs 测试集实际盈亏，Spearman）: {rank_ic:+.4f}")

    # 6. 保存模型
    metrics = {
        'auc': float(auc),
        'rank_ic': rank_ic,
        'split_mode': args.split_mode,
        'split_date_span': split_span,
        'test_size': len(X_test),
        'valid_size': len(X_valid),
        'train_size': len(X_train),
        'n_features': len(feature_cols),
        'feature_cols': feature_cols,
        'model_type': args.model_type,
        'positive_rate': float(y.mean())
    }
    
    save_model(model, feature_cols, metrics, output_path)
    
    _print_safe("\n" + "=" * 60)
    _print_safe("✅ 训练完成!")
    _print_safe("=" * 60)
    
    # 使用建议
    _print_safe("\n💡 使用建议:")
    _print_safe(f"  - 当前AUC: {auc:.4f}")
    if auc < 0.55:
        _print_safe("  - ⚠️  模型预测能力较弱，建议:")
        _print_safe("    1. 收集更多历史数据（目标1000+样本）")
        _print_safe("    2. 添加市场数据特征（真实VIX、SPY等）")
        _print_safe("    3. 工程化更多期权特征")
    elif auc < 0.65:
        _print_safe("  - ✅ 模型有一定预测能力，可用于初筛")
        _print_safe("  - 建议配合其他规则使用，ML权重不超过40%")
    else:
        _print_safe("  - 🎉 模型表现良好！")
        _print_safe("  - 可作为主要筛选依据，ML权重50-70%")
    
    _print_safe(f"\n  推荐阈值: 0.5 (精确率优先)")
    _print_safe(f"  模型文件: {output_path}")


if __name__ == '__main__':
    main()
