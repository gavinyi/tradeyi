# 告警推送断链修复 —— 实施计划（P0-2）

- 上游（已签字）：`docs/alert_delivery_fix_prd.md` + `docs/alert_delivery_fix_design.md`
- 门禁：本文档待**用户审批**（大需求：动表结构 + 告警链路 + 新增页面），批准后 Coder 才开工
- 冻结纪律：交易时段（北京时间 21:30–04:00）只写不部署；部署须「全量回归绿 + Reviewer 签字」

## 一、批次（串行）

| 步 | 内容 | 产出 | 责任人 |
|---|---|---|---|
| **S1** | 迁移：`alerts` 加 `webhook_delivered` / `feishu_delivered` / `muted` 三列（**幂等 ALTER + 存在性检查，走 `store.initialize()` 的迁移批次，不塞 CREATE TABLE**） | 存量库可用；`quick_check` 通过 | Coder |
| **S2** | `alerts.py` 推送逻辑反转：不可静音清单（代码常量）→ 静音清单（`runtime_config`）→ 默认推 ERROR+；三态写入；`delivered = max(webhook, feishu) > 0` 保持兼容 | 新告警三态正确 | Coder |
| **S3** | 投递探针：`run_service` 注册 30 分钟任务，**必须真实投递**；`!=1` 即失败，`-1`→`ALERT_CHANNEL_NOT_CONFIGURED`、`0`→`ALERT_DELIVERY_FAILED` | 通道故障能自报 | Coder |
| **S4** | 页面：①「未投递」视图（`ERROR/CRITICAL AND delivered=0 AND muted=0`）②按通道的配置警告 ③静音清单管理（写 `runtime_config`，显式事件名、禁通配、写批准人/原因） | 可见性闭环 | Coder |
| **S5** | 全量回归（950+）+ Reviewer 签 + Tester 绿灯 + 部署 + 部署后 30 分钟探针观察 | 上线 | Tester / Reviewer / Ops |

## 二、存量数据处理口径

- **不伪回填**三态列：存量行 `webhook_delivered/feishu_delivered` 保持 `-1`（未知/未尝试），`muted=0`；
- 存量 `delivered` 列**原值保留**，页面历史按 `delivered` 展示（`-1` 显示「历史·未记录通道」）；
- 与 `audit_events.actor=''` 同一原则：**宁可显示「未知」，不填假值**。

## 三、验收（对齐 PRD 8 条）

1. 未静音 ERROR 必推（`webhook_delivered=1` 或 `feishu_delivered=1`）
2. `LEDGER_MISMATCH` 进静音清单仍推（不可静音胜出）
3. 非关键事件静音：不推、仍落库（`muted=1`）
4. 静音清单填 `RISK_*` → 拒绝（禁通配）
5. 未投递视图出现失败项；被静音项**不出现**但仍在告警列表
6. 单通道清空 → 该通道警告、另一通道仍送达；两通道都断 → 红色「全部未配置」
7. `-1` 与 `0` 页面可区分
8. 两通道全断 → 探针 `-1`、告警发不出，但**未投递视图仍显示该探针记录**
- 附加（不计入 8 条）：变异测试 —— 把 `:67-68` drop 分支加回 → 守卫必须转红

## 四、回滚

- 回滚代码即可（三列是加法，保留无害）；`PRAGMA quick_check` + `alerts` 行数不变 + 回滚后服务正常启动；
- 静音清单改动写 `runtime_config`（`config_versions` 可回滚）。

## 五、风险

| 风险 | 措施 |
|---|---|
| 推送量放大 → 告警疲劳 | 不可静音清单 + 静音清单 + `ALERT_DEDUPE_SECONDS` 保留；先观察 24h |
| `alerts.py` 是热路径（每次告警都走） | 三态写入为单行 update；探针 30 分钟一次，不放大 |
| 页面新增读写 `runtime_config` | 走现有 `mutate_runtime_config`（含版本/留痕） |
| 与 P1-C（配置留痕）并行改 `store.py` | **C 批已冻结；本批只碰 `alerts` 表迁移**，冲突面小，仍按串行：本批先、C 批后 |

## 六、待用户批复

1. 本计划是否批准开工（S1–S5）；
2. 部署窗口：预计最早 **10-24 收盘后（04:00 之后）**，是否同意。
