# 策略架构规划

## 波动率期权策略新开仓门槛

`volatility_long_gamma` 新开仓现在必须同时满足：

1. 同一标的最多保留 2 个活动结构；
2. 质量评分至少 70；
3. 模型预期收益至少为预计总交易成本的 2 倍；
4. 买卖价差不得超过期权价格的 10%，缺少有效盘口时按不通过处理。

这些门槛只影响新开仓，不阻止已有仓位执行止盈、止损、到期和风险退出。

## 成交成本与周期绩效口径

系统将成交成本拆分为四类：

- **手续费**：券商成交回报中的实际 commission；若无回报则使用保守估算并标记来源。
- **价差成本**：成交时盘口中间价到买卖价差的一半，按合约乘数和成交数量计算。
- **价格冲击**：成交价相对盘口中间价的不利偏离，只在成交时有有效 bid/ask 时统计。
- **参考价滑点**：成交价相对下单参考价的方向性偏离，用于诊断，不与价差成本重复扣除。

逐笔成交写入 `fill_ledger`，通过成交 ID 幂等去重；轮询只能按累计成交数量增加量补账。周期报表按美东交易时间分为日、周、月，已实现盈亏使用 FIFO 平仓归因，成交现金流不直接等同于收益。缺少盘口时保留成交，但行情覆盖率必须单独展示。

系统按“策略实例”管理，而不是按页面或标的混合管理。

## 策略层

- 正股策略：`core_satellite`、均线交叉、RSI 均值回归、突破策略。
- 期权策略：`earnings_gamma`、`volatility_long_gamma`，以及未来新增的价差、备兑、保护性策略。
- 每个策略拥有独立的策略 ID、标的池、参数、运行模式、资金配额、审批证据和虚拟持仓。

`EARNINGS` 和 `VOLATILITY` 已拆为两个独立策略，因为它们的事件来源、入场条件、IV 阈值、
持有周期和验收方法不同：

- `earnings_gamma`：以双源财报事件为强触发。
- `volatility_long_gamma`：以行情波动、趋势和波动率结构为触发。

## 共用服务层

所有期权策略共享以下能力，但不共享策略状态：

- 期权链搜索与合约选择
- 行情快照、IV、Greeks 与流动性分析
- 组合级 Delta/Gamma/Vega/Theta 风控
- PAPER/SHADOW 执行、撤单、追价和幂等控制
- 独立持仓账本、决策日志、回测、审批和绩效归因

## 退出策略边界

财报 Gamma 和波动率期权只共享订单执行，不共享退出计算：

- `earnings_gamma` 按财报事件时间计算入场窗口、事件后止盈/回撤和财报后最长持有。
- `volatility_long_gamma` 从本轮持仓建立时间计算止盈、止损、盈利峰值回撤和最长持有，并独立监控实现波动收缩、IV 相对实际入场下降、Gamma/Theta 效率衰减与到期风险。
- 波动率活动每次从空仓进入持仓都会重置最高价、入场 IV、入场 Gamma/Theta 效率和收缩确认次数，上一轮数据不得进入下一轮计算。
- 退出订单提交不等于退出完成。只有 PAPER 或 SHADOW 持仓实际归零后，才启动退出冷却和下一轮周期编号。

共享执行编排器只负责获取行情、读取持仓、调用对应退出策略并提交模拟订单。策略语义必须保留在各自的纯决策函数中。

## 成熟期权生命周期与重入保护（1-11项整改）

波动率期权活动采用显式生命周期：`OPEN`、`EXIT_PENDING`、`FLAT_PENDING_CONFIRMATION`、`COOLDOWN`、`ROLLED`、`CLOSED`、`BLOCKED`。退出订单提交不等于退出完成；只有成交回报、富途持仓和本地 fill ledger 三者确认归零后，才开始按退出原因计冷却。

- 默认关闭强信号绕过冷却；止盈、盈利保护、硬止损和到期退出后禁止立即买回同一合约。
- 记录 `exit_episode`、`signal_epoch`、`exit_signal_epoch` 和合约代码，防止同一信号周期重复入场。只有动态状态真正经历 inactive→active，才产生新的信号 epoch。
- 同一标的、同一合约和同一到期日分别执行冷却；到期退出后只能滚动到更远到期日，默认新合约 DTE 为 5–21 天，且不得回滚原到期日。
- 多腿组合以 `combo_id` 和全部腿为交易单元。任一腿处于 EXECUTING/CLOSING/ROLLBACK_PENDING/失衡，均禁止新开仓；主腿归零不能掩盖残余保护腿或风险腿。
- 止盈参考可成交盘口、标的方向、IV/RV、Delta/Gamma/Theta/Vega 和 R 倍数；单张持仓明确为整张退出，不伪装成分批止盈。
- FIFO 是默认批次分配口径，LIFO/RISK_PRIORITY 只能显式选择；批次、组合、退出 episode 和重入原因必须可审计。

该设计参考 OCC 的标准化期权风险披露（到期、行权、流动性和组合风险）、Cboe Greeks 教材及 IBKR Bracket/OCA 生命周期文档。Futu OpenD 未提供交易所级原子 OCO，因此本系统的 OCO/Bracket 仍为应用层状态机，必须依赖成交确认、撤单确认和组合失衡保护。

