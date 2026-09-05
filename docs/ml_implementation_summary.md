# 机器学习标的筛选方案 - 实施总结

## 📦 已创建的文件

### 1. 设计文档
- **`docs/ml_symbol_selection.md`** - 完整的ML方案设计文档
  - 问题定义、数据准备、模型架构
  - 特征工程、训练流程、集成方案
  - 风险控制、监控优化

- **`docs/ml_quickstart.md`** - 快速入门指南
  - 7步实施流程
  - 常见问题FAQ
  - 监控与优化建议

### 2. 核心代码
- **`src/futu_quant/ml_symbol_scorer.py`** - ML评分器模块
  - `MLSymbolScorer` 类：加载模型、提取特征、打分
  - `allocate_capital_by_ml_score()` 函数：动态资金分配
  - 自动降级到规则打分（ML失效时）

### 3. 工具脚本
- **`scripts/ml_export_training_data.py`** - 导出训练数据
  - 从trading.db提取历史交易
  - 计算盈亏标签（7天窗口）
  - 计算历史统计特征

- **`scripts/ml_train_symbol_scorer.py`** - 训练模型
  - 支持LightGBM和RandomForest
  - 自动特征标准化
  - 生成评估报告和特征重要性

- **`scripts/ml_test_scorer.py`** - 测试评分器
  - 验证模型加载
  - 测试单个/批量评分
  - 检查降级机制

### 4. 配置更新
- **`pyproject.toml`** - 添加ML依赖
  ```toml
  [project.optional-dependencies]
  ml = ["lightgbm>=4.0", "scikit-learn>=1.3", "xgboost>=2.0", "shap>=0.42"]
  ```

---

## 🎯 核心设计思路

### 问题建模
将标的筛选转化为**二分类问题**：
- **输入**：标的的历史表现、市场特征、时间特征
- **输出**：未来7天该标的是否盈利（0/1）

### 特征工程
| 类别 | 特征示例 | 重要性 |
|------|---------|--------|
| 历史收益 | `symbol_7d_return`, `symbol_win_rate` | ⭐⭐⭐⭐⭐ |
| 市场特征 | `underlying_volatility_20d`, `vix_level` | ⭐⭐⭐⭐ |
| 期权特征 | `option_days_to_expiry`, `option_iv_rank` | ⭐⭐⭐⭐⭐ |
| 时间特征 | `day_of_week`, `hour_of_day` | ⭐⭐ |

### 模型架构
```
历史交易数据 
    ↓ (特征提取)
特征矩阵 (N样本 × M特征)
    ↓ (标准化)
LightGBM/XGBoost
    ↓ (预测)
盈利概率 (0-1)
    ↓ (排序/过滤)
优质标的列表
```

### 集成策略
```python
# 在波动率扫描器中使用
results = scan_volatility_watchlist(broker, store, symbols)

scorer = MLSymbolScorer()
for result in results:
    result['ml_score'] = scorer.score_symbol(result['symbol'], broker, store)

# 按ML分数重新排序
results = sorted(results, key=lambda x: x['ml_score'], reverse=True)
```

---

## 🚀 快速开始（3步走）

### Step 1: 安装依赖
```bash
uv sync --extra ml
```

### Step 2: 准备数据并训练
```bash
# 导出训练数据
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades.csv \
    --start-date 2026-06-01

# 训练模型
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/volatility_trades.csv \
    --model-output data/ml_models/volatility_scorer_v1.pkl
```

### Step 3: 测试验证
```bash
# 测试评分器
uv run python scripts/ml_test_scorer.py

# 检查是否正常工作
# 预期看到：各标的的ML分数(0-1)和排名(HIGH/MEDIUM/LOW)
```

---

## 📊 预期效果

### 当前问题
- ❌ 累计净亏损：-$25.37
- ❌ 部分标的持续亏损（COIN -$5.40, AMD -$5.20）
- ❌ 143个标的中只有少数真正盈利

### 使用ML后预期改善
| 指标 | 现状 | 目标 | 改善 |
|------|------|------|------|
| 累计盈亏 | -$25.37 | **+$50** | 转负为正 |
| 胜率 | 83.5% | **85%+** | +1.5% |
| 平均盈利 | -$0.08/笔 | **+$0.15/笔** | 翻正 |
| 单笔最大亏损 | -$5.20 | **-$2.00** | -60% |
| 交易标的数 | 143个 | **50-80个** | 精简45% |

### 时间表
- **1周内**：减少30%亏损标的交易
- **1个月内**：整体转为盈利
- **3个月内**：累计+$50盈利

---

## 🛡️ 风险控制

