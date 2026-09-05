# 机器学习标的筛选 - 快速入门指南

## 前提条件

确保已有足够的历史交易数据（建议至少1个月以上）

## 步骤1: 安装ML依赖

```bash
# 方式1: 安装lightgbm（推荐）
uv sync --extra ml

# 方式2: 如果lightgbm安装失败，可以只用sklearn
uv sync --extra dev
```

## 步骤2: 导出训练数据

```bash
# 导出最近3个月的交易数据
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades.csv \
    --start-date 2026-06-01 \
    --forward-days 7

# 检查导出的数据
head -20 data/ml_training/volatility_trades.csv
```

**预期输出**：
- ✅ 至少300+条交易记录
- ✅ 正负样本比例在30%-70%之间
- ✅ 包含symbol_avg_pnl, symbol_win_rate等特征列

## 步骤3: 训练模型

```bash
# 训练LightGBM模型
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/volatility_trades.csv \
    --model-output data/ml_models/volatility_scorer_v1.pkl

# 查看模型指标
cat data/ml_models/volatility_scorer_v1.json
```

**关键指标**：
- **AUC ≥ 0.65**: 模型有区分能力，可以使用
- **精确率 ≥ 60%**: 预测为盈利时，60%真的盈利
- **召回率 ≥ 55%**: 真实盈利的标的，55%被正确预测

如果指标太低（AUC < 0.6），说明：
1. 数据量不足（需要更多历史数据）
2. 特征不够丰富（需要添加更多特征）
3. 市场环境变化大（模型难以泛化）

## 步骤4: 测试模型（可选）

在Python中测试模型是否正常工作：

```python
from src.futu_quant.ml_symbol_scorer import MLSymbolScorer
from src.futu_quant.broker import FutuPaperBroker
from src.futu_quant.store import TradingStore
from src.futu_quant.config import Settings

# 初始化
settings = Settings.load()
broker = FutuPaperBroker(settings)
store = TradingStore("data/trading.db")

# 加载评分器
scorer = MLSymbolScorer("data/ml_models/volatility_scorer_v1.pkl")

# 为标的打分
test_symbols = ["US.AAPL", "US.NVDA", "US.TSLA"]
for symbol in test_symbols:
    score = scorer.score_symbol(symbol, broker, store)
    print(f"{symbol}: {score:.3f}")

# 批量排序
ranked = scorer.rank_symbols(test_symbols, broker, store)
print(ranked)
```

## 步骤5: 集成到策略（修改代码）

### 方式1: 手动修改扫描器（推荐用于理解原理）

编辑 `src/futu_quant/volatility_scanner.py`：

```python
# 在文件开头添加导入
from .ml_symbol_scorer import MLSymbolScorer

# 在scan_volatility_watchlist函数末尾添加
def scan_volatility_watchlist(broker, store, symbols=None):
    # ... 原有代码 ...
    
    # === 新增：ML评分 ===
    try:
        scorer = MLSymbolScorer()
        if scorer.enabled:
            for result in results:
                symbol = result['symbol']
                ml_score = scorer.score_symbol(symbol, broker, store)
                result['ml_score'] = ml_score
                result['ml_rank'] = 'HIGH' if ml_score >= 0.7 else \
                                   'MEDIUM' if ml_score >= 0.5 else 'LOW'
            
            # 按ML分数重新排序
            results = sorted(results, key=lambda x: x.get('ml_score', 0.5), reverse=True)
            
            store.audit("ML_SCORING_COMPLETED", {
                "symbols_scored": len(results),
                "high_score_count": sum(1 for r in results if r.get('ml_score', 0) >= 0.7)
            }, "volatility_scanner")
    except Exception as e:
        store.audit("ML_SCORING_FAILED", {"error": str(e)}, "volatility_scanner")
    
    return results
```

### 方式2: 使用配置开关（推荐用于生产）

添加配置到 `.env`:

```bash
# ML模型配置
ML_ENABLED=true
ML_MODEL_PATH=data/ml_models/volatility_scorer_v1.pkl
ML_MIN_SCORE=0.5
ML_FALLBACK_ON_ERROR=true
```

## 步骤6: 回测验证（强烈推荐）

在实际使用前，先用历史数据验证效果：