参考：
- OCC：[Characteristics and Risks of Standardized Options](https://prd-web.theocc.com/getmedia/23e090c7-000e-46f7-bd0c-148b3365caec/eo-short-put.pdf)
- Cboe：[Learning the Greeks](https://www.cboe.com/insights/posts/learning-the-greeks-an-experts-perspective)
- IBKR：[Bracket Orders & OCA Groups](https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/bracket-orders-oca-groups)

## 批次级止盈与保护

`volatility_long_gamma` 同时维护整仓周期状态和批次级状态：

- 每次首批入场或盈利确认后的金字塔加仓生成独立 tranche，记录入场时间、数量、成本、初始风险、MFE、MAE 和 R 倍数；
- `exit_allocation_policy` 支持 `FIFO`（默认兼容）、`LIFO` 和 `RISK_PRIORITY`，只影响分批减仓分配，不允许亏损摊平；
- R 倍数基于批次固定的 `initial_risk`，运行中不因价格变化重算；定义风险组合以净借记和费用为基础；
- 应用层 OCO/Bracket 状态使用 `oco_groups` 记录 TP/SL 触发和取消状态。OpenD 不提供原子 OCO，因此成交与取消存在竞态，任何未确认取消都禁止重复提交新的平仓保护单；
- 绩效报告输出批次成交、MFE、MAE、捕获率、R 倍数、加仓次数和加仓实现盈亏。历史无批次数据只按旧 FIFO 口径展示，不回填猜测批次。

## 任务看板定时器

统一服务（`scripts/run_service.py`）内建两个中国时间的每日定时器，可在
“系统监控 → 调度任务 → 任务看板定时器” 查看与启停：

- **策略复盘与执行质量**：每天 04:15（中国时间）生成上一个美东交易日的
  `build_strategy_review` HTML 报告，保存到 `data/reports/strategy_review_<日期>.html`，
  并把结论与 HTML 登记为“待 DSH IM 投递”记录（`DAILY_STRATEGY_REVIEW_IM_PENDING`，默认启用）。
  DSH 侧的定时 agent 到点读取该记录，用 agent 能力把复盘总结 + HTML 投递到用户已接好的
  微信 chat（dsh IM 插件通道），投递成功后回写 delivered，避免重发。
- **电脑关机**：每天 04:30（中国时间）执行系统关机（默认关闭，需手动开启；
  建议先用 `AUTO_SHUTDOWN_DRY_RUN=true` 演练模式验证，演练只写审计记录不真正关机）。

两个定时器均按“每日一次”幂等：当天已触发过则不再重复，服务停机错过触发点会在
下一次服务周期自恢复补跑。开关持久化在 SQLite `system_settings`
（`AUTO_DAILY_STRATEGY_REVIEW_ENABLED` / `AUTO_COMPUTER_SHUTDOWN_ENABLED`），
触发时间记录在 `last_board_timer:*` 键中。

### DSH 定时 agent 投递（04:15 复盘 → 微信）

**分工**：Python `run_service.py` 是**精确**触发器（04:15 生成 HTML + 登记待投递、
04:30 关机）；DSH 侧只挂**一条"轮询待投递"提醒**，让 agent 把已登记的复盘投到微信。
两者解耦：精确定时由 Python 保证，微信投递走 agent 能力（提示词与 `schedule_create`
命令见 `docs/dsh_schedule_prompts.md`）：

1. Python 04:15：`run_daily_strategy_review` 生成 HTML、登记
   `DAILY_STRATEGY_REVIEW_IM_PENDING`（PENDING）。
2. DSH Schedule 轮询提醒（如每 10 分钟）触发 agent 跑
   `uv run python scripts/dsh_task_board_agent.py poll`：
   - `pending=false` → 本轮无货，agent 安静结束；
   - `pending=true` → agent 把 `conclusion` 文本经当前 DSH IM 微信 chat 渠道发出，
     再调 `dsh_im_return_file` 把 `html_path` 的 HTML 文件交给同一渠道自动上传。
3. 发完跑 `uv run python scripts/dsh_task_board_agent.py deliver` 回写 DELIVERED；
   后续轮次因记录已非 PENDING 而不再重发（"每日一次、不重发"由 PENDING→DELIVERED 守卫）。

`scripts/dsh_task_board_agent.py` 的子命令（stdout 均为 JSON）：
`review`（手动生成+登记）、`poll`（只读待投递，供 DSH 轮询）、`deliver`（标记已投递）、
`shutdown`（04:30 关机，默认演练）。

## 页面层

- “交易配置”维护正股策略。
- “期权配置”维护期权标的池和通用合约筛选参数。
- “期权策略”下每个策略拥有独立配置与运行状态，不与 Core Satellite 混放。
- “账户总览、订单管理、风控中心、系统监控”跨策略汇总，但必须保留策略 ID 归属。

新增期权策略时，默认创建新的策略 ID 和配置模型，复用统一期权服务与统一执行层；只有入场逻辑
完全相同、仅参数档位不同的情况，才使用同一策略下的不同参数档案。
