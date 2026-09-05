# ML模型训练结果诊断报告

## 📊 训练结果总结

**训练时间**: 2026-08-28  
**模型类型**: RandomForest (LightGBM不可用，缺少libomp库)

### 数据概况
- **样本数**: 304条
- **特征数**: 13个
- **时间范围**: 2026-08-03 至 2026-08-28 (26天)
- **正样本比例**: 54.28%

### 性能指标
| 指标 | 数值 | 评价 |
|------|------|------|
| **AUC** | 0.4967 | ❌ 极差（接近随机猜测0.5） |
| **准确率** | 49% | ❌ 低于随机 |
| **精确率（盈利预测）** | 57% | ⚠️ 较低 |
| **召回率（盈利预测）** | 38% | ❌ 很低 |

### 混淆矩阵
```
              预测亏损    预测盈利
实际亏损         17         10
实际盈利         21         13
```

---

## 🔍 问题诊断

### 1. 样本量不足 ⚠️⚠️⚠️
**当前**: 304条记录  
**推荐**: 至少1000+条

**影响**:
- 训练集只有206条，测试集只有61条
- 模型无法学习到稳定的规律
- 过拟合风险高

**建议**:
```bash
# 扩大时间范围导出更多数据
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades.csv \
    --start-date 2026-05-01  # 从5月1日开始（而不是8月3日）
```

### 2. 特征质量问题 ⚠️⚠️

**特征重要性排名**:
1. `limit_price` (29.2%) - 订单限价
2. `average_fill_price` (26.8%) - 成交均价
3. `hour_of_day` (19.3%) - 交易时段
4. `day_of_week` (12.5%) - 星期几
5. `symbol_trade_count` (6.1%) - 历史交易次数

**问题**:
- 前2个重要特征都是价格，但**这是结果特征，不是预测特征**！
  - `limit_price`和`average_fill_price`在下单时还不知道
  - 模型实际上在用"成交价格"预测"是否盈利"，这是作弊
  
- 真正有预测价值的特征权重太低：
  - `symbol_win_rate` 只有1.4%
  - `symbol_avg_pnl` 只有0.4%

**建议**:
```python
# 移除结果特征
exclude_features = ['limit_price', 'average_fill_price', 'filled_quantity']

# 添加更多预测特征
new_features = [
    'underlying_volatility_20d',  # 正股波动率
    'underlying_return_5d',       # 正股近期涨跌幅
    'market_return',              # 大盘表现
    'option_days_to_expiry',      # 期权到期天数
    'option_iv_rank',             # 隐含波动率分位数
]
```

### 3. 数据泄露风险 🚨

**当前问题**: 训练数据包含了"未来信息"

- `limit_price` 和 `average_fill_price` 是订单执行后才知道的
- 模型在用"结果"预测"结果"，实际应用时无法获得这些特征

**正确做法**:
```python
# 只使用订单提交时可获得的特征
available_at_submission = [
    'symbol_win_rate',      # ✅ 历史胜率（订单前已知）
    'symbol_avg_pnl',       # ✅ 历史平均盈亏（订单前已知）
    'hour_of_day',          # ✅ 当前时间（订单前已知）
    'underlying_price',     # ✅ 正股价格（订单前已知）
    # ❌ 'limit_price',     # 错误：这是要优化的目标
    # ❌ 'average_fill_price',  # 错误：这是执行后的结果
]
```

---

## 🎯 改进方案

### 短期改进（1周内）

#### 1. 扩大数据集
```bash
# 导出更长时间范围的数据（建议3-6个月）
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades_full.csv \
    --start-date 2026-03-01  # 往前推3个月
```

#### 2. 修复特征泄露
修改 `scripts/ml_export_training_data.py`，在特征准备阶段：

```python
# 移除结果特征
exclude_cols = [
    'created_at', 'strategy_id', 'symbol', 'side', 'status',
    'metadata_json', 'trade_date', 'forward_days',
    'window_trades', 'window_buy_amount', 'window_sell_amount',
    'window_net_pnl', 'is_profitable',
    # 新增：移除结果特征
    'limit_price', 'average_fill_price', 'filled_quantity'
]
```

#### 3. 调整预测窗口
```bash
# 尝试不同的预测窗口
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades_3d.csv \
    --forward-days 3  # 从7天改为3天，可能更容易预测
```

### 中期改进（2-4周）

#### 1. 添加市场数据特征
创建 `scripts/ml_extract_market_features.py`：

