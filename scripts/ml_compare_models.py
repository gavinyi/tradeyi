#!/usr/bin/env python3
"""
对比v2和v3模型的性能
"""

import pickle
import json
from pathlib import Path
import pandas as pd


def _print_safe(*args, **kwargs) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑脚本会直接崩，`pytest -s` 也会假红。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)

def load_model(path):
    """加载模型"""
    with open(path, 'rb') as f:
        return pickle.load(f)

def load_metrics(path):
    """加载指标"""
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 70)
    _print_safe("📊 ML模型版本对比")
    print("=" * 70)
    
    models_dir = Path('data/ml_models')
    
    # 加载v2_clean
    v2_path = models_dir / 'volatility_scorer_v2_clean.pkl'
    v2_metrics_path = models_dir / 'volatility_scorer_v2_clean.json'
    
    # 加载v3
    v3_path = models_dir / 'volatility_scorer_v3.pkl'
    v3_metrics_path = models_dir / 'volatility_scorer_v3.json'
    
    if not v2_path.exists():
        _print_safe("⚠️  v2_clean模型不存在，跳过对比")
        v2_metrics = None
    else:
        v2_metrics = load_metrics(v2_metrics_path)
    
    if not v3_path.exists():
        _print_safe("❌ v3模型不存在")
        return
    
    v3_metrics = load_metrics(v3_metrics_path)
    
    # 对比表格
    _print_safe("\n📈 性能对比:")
    print("-" * 70)
    print(f"{'指标':<20} {'v2_clean':<15} {'v3':<15} {'提升':<15}")
    print("-" * 70)
    
    if v2_metrics:
        # AUC
        v2_auc = v2_metrics.get('auc', 0.534)
        v3_auc = v3_metrics['auc']
        auc_improvement = (v3_auc - v2_auc) / v2_auc * 100
        print(f"{'AUC':<20} {v2_auc:<15.4f} {v3_auc:<15.4f} {auc_improvement:>+14.1f}%")
        
        # 特征数
        v2_features = len(v2_metrics.get('feature_cols', []))
        v3_features = v3_metrics['n_features']
        print(f"{'特征数':<20} {v2_features:<15} {v3_features:<15} {v3_features - v2_features:>+14}")
        
        # 样本数
        v2_samples = v2_metrics.get('test_size', 61)
        v3_samples = v3_metrics['test_size']
        print(f"{'测试集大小':<20} {v2_samples:<15} {v3_samples:<15} {v3_samples - v2_samples:>+14}")
    else:
        print(f"{'AUC':<20} {'N/A':<15} {v3_metrics['auc']:<15.4f} {'N/A':<15}")
        print(f"{'特征数':<20} {'N/A':<15} {v3_metrics['n_features']:<15} {'N/A':<15}")
    
    print("-" * 70)
    
    # v3特征列表
    _print_safe("\n🔧 v3模型特征 ({}个):".format(v3_metrics['n_features']))
    for i, feat in enumerate(v3_metrics['feature_cols'], 1):
        print(f"  {i:2d}. {feat}")
    
    # 新增特征
    if v2_metrics:
        v2_features_set = set(v2_metrics.get('feature_cols', []))
        v3_features_set = set(v3_metrics['feature_cols'])
        
        new_features = v3_features_set - v2_features_set
        removed_features = v2_features_set - v3_features_set
        
        if new_features:
            _print_safe(f"\n✅ v3新增特征 ({len(new_features)}个):")
            for feat in sorted(new_features):
                print(f"  + {feat}")
        
        if removed_features:
            _print_safe(f"\n❌ v3移除特征 ({len(removed_features)}个):")
            for feat in sorted(removed_features):
                print(f"  - {feat}")
    
    # 总结
    print("\n" + "=" * 70)
    _print_safe("📝 总结:")
    print("=" * 70)
    
    if v2_metrics:
        if v3_auc > 0.75:
            _print_safe("🎉 v3模型表现优秀！AUC > 0.75")
            print("   - 可作为主要筛选依据")
            print("   - 推荐ML权重: 50-70%")
        elif v3_auc > 0.65:
            _print_safe("✅ v3模型表现良好！AUC > 0.65")
            print("   - 可用于初筛")
            print("   - 推荐ML权重: 30-50%")
        else:
            _print_safe("⚠️  v3模型仍需改进")
            print("   - 建议作为辅助参考")
            print("   - 推荐ML权重: 20-30%")
        
        print(f"\n   v2→v3提升: {auc_improvement:+.1f}%")
    else:
        _print_safe("✅ v3模型已生成")
        print(f"   AUC: {v3_metrics['auc']:.4f}")
    
    _print_safe(f"\n📁 模型文件:")
    print(f"   v3: {v3_path}")
    print(f"   指标: {v3_metrics_path}")


if __name__ == '__main__':
    main()
