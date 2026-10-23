# 告警推送断链修复 —— 轻量技术设计

> 状态：设计待签字（门禁第 3 步）
> 上游：[alert_delivery_fix_prd.md](alert_delivery_fix_prd.md)（Product PRD，已落盘）
> 关联：`docs/fail_closed_design_rules.md` §4（静默是最大的敌人）
> 代码冻结中：**本文档不授权写代码**

## 1. 现状（实测，`src/futu_quant/alerts.py`）

| 位置 | 行为 | 后果 |
| :--- | :--- | :--- |
| `:67-68` | `not opened and event_type not in WHITELIST → return` | **事故已 OPEN 且不在白名单 → 连告警行都不写** |
| `:94-96` | 飞书只推 `白名单 or CRITICAL` | **ERROR 级事件默认不推群** |
| `:60` | `self.webhook = os.getenv("ALERT_WEBHOOK_URL", "")` | **通道为空 → 静默无投递，无人知道** |
| `:105` | `add_alert(..., delivered or feishu_delivered)` | **单布尔** —— 分不清"没推"和"推失败" |
| — | **无投递失败升级** | 投递坏了没人知道 |
| **白名单（`alerts.py:12-52`）** | **现 18 项**（10-23 夜扩充，含 `OPTION_LIFECYCLE_RISK` / `DAILY_LOSS_KILL_SWITCH` / `REAL_TRADE_REJECTED` / `ORDER_ENV_MISMATCH` / `HARD_STOP_LIMIT_INVALID` / `ML_*`） | **剩余断点是「非白名单 ERROR 被默认静默」** —— **不再是原先那 5 个真金事件**（它们已在白名单内） |

> **所以"白名单只有 6 项"是过时信息。今天仍在漏推的是「不在白名单的新事件」** ——
> 实测今日 25 条告警全部 `delivered=0`，其中 **ERROR 13 条**全是新事件：
> `PORTFOLIO_STRESS_BREACH` 10、`STRATEGY_ERROR` 2、`VOLATILITY_SCANNER_ERROR` 1。
> **`OPTION_LIFECYCLE_RISK` 的 42 次属扩充前历史**，不可用作"仍在漏推"的证据。

`alerts` 表（`store.py:433-440`）：`id, severity, event_type, message, delivered, created_at`
—— **单 `delivered`，无通道、无静音标记**。

**实测代价（当前实况）**：今日 25 条告警**全部 `delivered=0`**，其中 **ERROR 13 条** —— `PORTFOLIO_STRESS_BREACH` 10 / `STRATEGY_ERROR` 2 / `VOLATILITY_SCANNER_ERROR` 1，**全部不在 18 项白名单内**。

> ⚠️ **口径纪律**：`OPTION_LIFECYCLE_RISK` 42 次等属**白名单扩充前历史**（这些事件现已在白名单内），**不得用作"当前仍在漏推"的证据** —— 当前漏推的是「非白名单的新 ERROR」。

---

## 2. 推送策略：方向反转（PRD ①）

```
推送判定（ERROR 及以上）：
  ① 在「不可静音清单」  → 必推（配置覆盖不了）
  ② 在「静音清单」      → 不推（但仍落库）
  ③ 默认               → 推
```

### 2.1 两个清单

| 清单 | 形态 | 可配置？ |
| :--- | :--- | :--- |
| **不可静音清单** `ALERT_NEVER_MUTE` | **代码常量**（`alerts.py`） | ❌ **不可** —— 静音它们等于把安全系统变哑 |
| **静音清单** | `runtime_config` 的 `alert_mute_list`（JSON 数组） | ✅ 可，**走 `mutate_runtime_config` → 自动获得 actor/reason** |

**不可静音清单**（PRD ②，硬编码）：
`DAILY_LOSS_KILL_SWITCH`、`REAL_TRADE_REJECTED`、`ORDER_ENV_MISMATCH`、`LEDGER_MISMATCH`、
`RISK_REJECTED`、`OPTION_LIFECYCLE_RISK`、`HARD_STOP_LIMIT_INVALID`、`KILL_SWITCH_ENGAGED`、
`OPEND_DISCONNECTED`、`RECONCILIATION_*`（**前缀匹配仅在此清单内允许**）。

### 2.2 静音清单四条约束（PRD ③）

1. **显式**：静音项必须是**完整 `event_type`**，**禁止通配**（`RECONCILIATION_*` 只在不可静音清单里作为前缀出现）；
2. **可审计**：存 `runtime_config`，**改动自动留痕**（谁/何时/为什么）；
3. **不可静音清单优先**：两清单交集 → **不可静音胜出**；
4. **静音仍落库**：`muted=1` + `delivered=0`，**页面可见**。

---

## 3. 投递通道可观测（PRD ④）

### 3.1 `alerts` 表加三列（幂等迁移）

```sql
ALTER TABLE alerts ADD COLUMN webhook_delivered INTEGER NOT NULL DEFAULT -1;
ALTER TABLE alerts ADD COLUMN feishu_delivered  INTEGER NOT NULL DEFAULT -1;
ALTER TABLE alerts ADD COLUMN muted             INTEGER NOT NULL DEFAULT 0;
```

**三态语义**（`-1` 是关键）：

| 值 | 含义 |
| :--- | :--- |
| **`-1`** | **未尝试**（通道未配置 / 被静音） |
| **`0`** | **尝试失败** |
| **`1`** | 成功 |

> **为什么必须区分 `-1` 和 `0`**：**"没推"是配置问题，"推失败"是故障** —— 混成一个 `0`
> 就分不出"通道空着"和"通道坏了"，**而这两者的处置完全不同**。

**保留 `delivered` 列**（向后兼容）：`delivered = max(webhook_delivered, feishu_delivered) > 0`。

