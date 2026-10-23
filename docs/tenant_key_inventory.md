# 租户化键完整清单（穷举，非回忆）

> 状态：清单已定稿，作为 Reviewer 逐键审查的唯一依据
> 生成方式：对 `src/`、`scripts/`、`pages/` 全量正则穷举 `get_setting/set_setting/settings_with_prefix`
> 与 `*_key`/`f"..."` 键字面量，逐条归类。**不靠"想起来"。**
> 关联：`docs/volatility_campaigns_tenant_partition_design.md`、`docs/risk_limits_tenant_scoping_design.md`

## 0. 用法

Reviewer 拿到本清单后，**逐键核"写侧 ⊇ 读侧"**：
- 读侧认的**每一种前缀变体 × 每一种拼写**，写侧都必须能产生；
- 写侧产生的每一种键，读侧都必须有对应的读取路径；
- **不按"大部分对"放行。**

---

## A. 已租户化（读侧已有三级回落）—— 复核对象，确认无残留

| 键族 | 前缀变体 | 读侧 | 写侧 |
| :--- | :--- | :--- | :--- |
| `strategy_enabled` | `{tenant}:{显示名}` / `{tenant}:{蛇形id}` / 无前缀 | `run_service.resolve_configured_strategies` | `reconciliation._disable/_recover`、`pages/9:113-114` |
| `strategy_execution_mode` | `{tenant}:{strategy}` / `{strategy}` / 全局 `execution_mode` | `execution.py:104-105` | `pages/9:116`、`pages/15:291` |
| `strategy_reconciliation_disabled` | `{prefix}{strategy}`，prefix ∈ {tenant, account, ""} | `risk.py:115-118` | `reconciliation.py:44` |
| `strategy_reconciliation_restore` | 同上 | `reconciliation.py:60` | `reconciliation.py:37,43,88` |
| `symbol_blocked` | `{account:}{symbol}` / `{symbol}` | `risk.py:97-98,202` | `reconciliation.py:135,147,157` |
| `recovery_blocked` | `{account}` / 无前缀 | `risk.py:122-126`、`ui.py:441` | `reconciliation.py:197`、`run_service.py:907,1312,1316`、`recovery.py:18` |
| `kill_switch` | `{tenant}` / `{account}` / 无前缀 | `store.py:1042-1047` | `store.py:1055-1059` |
| `real_account_config` | `{account_id}` | `execution.py:91`、`real_trading.py:69`、`ui.py:229` | `pages/27` |
| `volatility_new_entries_paused` | `{tenant_id}` / 无前缀 | `run_gamma_strategy.py:86,1284` | `run_gamma_strategy.py` |
| `volatility_long_gamma`（campaign state） | `{tenant}:{symbol}` / `{symbol}` | `order_push.py:157` | `order_push.py` |
| `custom_account_name` | `{account}` | `ui.py:222-223` | — |

---

## B. 必须租户化（当前全局，写读均无租户维度）

| 键族 | 读侧 | 写侧 |
| :--- | :--- | :--- |
| **`VOLATILITY_CAMPAIGNS`**（runtime_config） | `config.py:348` | `config.py:353,366`、`volatility_scanner.py:366`、`order_push.py:167`、`run_volatility_strategy.py:404,412,413` |
| `volatility_signal_active:{symbol}` | `volatility_scanner.py:154,168` | 同 |
| `volatility_signal_z:{symbol}` | `volatility_scanner.py:155,170` | 同 |
| `volatility_scanner_notified:{symbol}` | `volatility_scanner.py:190` | 同 |
| `volatility_scanner_paused:{symbol}` | `volatility_scanner.py:130`、`pages/11:105,113`、`pages/10:504` | 同 |
| `volatility_candidate_retry_until:{symbol}` | `volatility_scanner.py:204,431` | `:365` |
| `volatility_candidate_retry_z:{symbol}` | `volatility_scanner.py:207,435` | `:366` |
| `volatility_campaign_rejection:{symbol}` | `volatility_scanner.py:426` | 同 |
| **`VOLATILITY_RADAR_BUDGETS`**（runtime_config） | `config.py:280` `load_volatility_radar_budgets` | `config.py:304` `save_volatility_radar_budgets` |
| `volatility_cooldown:{underlying}` | `run_gamma_strategy.py:864` | `:491` |
| `volatility_cooldown_hours:{underlying}` | `run_gamma_strategy.py:868,906` | `:493` |
| `volatility_cooldown_reason:{underlying}` | `run_gamma_strategy.py:891` | `:492,907` |
| `volatility_expression_review:{underlying}` | `run_volatility_strategy.py:389` | 同 |
| `volatility_long_gamma:signal:{underlying}` | `run_volatility_strategy.py:216` | 同 |
| `volatility_long_gamma:{code}`（无前缀变体） | `run_volatility_strategy.py:214`、`volatility_orphans.py:462`、`pages/10:1322` | 同 |
| `strategy_policy_revision:{strategy}` | 审批指纹 | `tests/test_approval_fingerprint.py:34` |
| **`STRATEGY_BUDGETS`**（runtime_config） | `portfolio_allocator` | 同 |
| **`last_run:{task}`** | `run_service.due` | `mark_run` |
| **`task_backoff_until:{task}`** | `run_service.py:517` | `:529,561,577` |
| **`task_failure_count:{task}`** | `run_service.py:528,589` | `:591` |
| **`decision_logs`（表，无 tenant 列）** | `store.decisions` `store.py:2345` | `store.decision` `store.py:2318`、`volatility_scanner.py:153,199,464,475`、`run_volatility_strategy.py:167,497,505`、`run_gamma_strategy.py`（15+ 处） |
| **`_DECISION_CACHE[(strategy_id, symbol)]`** | `store.py:2335` `decision_if_changed` | `store.py:2314,2320` —— **进程内全局，无租户维度** |

