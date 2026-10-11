# 动态标的池扩充与分级孵化系统设计规范 (Dynamic Universe Expansion Architecture)

## 一、架构总览与核心设计哲学

针对期权多头买方（`volatility_long_gamma`）策略，系统构建了一套**工业级全生命周期标的池扩充系统**。该设计借鉴了互联网高并发推荐与搜广推（Search/Recommendation/Advertising）系统的经典架构：  
**`全市场召回 (Recall)` $\to$ `沙盒孵化 (Sandbox)` $\to$ `模型精排 (Ranking)` $\to$ `多样性重排 (Diversity)` $\to$ `动态实盘晋级`**。

### 1.1 双层池化架构 (Two-Tier Universe Model)

系统明确隔离为**固定核心底仓池**与**动态晋级扩展池**，彻底杜绝全局大洗牌带来的破坏性：

```text
                  ┌────────────────────────────────────────────────────────┐
                  │             量化执行交易池 (Live Trading Universe)       │
                  └───────────────────────────┬────────────────────────────┘
                                              ▲
                           ┌──────────────────┴──────────────────┐
                           │                                     │
               【固定核心底仓池 (Core Baseline)】       【动态晋级扩展池 (Satellite Expansion)】
                     (13只，永久常驻，不淘汰)                 (3~5只，严格赛马，动态追加)
                           │                                     │
                COIN, AMD, NVDA, AVGO,                           ▲
                AAPL, META, AMZN, GOOG,                          │ 满足晋级硬门槛 (Promotion)
                MSFT, TSM, QQQ, SPY, GLD                         │
                           │                        ┌────────────┴────────────┐
                           │                        │  Step 4: 行业重排打散   │
                           │                        ├─────────────────────────┤
                           │                        │  Step 3: ML 精排 (Top 10)│
                           │                        ├─────────────────────────┤
                           │                        │  Step 2: 沙盒孵化 (10日) │
                           │                        ├─────────────────────────┤
                           │                        │  Step 1: Top 50 粗排召回 │
                           │                        └─────────────────────────┘
```

### 1.2 核心底仓与外援的边界原则
- **底仓守正（Core 13 只）**：作为系统的盈利压舱石与基本盘，**永久常驻，不参与全市场流水线的淘汰与降级机制**，确保系统每天都有高流动性、紧密盘口与经过实战检验的核心标的运转。
- **外援出奇（Satellite 3~5 只）**：面向美股全市场 10,000+ 标的进行淘金，只负责捕捉特定周期内爆发的 Alpha 动量黑马。任何外部标的必须在沙盒中跑出真实胜率，并通过精排与风控后，才被**追加合并**到实盘交易池中。

---

## 二、四级漏斗工业流程与指标口径

### 2.1 第一级：Top 50 粗排召回层 (Recall Layer)
- **核心目标**：从美股全市场中筛除 99% 的劣质流动性垃圾，圈定做市商高度活跃、买卖价差极小的物理可行标的。
- **数据来源**：全市场期权日成交量榜（Option Volume Rank）、持仓量榜（Open Interest）与正股成交额。
- **物理硬门槛**：
  1. **期权日均成交量**：近 5 日日均期权成交量 $> 50,000$ 张；
  2. **盘口买卖价差中位数**：近月平值（ATM）期权买卖价差比例 $\le 3.0\%$（或绝对价差 $\le \$0.05$）；
  3. **正股日均成交额**：正股近 20 日日均成交额 $> \$5\text{亿}$；
  4. **硬性黑名单剔除**：剔除已在 Core 底仓中的 13 只标的、剔除 MEME 炒作仙股、剔除防守型公共事业股。
- **输出产物**：产生 50 只具备物理交易可行性的备选候选池，写入 `candidate_universe` 表。

---

### 2.2 第二级：隔离沙盒孵化层 (Sandbox & Shadow Layer)
- **核心目标**：解决“样本冷启动与微观结构摩擦测算”。名义成交量大不等于买入赚钱，必须在真实市场中验证其信号留存率与抗洗盘能力。
- **隔离机制**：
  - 这 50 只候选标的在后台运行纯 **SHADOW（影子演练）** 或**虚拟信号捕获**，绝不调用券商接口发出真实订单；
  - 观察周期为**滚动 10 个交易日（滑动 2 周窗口）**。