### 3.2 「未投递」独立视图

```sql
SELECT * FROM alerts
WHERE severity IN ('ERROR','CRITICAL')
  AND delivered = 0
  AND muted = 0          -- 🔴 静音是人工明确决策，不算「未投递」
ORDER BY id DESC
```

> **为什么必须加 `AND muted=0`**：静音是**人主动按下的**，把它混进「未投递」= 
> **把人工决策当成故障报**。静音一多，这个视图就被训练成噪声 —— 
> **而它是本次修复的核心可见性，被噪声淹没就等于没做。**

**这是本次修复的核心可见性** —— PRD 说的"今天的缺口本质是**以为配了、其实没推**"，
只有这个视图能让下次立刻发现。

### 3.3 通道未配置必须可见（PRD ⑤）

| 通道 | 配置项 | 未配置时 |
| :--- | :--- | :--- |
| Webhook | `ALERT_WEBHOOK_URL` | 页面**醒目警告** + 事件 `ALERT_CHANNEL_NOT_CONFIGURED` |
| 飞书 | `FEISHU_WEBHOOK_URL` / `FEISHU_GROUP_CHAT_ID` | 同上 |

- `ALERT_CHANNEL_NOT_CONFIGURED` **每天最多一次**（不刷屏）；
- **启动时检查一次** + **页面常驻显示**。

---

## 4. 投递失败探针（PRD ⑥ 附加）

- **间隔**：`ALERT_PROBE_INTERVAL_SECONDS`（默认 1800）；
- **动作**：发一条探针事件 `ALERT_DELIVERY_PROBE`，**必须真实投递**（进入不可静音清单）——
  **"静音"只允许是页面折叠 / 不重复刷屏，绝不能是不投递**：不投递的探针永远是 `-1`，**什么也验不到**；
- **判据**：**`webhook_delivered != 1` 或 `feishu_delivered != 1` 即算失败** ——
  **`-1`（未尝试）与 `0`（尝试失败）都必须算失败**，否则**通道空着时探针永远不告警**，正好漏掉本次要修的核心场景；
  按取值区分上报：**`-1` → `ALERT_CHANNEL_NOT_CONFIGURED`；`0` → `ALERT_DELIVERY_FAILED`（CRITICAL）**（两者都在不可静音清单里）；
- **递归防护**：探针自己也不投递时，**由页面「未投递」视图兜底** ——
  **不靠告警去发现告警故障**（那是无限递归）。

---

## 5. 迁移与回滚

| 步骤 | 内容 | 回滚 |
| :--- | :--- | :--- |
| 1 | `alerts` 加三列（`DEFAULT -1/0`） | **回滚代码即可**（列是加法，留着无害） |
| 2 | `alerts.py` 推送逻辑改造 | 同上 |
| 3 | `alert_mute_list` 写入 `runtime_config`（**已有 `config_versions` 留痕**） | 恢复配置值 |
| 4 | 页面「未投递」视图 + 通道警告 | 回滚代码 |

> **实现细节（实测）**：`alerts` 表已用**迁移批次**加过列 ——
> `acknowledged` / `action_status` 来自 `store.py:763-764` 的 ALTER 批（**不在建表语句里**）。
> **新三列走同一模式**（幂等 ALTER + `IF NOT EXISTS` 式存在性检查），
> **不要**塞进 `CREATE TABLE`（那只影响新建库，改不了存量库）。

**回滚判据**：`PRAGMA quick_check` 通过 + `alerts` 行数不变 + **回滚代码后服务正常启动**。

---

## 6. 与配置留痕（P1-C）的关系

**静音清单写 `runtime_config` → 自动获得 actor/reason 留痕** ——
**这是 P1-C 设计里"新增配置一律写 `runtime_config`"的第一个实际收益**，
两批可以**共用同一条迁移路径**。

**不可静音清单是代码常量，不是配置** —— **安全默认不能被配置关掉**。

---

## 7. 验收（**8 条，与 PRD 同数同序**）

1. **ERROR 必推**：未静音的 ERROR → `webhook_delivered=1` 或 `feishu_delivered=1`；
2. **不可静音清单生效**：把 `LEDGER_MISMATCH` 加进静音清单 → **仍推送**（不可静音胜出）；
3. **静音清单生效**：把某 ERROR 事件静音 → **不推送但落库**（`muted=1`）；
4. **通配禁止**：静音项写 `RECONCILIATION_*` → **拒绝**（静音清单只接受完整 event_type）；
5. **未投递视图口径**（**同一视图的两面，故合并**）：
   - 构造投递失败 → 页面「未投递」视图**出现该条**；
   - 被静音的 ERROR **不出现在「未投递」视图**（`muted=0` 过滤），
    **但仍在告警列表里可见** —— 静音是人工决策，不是故障，**但不该因此消失**；
6. **通道未配置可见（按通道）**：只清空 `ALERT_WEBHOOK_URL`（飞书可用）→ 页面显示 **webhook 通道警告**，**且告警仍经飞书送达**；两通道都不可用 → 红色「通道全部未配置」+ `ALERT_CHANNEL_NOT_CONFIGURED`；
7. **投递探针（`-1` 与 `0` 都要能报）**：通道故障 → `0` → `ALERT_DELIVERY_FAILED`（CRITICAL）**必须发出**；通道未配置 → `-1` → `ALERT_CHANNEL_NOT_CONFIGURED` **也必须发出**（**否则通道空着时探针永远不告警**）；
8. **`-1` 与 `0` 可区分**：通道未配置 → `-1`；通道配置但失败 → `0`。

**附加（不计入 8 条）**：**变异测试** —— 把 `:67-68` 的 drop 分支加回来 → 守卫**必须转红**。
**守卫必须能失败**，否则是给自己发合格证。
