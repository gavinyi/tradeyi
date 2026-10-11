# 动态标的池扩充与分级孵化系统详细设计与开发规范 (Technical Implementation Specification)

## 一、系统架构与模块边界划分

本系统定位为量化交易系统的**“离线/近线智能选股与标的孵化中台”**，与盘中 30 秒高频交易引擎（`run_service.py`）保持异步解耦。

### 1.1 代码目录与新增模块划分

```text
src/futu_quant/
├── universe/                               # [新增子包] 标的池智能扩充与孵化子系统
│   ├── __init__.py                         # 包导出
│   ├── recall_scanner.py                   # 模块1: 全市场流动性Top 50粗排召回器
│   ├── incubator_engine.py                 # 模块2: 隔离沙盒影子演练与微观特征提取器
│   ├── ranking_pipeline.py                 # 模块3: ML精排多维特征提取与模型打分器
│   ├── diversity_filter.py                 # 模块4: 行业配额与相关性打散重排器
│   └── lifecycle_manager.py                # 模块5: 晋级准入、迟滞缓冲与秒踢仲裁器
│
scripts/
├── run_universe_recall.py                  # [新增脚本] 周度全市场Top 50批处理入口
├── run_universe_ranking.py                 # [新增脚本] 每日盘后ML精排与打散批处理入口
└── run_universe_sync.py                    # [新增脚本] 手动/定时合并生效标的至runtime_config
│
tests/
├── test_universe_recall.py                 # [新增测试] 召回过滤与盘口门槛单元测试
├── test_universe_incubator.py              # [新增测试] 沙盒指标聚合与滑点测算测试
├── test_universe_ranking.py                # [新增测试] 特征工程与ML接口打分测试
├── test_universe_diversity.py              # [新增测试] MMR行业打散与配额测试
└── test_universe_lifecycle.py              # [新增测试] 晋级准入、迟滞防抖与秒踢测试
```

---

## 二、详细数据库设计与状态机 (Database & State Machine)

系统在 `data/trading.db` 中引入 3 张核心表与 1 张历史归档表。

### 2.1 数据库表结构 (DDL)