### 1. 降级机制
```python
if ML模型失败:
    → 自动切换到规则打分
    → 记录审计日志
    → 不影响策略继续运行
```

### 2. 分阶段部署
```
SHADOW模式(1周) → 观察不下单
    ↓
PAPER模式(2周) → 纸面验证
    ↓
正式使用 → 持续监控
```

### 3. 性能监控
- 每日计算预测准确率
- 对比高分vs低分标的实际表现
- 追踪特征重要性变化
- 设置准确率告警（< 60%）

### 4. 自动更新
```python
if 距离上次训练 > 30天 OR 准确率 < 60% OR 新数据 > 500条:
    自动触发重训练
```

---

## 🔧 高级功能（可选）

### 1. 添加更多特征
```python
# 期权Greeks
features['delta'] = get_option_delta(symbol)
features['gamma'] = get_option_gamma(symbol)
features['vega'] = get_option_vega(symbol)

# 市场情绪
features['vix'] = get_vix_level()
features['put_call_ratio'] = get_put_call_ratio()

# 行业动量
features['sector_return'] = get_sector_return(symbol)
```

### 2. 集成多个模型
```python
from sklearn.ensemble import VotingClassifier

ensemble = VotingClassifier([
    ('lgb', lgb_model),
    ('xgb', xgb_model),
    ('rf', rf_model)
], voting='soft', weights=[2, 2, 1])
```

### 3. 针对不同市场环境
```python
if vix < 15:  # 低波动环境
    model = low_vol_model
elif vix > 25:  # 高波动环境
    model = high_vol_model
else:  # 正常环境
    model = normal_model
```

### 4. A/B测试
```python
# 50%使用ML，50%使用原规则，对比效果
if random.random() < 0.5:
    symbols = ml_select(candidates)  # A组
else:
    symbols = rule_select(candidates)  # B组
```

---

## 📝 下一步行动清单

### 立即执行（本周）
- [ ] 安装ML依赖：`uv sync --extra ml`
- [ ] 导出训练数据并检查质量
- [ ] 训练第一个基准模型
- [ ] 运行测试脚本验证功能

### 短期优化（2-4周）
- [ ] 在SHADOW模式下运行1周
- [ ] 分析预测准确率和特征重要性
- [ ] 切换到PAPER模式实测
- [ ] 根据结果调整阈值和参数

### 中期改进（1-3个月）
- [ ] 添加期权Greeks特征
- [ ] 收集更多历史数据重训练
- [ ] 开发Streamlit监控页面
- [ ] 实现自动重训练流程

### 长期规划（3-6个月）
- [ ] 构建模型评估体系
- [ ] 开发多策略专用模型
- [ ] 实现自动特征工程
- [ ] 研究深度学习方案

---

## 🤝 技术支持

### 文档参考
1. **设计方案**：`docs/ml_symbol_selection.md`
2. **快速入门**：`docs/ml_quickstart.md`
3. **策略架构**：`docs/strategy_architecture.md`

### 代码参考
1. **评分器**：`src/futu_quant/ml_symbol_scorer.py`
2. **训练脚本**：`scripts/ml_train_symbol_scorer.py`
3. **数据导出**：`scripts/ml_export_training_data.py`

### 调试技巧
```python
# 查看特征重要性
import pickle
with open('data/ml_models/volatility_scorer_v1.pkl', 'rb') as f:
    checkpoint = pickle.load(f)
    importance = checkpoint['model'].feature_importances_
    print(dict(zip(checkpoint['feature_names'], importance)))

# 查看预测详情
scorer = MLSymbolScorer()
features = scorer.extract_features('US.AAPL', broker, store)
print(features)  # 看看提取了哪些特征
```

---

## 💡 核心理念

**机器学习不是魔法**，而是：
1. ✅ 从历史数据中学习规律
2. ✅ 自动化繁琐的统计分析
3. ✅ 处理多维度特征组合
4. ✅ 持续优化决策质量

**成功的关键**：
- 🎯 清晰的问题定义
- 📊 高质量的训练数据
- 🔧 科学的特征工程
- 🛡️ 完善的风险控制
- 📈 持续的监控优化

---

## ⚠️ 重要提醒

1. **保持合理预期**
   - ML能提升10-20%已经很好
   - 不要期望一夜暴富
   - 市场永远充满不确定性

2. **持续监控**
   - 每周检查准确率
   - 对比预测vs实际
   - 及时调整参数

3. **做好风险控制**
   - 设置止损上限
   - 分散投资标的
   - 保留人工干预权

4. **不要过度依赖**
   - ML是辅助工具，不是全部
   - 结合其他信号（技术面、基本面）
   - 保持策略多样性

---

**祝交易顺利！📈**
