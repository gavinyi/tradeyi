# 波动率 Campaign 租户分区设计

> 状态：设计已定稿，待实现（并入 B 项）
> 关联：`docs/dual_tenant_architecture_tech_spec.md`、`docs/multi_tenant_product_requirements.md`
> 背景事件：2026-10-22 夜间，实盘租户自动成交 `US.META261030P660000`，该仓位在成交后 10 分钟内
> 对止损系统不可见（未被任何 campaign 认领），最终亏损 $366。根因之一即本文要解决的问题。

## 1. 问题

`VOLATILITY_CAMPAIGNS` 是**一个全局键**，没有租户维度：

| 位置 | 行为 |
| :--- | :--- |
| `config.py:348` `load_volatility_campaigns()` | 无 `tenant` 参数，读全局键 |
| `config.py:353` `save_volatility_campaigns()` | 写全局键 |
| `config.py:361` `upsert_campaign(config_key, campaign)` | 已支持任意 `config_key`（可复用） |
| `volatility_scanner.py:366` | 雷达写全局键 |
| `run_volatility_strategy.py:102` | 每个租户 Worker 读**同一份**清单 |
| `order_push.py:167` | `register_fill_to_campaign` 写全局键 |

后果（实测）：REAL 与 PAPER 两侧每轮打印的 campaign 完全相同。实盘接管出来的孤儿
campaign、滚动改掉的合约号，都会串到模拟盘；反之亦然。**这不是"隔离不完善"，是"没有隔离"。**

## 2. 键结构

与团队既有约定同构（`strategy_enabled:{tenant}:{strategy}`、
`strategy_reconciliation_disabled:{tenant}:{strategy}`）：

| 角色 | 键 | 说明 |
| :--- | :--- | :--- |
| **主键（读写优先）** | `VOLATILITY_CAMPAIGNS:{tenant_id}` | 例：`VOLATILITY_CAMPAIGNS:futu_real_master` |
| **兼容键（只读兜底）** | `VOLATILITY_CAMPAIGNS` | 迁移前存量；**只读，不再写** |

读侧规则：**租户键优先、无前缀旧键回退**。

## 3. 读写侧改法

- 新增助手 `campaigns_config_key(tenant_id) -> str`，**集中拼键**，杜绝各处手拼出错。
- `load_volatility_campaigns(tenant_id=None)`：
  - 有租户 → 读 `VOLATILITY_CAMPAIGNS:{tenant_id}`；该键不存在 → 回落无前缀旧键（迁移前状态）。
  - 无租户 → 读无前缀旧键（兼容现有调用方）。
- `upsert_campaign` / `delete_campaign` **已接受 `config_key` 参数，零改动**，调用方传分区键即可。
- **写侧必须显式传 `tenant_id`**；缺失或不在注册表 → **抛异常**，绝不静默写全局键。

### 3.1 调用方清单（Reviewer 硬审查项）

以下每个调用点都必须传租户，漏一个即视为未完成：

- `order_push.py` — `register_fill_to_campaign` / 孤儿 ADOPT
- `run_volatility_strategy.py` — 加载、滚动、`prepared` 回写
- `volatility_scanner.py` — 雷达滚动、候选重试
- `volatility_orphans.py` — `adopt_volatility_orphan_position`
- `campaign_cleanup.py` — 清理
- `pages/` 下所有 `load/upsert/delete_campaign` 调用点

> 教训：2026-10-22 已栽过一次"抽了共享护栏函数、雷达那条路径漏调"。**抽函数不算完，
> 逐个调用点核才算完。**

## 4. 存量 11 条的归属判据（按优先级，不猜）

1. campaign 内已有 `tenant_id` / `account_id` 字段 → 用它；
2. 该 `code` 在某账户券商持仓里 → 归该账户的租户；
3. 该 `code` 的成交意图（`order_intents`）带 `tenant_id` → 用它；
4. 都不满足 → 归雷达扫描所用账户的租户（现状默认 REAL），**并记 WARN 审计，不静默**。

## 5. 迁移

- 脚本：`scripts/migrate_volatility_campaigns_tenant_scope.py`，**默认 dry-run**，打印每条归属判据；
  `--apply` 才写。
- **一次性、显式、可回滚**；只写 `{tenant}:` 键。
- **无前缀键原样保留不动**（按既有约定：legacy 键留作 inert，不删）。
- 迁移后无前缀键**没有任何写入方** → 自然 inert；读侧回落只为兼容历史查询。
- **不碰任何订单、持仓、账本** —— 真金零风险。

### 5.1 迁移与重启必须背靠背

> 前提：迁移后无前缀键 inert，**只在跑新代码时成立**。
> 旧代码仍在写无前缀键，迁移完立刻会被再写一次、快照当场过期。

正确顺序**只有一个**：

