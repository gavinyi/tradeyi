# 机器学习标的筛选模型设计方案

## 一、问题定义

### 1.1 核心目标
为波动率期权策略构建机器学习模型，**预测标的未来7天的交易盈利概率**，从而优化标的筛选和资源配置。

### 1.2 当前痛点
- 部分标的（COIN、AMD）持续亏损，拖累整体收益
- 标的池143个标的中，只有少数真正盈利
- 缺乏量化的标的质量评分机制
- 无法动态调整各标的的资金权重

### 1.3 目标指标
- **准确率**: ≥70% (预测盈利/亏损的准确度)
- **精确率**: ≥65% (预测为盈利时的真实盈利比例)
- **召回率**: ≥60% (实际盈利的标的中被正确预测的比例)
- **投资回报**: 使用模型后整体盈利率提升至正值

---

## 二、数据准备

### 2.1 训练数据来源

#### 历史交易数据（主要数据源）
```sql
-- 从trading.db提取特征
SELECT 
    strategy_id,
    symbol,
    DATE(created_at) as trade_date,
    side,
    quantity,
    limit_price,
    average_fill_price,
    status,
    filled_quantity
FROM order_intents
WHERE strategy_id = 'volatility_long_gamma'
AND status = 'FILLED'
```

#### 市场数据（补充数据源）
- 正股历史价格（K线数据）
- 期权隐含波动率IV
- 期权希腊值（Delta、Gamma、Vega、Theta）
- 市场整体波动率（VIX指数）

### 2.2 特征工程

#### 基础特征（从历史数据直接提取）
| 特征类别 | 特征名称 | 说明 | 重要性预估 |
|---------|---------|------|-----------|
| **历史收益** | `symbol_7d_return` | 标的过去7天平均收益率 | ⭐⭐⭐⭐⭐ |
| | `symbol_30d_return` | 标的过去30天平均收益率 | ⭐⭐⭐⭐ |
| | `symbol_win_rate` | 标的历史胜率 | ⭐⭐⭐⭐⭐ |
| | `symbol_avg_profit` | 标的平均盈利金额 | ⭐⭐⭐⭐ |
| | `symbol_max_drawdown` | 标的最大回撤 | ⭐⭐⭐ |
| **市场特征** | `underlying_volatility_20d` | 正股20日波动率 | ⭐⭐⭐⭐⭐ |
| | `underlying_return_5d` | 正股近5日涨跌幅 | ⭐⭐⭐⭐ |
| | `underlying_volume_ratio` | 正股成交量相对均值 | ⭐⭐⭐ |
| | `market_return` | 大盘(SPY)当日涨跌幅 | ⭐⭐⭐ |
| | `vix_level` | VIX恐慌指数水平 | ⭐⭐⭐⭐ |
| **期权特征** | `option_days_to_expiry` | 距离到期天数 | ⭐⭐⭐⭐⭐ |
| | `option_moneyness` | 期权实值/虚值程度 | ⭐⭐⭐⭐ |
| | `option_iv_rank` | 隐含波动率分位数 | ⭐⭐⭐⭐⭐ |
| | `option_bid_ask_spread` | 期权买卖价差 | ⭐⭐⭐ |
| | `option_open_interest` | 期权未平仓量 | ⭐⭐⭐ |
| **时间特征** | `day_of_week` | 星期几（1-5） | ⭐⭐ |
| | `hour_of_day` | 交易时段（开盘/盘中/收盘） | ⭐⭐⭐ |
| | `days_since_earnings` | 距离上次财报天数 | ⭐⭐⭐⭐ |
| **行业特征** | `sector` | 行业分类（科技/金融/能源等） | ⭐⭐⭐ |
| | `sector_momentum` | 行业板块动量 | ⭐⭐⭐ |

#### 衍生特征（计算生成）
```python
# 相对强弱
relative_strength = underlying_return_5d - market_return

# 波动率突破
volatility_breakout = (underlying_volatility_20d > underlying_volatility_60d_mean + 1.5 * std)

# 动量信号
momentum_score = underlying_return_5d * volume_ratio * (1 if volatility_20d > volatility_60d else 0.5)

# 期权性价比
option_value_score = option_iv_rank * (1 / bid_ask_spread_pct) * sqrt(open_interest)
```

### 2.3 标签定义

**目标变量**: `is_profitable` (二分类)

