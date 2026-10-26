# 巡检 Runbook（Ops 基准，2026-10-27 起）

> 归口：Ops。脚本：`scripts/scheduled_log_audit.py`（只读、只告警、绝不改数据/开关）。
> 调度：会话内定时任务「盘中静默监控（只读 · 禁 python · 含 shadow 观测）」；危险的全量 Python 巡检不得在生产目录执行（见 §5）。
> 重启后核验见 [restart_verification_runbook.md](restart_verification_runbook.md)。

---

## 1. 巡检项清单（现行）

| 项 | 函数 | 级别 | 说明 |
| :--- | :--- | :--- | :--- |
| 进程失联双校验 | `check_processes` | CRITICAL | PID 身份 + 心跳时效；不受交易时段限制；维护标记期间静默 |
| 训练/回测 CSV 新鲜度与列契约 | `check_csv_freshness` | DEGRADED | P1-07：缺文件/缺 5 个必需列/覆盖率 <99%/落后 >5 个交易日 → 报 |
| 期权快照断采 | `check_snapshot_gaps` | ERROR | 近 10 交易日「有 FILLED 成交但 `option_history` 0 条」→ 报（数据源必须是 `order_intents`，不能用 `fill_ledger`） |
| 费用占位值 | `check_fee_placeholder_ratio` | 告警 | P1-14：近 **7 天**窗口内 `fee_total==1.0` 占位占比 **>5%** → 报；历史行不回填、按全表算会永久误报 |
| 费用账本信息行 | `fee_ledger_summary_line` | 信息 | 历史 `legacy_placeholder_1usd` 始终展示（迁移落地后不得显示成 0） |
| 关注标的纳管 | §4.5 | 信息/告警 | 配置 `runtime_config.ops_watch_symbols`（JSON 数组）；分租户孤儿判定；增删标的只改配置不改代码 |
| 三权/三账对账与挂起 | 对账段落 | ERROR | `MISMATCH` / 策略挂起（`strategy_reconciliation_disabled`、`recovery_blocked`）**严禁静默**，报告顶部置顶 |

### 1.1 已修项的落点（供追溯，不必重做）

- **P1-09 巡检两处 fail-closed 补强**：`6f3c0bb`（2026-10-25 21:16）——列契约「必需列必须存在 + 新列须登记」、日历/日期解析失败**不得静默**（`scheduled_log_audit.py:210` 一带）。
- **P1-14 费用占位值告警**：`6f3c0bb` + `6c05132`（2026-10-25 21:19）——阈值 `FEE_PLACEHOLDER_ALERT_RATIO = 0.05`、窗口 `FEE_PLACEHOLDER_WINDOW_DAYS = 7`，已接入 `inspect_and_alert`。

## 2. 告警口径（不可协商）

- 默认推送 **ERROR 及以上** + 任何带真金敞口/资金/风控门禁标签的事件。
- **不可静默清单（硬编码）**：`DAILY_LOSS_KILL_SWITCH`、`REAL_TRADE_REJECTED`、`ORDER_ENV_MISMATCH`、`LEDGER_MISMATCH`、`RISK_REJECTED`、急停、`RECONCILIATION_*`。
- 防疲劳只靠**显式静音清单**（谁批 / 为何 / 何时复审，禁止通配）。
- 落库与推送解耦：告警写库与飞书投递互不阻塞；投递需端到端探针（`alert_delivery_probe` 心跳）。

## 3. 季度费用校准（P1-16，人工，每季度一次）

**背景（必须写进任何对外文档）**：OpenAPI 拿不到券商侧真实费用——模拟盘 `history_deal_list_query` 直接返回 `ret=-1`；实盘成交查询无 commission/fee 字段；结单只能人工下载。
因此费用口径**永久是估算**：`$60.5/日 = 全费用估算（order_intents.fees 口径 $1,815.34 / 30 交易日）`，**不是已扣款**；任何文档不得写成「实际支出」。

**执行步骤**

1. 从富途客户端/网页下载**最近一个季度结单** → 归档 `data/qa/fee_statements/<年>-Q<季>/`（原件或校验和二者留一）。
2. 比对「结单总费用」与同期 `order_intents.fees` 合计，算**估算系数** = 结单总费用 / 估算总费用。
3. 把「季度、结单总费用、估算总费用、系数、偏差原因（>15% 必须写明）」写入 `data/qa/fee_calibration_log.md`。
4. 系数偏离 1.0 超过 **±15%** → 巡检报 `DEGRADED`，并提「更新 `fee_model` 系数」的改动单（走正常门禁，不自动改）。
5. 校准完成后在群里报一句结论 + 日志路径。

**留痕要求**：结单原件（或校验和）+ 校准日志，两者缺一不算完成。

**不做的事**：不因校准结果自动改限额/熔断阈值；不改 `fee_model` 代码（属代码改动）。

**季度提醒**：每季度首月（1/4/7/10）由 Ops 在自动化任务里建一次提醒；漏跑不得用「估算是老值」静默顶替，必须在下一次巡检报告中写明「本季度未校准」。

## 4. 采集侧口径备忘

- `option_history.quality_status` 是 **ML 训练标签**，口径不可动（`option_history.py` 顶部注释）；心跳的「可用/降级」与训练标签是两回事。
- 期权快照 `update_time` 是**成交驱动**：无成交即冻结，冻结 ≠ 过期；盘口新鲜度按盘口变化年龄判（`data_quality.option_quote_activity`，第三档 `book_active_seconds`）。
- 采集批次在 `option_history.fetched_at`（采集时刻），`time_key` 是合约自身 `update_time`——对账年龄别拿错。

## 5. 执行纪律

- **部署后、重启前禁止在生产目录跑任何构造 `TradingStore()` 的 python**（会触发待迁移检查；授权改造前会真迁移）。
- 巡检只读：不写业务表、不改开关、不删文件。
- `scripts/scheduled_log_audit.py` 是共享文件：改它先在群里报备，随批次合入 `test` → 全量 → `master`。
- 增删关注标的用 `set_setting("ops_watch_symbols", ...)`，不改代码。
