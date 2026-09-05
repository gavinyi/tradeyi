# ML模型训练完整运行报告

**运行时间**: 2026-08-29  
**版本**: v2_clean（修复特征泄露后）

---

## ✅ 执行流程

### Step 1: 修复特征泄露
- ✅ 从导出查询中移除 `limit_price`
- ✅ 在训练脚本中显式排除 `average_fill_price`, `filled_quantity`, `limit_price`

### Step 2: 导出训练数据
```bash
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades_fixed.csv \
    --start-date 2026-06-01 \
    --forward-days 7
```

**结果**:
- 样本数: 304条
- 时间范围: 2026-08-03 至 2026-08-28（仍然只有26天）
- 正负样本: 54.3% 盈利 / 45.7% 亏损
- 特征数: 25列（移除结果特征前）

### Step 3: 训练模型
```bash
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/volatility_trades_fixed.csv \
    --model-output data/ml_models/volatility_scorer_v2_clean.pkl
```

---

## 📊 模型性能对比

| 指标 | v1 (有数据泄露) | v2_clean (修复后) | 变化 |
|------|----------------|-------------------|------|
| **AUC** | 0.4967 | **0.5338** | +0.037 ✅ |
| **准确率** | 49% | **52%** | +3% ✅ |
| **精确率（盈利）** | 57% | **69%** | +12% ✅ |
| **召回率（盈利）** | 38% | **26%** | -12% ⚠️ |

### 混淆矩阵对比

**v1 (有数据泄露)**:
```
          预测亏损  预测盈利
实际亏损      17      10
实际盈利      21      13
```

**v2_clean (修复后)**:
```
          预测亏损  预测盈利
实际亏损      23       4     ← 假阳性减少！
实际盈利      25       9
```

**改进**:
- ✅ 假阳性从10降至4（减少60%）
- ✅ 真阴性从17升至23（提升35%）
- ⚠️ 假阴性从21升至25（略有上升）

---

## 🔍 特征重要性分析

### v1 (有数据泄露)
1. **limit_price** (29.2%) ❌ 结果特征
2. **average_fill_price** (26.8%) ❌ 结果特征
3. hour_of_day (19.3%)
4. day_of_week (12.5%)

### v2_clean (修复后)
1. **hour_of_day** (39.5%) ✅ 预测特征
2. **day_of_week** (28.7%) ✅ 预测特征
3. **symbol_trade_count** (15.1%) ✅ 历史特征
4. symbol_max_pnl (4.0%)
5. symbol_avg_pnl (3.5%)
6. symbol_7d_avg_pnl (3.4%)
7. symbol_win_rate (2.8%)

**分析**:
- ✅ 结果特征已完全移除
- ✅ 时间特征（hour, day_of_week）占主导（68%）
- ⚠️ 历史盈亏特征权重较低（<15%）

---

## 📈 不同阈值的性能

| 阈值 | 精确率 | 召回率 | 说明 |
|------|--------|--------|------|
| 0.4 | 52.6% | 88.2% | 宽松：多预测盈利 |
| 0.5 | **69.2%** | 26.5% | 推荐：精确率高 |
| 0.6 | 100% | 8.8% | 严格：极少预测盈利 |

**推荐策略**:
- 使用阈值 **0.5** 作为过滤标准
- 预测为盈利时，有69%的准确率
- 但会错过73%的真实盈利机会

---

## ⚠️ 当前问题

### 1. 样本量仍然不足
- **当前**: 304条（26天）
- **需要**: 1000+条（3-6个月）
- **影响**: 模型泛化能力弱

**原因分析**:
查询从2026-06-01开始，但实际只有8月3日之后的数据。说明：
- 波动率策略在6-7月没有运行
- 或数据库中没有更早的记录

**解决方案**:
```bash
# 检查数据库中最早的记录
sqlite3 data/trading.db "SELECT MIN(created_at) FROM order_intents WHERE strategy_id='volatility_long_gamma'"
```

### 2. 特征仍然不够丰富
**当前特征**:
- 时间特征: hour_of_day, day_of_week
- 历史统计: symbol_win_rate, symbol_avg_pnl等

**缺少的关键特征**:
- ❌ 正股市场数据（波动率、涨跌幅）
- ❌ 期权特征（到期天数、Greeks、IV Rank）
- ❌ 市场环境（VIX、大盘走势）