```python
# 规则：标的在未来7天内的交易是否盈利
def label_profitable(symbol, start_date):
    """
    计算标的在start_date后7天内的交易盈亏
    
    Returns:
        1: 盈利 (卖出总额 - 买入总额 > 0)
        0: 亏损或持平
    """
    end_date = start_date + timedelta(days=7)
    
    # 查询该时段内的所有成交订单
    buy_amount = sum(买入订单的 quantity * average_fill_price)
    sell_amount = sum(卖出订单的 quantity * average_fill_price)
    
    net_pnl = sell_amount - buy_amount
    
    return 1 if net_pnl > 0 else 0
```

### 2.4 数据集划分

```python
# 时间序列数据，必须按时间顺序划分
train_data = 数据[日期 < '2026-08-01']      # 训练集：8月之前
valid_data = 数据['2026-08-01' <= 日期 < '2026-08-15']  # 验证集：8月上半月
test_data = 数据[日期 >= '2026-08-15']      # 测试集：8月下半月
```

---

## 三、模型架构

### 3.1 模型选择策略

采用**集成学习**方案，结合多个模型的优势：

#### 方案1：LightGBM（推荐首选）
```python
import lightgbm as lgb

model = lgb.LGBMClassifier(
    n_estimators=200,
    max_depth=8,
    learning_rate=0.05,
    num_leaves=64,
    min_child_samples=20,
    subsample=0.8,
    colsample_bytree=0.8,
    class_weight='balanced',  # 处理类别不平衡
    random_state=42
)
```

**优势**：
- ⚡ 训练速度快
- 🎯 处理表格数据效果好
- 📊 特征重要性分析直观
- 💾 模型文件小，部署简单

#### 方案2：XGBoost
```python
import xgboost as xgb

model = xgb.XGBClassifier(
    n_estimators=200,
    max_depth=8,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    scale_pos_weight=计算类别权重,
    random_state=42
)
```

#### 方案3：集成投票
```python
from sklearn.ensemble import VotingClassifier

ensemble = VotingClassifier(
    estimators=[
        ('lgb', lgb_model),
        ('xgb', xgb_model),
        ('rf', random_forest_model)
    ],
    voting='soft',  # 概率加权投票
    weights=[2, 2, 1]  # LGB和XGB权重更高
)
```

### 3.2 模型训练流程

```python
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import classification_report, roc_auc_score

def train_model(X_train, y_train, X_valid, y_valid):
    """
    训练波动率标的筛选模型
    """
    # 1. 特征标准化（仅针对数值特征）
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_valid_scaled = scaler.transform(X_valid)
    
    # 2. 训练模型
    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train_scaled, y_train,
        eval_set=[(X_valid_scaled, y_valid)],
        eval_metric='auc',
        early_stopping_rounds=20,
        verbose=10
    )
    
    # 3. 评估性能
    y_pred = model.predict(X_valid_scaled)
    y_proba = model.predict_proba(X_valid_scaled)[:, 1]
    
    print("分类报告：")
    print(classification_report(y_valid, y_pred))
    print(f"AUC: {roc_auc_score(y_valid, y_proba):.4f}")
    
    # 4. 特征重要性分析
    feature_importance = pd.DataFrame({
        'feature': X_train.columns,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)
    
    return model, scaler, feature_importance
```

---

## 四、模型集成到策略

### 4.1 实时预测模块