```sql
-- 1. 全市场候选召回表
CREATE TABLE IF NOT EXISTS candidate_universe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,                  -- 交易周标识 (YYYY-MM-DD，如 '2026-10-12')
    symbol TEXT NOT NULL,                      -- 美股标的代码 (标准化为 'US.XXXX')
    underlying_name TEXT NOT NULL DEFAULT '',  -- 标的名称
    industry TEXT NOT NULL DEFAULT 'OTHER',    -- 所属行业/板块分类 (如 'SEMICONDUCTOR', 'TECH_MEGA', 'CRYPTO')
    option_daily_volume REAL NOT NULL,         -- 近5日日均期权成交量
    open_interest REAL NOT NULL,               -- 期权总持仓量
    stock_turnover_usd REAL NOT NULL,          -- 正股近20日日均成交额 (美元)
    median_spread_pct REAL NOT NULL,           -- 近月平值期权买卖价差中位数比例 (例如 0.025 表示 2.5%)
    status TEXT NOT NULL DEFAULT 'ACTIVE',     -- 状态: 'ACTIVE' (活跃), 'IN_SANDBOX' (孵化中), 'EXCLUDED' (排除)
    created_at TEXT NOT NULL,
    UNIQUE(week_start, symbol)
);
CREATE INDEX IF NOT EXISTS idx_candidate_lookup ON candidate_universe(week_start, status);

-- 2. 沙盒影子孵化记录表
CREATE TABLE IF NOT EXISTS shadow_incubator (
    symbol TEXT PRIMARY KEY,                   -- 标的代码
    first_observed_at TEXT NOT NULL,           -- 首次进入沙盒日期
    observation_days INTEGER NOT NULL DEFAULT 0, -- 已累计有效观察交易日数
    signal_triggers INTEGER NOT NULL DEFAULT 0, -- 触发有效突破信号总次数
    simulated_wins INTEGER NOT NULL DEFAULT 0,  -- 虚拟盈利次数
    simulated_losses INTEGER NOT NULL DEFAULT 0, -- 虚拟亏损次数
    simulated_pnl_usd REAL NOT NULL DEFAULT 0.0, -- 虚拟累计已实现净盈亏
    simulated_max_dd REAL NOT NULL DEFAULT 0.0, -- 虚拟最大回撤比例 (如 0.12 表示 12%)
    avg_slippage_pct REAL NOT NULL DEFAULT 0.0, -- 平均撮合滑点比例
    volume_retention_ratio REAL NOT NULL DEFAULT 0.0, -- 5分钟量能留存率中位数
    latest_ml_score REAL NOT NULL DEFAULT 0.0,  -- 最近一次 ML 模型评分 (0.0 ~ 1.0)
    promotion_eligible INTEGER NOT NULL DEFAULT 0, -- 是否满足晋级门槛 (0:否, 1:是)
    last_evaluated_at TEXT NOT NULL,            -- 最近一次评估时间
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incubator_score ON shadow_incubator(promotion_eligible, latest_ml_score DESC);

-- 3. 动态晋级生效外援表
CREATE TABLE IF NOT EXISTS promoted_universe (
    symbol TEXT PRIMARY KEY,                   -- 晋级生效标的代码
    promoted_at TEXT NOT NULL,                 -- 晋级时间戳 (ISO-8601)
    expires_at TEXT NOT NULL,                  -- 授权到期时间戳 (默认 14 ~ 28 天)
    promotion_score REAL NOT NULL,             -- 晋级时的 ML 综合分数
    industry TEXT NOT NULL DEFAULT 'OTHER',    -- 标的行业
    live_trades_count INTEGER NOT NULL DEFAULT 0, -- 实盘交易笔数
    live_realized_pnl REAL NOT NULL DEFAULT 0.0,  -- 实盘累计净盈亏
    consecutive_hard_stops INTEGER NOT NULL DEFAULT 0, -- 近期连续硬止损次数
    status TEXT NOT NULL DEFAULT 'ACTIVE',     -- 状态: 'ACTIVE' (生效中), 'EXPIRED' (自然到期), 'EVICTED' (应急清退)
    eviction_reason TEXT DEFAULT '',           -- 清退原因说明
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promoted_active ON promoted_universe(status);

-- 4. 标的池变更与仲裁审计历史表
CREATE TABLE IF NOT EXISTS universe_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,                  -- 'RECALL_COMPLETED', 'PROMOTION', 'DEMOTION', 'FAST_EVICTION', 'SYNC_RUNTIME'
    symbol TEXT NOT NULL,                      -- 涉及标的代码 (或 'ALL')
    details_json TEXT NOT NULL DEFAULT '{}',   -- 审计载荷详情
    created_at TEXT NOT NULL
);
```

### 2.2 外援标的状态机设计 (State Machine)

```text
[全市场候选] 
    │ (召回硬门槛通过)
    ▼
[CANDIDATE_ACTIVE] 
    │ (进入沙盒观察)
    ▼
[IN_SANDBOX] ──(连续10日演练 + 胜率/回撤/ML达标)──> [PROMOTED_ACTIVE] 
    │ (样本不合格 / 超期淘汰)                           │
    ▼                                                  ├─(2~4周到期且ML评分衰减)──> [EXPIRED]
[EXCLUDED]                                             │
                                                       └─(盘中价差恶化 / 连触2次硬止损)──> [EVICTED]
```

---

## 三、各核心模块算法与代码设计

### 3.1 模块 1：全市场流动性 Top 50 粗排召回器 (`recall_scanner.py`)

#### 类与函数接口设计
```python
from dataclasses import dataclass
from typing import Sequence
import pandas as pd
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

@dataclass(frozen=True)
class CandidateSymbol:
    symbol: str
    underlying_name: str
    industry: str
    option_daily_volume: float
    open_interest: float
    stock_turnover_usd: float
    median_spread_pct: float

class RecallScanner:
    def __init__(self, broker: FutuPaperBroker, store: TradingStore):
        self.broker = broker
        self.store = store
        
    def fetch_market_option_leaders(self, top_n: int = 100) -> list[dict]:
        """从券商/市场数据接口拉取美股期权总成交量与持仓量头部标的。"""
        ...

    def filter_candidates(self, raw_candidates: Sequence[dict]) -> list[CandidateSymbol]:
        """执行硬性物理门槛过滤：
        1. 排除已在 Core 13 只底仓中的标的；
        2. 正股日均成交额 > 5 亿美元；
        3. 期权日均成交量 > 50,000 张；
        4. 近月平值期权买卖价差中位数 <= 3.0% (或 <= $0.05)；
        5. 截取前 50 只合格标的。
        """
        ...

    def run_weekly_recall(self, week_start: str) -> list[CandidateSymbol]:
        """周度批处理入口，过滤结果持久化至 candidate_universe 表。"""
        ...
```

