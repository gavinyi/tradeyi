# Tradeyi 待办优先级表（2026-10-24 确认版）

- 用户确认：2026-10-24 00:0x 「确认，可以按此实施。很合理，加油」
- 门禁：Product PRD → Architect 设计 → Chief 签字 → Chief 出计划 → 大需求经用户审批 → 才可写代码
- 部署纪律：交易时段（北京时间 21:30–04:00）只写不部署；部署须「全量回归绿 + Reviewer 签字」

## P0-0（最高优先 · 用户 2026-10-24 裁定：**先解决这个，之后随时可开工**）

| # | 事项 | 责任人 | 前置 / 依赖 | 状态 |
|---|---|---|---|---|
| 0 | **测试环境隔离 Tier 1**（已完成） | Chief（用户裁定不出 PRD，直接实施） | 设计稿：`docs/test_environment_isolation_design.md` | ✅ **已完成 2026-10-24 00:55**：测试区 `E:\Thisyi\trade-test`（worktree，`test` 分支 `c3627fa`）+ 独立 venv；**生产目录停 `master`（正式基线）**；隔离区全量 **959 passed / 20 skipped / 0 failed（349.77s）**；会话守卫比对生产 `runtime_config` 指纹前后一致。**分支模型（用户 10-24 定）**：`master`=正式基线/部署，`test`=开发验证，旧 master 存 `archive/2026-09-windows-migration` tag，今夜生产态存 `snapshot/2026-10-23-production` tag。**待收盘后**：`test` 领先 master 的 1 个提交（隔离守卫+测试修复）经 Reviewer 签 + 用户批准后合入 master |

> **用户裁定（2026-10-24）**：**先解决环境隔离，之后什么时候都可以开工**；在此之前**所有开发项不动手**；
> 生产相关改动一律**收盘后（北京时间 04:00 之后）**再动。隔离环境搭建本身只新建目录 / 独立 venv / 独立库，
> **不触碰生产运行时**（生产目录、生产库、生产锁均不动）。

## P0（先做）

| # | 事项 | 责任人 | 前置 / 依赖 | 状态 |
|---|---|---|---|---|
| 1 | 配置留痕 C1（`audit_events.actor` 列+索引、`store.audit` 必填、55 处）→ C2（`setting_versions` + 登记表）→ C3（`set_setting(actor,reason)` + 值相同跳过 + 写入量对比） | Coder → Reviewer → Tester | 恢复实盘交易的硬前提（Product 红线） | **先冻结，等用户单独放行** |
| 2 | 告警推送断链修复（PRD + 设计**已签字**；实施计划已出 `docs/alert_delivery_fix_plan.md`） | Coder → Reviewer → Tester | **待 P0-0 完成 + 收盘后** | **已过闸，等开工** |
| 3 | 「暂停新开仓」生效口径核实 | Ops / Chief | — | ✅ 已核实（见下方口径） |
| 4 | BUY 路径是否写 `registered_fill_*`、读侧缺口 | Reviewer（只读） | — | 进行中 |
| 5 | **候选合约质量过滤（P0-5）**：低流动性 / 宽价差 / 当日到期合约在**候选池阶段**排除；`exclusion_reason` 审计 + 监控页可见；降级放行只对未排除候选生效；降级上限 900s→300s（需用户拍板） | Product（PRD）→ Architect（设计）→ Reviewer（红区审）→ **用户拍板** → Coder | 根因①已由 Ops 探针确认（低流动性报价冻结，非订阅故障） | **PRD 进行中**；门禁维持现状 |

## P1