- **记录特征指标**：
  1. **突破信号频次**：10 个交易日内触发动态波动（Volume Ratio $\ge 1.2$, Z-score $\ge 2.0$）的有效次数；
  2. **5 分钟量能留存率**：异动后 5 分钟内成交量与价格维持度（要求 $\ge 65\%$）；
  3. **虚拟交易表现**：按 Volatility Long Gamma 买方规则推演的虚拟胜率、虚拟盈亏比、以及虚拟最大回撤（Max Drawdown）；
  4. **平均成交滑点**：模拟买入时的中间价与实际可撮合 Ask 价差成本。
- **输出产物**：为每只候选标的生成包含真实微观摩擦的统计档案，写入 `shadow_incubator` 表。

---

### 2.3 第三级：机器学习精排层 (Machine Learning Ranking Layer)
- **核心目标**：利用系统内置的机器学习评分器（`MLSymbolScorer`）进行多维截面打分，预测标的未来 5 个交易日的期权买方期望收益。
- **特征工程输入**：
  - **正股动量因子**：20 日 ATR 变化率、Z-score、相对标普 500（SPY）的超额收益 Beta；
  - **期权定价与结构因子**：当前 IV 相对近 30 日实现波动率（RV）的溢价倍数、IV 历史百分位数；
  - **沙盒微观表现因子**：近 10 日虚拟交易胜率、虚拟盈亏比、量能留存率；
  - **绝对 IV 防御惩罚**：若绝对 IV 超过 80%，施加非线性惩罚项，抑制高 IV 泡沫接盘。
- **打分输出**：
  - 输出 $0.0 \sim 1.0$ 的概率得分 `ml_score`；
  - 筛选得分 $\ge 0.70$ 的标的，截取前 10 名进入待选梯队（Top 10）。

---

### 2.4 第四级：多样性重排与风控层 (Re-ranking & Diversity Layer)
- **核心目标**：解决“行业过度集中与系统性踩踏”。精排前列的标的极易同属单一热门板块（例如全为半导体），必须通过打散算法保证组合安全性。
- **多样性约束**：
  1. **行业配额上限**：单一细分行业（如半导体设备、硬件代工、加密资产）最多允许 **1 个席位**；
  2. **两两相关系数阈值**：最终选出的标的之间，60 日日收益率相关系数矩阵中，任意两两相关系数必须 $\le 0.70$；
  3. **大盘高低波对齐**：必须保留至少 1 个大盘指数或宏观关联品种。
- **输出产物**：经过行业打散与配额约束后，锁定最终表现最强、互不踩踏的 **3 ~ 5 只外援晋级标的**。

---

## 三、外援标的全生命周期管理 (Promotion & Demotion)

```text
       [全市场 Top 50 召回]
               │
               ▼
       [10 个交易日沙盒孵化]
               │
   ┌───────────┴───────────┐
   │ 评估准入硬门槛         │
   │ 1. 虚拟胜率 > 50%     │ ──未达标──> 继续留在沙盒观察或自然淘汰
   │ 2. 虚拟回撤 < 25%     │
   │ 3. ML 评分 >= 0.75    │
   └───────────┬───────────┘
               │ 达标
               ▼
       [授予实盘资格 (Promotion)] ──> 合并追加进 VOLATILITY_SYMBOLS
               │
               ▼
       [实盘监控与淘汰评估]
               │
   ┌───────────┴───────────────────────────────┐
   │ 触发退出条件之一:                          │
   │ 1. 2~4 周自然到期且 ML 评分 < 0.50        │ ──触发──> [移出交易池 (Demotion)]
   │ 2. 盘中买卖价差恶化 > 10% (流动性枯竭)      │          (平仓后取消后续监控)
   │ 3. 3 天内连续触发 2 次硬止损 (洗盘排斥)    │
   └───────────────────────────────────────────┘
```