#### 关键实现逻辑与行业打标对照表
```python
INDUSTRY_MAPPING = {
    # 科技与芯片
    "US.NVDA": "SEMICONDUCTOR", "US.AMD": "SEMICONDUCTOR", "US.TSM": "SEMICONDUCTOR",
    "US.AVGO": "SEMICONDUCTOR", "US.ARM": "SEMICONDUCTOR", "US.MU": "SEMICONDUCTOR",
    "US.AAPL": "TECH_HARDWARE", "US.MSFT": "TECH_SOFTWARE", "US.GOOG": "INTERNET",
    "US.META": "INTERNET", "US.AMZN": "E_COMMERCE",
    # 高弹性题材
    "US.COIN": "CRYPTO", "US.PLTR": "AI_BIGDATA", "US.CRWD": "CYBER_SECURITY",
    # 指数ETF
    "US.SPY": "INDEX_ETF", "US.QQQ": "INDEX_ETF", "US.GLD": "COMMODITY_ETF",
}
```

---

### 3.2 模块 2：隔离沙盒影子演练器 (`incubator_engine.py`)

#### 类与函数接口设计
```python
class IncubatorEngine:
    """负责在交易时段后台静默接收并处理 Top 50 候选标的的虚拟突破信号。"""
    
    def __init__(self, store: TradingStore):
        self.store = store
        
    def record_simulated_signal(
        self, symbol: str, signal_type: str, z_score: float, 
        volume_ratio: float, bid_ask_spread_pct: float
    ) -> None:
        """记录沙盒中的虚拟突破信号，不触发券商下单。"""
        ...
        
    def update_simulated_trade_outcome(
        self, symbol: str, is_win: bool, pnl_usd: float, return_ratio: float,
        slippage_pct: float, hold_minutes: float
    ) -> None:
        """按虚拟平仓更新该标的的胜率、回撤和盈亏分布。"""
        ...
        
    def aggregate_daily_metrics(self) -> None:
        """每日收盘后更新 shadow_incubator 中的 observation_days、simulated_max_dd、
        平均滑点与量能留存率。
        """
        ...
```

#### 虚拟最大回撤与期望值算法公式
- **期望每笔收益 ($E$)**：
  $$E = (WinRate \times AvgWin) - ((1 - WinRate) \times AvgLoss) - Slippage$$
- **虚拟最大回撤 ($MDD$)**：
  $$MDD = \max_{\tau \le t} \left( \frac{Peak\_PnL_{\tau} - PnL_t}{Peak\_PnL_{\tau} + Capital\_Base} \right)$$
- 若 $MDD \ge 0.25$，则强制置 `promotion_eligible = 0`。

---

### 3.3 模块 3：机器学习精排评分器 (`ranking_pipeline.py`)

#### 特征向量提取规范 (`Feature Vector Definition`)
```python
class RankingPipeline:
    def __init__(self, model_path: str = "data/ml_models/volatility_scorer_v3.pkl"):
        from futu_quant.ml_symbol_scorer import MLSymbolScorer
        self.scorer = MLSymbolScorer(model_path=model_path, threshold=0.50, enabled=True)

    def extract_features(self, symbol: str, broker, store) -> dict[str, float]:
        """
        特征构建字典:
        - f_atr_ratio_20d: 20日ATR变化率
        - f_zscore_20d: 20日成交量及波幅复合Z值
        - f_beta_to_spy: 相对SPY的超额Alpha/Beta
        - f_iv_rv_ratio: 当前期权IV / 30日历史RV
        - f_iv_percentile: IV历史分位数
        - f_sandbox_win_rate: 沙盒虚拟胜率
        - f_sandbox_expectancy: 沙盒期望收益
        - f_avg_slippage: 真实盘口摩擦率
        """
        ...

    def rank_incubated_symbols(self, symbols: list[str]) -> pd.DataFrame:
        """调用 MLSymbolScorer 对沙盒标的批量打分，按 ml_score 降序输出。"""
        ...
```

---

### 3.4 模块 4：多样性打散与重排器 (`diversity_filter.py`)

#### MMR (Maximal Marginal Relevance) 贪心打散算法
为了防止选出的外援全部集中在单一行业，采用 MMR 算法迭代选择：

