# 期权退出策略拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将财报 Gamma 与波动率期权的退出决策和周期状态彻底拆分，同时保留统一执行基础设施。

**Architecture:** 新建纯函数波动率决策器，财报决策器保持事件语义；共享 runner 只负责收集行情和提交订单，并按触发模式分派。波动率周期状态在持仓边沿初始化和清理，页面与雷达写入独立参数。

**Tech Stack:** Python 3.11、pytest、Streamlit、SQLite、Futu Paper API。

## Global Constraints

- 全程仅允许模拟盘或 Shadow，不增加任何实盘路径。
- 不改变统一风控、对账、下单和撤单接口。
- 新行为必须先有失败测试，随后最小实现并运行全量回归。

---

### Task 1: 独立波动率退出决策器

**Files:**
- Modify: `tests/test_volatility_optimizer.py`
- Modify: `src/futu_quant/volatility_optimizer.py`

- [ ] 写入止盈立即生效、最长持有按开仓时间、峰值回撤只在盈利后生效、衰减阈值可配置的失败测试。
- [ ] 运行专项测试并确认因缺少新接口而失败。
- [ ] 实现 `decide_volatility_action` 和参数化 `volatility_exit_reason`。
- [ ] 运行专项测试确认通过。

### Task 2: Runner 分派与周期状态

**Files:**
- Modify: `tests/test_gamma_automation.py`
- Modify: `scripts/run_gamma_strategy.py`

- [ ] 写入波动率不调用财报事件决策、周期初始化/重置的失败测试。
- [ ] 运行测试确认失败原因正确。
- [ ] 在共享编排器中分派两套决策器，并在持仓边沿记录本轮基线。
- [ ] 运行财报与波动率专项测试。

### Task 3: 独立配置入口

**Files:**
- Modify: `pages/10_volatility_options_strategy.py`
- Modify: `src/futu_quant/volatility_scanner.py`
- Test: `tests/test_volatility_scanner.py`

- [ ] 写入雷达默认退出参数测试并确认失败。
- [ ] 增加波动率独立退出参数控件及活动字段。
- [ ] 自动雷达写入相同默认值。
- [ ] 运行页面静态检查和雷达测试。

### Task 4: 回归验收

**Files:**
- Modify: `docs/strategy_architecture.md`

- [ ] 更新策略退出边界说明。
- [ ] 运行 Ruff。
- [ ] 运行全量 pytest。
- [ ] 运行 Streamlit 页面检查。
- [ ] 检查 8501 端口并确认热更新状态。

说明：当前目录不是 Git 仓库，因此无法执行计划模板中的分步提交；以逐步测试证据替代提交检查点。
