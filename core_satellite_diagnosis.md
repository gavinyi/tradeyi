# Core Satellite 策略诊断报告

生成时间: 2024-09-05

## 一、策略执行概况

### 订单统计
- **总订单数**: 60 笔
- **成功成交**: 54 笔 (90.0%)
- **失败**: 2 笔 (3.3%)
- **取消/拒绝**: 4 笔 (6.7%)
- **买入订单**: 29 笔
- **卖出订单**: 31 笔

### 交易活跃度
- **活跃交易月份**: 2026年7月-8月
- **最近失败**: 2026-09-02 和 2026-09-03 连续两天 NVDA 买入失败
- **失败原因**: "持仓不足" (模拟账户资金/持仓问题)

---

## 二、发现的核心问题

### 🔴 问题 1: 订单频繁失败（最近）
**现象**: 
- 9月初连续两天 NVDA 买入失败
- 错误信息: `place simulated order failed: 持仓不足`

**根本原因**:
- 模拟账户可能缺乏足够的可用资金
- 或者系统配置的可交易资金额度不足

**影响**: 错失 NVDA 买入机会，可能导致组合偏离目标配置

---

### 🟡 问题 2: 决策频率过低
**现象**:
- 8月中下旬出现多天 `HOLD` 决策（"没有达到最低交易金额的调仓指令"）
- 8月17日、14日、13日连续三天无实际交易

**配置参数**:
```bash
CORE_SAT_REBALANCE_FREQUENCY=weekly  # 周度调仓
CORE_SAT_MIN_TRADE_USD=1000.0        # 最小交易金额
CORE_SAT_WEIGHT_TOLERANCE=0.02       # 权重容忍度 2%
CORE_SAT_MAX_SINGLE_REBALANCE_PCT=0.5 # 单次调仓比例 50%
```

**根本原因**:
1. **最小交易金额门槛过高** (`1000 USD`): 小幅权重偏差不会触发交易
2. **权重容忍度设置** (`2%`): 允许较大的组合偏离
3. **单次调仓比例限制** (`50%`): 防止激进调仓，但也限制了响应速度

**影响**: 
- 组合跟踪目标权重的及时性下降
- Satellite 机会捕获可能延迟

---

### 🟡 问题 3: Core 资产频繁卖出
**现象**:
- SPY 卖出 7 笔，买入 0 笔
- QQQ 卖出 7 笔，买入 0 笔
- VOO/IVV 买入较多，但 SPY/QQQ 持续减仓

**数据**:
```
SPY   SELL: 7 笔, $195,464
QQQ   SELL: 7 笔, $133,805
VOO   BUY:  4 笔, $95,502
IVV   BUY:  5 笔, $74,852
```

**策略逻辑分析**:
查看 `core_satellite.py` 第 149-177 行 `core_quality_score()`:
- SPY/QQQ/VOO/IVV/SCHD 都属于 Core 资产
- Core 资产评分受 `risk_on` 状态影响
- 当 `risk_on=False` (SPY < SMA200) 时，Core 降权到 0.20-0.35

**可能原因**:
1. **大盘风险状态切换**: 如果 SPY 在 SMA200 附近震荡，`risk_on` 状态频繁切换
2. **Core 内部再平衡**: 策略可能在不同 Core ETF 之间轮换（SPY/QQQ → VOO/IVV）
3. **配置的 Core 权重**: 
   ```python
   "balanced": {
       "core_weights": {
           "US.SPY": 0.30, "US.VOO": 0.25, "US.IVV": 0.20,
           "US.QQQ": 0.15, "US.SCHD": 0.10,
       }
   }
   ```

**影响**:
- 频繁卖出 Core 资产可能增加交易成本
- 如果是因为 `risk_on` 切换导致，说明策略对市场状态过于敏感

---

### 🟢 问题 4: Satellite 交易集中在少数标的
**现象**:
- NVDA 交易最频繁：买入 6 笔，卖出 8 笔
- AAPL 次之：买入 6 笔，卖出 4 笔
- GOOG 较少：买入 2 笔，卖出 2 笔
- META 没有交易记录

**配置的 Satellite 池**:
```bash
CORE_SAT_SYMBOLS=US.SPY,US.QQQ,US.NVDA,US.AAPL,US.GOOG,US.META,US.VOO,US.IVV,US.SCHD
```

