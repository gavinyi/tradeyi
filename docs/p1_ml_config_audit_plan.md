# P1 实施计划：ML 模型管理补回 + 配置留痕审计

- 签发：Chief，2026-10-23
- 依据（已签字）：`docs/ml_model_management_prd.md`、`docs/config_change_audit_prd.md`、
  `docs/ml_model_management_design.md`、`docs/config_storage_unification_design.md`、
  `docs/config_audit_runbook.md`
- 状态：**待用户审批（门禁第 5 条：大需求须用户确认后开工）**

## 一、批次（严格串行：前一批「全量回归绿 + Reviewer 签」才开下一批）

| 批 | 内容 | 主要改动 | 责任人 | 备注 |
|---|---|---|---|---|
| **A** | ML 1a：降级原因显式化 + 新鲜度守卫 + 只读状态卡 | `volatility_roll.py`（降级原因五种）、`ml_freshness.py`（已有草稿）、状态卡页面、`alerts.py` 三个白名单事件 | Coder → Reviewer → Tester | **无行为变更** |
| **B** | AUC 门控修正 + SKIP 治理 | `ml_retrain_monthly.py`：绝对下限 0.55 ∧ 优于旧 AUC ∧ 最小边际 0.01；`ml_retrain_history` 留痕（含 SKIP 原因列）；连续 ≥2 次 SKIP 告警；特征不匹配降级原因 | Coder → Reviewer → Tester | 必须在 D 之前 |
| **C1** | `audit_events.actor` 列 + 索引 + `store.audit` actor 必填 | `store.py` + **55 处调用点同批改完**（actor 取值表见 runbook） | Coder → Reviewer → Tester | 顺序：先加列 → 再改签名 → 同批改完 |
| **C2** | `setting_versions` 表 + 配置键登记表 + 双 actor 索引 | `store.py`、`config_key_registry.py`（新建，Python 模块不是 DB 表） | 同上 | |
| **C3** | `set_setting(actor, reason)` + 值相同跳过 | `store.py` + 76 处调用点；`reconciliation.py` 热路径必须传 `service:reconciliation` | 同上 | **验收：对比改造前后写入量，放大即回滚** |
| **D** | ML 1b：`uv sync --extra ml` + 受控重启 | 依赖 + 一次重启 | Ops 执行，Coder 待命 | **需用户单独签字** |
| **E** | 手动重训一次 + 人工验收 | `scripts/ml_retrain_monthly.py` | Coder + Product 验收 | 之后才谈自动月度（**需用户拍板**） |
| **P1-F**（本期单列） | `decision_logs` 租户化 + ML 对照视图 | `store.py`（加 `tenant_id` + 回填 + 索引）、ML 页面区块④ | 待排期 | **本轮不含对照视图** |

## 二、放行门（每批都走）

1. 源码全绿：全量 `pytest`（950+ 用例）零失败；
2. Reviewer 代码审查签字（红黄绿分级，`real_trading.py` 门禁类改动走红区）；
3. 大需求：**用户审批确认**；
4. 部署窗口：**交易时段（北京时间 21:30–04:00）只写不部署**；部署放收盘后或开盘前；
5. 部署后：30 分钟探针（新开仓关闭状态下观察决策/对账/告警）+ Ops 巡检。

## 三、回滚

- C1：**回滚代码，不 DROP 列**（列加法无害，`DEFAULT ''`）；
- C2/C3：迁移前备份库（含 sha256）+ 磁盘余量检查；失败即回滚代码；
- C3：写入量对比不通过 → 回滚；
- 全部批次：`quick_check` 通过 + 行数与基线一致 + 存量 `actor=''` 不被伪造。

## 四、红线与不做项

- **C 批（配置留痕）必须在恢复实盘交易之前完成**（Product 定）；
- 不物理合并 `system_settings` / `runtime_config`；
- 不改存量 `actor` 语义（payload 里的自由文本是数据，不是参数）；
- 不加第三处配置存储；
- P2 性能优化（VACUUM、缓存、对账降采样）**排在全部功能批次之后**。

## 五、待用户批复

1. 上述批次与放行门是否批准开工；
2. ML 1b（装 sklearn + 受控重启）是否授权；
3. 是否恢复 ML 自动月度重训（建议 E 手动验收通过后再定）。