```python
def add_market_features(df, broker):
    """添加市场数据特征"""
    for idx, row in df.iterrows():
        symbol = row['symbol']
        
        # 提取正股代码
        underlying = symbol.split('C')[0].split('P')[0]
        
        # 获取正股历史数据
        bars = broker.candles(underlying, count=60)
        
        # 计算特征
        df.at[idx, 'underlying_volatility_20d'] = bars['close'].pct_change().rolling(20).std()
        df.at[idx, 'underlying_return_5d'] = bars['close'].iloc[-1] / bars['close'].iloc[-6] - 1
        df.at[idx, 'underlying_volume_ratio'] = bars['volume'].iloc[-1] / bars['volume'].rolling(20).mean().iloc[-1]
    
    return df
```

#### 2. 使用正确的模型（修复LightGBM）
```bash
# 安装OpenMP库
brew install libomp

# 重新训练
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/volatility_trades_full.csv \
    --model-output data/ml_models/volatility_scorer_v2.pkl
```

LightGBM通常比RandomForest效果好10-20%。

### 长期改进（1-3个月）

1. **期权Greeks特征**: Delta, Gamma, Vega, Theta
2. **技术指标**: RSI, MACD, 布林带
3. **情绪指标**: VIX, Put/Call Ratio
4. **集成学习**: 组合多个模型

---

## 💡 临时解决方案

### 方案A: 使用规则打分（推荐）

在收集更多数据前，使用简单规则打分：

```python
def rule_based_score(symbol: str, store) -> float:
    """基于历史统计的规则打分"""
    # 查询历史数据
    history = get_symbol_history(symbol, store)
    
    if len(history) < 5:
        return 0.5  # 数据不足，中性分数
    
    # 计算关键指标
    win_rate = (history['pnl'] > 0).mean()
    avg_pnl = history['pnl'].mean()
    recent_trend = history.tail(5)['pnl'].mean()
    
    # 加权评分
    score = (
        0.4 * win_rate +                          # 40%权重：历史胜率
        0.3 * normalize(avg_pnl, -2, 2) +         # 30%权重：平均盈亏
        0.3 * normalize(recent_trend, -2, 2)      # 30%权重：近期趋势
    )
    
    return max(0, min(1, score))

def normalize(value, min_val, max_val):
    """归一化到0-1"""
    return (value - min_val) / (max_val - min_val)
```

### 方案B: 降低使用ML的期望

当前模型虽然AUC低，但仍可用于：
- **粗筛选**: 过滤掉明显差的标的（分数<0.3）
- **排序**: 优先处理高分标的
- **A/B测试**: 对比ML vs 规则的实际效果

不要期望模型完美预测，把它当作**辅助工具**。

---

## 📋 立即行动清单

### 优先级P0（本周）
- [ ] 修复特征泄露：移除limit_price和average_fill_price
- [ ] 扩大数据集：导出3-6个月的历史数据
- [ ] 重新训练模型，查看AUC是否提升

### 优先级P1（2周内）
- [ ] 添加市场数据特征（正股波动率、涨跌幅）
- [ ] 尝试不同预测窗口（3天、5天、7天）
- [ ] 修复LightGBM依赖（brew install libomp）

### 优先级P2（1个月内）
- [ ] 实现规则打分作为基准对比
- [ ] 添加期权特征（Greeks、IV Rank）
- [ ] 开发监控面板跟踪模型效果

---

## 🎓 经验教训

### 机器学习不是万能的

1. **数据质量 > 模型复杂度**
   - 300条数据 + 复杂模型 < 1000条数据 + 简单模型

2. **特征工程是关键**
   - 好特征 + 简单模型 > 差特征 + 复杂模型
   - 必须避免数据泄露

3. **循序渐进**
   - 先用规则打分建立基准
   - 再用简单ML（如逻辑回归）验证
   - 最后才用复杂模型（树模型、神经网络）

### 波动率策略的特殊性

期权交易本身就很随机，可能：
- 市场效率高，难以预测
- 波动率策略依赖运气成分大
- 历史规律在未来不一定有效

**建议**: 将ML作为辅助工具，而不是完全依赖。

---

## ✅ 结论

当前模型**不建议直接使用**，因为：
1. ❌ AUC=0.50，等同随机猜测
2. ❌ 存在数据泄露（使用了结果特征）
3. ❌ 样本量太少（304条）

**下一步**:
1. 修复特征泄露问题
2. 扩大数据集至1000+条
3. 重新训练并评估

预计修复后，AUC可提升至0.60-0.65（基本可用）。如果仍然<0.60，建议使用规则打分方案。