```python
# src/futu_quant/ml_symbol_scorer.py

from __future__ import annotations
import pickle
from pathlib import Path
import pandas as pd
import numpy as np

class MLSymbolScorer:
    """机器学习标的评分器"""
    
    def __init__(self, model_path: str = "data/ml_models/volatility_scorer_v1.pkl"):
        self.model_path = Path(model_path)
        self.model = None
        self.scaler = None
        self.feature_names = None
        self.load_model()
    
    def load_model(self):
        """加载训练好的模型"""
        if not self.model_path.exists():
            raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
        
        with open(self.model_path, 'rb') as f:
            checkpoint = pickle.load(f)
            self.model = checkpoint['model']
            self.scaler = checkpoint['scaler']
            self.feature_names = checkpoint['feature_names']
    
    def extract_features(self, symbol: str, broker, store) -> dict:
        """
        提取单个标的的实时特征
        
        Args:
            symbol: 标的代码（如US.AAPL）
            broker: 经纪商接口
            store: 数据存储接口
        
        Returns:
            特征字典
        """
        features = {}
        
        # 1. 历史收益特征
        features['symbol_7d_return'] = self._calculate_historical_return(
            symbol, store, days=7
        )
        features['symbol_30d_return'] = self._calculate_historical_return(
            symbol, store, days=30
        )
        features['symbol_win_rate'] = self._calculate_win_rate(symbol, store)
        
        # 2. 市场特征
        bars = broker.candles(symbol, count=60)
        features['underlying_volatility_20d'] = bars['close'].pct_change().rolling(20).std()
        features['underlying_return_5d'] = (bars['close'].iloc[-1] / bars['close'].iloc[-6] - 1)
        
        # 3. 期权特征（需要传入具体期权代码）
        # features['option_days_to_expiry'] = ...
        # features['option_iv_rank'] = ...
        
        return features
    
    def score_symbol(self, symbol: str, broker, store) -> float:
        """
        为标的打分（0-1之间，越高越好）
        
        Returns:
            盈利概率 (0-1)
        """
        try:
            # 提取特征
            features = self.extract_features(symbol, broker, store)
            
            # 转换为DataFrame
            X = pd.DataFrame([features])[self.feature_names]
            
            # 标准化
            X_scaled = self.scaler.transform(X)
            
            # 预测概率
            prob = self.model.predict_proba(X_scaled)[0, 1]
            
            return float(prob)
        
        except Exception as e:
            # 特征提取失败时返回默认分数
            store.audit(
                "ML_SCORING_FAILED",
                {"symbol": symbol, "error": str(e)},
                "ml_symbol_scorer"
            )
            return 0.5  # 中性分数
    
    def rank_symbols(self, symbols: list[str], broker, store) -> pd.DataFrame:
        """
        对标的列表进行排序
        
        Returns:
            排序后的DataFrame，包含symbol和score列
        """
        scores = []
        for symbol in symbols:
            score = self.score_symbol(symbol, broker, store)
            scores.append({'symbol': symbol, 'ml_score': score})
        
        df = pd.DataFrame(scores).sort_values('ml_score', ascending=False)
        return df
    
    def _calculate_historical_return(self, symbol: str, store, days: int) -> float:
        """计算历史平均收益率"""
        # 从order_intents表计算
        # TODO: 实现具体逻辑
        return 0.0
    
    def _calculate_win_rate(self, symbol: str, store) -> float:
        """计算历史胜率"""
        # TODO: 实现具体逻辑
        return 0.5
```

### 4.2 集成到波动率扫描器

```python
# 修改 src/futu_quant/volatility_scanner.py

from .ml_symbol_scorer import MLSymbolScorer

def scan_volatility_watchlist_with_ml(
    broker, store, symbols: tuple[str, ...] | None = None,
) -> list[dict]:
    """增强版扫描器：集成ML评分"""
    
    # 1. 原有的波动率信号扫描
    results = scan_volatility_watchlist(broker, store, symbols)
    
    # 2. 加载ML评分器
    try:
        scorer = MLSymbolScorer()
        ml_enabled = True
    except FileNotFoundError:
        store.audit("ML_MODEL_NOT_FOUND", {
            "message": "ML模型未找到，将跳过机器学习评分"
        }, "volatility_scanner")
        ml_enabled = False
    
    # 3. 为每个标的添加ML分数
    if ml_enabled:
        for result in results:
            symbol = result['symbol']
            try:
                ml_score = scorer.score_symbol(symbol, broker, store)
                result['ml_score'] = ml_score
                result['ml_rank'] = 'HIGH' if ml_score >= 0.7 else \
                                   'MEDIUM' if ml_score >= 0.5 else 'LOW'
            except Exception as e:
                result['ml_score'] = 0.5
                result['ml_rank'] = 'UNKNOWN'
        
        # 4. 重新排序：优先选择ML分数高的标的
        results = sorted(results, key=lambda x: (
            x.get('ml_score', 0.5),  # 优先ML分数
            x.get('active', False),   # 其次是否激活
        ), reverse=True)
    
    return results
```

### 4.3 动态资金分配