> **`VOLATILITY_RADAR_BUDGETS` 归类裁定（Chief，2026-10-23）**：归 **B（必须按账户/租户）**。
> 依据 Product 自定的切分法 —— **"凡是钱的参数必须按账户；凡是市场的参数天然全局"**。
> 权利金预算**是钱**，与账户资金量相关，不是市场常量。
> 与 `risk_limits`、`STRATEGY_BUDGETS` **同一类**，由 Migrator 记入接线清单。

---

## C. 🔴 本次穷举的新发现：任务调度键跨租户串扰

`last_run:*` / `task_backoff_until:*` / `task_failure_count:*` **是全局键，被所有租户 Worker 共用**。

**后果（可推导，非推测）**：
1. 租户 A 的 Worker 跑完 `volatility_long_gamma`，调 `mark_run(store, "last_run:volatility_long_gamma", now)`；
2. 租户 B 的 Worker 下一轮 `due(store, "last_run:volatility_long_gamma", 30, now)` → **返回 False**；
3. **租户 B 跳过本轮策略执行**，直到时间戳老化。

同理，一个租户任务失败写入 `task_backoff_until:{task}`，**另一个租户的同名任务也会被退避压住**。

**这与今晚"两个租户不同步"是同一类问题的又一处实例**，且此前未被任何一份清单收录。

### 已立项：P0-8｜任务调度键租户化

- **排期**：P0-3 之后、P0-1 之前（理由：它造成的是"静默跳过"，而静默是今晚所有事故的母题）
- **键形**：`last_run:{tenant_id}:{task}`、`task_backoff_until:{tenant_id}:{task}`、`task_failure_count:{tenant_id}:{task}`
- **读侧**：租户键优先、无前缀兜底；**迁移后无前缀键必须零写入方**
- **归口**：`run_service.py` 的 `due`/`mark_run` + 全部调用点
- **守卫要求**：**两个租户并行跑同一个任务，断言两边都真的执行了** —— 不是断言键存在
  （那又是空转守卫）
- **副产物解释**：两个租户的任务时长总是成对、互相错开 —— 不是巧合，是同一个键在互相压制
  （每个租户实际只拿到约一半的周期）。

---

## D. 天然全局（**不**租户化）

| 键族 | 理由 |
| :--- | :--- |
| `correlation_clusters` | 市场结构，与账户无关（`risk.py:241` 已是 store 设置） |
| `execution_mode` | 全局默认执行模式（租户有 `strategy_execution_mode:{tenant}:` 覆盖） |
| `connectivity_degraded`、`order_push_connected` | 进程/通道级状态，非业务状态 |
| `strategy_budget_schema_version` | 结构版本号 |
| `validation_auto_downgrade` | 系统行为开关 |
| `core_satellite_profit_bonus_usd` | 策略常量 |
| `volatility_loss_cooldown_after`、`volatility_loss_budget_factor_one/two` | 策略常量（`volatility_optimizer.py:629-631`） |
| `volatility_default_*` 系列 | 默认配置常量 |
| `FEISHU_*`、`REAL_ACCOUNT_ID` | 通知/账户标识 |
| `last_campaign_cleanup_result` | 运维结果快照 |
| `VOLATILITY_SYMBOLS`、`GAMMA_CAMPAIGNS` | 全局配置（标的池/活动） |
| 市场微观结构常量（乘数 100、`OPTION_PATTERN`、合约解析规则） | 与账户无关；按账户反而制造不一致 |

---

## E. 独立体系（US 市场不涉及，本次不动）

`crypto_*` 全部键族（`crypto_cooldown_until`、`crypto_swap_margin`、`crypto_daily_trade_count` 等）
—— 属加密交易体系，与本清单无关，**不纳入本次租户化范围**。

---

## F. 审查不变量（Reviewer 逐键核）
1. **写侧 ⊇ 读侧**：读侧认的每种前缀 × 拼写，写侧必须能产生。
2. **读侧 ⊇ 写侧**：写侧产生的每种键，读侧必须有读取路径（否则是孤儿键）。
3. **无前缀键是否仍有写入方**：迁移后无前缀键应**零写入方**（只读兜底）。
4. **新增族是否被收录**：任何新增 `*:{symbol}` / `*:{underlying}` 形态的键，必须回到本清单归类。

---

## G. 租户解析入口（非存储键，但同属"一个概念一份实现"）

