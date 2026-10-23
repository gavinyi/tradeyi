# Tradeyi 待办优先级表（2026-10-24 确认版）

- 用户确认：2026-10-24 00:0x 「确认，可以按此实施。很合理，加油」
- 门禁：Product PRD → Architect 设计 → Chief 签字 → Chief 出计划 → 大需求经用户审批 → 才可写代码
- 部署纪律：交易时段（北京时间 21:30–04:00）只写不部署；部署须「全量回归绿 + Reviewer 签字」

## P0-0（最高优先 · 用户 2026-10-24 裁定：**先解决这个，之后随时可开工**）

| # | 事项 | 责任人 | 前置 / 依赖 | 状态 |
|---|---|---|---|---|
| 0 | **测试环境隔离 Tier 1**：独立 worktree + 独立 CWD/data + `TRADEYI_ENV=test` + UI 端口 8502 + 生产库路径拒连守卫 + git 分支策略 | Product（PRD）→ Architect（实施方案）→ Coder → Tester | 设计稿已有：`docs/test_environment_isolation_design.md`（飞书 https://my.feishu.cn/docx/OglHdiSPGo39lIxn1PccOEqknPe ）；**待补 PRD + 实施方案 + 签字 + 计划** | **进行中（今晚出文档）** |

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

## P2

| # | 事项 | 说明 |
|---|---|---|
| 13 | ML 板块整体（A→B→C 串行 + 自动月度重训规划） | 用户 2026-10-23 决定：自动月度重训统一并入 ML 板块后再做；当前批次未放行 |
| 14 | 效率优化（停机 VACUUM 回收约 663MB、缓存/索引写进 `initialize()`、对账日志降采样） | 用户明确：所有功能事项完结后再做 |

## 已确认口径

1. **暂停新开仓**：`run_gamma_strategy.py:63-84` 为**租户级优先、全局兜底**。
   当前实盘 `futu_real_master` / 模拟 `futu_paper_sandbox` 经生产解析函数均为 **False（新开仓放行，红线已解除）**；
   `volatility_new_entries_paused=true`（全局）只是**兜底**，**不是**「实盘已暂停」。
   页面/监控判断生效值**必须走生产解析函数**，不得读全局键。
2. **告警投递**：`alerts.py:60` 读取 `ALERT_WEBHOOK_URL`（当前为空）；
   `:92-105` 仅白名单事件 / CRITICAL 才推飞书卡片，其余 ERROR 只落库 → `delivered=0`。
   截至 2026-10-24 00:00，未投递告警 **20 条**。