### 3. 时间特征占主导
**问题**: hour_of_day + day_of_week 占68%权重

**可能原因**:
- 策略主要在特定时段交易（收盘前）
- 历史特征信息量不足
- 市场周期性明显

**风险**: 过度依赖时间特征，容易过拟合

---

## 💡 改进建议

### 短期（本周）
1. ✅ **已完成**: 修复特征泄露
2. ⚠️ **待做**: 扩大数据集
   ```bash
   # 查找数据库中的实际日期范围
   sqlite3 data/trading.db "
   SELECT 
       DATE(MIN(created_at)) as earliest,
       DATE(MAX(created_at)) as latest,
       COUNT(*) as total
   FROM order_intents 
   WHERE strategy_id='volatility_long_gamma'
   "
   ```

### 中期（2-4周）
1. **添加市场数据特征**
   - 从broker获取正股K线数据
   - 计算波动率、动量、成交量指标
   
2. **添加期权特征**
   - 解析期权代码提取到期天数
   - 计算实值/虚值程度（moneyness）
   
3. **安装LightGBM**
   ```bash
   brew install libomp
   # 重新训练，预期AUC提升5-10%
   ```

### 长期（1-3个月）
1. 收集更多历史数据（目标1000+条）
2. 开发在线学习机制（增量更新模型）
3. 实施A/B测试对比效果

---

## 🎯 使用建议

### 当前模型可用性评估

**AUC 0.53** 的含义：
- ✅ 略好于随机猜测（0.5）
- ⚠️ 预测能力很弱
- ❌ 不建议作为主要决策依据

**推荐用法**:

#### 方案A: 粗筛选（推荐）
```python
# 过滤掉明显差的标的
candidates = all_symbols
good_symbols = [s for s in candidates if ml_score(s) >= 0.4]

# ML作为第一道筛选，其他因素作为最终决策
```

#### 方案B: 排序参考
```python
# 按ML分数排序，优先处理高分标的
ranked = sorted(symbols, key=lambda s: ml_score(s), reverse=True)
top_picks = ranked[:20]  # 取前20个
```

#### 方案C: 规则组合
```python
def final_score(symbol):
    ml_score = model.predict(symbol)  # 0-1
    rule_score = calculate_rule_score(symbol)  # 0-1
    
    # 加权组合
    return 0.3 * ml_score + 0.7 * rule_score  # ML权重30%
```

---

## 📋 文件清单

### 训练数据
- ✅ `data/ml_training/volatility_trades_fixed.csv` - 修复后的训练数据（304条）

### 模型文件
- ⚠️ `data/ml_models/volatility_scorer_v1.pkl` - 旧版本（有数据泄露）
- ⚠️ `data/ml_models/volatility_scorer_v2.pkl` - 中间版本
- ✅ **`data/ml_models/volatility_scorer_v2_clean.pkl`** - 当前最佳版本
- ✅ `data/ml_models/volatility_scorer_v2_clean.json` - 模型指标

### 代码更新
- ✅ `scripts/ml_export_training_data.py` - 已更新注释
- ✅ `scripts/ml_train_symbol_scorer.py` - 已排除结果特征

---

## ✅ 总结

### 完成的工作
1. ✅ 识别并修复特征泄露问题
2. ✅ 重新训练模型，AUC从0.50提升至0.53
3. ✅ 验证特征正确性（无结果特征）
4. ✅ 生成完整的性能报告

### 当前状态
- **模型版本**: v2_clean
- **AUC**: 0.53（略好于随机）
- **可用性**: 可作为辅助筛选，不建议作为主要依据
- **主要限制**: 样本量不足（304条）

### 下一步行动
1. **优先**: 扩大训练数据集至1000+条
2. **重要**: 添加市场和期权特征
3. **可选**: 安装LightGBM提升性能

### 预期改进路径
- 当前: AUC 0.53（304样本，10特征）
- 扩大数据集: AUC 0.60（1000+样本，10特征）
- 增加特征: AUC 0.65（1000+样本，30+特征）
- 使用LightGBM: AUC 0.68（1000+样本，30+特征）

---

**最后更新**: 2026-08-29  
**模型文件**: `data/ml_models/volatility_scorer_v2_clean.pkl`  
**状态**: ⚠️ 可用但需优化