```python
# src/futu_quant/ml_portfolio_allocator.py

def allocate_capital_by_ml_score(
    symbols: list[str],
    ml_scores: dict[str, float],
    total_capital: float,
    min_allocation: float = 0.02,  # 最小2%
    max_allocation: float = 0.15   # 最大15%
) -> dict[str, float]:
    """
    根据ML分数动态分配资金
    
    高分标的获得更多资金，低分标的减少资金
    """
    allocations = {}
    
    # 1. 过滤掉低分标的（< 0.4）
    qualified_symbols = [s for s in symbols if ml_scores.get(s, 0) >= 0.4]
    
    if not qualified_symbols:
        return {}
    
    # 2. 计算权重（分数越高权重越大）
    total_score = sum(ml_scores.get(s, 0.5) ** 2 for s in qualified_symbols)  # 平方放大差异
    
    for symbol in qualified_symbols:
        score = ml_scores.get(symbol, 0.5)
        weight = (score ** 2) / total_score
        
        # 3. 应用上下限
        weight = max(min_allocation, min(weight, max_allocation))
        
        allocations[symbol] = total_capital * weight
    
    # 4. 归一化（确保总和=total_capital）
    actual_total = sum(allocations.values())
    for symbol in allocations:
        allocations[symbol] = allocations[symbol] / actual_total * total_capital
    
    return allocations
```

---

## 五、实施计划

### 第一阶段：数据准备（1-2天）

```bash
# 1. 导出历史交易数据
uv run python scripts/ml_export_training_data.py \
    --output data/ml_training/volatility_trades.csv

# 2. 提取市场数据特征
uv run python scripts/ml_extract_market_features.py \
    --input data/ml_training/volatility_trades.csv \
    --output data/ml_training/features.csv

# 3. 数据质量检查
uv run python scripts/ml_validate_data.py \
    --input data/ml_training/features.csv
```

### 第二阶段：模型训练（1-2天）

```bash
# 1. 训练初始模型
uv run python scripts/ml_train_symbol_scorer.py \
    --data data/ml_training/features.csv \
    --model-output data/ml_models/volatility_scorer_v1.pkl \
    --metrics-output data/ml_models/v1_metrics.json

# 2. 超参数调优
uv run python scripts/ml_hyperparameter_tuning.py \
    --data data/ml_training/features.csv \
    --trials 50

# 3. 模型评估
uv run python scripts/ml_evaluate_model.py \
    --model data/ml_models/volatility_scorer_v1.pkl \
    --test-data data/ml_training/test_set.csv
```

### 第三阶段：回测验证（2-3天）

```bash
# 使用ML模型回测历史数据
uv run python scripts/ml_backtest_strategy.py \
    --model data/ml_models/volatility_scorer_v1.pkl \
    --start-date 2026-07-01 \
    --end-date 2026-08-28 \
    --output data/ml_backtest_results.csv
```

### 第四阶段：模拟运行（1周）

```bash
# 在SHADOW模式下运行，不实际下单
uv run python scripts/run_volatility_strategy_with_ml.py \
    --ml-mode SHADOW \
    --loop
```

### 第五阶段：正式部署（1周后）

```bash
# 切换到PAPER模式，实际纸面交易
uv run python scripts/run_volatility_strategy_with_ml.py \
    --ml-mode PAPER \
    --loop
```

---

## 六、监控与迭代

### 6.1 性能监控指标

创建监控面板（Streamlit页面）：

```python
# pages/90_ML模型监控.py

import streamlit as st
import pandas as pd

st.title("📊 ML模型性能监控")

# 1. 实时预测准确率
st.subheader("预测准确率（最近30天）")
accuracy_data = load_ml_prediction_accuracy()
st.line_chart(accuracy_data)

# 2. 特征重要性
st.subheader("特征重要性排名")
feature_importance = load_feature_importance()
st.bar_chart(feature_importance)

# 3. 预测vs实际对比
st.subheader("预测vs实际盈亏")
comparison = load_prediction_vs_actual()
st.dataframe(comparison)

# 4. 标的评分分布
st.subheader("当前标的ML评分分布")
scores = load_current_symbol_scores()
st.histogram(scores['ml_score'])
```

### 6.2 模型更新策略