**Satellite 筛选逻辑** (`core_satellite.py` 第 186-220 行):
```python
satellite_min_score: float = 2.5  # 配置为 4.0
```

配置参数:
```bash
CORE_SAT_SATELLITE_MIN_SCORE=4.0  # 每日机会门槛较高
```

**根本原因**:
1. **高门槛**: `satellite_quality_score()` 最高 6.5 分，门槛 4.0 意味着需要满足大部分条件
2. **相对强度要求**: 必须相对 SPY 有 20日/60日超额收益
3. **META 可能长期不满足筛选条件**

**影响**:
- Satellite 池利用率低
- 可能错失部分标的的机会

---

## 三、策略配置评估

### 当前配置
```bash
CORE_SAT_PROFILE=balanced
CORE_SAT_CORE_BUDGET=0.4           # Core 40%
CORE_SAT_SATELLITE_BUDGET=0.25     # Satellite 25%
CORE_SAT_CASH_BUFFER=0.08          # 现金缓冲 8%
CORE_SAT_MAX_ORDER_NOTIONAL=50000  # 单笔限额 $50K
CORE_SAT_MAX_TARGET_CHANGE=0.05    # 单次权重变化上限 5%
CORE_SAT_SATELLITE_DAILY_SCAN=true # 每日 Satellite 扫描
```

### 配置合理性分析

#### ✅ 合理的参数
1. **资金分配**: Core 40% + Satellite 25% + Cash 8% = 73%，留有余地
2. **单笔限额**: $50K 对于中等资金量账户合理
3. **每日 Satellite 扫描**: 有助于捕获短期机会

#### ⚠️ 需要调整的参数
1. **最小交易金额** (`1000 USD`): 对小账户可能过高
2. **权重容忍度** (`2%`): 可能导致跟踪误差
3. **Satellite 评分门槛** (`4.0/6.5`): 过于严格，限制了交易机会
4. **单次调仓比例** (`50%`): 在高波动期可能不够响应

---

## 四、策略效果不理想的核心原因

### 主要问题
1. **参数设置偏保守**: 
   - 最小交易金额、权重容忍度、Satellite 评分门槛都设置较严格
   - 导致交易频率低、响应慢

2. **资金/持仓管理问题**:
   - 最近两天连续失败说明账户状态可能有问题
   - 需要检查模拟账户配置

3. **Core 资产轮换逻辑**:
   - SPY/QQQ 频繁卖出可能是策略设计导致
   - 需要确认是否符合预期

### 次要问题
1. **Satellite 池利用不足**: META 等标的长期未交易
2. **周度调仓频率**: 在快速变化的市场中可能反应不够快

---

## 五、优化建议

### 🎯 立即修复（高优先级）

#### 1. 解决订单失败问题
```bash
# 检查模拟账户状态
uv run python scripts/check_connection.py

# 查看账户资金
# 可能需要调整 STRATEGY_BUDGETS 中 core_satellite 的配额
```

**行动**: 
- 确认模拟账户有足够资金
- 检查 `STRATEGY_BUDGETS` 配置是否与实际账户匹配

---

#### 2. 降低交易门槛（提高灵活性）
```bash
# 建议修改 .env
CORE_SAT_MIN_TRADE_USD=500.0         # 降低到 $500
CORE_SAT_WEIGHT_TOLERANCE=0.015      # 收紧到 1.5%
CORE_SAT_SATELLITE_MIN_SCORE=3.0     # 降低到 3.0 (中等偏上)
```

**理由**:
- 更频繁的小额调仓 > 大额但不及时的调仓
- Satellite 机会捕获率会提升
- 权重跟踪更精确

---

### 🔧 深度优化（中优先级）

#### 3. 调整调仓策略
```bash
# 选项 A: 提高单次调仓比例（更激进）
CORE_SAT_MAX_SINGLE_REBALANCE_PCT=0.75  # 提高到 75%

# 选项 B: 改为每日调仓（如果希望更敏锐）
CORE_SAT_REBALANCE_FREQUENCY=daily

# 选项 C: 增加高波动期的调仓速度
CORE_SAT_HIGH_VOL_TRADE_FACTOR=0.65  # 从 0.5 提高到 0.65
```