$$\text{NextSymbol} = \arg\max_{s_i \in \text{Candidates}} \left[ \lambda \cdot \text{Score}(s_i) - (1 - \lambda) \cdot \max_{s_j \in \text{Selected}} \text{Corr}(s_i, s_j) \right]$$

- **$\lambda = 0.65$**：兼顾模型收益评分（权重 65%）与行业相关性惩罚（权重 35%）。
- **硬性约束**：
  1. `Selected` 集合中同 `industry` 的标的数量 $\le 1$；
  2. $\text{Corr}(s_i, s_j) \le 0.70$。

```python
class DiversityFilter:
    def __init__(self, max_satellites: int = 5, correlation_threshold: float = 0.70):
        self.max_satellites = max_satellites
        self.correlation_threshold = correlation_threshold

    def select_diverse_portfolio(
        self, ranked_df: pd.DataFrame, corr_matrix: pd.DataFrame, industry_map: dict[str, str]
    ) -> list[str]:
        """执行行业配额与相关性打散，产出最终 3~5 只外援名单。"""
        selected: list[str] = []
        selected_industries: set[str] = set()
        
        for _, row in ranked_df.iterrows():
            if len(selected) >= self.max_satellites:
                break
            symbol = row["symbol"]
            ind = industry_map.get(symbol, "OTHER")
            
            # 1. 行业配额检查
            if ind != "INDEX_ETF" and ind in selected_industries:
                continue
                
            # 2. 两两相关性检查
            conflict = False
            for prev in selected:
                if corr_matrix.loc[symbol, prev] > self.correlation_threshold:
                    conflict = True
                    break
            if conflict:
                continue
                
            selected.append(symbol)
            selected_industries.add(ind)
            
        return selected
```

---

### 3.5 模块 5：晋级仲裁与生命周期管理器 (`lifecycle_manager.py`)

#### 准入、迟滞防抖与清退仲裁逻辑
```python
class LifecycleManager:
    def __init__(self, store: TradingStore):
        self.store = store

    def evaluate_promotions(self, current_active: list[str], top_candidates: list[str]) -> list[str]:
        """
        实施迟滞缓冲 (Hysteresis Buffer):
        - 如果当前外援池未满 ( < 3只)，直接晋级打分 >= 0.75 的候选；
        - 如果需要替换，新候选得分必须比现任最低分外援高出至少 10%，且连续2天保持领先；
        - 现任只要仍在前8名且无严重回撤，保持在岗。
        """
        ...

    def evaluate_evictions(self, broker) -> list[str]:
        """
        盘中或盘后清退检查:
        1. 检查 promoted_universe 中是否有连续遭遇 2 次硬止损的标的；
        2. 检查买卖价差是否恶化超过 10%；
        3. 检查授权是否超过到期时间 (expires_at)。
        触发清退的标的自动设置 status='EVICTED' 并从交易池移出。
        """
        ...
```

---

## 四、与主交易引擎 (`run_service.py`) 的无缝集成

### 4.1 标的池动态合并与原子热加载

在 `src/futu_quant/config.py` 中重构 `load_volatility_symbols()` 函数：

```python
DEFAULT_CORE_SYMBOLS: tuple[str, ...] = (
    "US.COIN", "US.AMD", "US.NVDA", "US.AVGO", "US.AAPL",
    "US.META", "US.AMZN", "US.GOOG", "US.MSFT", "US.TSM",
    "US.QQQ", "US.SPY", "US.GLD",
)

def load_volatility_symbols() -> tuple[str, ...]:
    """
    实际交易标的池 = 永久核心底仓 (13只) ∪ 当前有效晋级外援 (3~5只)
    保持完全去重与排序稳定。
    """
    from futu_quant.store import TradingStore
    store = TradingStore()
    
    # 1. 核心底仓 (永久守正)
    active_symbols = list(DEFAULT_CORE_SYMBOLS)
    
    # 2. 读取 promoted_universe 中处于 ACTIVE 且未过期的外援
    try:
        with store.connection() as conn:
            cursor = conn.execute("""
                SELECT symbol FROM promoted_universe 
                WHERE status = 'ACTIVE' AND expires_at >= datetime('now')
            """)
            promoted = [row["symbol"] for row in cursor.fetchall()]
            active_symbols.extend(promoted)
    except Exception:
        pass  # 数据库异常时自动降级仅使用底仓，零阻断
        
    return tuple(dict.fromkeys(active_symbols))
```