### 3.1 晋级准入硬门槛 (Promotion Criteria)
任何外部候选标的进入实盘交易池，必须同时通过以下验收：
1. **沙盒样本充足**：在 10 个交易日内产生不少于 3 次有效突破演练样本；
2. **虚拟期望值为正**：沙盒推演净收益 $> 0$，且期间最大虚拟回撤 $< 25\%$；
3. **ML 综合评分优异**：最新截面打分 $\ge 0.75$；
4. **盘口稳定性**：盘中买卖价差均值保持在 $\le 5\%$。

### 3.2 迟滞缓冲防抖机制 (Hysteresis Buffer)
- **防止临界点反复横跳**：备选标的得分必须高于现任外援至少 $10\%$，且在打分榜上**连续 2 天保持领先**，才允许触发替换；
- **现任保护**：现任外援只要仍在前 8 名以内且实盘未触碰风控，保持在岗，避免过度调仓带来的无谓摩擦。

### 3.3 降级与清退规则 (Demotion & Eviction)
底仓 13 只标的永远不退出，外援标的根据以下规则严格执行清退：
1. **动量衰竭自然退出**：
   - 外援标的初始授予 **2 周实盘观察周期**（最长不超过 4 周）；
   - 到期评估时，若 ML 评分回落至 $< 0.50$，视为主动量周期结束，本轮持仓平仓后安全移出。
2. **盘中应急红线秒踢 (Fast Eviction)**：
   - **流动性骤降**：买卖价差突然扩大到 $> 10\%$；
   - **剧烈洗盘假突破**：实盘中 3 天内连续遭遇 2 次硬止损；
   - **处理动作**：清空当前关联持仓，立即从交易池注销，并进入为期 7 天的冷却隔离期。

---

## 四、频率与时钟调度体系 (Timing Architecture)

系统遵循**“粗排看周、精排看日、盘中秒级执行、持仓避免折腾”**的高低频解耦机制：

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. 每周日 20:00 (北京时间周一上午) [离线全市场周度批处理]                    │
│    • 执行 Step 1: 抓取美股全市场期权交易数据 -> 生成最新 Top 50 候选池      │
│    • 执行 Step 4: 检阅现有外援表现，结合迟滞缓冲确定新一周的 Top 3~5 外援   │
│    • 动态更新数据库中的交易池配置                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. 每交易日 16:30 (美东收盘后) [离线特征与模型日度批处理]                   │
│    • 读取当日最新正股日K与期权收盘数据                                      │
│    • 运行 MLSymbolScorer 对 50 只候选标的刷新特征库与截面得分排名           │
│    • 检查外援是否触碰降级预警线                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. 每交易日 09:30 - 16:00 (美股交易时段) [在线高频交易与静默沙盒并行]       │
│    • 线上交易线: 【Core 13只 + 生效外援 3~5只】 -> 30 秒高频轮询执行        │
│    • 线下孵化线: 【沙盒其余 45 只候选标的】 -> 静默监听信号，记录微观摩擦   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 五、数据模型设计 (Data Schema)

在 SQLite `trading.db` 中引入三张专用表，保持与主交易流程的完全解耦：

### 5.1 候选全市场召回表 (`candidate_universe`)
```sql
CREATE TABLE IF NOT EXISTS candidate_universe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,          -- 归属交易周（如 '2026-10-12'）
    symbol TEXT NOT NULL,              -- 标的代码（如 'US.PLTR'）
    option_daily_volume REAL NOT NULL, -- 期权日均成交量
    open_interest REAL NOT NULL,       -- 期权总持仓量
    stock_turnover_usd REAL NOT NULL,  -- 正股日均成交额
    median_spread_pct REAL NOT NULL,   -- 平值期权买卖价差中位数
    status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'IN_SANDBOX', 'EXCLUDED'
    created_at TEXT NOT NULL,
    UNIQUE(week_start, symbol)
);
CREATE INDEX IF NOT EXISTS idx_candidate_week ON candidate_universe(week_start, status);
```