```python
# scripts/ml_auto_retrain.py

def should_retrain_model(store) -> bool:
    """
    判断是否需要重新训练模型
    
    触发条件：
    1. 距离上次训练超过30天
    2. 预测准确率连续7天低于60%
    3. 新增交易数据超过500条
    """
    last_train_date = store.get_setting("ml_last_train_date")
    current_accuracy = calculate_recent_accuracy(store, days=7)
    new_trade_count = count_new_trades_since_last_train(store)
    
    if last_train_date is None:
        return True
    
    days_since_train = (datetime.now() - parse_date(last_train_date)).days
    
    return (
        days_since_train >= 30 or
        current_accuracy < 0.60 or
        new_trade_count >= 500
    )

def auto_retrain_if_needed():
    """自动重训练流程"""
    if should_retrain_model(store):
        logger.info("触发模型自动重训练...")
        
        # 1. 导出最新数据
        export_training_data()
        
        # 2. 训练新模型
        new_model = train_model()
        
        # 3. 验证新模型性能
        if validate_new_model(new_model):
            # 4. 备份旧模型
            backup_old_model()
            
            # 5. 部署新模型
            deploy_new_model(new_model)
            
            store.audit("ML_MODEL_RETRAINED", {
                "trigger": "auto",
                "old_accuracy": old_accuracy,
                "new_accuracy": new_accuracy
            }, "ml_auto_retrain")
```

---

## 七、风险控制

### 7.1 模型降级机制

```python
def safe_ml_prediction(symbol: str, scorer: MLSymbolScorer, broker, store) -> float:
    """
    安全的ML预测，带降级逻辑
    """
    try:
        score = scorer.score_symbol(symbol, broker, store)
        
        # 检查分数合理性
        if not 0 <= score <= 1:
            raise ValueError(f"异常分数: {score}")
        
        return score
    
    except Exception as e:
        # 降级到规则打分
        store.audit("ML_PREDICTION_FAILED_FALLBACK", {
            "symbol": symbol,
            "error": str(e),
            "fallback": "rule_based_score"
        }, "ml_symbol_scorer")
        
        return rule_based_score(symbol, store)

def rule_based_score(symbol: str, store) -> float:
    """
    规则化打分（ML失效时的降级方案）
    """
    # 基于历史胜率的简单规则
    win_rate = calculate_win_rate(symbol, store)
    avg_return = calculate_avg_return(symbol, store)
    
    # 简单加权
    score = 0.6 * win_rate + 0.4 * (avg_return + 1) / 2
    return max(0, min(1, score))
```

### 7.2 A/B测试

```python
# 同时运行ML版本和原版本，对比效果

def run_ab_test():
    """
    A组：使用ML筛选
    B组：使用原规则筛选
    """
    if random.random() < 0.5:
        # A组：ML增强
        symbols = select_symbols_with_ml(broker, store)
        group = 'ML'
    else:
        # B组：原规则
        symbols = select_symbols_original(broker, store)
        group = 'BASELINE'
    
    store.audit("AB_TEST_GROUP_ASSIGNED", {
        "group": group,
        "symbols": symbols
    }, "ab_test")
    
    return symbols, group
```

---

## 八、预期效果

### 8.1 短期目标（1个月内）
- ✅ 减少30%的亏损标的交易
- ✅ 整体胜率从83.5%提升至85%+
- ✅ 平均单笔盈利从-$0.08提升至+$0.15

### 8.2 中期目标（3个月内）
- ✅ 累计净盈利从-$25转为+$50
- ✅ 最大单笔亏损控制在-$2以内
- ✅ 交易标的数从143个精简至50-80个优质标的

### 8.3 长期目标（6个月内）
- ✅ 建立完整的模型迭代体系
- ✅ 开发多个策略专用的ML模型
- ✅ 实现自动化的特征工程和模型更新

---

## 九、技术栈

### 依赖安装

```toml
# 在pyproject.toml中添加
[project.optional-dependencies]
ml = [
    "lightgbm>=4.0",
    "scikit-learn>=1.3",
    "xgboost>=2.0",
    "shap>=0.42",  # 模型可解释性
]
```

```bash
# 安装ML依赖
uv sync --extra ml
```

---

## 十、总结

机器学习模型将从以下方面优化波动率策略：

1. **智能筛选**: 自动识别高潜力标的，过滤低质量标的
2. **动态分配**: 根据预测概率动态调整各标的资金权重
3. **风险控制**: 提前识别可能亏损的标的，避免交易
4. **持续优化**: 模型自动学习最新市场规律，不断改进

关键成功要素：
- ✅ 高质量的历史数据
- ✅ 科学的特征工程
- ✅ 严格的回测验证
- ✅ 完善的监控体系
- ✅ 安全的降级机制

建议采用**渐进式部署**：先SHADOW模式验证 → 再小规模PAPER测试 → 最后全量上线。