```bash
# TODO: 创建回测脚本
uv run python scripts/ml_backtest_with_model.py \
    --model data/ml_models/volatility_scorer_v1.pkl \
    --start-date 2026-08-01 \
    --end-date 2026-08-28
```

对比使用ML前后的表现：
- **总盈亏**：是否从负转正
- **胜率**：是否提升
- **最大回撤**：是否降低

## 步骤7: 实盘运行

### SHADOW模式（推荐第1周）

只记录决策，不实际下单：

```bash
# 在配置中设置
EXECUTION_MODE=SHADOW
ML_ENABLED=true

# 运行服务
uv run python scripts/run_service.py
```

观察1周，检查：
- ML评分是否合理
- 高分标的是否真的表现好
- 有无异常报错

### PAPER模式（推荐第2-3周）

真实纸面交易：

```bash
# 切换到PAPER模式
EXECUTION_MODE=PAPER
ML_ENABLED=true

# 运行服务
uv run python scripts/run_service.py
```

对比使用ML前后的实际盈亏。

### 正式使用（第4周+）

如果效果验证良好，继续使用PAPER模式，或根据风险偏好调整策略参数。

## 常见问题

### Q1: 模型训练失败，提示"样本不足"

**原因**：历史交易数据太少（< 100条）

**解决**：
1. 等待积累更多数据（建议至少300条）
2. 调整`--forward-days`参数（从7改为3或14）
3. 降低训练复杂度（减少`max_depth`等参数）

### Q2: AUC只有0.5左右，等于随机猜测

**原因**：
1. 特征信息量不足
2. 标的表现过于随机
3. 市场环境变化太快

**解决**：
1. 添加更多特征（市场数据、期权数据）
2. 收集更长时间的数据
3. 考虑使用更简单的规则策略

### Q3: 模型在训练集上表现好，但实盘差

**原因**：过拟合

**解决**：
1. 增加正则化（降低`max_depth`，增加`min_child_samples`）
2. 使用更多数据训练
3. 简化特征工程

### Q4: ML评分器加载失败

**检查**：
1. 模型文件是否存在：`ls -lh data/ml_models/`
2. 文件是否损坏：`file data/ml_models/volatility_scorer_v1.pkl`
3. 依赖是否安装：`uv pip list | grep lightgbm`

### Q5: 如何更新模型

**手动更新**（每月一次）：
```bash
# 1. 导出最新数据
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades_new.csv

# 2. 重新训练
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/volatility_trades_new.csv \
    --model-output data/ml_models/volatility_scorer_v2.pkl

# 3. 验证新模型
# 对比v1和v2的指标，如果v2更好，则替换

# 4. 备份旧模型并部署新模型
mv data/ml_models/volatility_scorer_v1.pkl data/ml_models/volatility_scorer_v1.pkl.bak
mv data/ml_models/volatility_scorer_v2.pkl data/ml_models/volatility_scorer_v1.pkl
```

**自动更新**（TODO）：
```bash
# 设置定时任务，每月1号自动重训练
# crontab -e
# 0 2 1 * * cd /path/to/trade && uv run python scripts/ml_auto_retrain.py
```

## 监控与优化

### 监控指标

创建监控面板（Streamlit页面）来跟踪：

1. **预测准确率**：最近30天的准确率趋势
2. **特征重要性**：哪些特征对预测最重要
3. **分数分布**：当前标的池的分数分布
4. **盈亏对比**：高分vs低分标的的实际表现

### 优化建议

1. **特征优化**：
   - 添加期权Greeks（Delta、Gamma、Vega）
   - 添加市场情绪指标（VIX、恐慌指数）
   - 添加行业动量特征

2. **模型优化**：
   - 尝试XGBoost、CatBoost等其他模型
   - 集成多个模型（投票/加权平均）
   - 针对不同行业训练专门模型

3. **策略优化**：
   - 动态调整分数阈值
   - 根据市场环境切换模型
   - 结合其他信号（技术指标、基本面）

## 下一步

- [ ] 添加更多市场数据特征
- [ ] 开发自动回测框架
- [ ] 实现模型A/B测试
- [ ] 创建Streamlit监控页面
- [ ] 设置自动重训练流程

---

**重要提醒**：机器学习不是万能的，始终要：
1. 保持合理预期（提升10-20%已经很好）
2. 持续监控效果
3. 做好风险控制
4. 不要过度依赖单一模型
