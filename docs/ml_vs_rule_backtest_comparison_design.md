# ML 与规则对照回测与模型满足度验证技术架构设计方案

> **设计文档**: docs/ml_vs_rule_backtest_comparison_design.md  
> **文档版本**: 1.0 (Architecture Design)  
> **责任人**: Architect (架构师)  
> **对齐需求**: docs/ml_vs_rule_backtest_comparison_prd.md (Product)  
> **状态**: 待 Chief 审批签字与计划排期

---

## 一、 架构定位与设计原则

1. **唯一控制变量原则（Single Independent Variable）**：
   - 规则组与 ML 组使用完全相同的：**初始资金、入场门槛（如 DTE/流动性/价差硬筛）、头寸规模、退出规则（-25% / $300 止损与到期平仓）**；
   - **唯一变量是候选标的的排序打分口径**（规则分 `volatility_quality_score` vs `ml_adjusted_score`）。
2. **纯净归因与零未来函数（Zero Lookahead Bias）**：
   - 历史回放时，打分特征只能使用入场时点截面的可用行情数据；
   - 真实平仓收益作为后验评估标准，绝不允许未来标签渗透到前向决策。
3. **计算轻量与非阻塞（Non-blocking Execution）**：
   - 核心回测逻辑向量化，单次 30~90 天回测耗时控制在 3 秒以内；
   - Streamlit 页面提供独立交互 Tab，采用进度汇报模式，严禁阻塞 UI 交互。

---

## 二、 系统分层与核心模块设计

### 1. 整体架构流程图

```text
[ 历史期权快照与行情缓存 (data/option-chain-cache.db / parquet) ]
                           │
                           ▼
          [ 特征组装引擎 (Feature Assembly) ]
                           │
        ┌──────────────────┴──────────────────┐
        ▼                                     ▼
 [ 规则打分引擎 (Rule) ]              [ ML 预测引擎 (ML Scorer) ]
        │                                     │
   规则排序分流                          ML 加权排序分流
        │                                     │
        ▼                                     ▼
 [ 统一事件驱动回测器 (A/B Parallel Backtest Simulator) ]
        │
        ▼
 [ 评估与满足度统计分析层 (Evaluation & Readiness Evaluator) ]
        ├── 绩效对比矩阵 (Total Return, Win Rate, Sharpe, Max Drawdown)
        ├── 单调性与 Rank IC 检验 (Quintile Monotonicity & IC)
        ├── 选品分歧账本 (Divergence Ledger: 避雷与淘金明细)
        └── 准入红绿灯评估 (Readiness Pass/Fail Checklist)
        │
        ▼
 [ Web 可视化看板 (pages/21_ml_model_management.py Tab 3) ]
```

---

## 三、 详细技术规格

### 1. 核心回测比对引擎 (`src/futu_quant/ml_backtest_comparator.py`)
新增独立比对引擎模块，主要接口设计：

```python
@dataclass
class BacktestComparisonResult:
    rule_metrics: dict              # 规则组绩效指标
    ml_metrics: dict                # ML组绩效指标
    equity_series: pd.DataFrame     # 两条净值走势曲线 (日期, 规则净值, ML净值)
    quintile_returns: pd.DataFrame  # 五档分层平均收益率 (Q1 ~ Q5)
    rank_ic: float                  # ML 评分 Rank IC
    rule_ic: float                  # 规则评分 Rank IC
    divergence_records: list[dict]  # 分歧标的明细账本 (避雷/淘金案例)
    readiness_report: dict          # 生产准入判定卡 (是否达标与原因)

def run_ml_vs_rule_comparison(
    store: TradingStore,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
    model_path: str | None = None,
) -> BacktestComparisonResult:
    """
    执行规则组与 ML 组的完全对照回测
    """
```

### 2. 模型满足度（准入判定）判定逻辑
自动化生产准入判定规则（严格量化）：

$$\text{Satisfied} = (R_{\text{ML}} > R_{\text{Rule}}) \land (\text{WinRate}_{\text{ML}} \ge \text{WinRate}_{\text{Rule}} + 3\%) \land (\text{MDD}_{\text{ML}} \le \text{MDD}_{\text{Rule}}) \land (\text{Q1\_Return} > \text{Q5\_Return})$$

1. **总收益率跑赢**：$R_{\text{ML}} > R_{\text{Rule}}$；
2. **胜率明显提升**：$\text{WinRate}_{\text{ML}} \ge \text{WinRate}_{\text{Rule}} + 3\%$；
3. **最大回撤未劣化**：$\text{MDD}_{\text{ML}} \le \text{MDD}_{\text{Rule}}$；
4. **单调性有效**：Top 20% 分组平均收益 > Bottom 20% 分组平均收益，且 Rank IC > 0.03。

若全部满足，返回 🟢 **「满足实盘准入标准」**；任一项不达标，标明具体缺失项并返回 🔴 **「不建议上线实盘」**。

### 3. 分歧归因明细（Divergence Ledger）逻辑
- **ML 避雷 (Avoided Losses)**：
  规则排序在 Top 20%（规则想买）、但 ML 评分在 Bottom 40%（ML 压低排名未买），且该期权实际走势亏损；
- **ML 淘金 (Captured Alpha)**：
  规则排序在 Bottom 40%（规则漏选）、但 ML 评分在 Top 20%（ML 提拔买入），且该期权实际走势盈利；
- 记录到下钻明细表，支持导出或直接在界面表格查看。

### 4. 界面集成 (`pages/21_ml_model_management.py`)
在现有「ML 模型管理」页面中新增 **Tab 3：「🔬 规则 vs ML 对照回测与满足度验证」**：
- 顶部控制器：回测时间窗口选择（最近 30 天 / 60 天 / 90 天）、初始资金输入、一键回测按钮；
- 第一区域：**生产准入判定卡**（红/绿指示徽章 + 达标指标项清单）；
- 第二区域：**核心指标对照矩阵**（总收益、胜率、夏普、回撤并列卡片，高亮标注胜出方）；
- 第三区域：**净值曲线叠加走势图**（Plotly 双曲线图表）与 **五档分层柱状图**；
- 第四区域：**选品分歧下钻明细表**（展示避雷与淘金标的明细）。

---

## 四、 风险分析与防故障机制

1. **样本不足风险**：
   - 若回测区间内期权成交样本过少（如 `< 15` 笔），系统提示“有效样本不足，统计结论可能失真”，避免小样本过拟合误判。
2. **字符集与编码防御**：
   - 彻底修复 GBK 控制台打印 Emoji 导致的 `UnicodeEncodeError`：在所有输出/日志路径强制 `sys.stdout.reconfigure(encoding='utf-8')` 或使用纯文本安全标签替代生硬字符。
3. **数据一致性**：
   - 回测完全复用现有 `volatility_quality_score` 规则函数与 `load_ml_model` 模型加载器，杜绝两套打分口径不一致。

---

## 五、 验收标准与交付物

1. **单元测试与引擎验证**：
   - 编写 `tests/test_ml_backtest_comparator.py`，验证：
     - 在合成样本数据下，对照回测能正确产出两套指标与净值；
     - 当构造单调性破坏数据时，准入判定卡准确输出 🔴 未达标状态；
     - 归因分歧账本准确筛选出“避雷”与“淘金”样本。
2. **界面交互与图表验证**：
   - 打开 `pages/21` 切换至 Tab 3，选择最近 30 天点击运行，3 秒内完成计算并完整渲染指标、双线净值图与五档柱状图。