| # | 事项 | 责任人 |
|---|---|---|
| 5 | 心跳 / PID 陈旧清理（`trading_service` 仍 STOPPED、`data/trading-service.pid` 旧值 34168） | Ops |
| 6 | 页面 / 控制面租户感知（pages/10、pages/15、`execution_mode`） | Coder |
| 7 | Worker 侧 `decision()` 补 `tenant_id`（仅审计） | Coder |
| 8 | `PORTFOLIO_STRESS` 告警租户化 + 滞回阈值（当前每约 15 分钟 flapping） | Coder |
| 9 | 页面「暂停新开仓」开关（写 `volatility_new_entries_paused:{tenant}`，禁用 `strategies_paused`） | Coder |
| 10 | 旧无前缀死键清理 101 条（retry_until 19 / retry_z 19 / rejection 18 / notified 45） | Ops |
| 11 | ~~测试环境隔离落地~~ → **已提升为 P0-0** | Coder |
| 12 | git 白名单提交 + `test` 分支指向该提交（并入 P0-0 的分支策略） | Chief |
| 13 | **生产进程启动方式与守护**：生产 Worker/雷达**不得从 DSH/Agent 会话的 tool call 启动**（关闭 DSH 会连带杀死进程树，2026-10-24 00:15 事故已验证）；必须走桌面双击 `start.bat`（conhost 父进程）；补进程失联守护 + 告警 | Ops / Chief |

## P2

| # | 事项 | 说明 |
|---|---|---|
| 13 | ML 板块整体（A→B→C 串行 + 自动月度重训规划） | 用户 2026-10-23 决定：自动月度重训统一并入 ML 板块后再做；当前批次未放行 |
| 14 | 效率优化（停机 VACUUM 回收约 663MB、缓存/索引写进 `initialize()`、对账日志降采样） | 用户明确：所有功能事项完结后再做 |

## 移出范围（用户 2026-10-24 裁定）

- **财报策略（Earnings Gamma / Earnings Gamma Manual）**：不再使用，**移出一切改造与修复范围**。
  已按裁定把 `strategy_enabled:Earnings Gamma` / `Earnings Gamma Manual` 置 **false**（审计事件 `EARNINGS_GAMMA_DISABLED`）；
  `GAMMA_CAMPAIGNS=0`、执行模式 SHADOW，**无持仓风险**，开关可随时恢复。
- **币（crypto）相关功能**：不再使用，移出范围（`strategy_enabled:crypto_volatility_swap` 已为 false；历史 `crypto_*` 状态键暂不清理）。
- **影响**：时区修复范围**不含** `earnings_calendar.py` 三处（只有财报策略链路用）。
  仍需修：`gamma_debug.py:47`（65 次假 BLOCK 直接来源，VOLATILITY campaign 同样命中「合约已于」硬判据）、
  `option_ranking.py:58`（roll 调用未传 `as_of`）、`run_volatility_strategy.py:141`（平仓路径提前 ≤4h）、
  `volatility_optimizer.py:932` 默认值（调用处已传 `ny_today`），外加 `hard_campaign_issues` 的 return 分家。

## 已确认口径

1. **暂停新开仓**：`run_gamma_strategy.py:63-84` 为**租户级优先、全局兜底**。
   当前实盘 `futu_real_master` / 模拟 `futu_paper_sandbox` 经生产解析函数均为 **False（新开仓放行，红线已解除）**；
   `volatility_new_entries_paused=true`（全局）只是**兜底**，**不是**「实盘已暂停」。
   页面/监控判断生效值**必须走生产解析函数**，不得读全局键。
2. **告警投递**：`alerts.py:60` 读取 `ALERT_WEBHOOK_URL`（当前为空）；
   `:92-105` 仅白名单事件 / CRITICAL 才推飞书卡片，其余 ERROR 只落库 → `delivered=0`。
   截至 2026-10-24 00:00，未投递告警 **20 条**。

## 测试策略（用户 2026-10-24 定）

- **单需求/单 bug 分支：只跑该批自己的用例**（目标文件级），不跑全量；
- **全量回归只在「版本合 `master` 上生产」时跑一次**（发布闸门）；
- 单批用例清单必须在交付消息里写明（文件 + 通过数），否则不算验证；
- 例外：改动触及共享核心（`store.py` / `execution.py` / `reconciliation.py` / 迁移）时，**允许要求该批补跑与其调用点相关的用例集**，但仍不跑全量。