### 4.2 零停机与故障隔离保障 (Fault-Tolerance)
1. **主交易进程完全免受外部流水线故障影响**：若抓取 Top 50 失败、ML 模型异常或网络断开，主策略 `run_service.py` 自动降级为底仓 13 只标的，**永远不会抛异常中断交易**；
2. **热更新无需重启服务**：外援标的的晋升或退出仅通过写入 SQLite 即可完成，由于 `load_volatility_symbols()` 每次策略扫描周期动态读取并合并，变动在下一个 30 秒周期**自动无感热生效**。

---

## 五、定时任务与调度时钟集成 (`Scheduled Tasks`)

在 `src/futu_quant/scheduled_tasks.py` 中注册两个标准离线调度任务：

```python
# 1. 周度全市场期权成交召回任务
WEEKLY_UNIVERSE_RECALL_TASK = ScheduledTaskDefinition(
    task_id="weekly_universe_recall",
    name="全市场期权流动性 Top 50 召回",
    interval_hours=168,  # 每周一次
    default_enabled=True,
    description="每周日晚抓取全市场期权成交量前 50 标的并进入沙盒观察",
)

# 2. 每日盘后特征更新与 ML 赛马精排任务
DAILY_UNIVERSE_RANKING_TASK = ScheduledTaskDefinition(
    task_id="daily_universe_ranking",
    name="标的池 ML 精排与打散轮换仲裁",
    schedule_time_ny="16:35:00",  # 美东收盘后 35 分钟执行
    default_enabled=True,
    description="收盘后计算候选标的微观特征，运行 ML 精排模型并执行外援晋级/清退",
)
```

---

## 六、测试验证规范与质量门禁 (Test Suite Specification)

| 测试文件 | 覆盖的测试场景 | 验收门槛 |
| :--- | :--- | :--- |
| `test_universe_recall.py` | • 测试全市场成交量前 100 标的的流动性过滤规则<br>• 测试平值价差 $> 3\%$ 标的被正确排除<br>• 验证底仓 13 只标的不会被重复写入候选表 | 100% 通过，过滤逻辑零遗漏 |
| `test_universe_incubator.py` | • 模拟 10 日突破信号记录与特征聚合<br>• 验证虚拟最大回撤 $\ge 25\%$ 时 `promotion_eligible` 正确置 0<br>• 验证量能留存率计算准确性 | 100% 通过，微观指标计算分毫不差 |
| `test_universe_ranking.py` | • 验证 ML 特征向量构建格式严格对齐 V3 Scorer 规范<br>• 测试绝对 IV $> 85\%$ 标的获得惩罚低分 | 100% 通过，打分排序符合预期 |
| `test_universe_diversity.py` | • 模拟 5 只全为半导体的极端高分候选池<br>• 验证 MMR 打散算法强制保证每个细分行业最多 1 席<br>• 验证两两相关系数 $> 0.70$ 的标的被正确跳过 | 100% 通过，行业与相关性打散有效 |
| `test_universe_lifecycle.py` | • 测试得分仅高出 2% 时触发迟滞缓冲不换人<br>• 测试连续 2 次硬止损触发应急秒踢（`FAST_EVICTION`）<br>• 验证底仓 13 只标的在任何情况下均不被剔除 | 100% 通过，状态机流转零死锁 |

---

## 七、实施路线图与交付检查清单 (Delivery Checklist)

- [ ] **Step 1: 数据层落地**
  - 在 `src/futu_quant/store.py` 的初始化建表脚本中添加 `candidate_universe`, `shadow_incubator`, `promoted_universe`, `universe_lifecycle_events` 四张表及索引。
- [ ] **Step 2: 核心组件实现**
  - 实现 `src/futu_quant/universe/` 下的 5 大核心模块（召回、沙盒、精排、重排、仲裁）。
- [ ] **Step 3: 主交易接口无缝挂接**
  - 更新 `src/futu_quant/config.py` 中的 `load_volatility_symbols()` 实现自动并集逻辑与安全降级。
- [ ] **Step 4: 定时任务与 UI 可视化看板**
  - 注册定时调度脚本，并在 Streamlit 驾驶舱中增加**“标的孵化赛马中心”**页面，展示当前沙盒观察名单、ML 积分榜及当前生效外援。
- [ ] **Step 5: 单元测试全覆盖**
  - 执行 `pytest tests/test_universe_*.py` 并通过全部用例，代码风格 `ruff check` 零警告。