**问题**：`broker.tenant_id` 在生产环境**恒为空** —— 全库 `.tenant_id =` 赋值点**零处**，
`FutuPaperBroker` 没有该属性。六处读取里只有两处做了 `account_id` 反解。

| 位置 | 写法 | 定性 |
| :--- | :--- | :--- |
| `risk.py:144-151` | `getattr(broker,"tenant_id")` → 回落 `resolve_tenant(account_id=...)` | ✅ 正确 |
| `risk_limits.py:132-158` | `_broker_tenant_id(broker, account_id)` + 注册表校验 | ✅ 正确 |
| `order_push.py:20` | `getattr(self.broker, "tenant_id", None)` | ❌ **no-op** |
| `order_push.py:62` | 同上 | ❌ **no-op** |
| `volatility_orphans.py:304` | `str(getattr(broker, "tenant_id", "") or "")` | ❌ **no-op** |
| `volatility_orphans.py:374` | 同上 | ❌ **no-op** |

**结论**：`tenant.py:124 broker_scope` 的 docstring 早已写明"生产 `FutuPaperBroker` 只带
`account_id`、不带 `tenant_id`" —— **事实早已知道，只在两处做了处理**。

**修法**：抽 `tenant_of_broker(broker) -> (tenant_id, account_id)` = `broker_scope` +
`resolve_tenant(account_id=...)` + `DEFAULT_TENANTS` 注册表校验，**六处全部改走它**。
与 `resolve_tenant_config_key` 同批、同文件（`tenant.py`）。

### G.1 空租户必须抛异常，**不得退回全局键**

`order_push.py:157-158` 的 `if tenant_id else f"...:{symbol}"` 是**静默回落**。
抽共享入口后**必须去掉**：共享入口对空租户**抛异常**（与 `resolve_tenant_config_key` 一致）。

> **但"抛异常"不等于"让推送线程崩掉"**：`DealPushHandler` 是推送回调，抛异常会打断整个
> 推送处理。正确做法是**调用方捕获后拒绝本次登记并记 ERROR 审计** ——
> **fail-closed 的含义是"不做不安全的事"，不是"崩溃"**。

### G.2 `campaign_id` 命名空间必须**整体**租户化

`volatility_orphans.py` 的 ADOPT 产物是 `campaign_id = orphan:{symbol}`（全局形态）。
抽共享入口时一并改为 `{tenant_id}|orphan:{symbol}`。

**陷阱**：`upsert_campaign` 的 `identity = campaign.get("campaign_id", campaign.get("code"))`。
若只给 ADOPT 产物加前缀、而**扫描器创建的 campaign 仍用无前缀 id**，则同一个逻辑
campaign 在清单里会有**两个不同 identity** → 互相视为不同条目、重复登记。

**规则**：**分区清单内所有 `campaign_id` 必须统一带租户前缀**（含扫描器创建的）。

### G.3 顺序：①②③ 一批过、一批审、一批重启

```
① tenant.py 共享解析入口 tenant_of_broker
② order_push / volatility_orphans / risk / risk_limits 四处改走它（去掉 else 兜底）
③ active_keys + protected_volatility_state_keys 产出带租户形态
```

**③ 是 ①② 的释放条件**：一旦 ①② 让 `order_push` 真的写带租户的键而 ③ 没修，
`prune_campaign_states`（`store.py:985`）会**当场删掉它** —— **先做 ①② 等于激活那个删除 bug**。

### G.4 守卫必须跨两轮

断言"实盘写入的 state 键仍存在"必须跑 **写入 → `run_once` → 断言仍在**。
只跑一轮会空转通过 —— **剪枝发生在 `run_once` 内部**。

---

## H. 🔴 两套设置表（"一个概念两个地方"）

| 表 | 列 | 访问 API | 并发语义 |
| :--- | :--- | :--- | :--- |
| `system_settings` | `key, value, updated_at` | `get_setting` / `set_setting` / `settings_with_prefix` / `delete_setting` | **无版本号**，最后写入者胜 |
| `runtime_config` | `config_key, value_json, version, updated_at` | `get_runtime_config` / `set_runtime_config` / `mutate_runtime_config` | **有 `version`**，`mutate_runtime_config` 做原子读改写 |

**实测归属**：
- `real_account_config:*`、`risk_limits:*`、`strategy_enabled:*`、`VOLATILITY_CAMPAIGNS` **以外的所有
  `*:{symbol}` 状态键** → `system_settings`
- `VOLATILITY_CAMPAIGNS`、`GAMMA_CAMPAIGNS`、`STRATEGY_BUDGETS`、`VOLATILITY_SYMBOLS`、
  `VOLATILITY_RADAR_BUDGETS` → `runtime_config`

**风险**：**同一类东西（账户/租户级配置）分住两张表、两套 API、两种并发语义。**
迁移脚本若同时触碰两者，**写语义不同**（一边原子、一边不原子）——
这正是"一个概念两个地方"的又一实例，**列为 P0-7/B 迁移的显式约束**：
**迁移脚本必须按表分别处理，不得假设两边写入语义一致。**