**权衡**:
- 选项 A: 更快达到目标配置，但可能增加交易成本
- 选项 B: 最敏感，适合日内波动大的市场
- 选项 C: 折中方案，只在高波动时加快

---

#### 4. 优化 Core 资产配置
**当前问题**: SPY/QQQ 频繁卖出

**调查步骤**:
```bash
# 检查最近的市场状态
uv run python -c "
from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings

settings = Settings.load()
broker = FutuPaperBroker(settings)
spy = broker.candles('US.SPY', count=210)
print('SPY 最新价:', spy['close'].iloc[-1])
print('SPY SMA200:', spy['close'].iloc[-200:].mean())
print('risk_on:', spy['close'].iloc[-1] >= spy['close'].iloc[-200:].mean())
"
```

**可能的调整**:
1. 如果 `risk_on` 频繁切换 → 考虑增加 SMA200 判断的滞后性
2. 如果 Core 内部轮换符合预期 → 无需调整
3. 如果想减少 Core 调仓 → 提高 Core 的 `weight_tolerance`

---

### 📊 监控改进（低优先级）

#### 5. 增加诊断日志
在 `run_strategies.py` 中增加详细日志：
```python
# 建议在第 96 行后增加
print(f"Account value: ${account_value:,.2f}")
print(f"Effective stock cap: {effective_stock_cap:.1%}")
print(f"Core budget: {core_budget}")
print(f"Symbols: {symbols}")
print(f"Satellite only mode: {satellite_only}")
```

#### 6. 添加性能追踪
```bash
# 定期运行性能报告
uv run python -c "
import sqlite3
import pandas as pd

db = sqlite3.connect('data/trading.db')
orders = pd.read_sql('''
    SELECT created_at, symbol, side, quantity, limit_price, 
           filled_quantity, average_fill_price, status
    FROM order_intents 
    WHERE strategy_id = 'core_satellite'
    ORDER BY created_at
''', db)

# 计算实现盈亏
# TODO: 需要配对买卖订单
print(orders.tail(20))
db.close()
"
```

---

## 六、验证计划

### 第一阶段: 修复基础问题（1-2天）
1. ✅ 解决订单失败问题
2. ✅ 调整交易门槛参数
3. ✅ 观察交易频率是否提升

### 第二阶段: 优化策略逻辑（1周）
1. 根据 `risk_on` 状态分析 Core 卖出是否合理
2. 评估 Satellite 评分体系
3. 考虑调整调仓频率

### 第三阶段: 长期监控（持续）
1. 每周查看订单成功率
2. 每月评估组合跟踪误差
3. 对比 Satellite 实际收益 vs 错失机会成本

---

## 七、关键指标监控

建议每周关注:
```bash
# 1. 订单成功率
成功率 = FILLED / (FILLED + FAILED + REJECTED)
目标: > 95%

# 2. 决策执行率
执行率 = (BUY + SELL) / (BUY + SELL + HOLD + NOT_DUE)
目标: > 30% (周度调仓)

# 3. Satellite 利用率
利用率 = 实际交易的 Satellite 数 / 配置的 Satellite 池大小
当前: 3/4 (NVDA, AAPL, GOOG / META未交易)
目标: > 60%

# 4. 权重偏离度
当前持仓权重 vs 目标权重的最大偏差
目标: < 3%
```

---

## 八、总结

### 策略本身设计
✅ **逻辑合理**: Core-Satellite 分层、风险状态感知、相对强度筛选
✅ **防护完善**: 单笔限额、调仓限速、权重变化上限

### 当前主要问题
❌ **参数过于保守**: 导致交易不够活跃
❌ **账户配置问题**: 订单失败需立即解决
⚠️ **监控不足**: 缺少性能指标追踪

### 优化后预期
- **订单成功率**: 从 90% → 98%+
- **交易频率**: 提升 30-50%
- **Satellite 捕获**: 从 3/4 → 全池覆盖
- **权重跟踪**: 误差降低到 1.5% 以内

---

**下一步行动**: 
1. 立即检查模拟账户状态
2. 修改 `.env` 中的三个参数（MIN_TRADE_USD, WEIGHT_TOLERANCE, SATELLITE_MIN_SCORE）
3. 运行一次手动调仓观察效果
4. 持续监控一周后复盘