### 5.2 沙盒影子孵化记录表 (`shadow_incubator`)
```sql
CREATE TABLE IF NOT EXISTS shadow_incubator (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,              -- 标的代码
    observation_days INTEGER NOT NULL, -- 已观察交易日数
    signal_triggers INTEGER NOT NULL,  -- 触发突破信号次数
    simulated_wins INTEGER NOT NULL,   -- 虚拟盈利次数
    simulated_losses INTEGER NOT NULL, -- 虚拟亏损次数
    simulated_pnl_usd REAL NOT NULL,   -- 虚拟累计盈亏
    simulated_max_dd REAL NOT NULL,    -- 虚拟最大回撤比例
    avg_slippage_pct REAL NOT NULL,    -- 平均盘口滑点
    volume_retention_ratio REAL NOT NULL, -- 5分钟量能留存率
    latest_ml_score REAL NOT NULL,     -- 最新模型预测得分
    promotion_eligible INTEGER NOT NULL DEFAULT 0, -- 是否满足晋级条件 (0/1)
    updated_at TEXT NOT NULL,
    PRIMARY KEY(symbol)
);
```

### 5.3 晋级生效外援表 (`promoted_universe`)
```sql
CREATE TABLE IF NOT EXISTS promoted_universe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,       -- 晋级生效标的
    promoted_at TEXT NOT NULL,         -- 晋级时间
    expires_at TEXT NOT NULL,          -- 授权到期时间（默认2~4周）
    promotion_score REAL NOT NULL,     -- 晋级时的 ML 分数
    eviction_reason TEXT,              -- 退出原因（若已退出）
    status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'EVICTED', 'EXPIRED'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promoted_status ON promoted_universe(status);
```

---

## 六、主交易系统接口与解耦公式

主交易进程 `run_service.py` 内部对于标的池的解析逻辑保持完全纯粹与透明：

$$\text{VOLATILITY\_SYMBOLS} = \text{DEFAULT\_CORE\_SYMBOLS (13只)} \cup \{ s \in \text{promoted\_universe} \mid status = \text{'ACTIVE'} \}$$

1. **零性能损耗**：主交易服务无需在盘中进行任何全市场筛选计算，仅读取合并后的结果；
2. **故障隔离**：如果周度召回或离线打分由于网络或数据源异常失败，主策略自动回退到 13 只核心底仓运行，**系统永远不会因外部淘金模块故障而停摆**；
3. **配置热加载**：外部外援晋级或退出时，通过调用 `save_volatility_symbols()` 动态更新 SQLite `runtime_config`，服务在下一个周期毫秒级热生效。

---

## 七、实施路径与里程碑规划 (Implementation Roadmap)

| 阶段 | 核心任务 | 交付产物 |
| :--- | :--- | :--- |
| **Phase 1: 召回与数据表就绪** | • 创建三张数据表与迁移逻辑<br>• 实现 `scripts/scanner_universe_recall.py` 抓取全市场期权成交前 50 标的 | 数据库 Schema + 周度全市场 Top 50 自动化抓取脚本 |
| **Phase 2: 沙盒孵化引擎搭建** | • 实现 `src/futu_quant/shadow_incubator.py`<br>• 在后台静默记录 Top 50 的突破信号、盘口摩擦与虚拟盈亏 | 沙盒演练模块 + 每日特征提取自动化 |
| **Phase 3: 精排与重排打散落地** | • 集成 `MLSymbolScorer` 模型截面打分<br>• 编写行业配额与相关性打散算法（`universe_diversity.py`） | ML 打分脚本 + MMR 打散晋级仲裁器 |
| **Phase 4: 生命周期全自动闭环** | • 串联周度定时调度（晋级准入 + 迟滞缓冲 + 应急秒踢）<br>• 在 Streamlit 监控中心增加“标的孵化与赛马驾驶舱”视图 | 完整的全天候自动化标的扩充系统 + UI 展示看板 |

---

## 八、总结

本架构严格遵循“**守正出奇**”的设计哲学：
1. **底仓 13 只黄金标的坚如磐石**，保证策略基石不受任何扰动；
2. **全市场 Top 50 漏斗系统**构建了一个严苛的“赛马沙盒”，只有胜率高、滑点低、动量强的极品黑马才能晋级实盘；
3. 实现了量化策略在容量上限（Capacity）和阿尔法收益（Alpha）上的无缝扩展。