```
停 Worker → 跑迁移 --apply → 立刻用新代码 start
```

中间**不能有任何旧 Worker 活着**。

## 6. 雷达扇出

- 雷达改为：**一次扫描**（行情与账户无关，共用一份）→ 对 `tenant.DEFAULT_TENANTS` 中
  `is_active=True` 的每个租户，用**各自账户**做持仓/挂单校验 → **各写一份** `{tenant}:` 清单。
- 租户 → 账户用 `tenant.resolve_tenant(tenant_id=...)`，**不新增映射表**。
- 明确否决的方案：**不给每个租户各配一条雷达进程** —— 两条雷达会同时写同一份清单、互相覆盖。

## 7. 红线

1. **identity 必须带租户前缀**：`campaign_id` 现为 `orphan:{symbol}` 这种全局形态，
   两租户同一 code 会**互相顶掉**。分区后 identity 用 `f"{tenant_id}|{campaign_id}"`。
2. **跨租户写入必须报错**：写侧校验租户属于注册表，否则抛异常。
3. **迁移只读不删**：无前缀键永不删除。

## 8. 验收（交给 Tester）

1. `load_volatility_campaigns("futu_real_master")` 只返回实盘那份，不含模拟；
2. 无前缀旧键仍在，读侧回落不报错；
3. **两租户同 code 的 campaign 互不覆盖**（identity 带租户前缀）；
4. 写侧 `tenant_id=""` → 抛异常，不静默写全局键；
5. 迁移 dry-run 输出 11 条归属判据；`--apply` 后无前缀键内容**逐字节不变**；
6. 雷达扇出后，两租户清单各自独立、互不含对方条目。

### 8.1 逐条核销表（P0-9 已修 vs 仍待 B 批次）

Chief 2026-10-23 裁定：**第 3 条移入 P0-9 修**（identity 层，一行级），**其余仍归 B 批次**。
本表**可数、可核**，不靠记忆 —— 每条附现状行号与判据。

| # | 项 | 现状（file:line） | 判据（怎么算过） | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **3** | 两租户同 code 的 campaign 互不覆盖 | `config.py:365-376` `_campaign_identity` 只按 `code` 去重 | `tests/test_campaign_tenant_isolation_guards.py::test_two_tenants_adopting_same_code_do_not_overwrite` 绿（当前**红**，待 Coder 加租户前缀） | **P0-9 修** |
| **7** | campaign 清单**键分区** | `volatility_orphans.py:529` 仍写**全局键** `VOLATILITY_CAMPAIGNS`；`tenant.py` 的 `volatility_campaigns_key` **零调用点**（已写好但没接） | 写侧一律走 `VOLATILITY_CAMPAIGNS:{tenant_id}`；**无前缀键零写入方**（读侧仅作回落）；`volatility_campaigns_key` 至少 2 个调用点（写 + 读） | **待 B** |
| **8** | 接管产出的 **state 键带租户** | `volatility_orphans.py:530` 仍写 `volatility_long_gamma:{item.code}`（**非带租户形态**） | 接管产出的 state 键含租户段；两租户 ADOPT 同 code 时 state 互不覆盖（`volatility_state_keys` 已能产带租户形态，此处没用） | **待 B** |
| **9** | 读侧**租户过滤**（第 7 条的另一半） | `run_volatility_strategy.py:119` `load_volatility_campaigns()` 无租户参数 → 两租户 Worker 读**同一份**清单 | `load_volatility_campaigns(tenant_id)` 按 §3 实现；每租户只读到自己那份（含无前缀回落） | **待 B** |
| **10** | 控制页**清理按钮**租户盲（Reviewer 报，Chief 同意并入 B） | `pages/10_volatility_options_strategy.py:1313-1327` 候选列表按 **code** 查持仓、不分租户；`:1374-1375` 传**裸 code** 删除 | ① 候选列表按租户取持仓（不得读别家的持仓来判断"无持仓"）；② 删除传**完整身份** `{tenant}\|{code}`。**当前**：删除已 fail-closed（模糊即拒绝，`config.py:477-486`）→ 危险面已堵，但**候选列表仍租户盲**，会列出不该清理的条目 | **待 B** |

**边界（Chief 划）**：第 3 条是 **identity 层**，第 7/8/9 条是**键结构层**。
先修 identity、后修键分区 —— 过渡期是「**一份清单、两套身份**」，不是「一份清单、互相顶掉」。

**验收方式**：第 7/8/9 条每条落成**一个能在修复前失败的行为守卫**（与 P0-9 的 ②③ 同规格：
端到端 + 变异敏感），不得以「键名存在」这类结构断言充数。

## 9. 落地顺序

按 Chief 排期：**A（`fill_ledger` 双计）→ B/D（清单与开关租户化）→ P0-7 → P1/P2**。
**B 不修完，重启不做。**
