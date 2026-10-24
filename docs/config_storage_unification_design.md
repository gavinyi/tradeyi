# 配置存储统一与审计留痕 —— 架构项

> 状态：设计已定稿，待实施（P1，不阻塞 ML 第 1 步）
> 来源：Product 立的独立架构项 —— "配置存储统一 + `audit_events` 加 actor"
> 关联：`docs/tenant_key_inventory.md` §H、`docs/fail_closed_design_rules.md`、`docs/ml_model_management_design.md` §2.4

## 1. 问题（实测）

### 1.1 同一类配置分住两张表、两套 API、**两种留痕能力**

| 表 | 列 | 访问 API | 并发语义 | 留痕 |
| :--- | :--- | :--- | :--- | :--- |
| `system_settings` | `key, value, updated_at` | `get_setting` / `set_setting` / `settings_with_prefix` / `delete_setting` | 最后写入者胜 | ❌ **无 actor、无历史** |
| `runtime_config` | `config_key, value_json, version, updated_at` | `get_runtime_config` / `set_runtime_config` / `mutate_runtime_config` | 有 `version`，原子读改写 | ✅ `config_versions(config_key, value_json, **actor**, **reason**, created_at)` |

### 1.2 `audit_events` 没有 `actor` 列

`store.py:292-298`：`audit_events(id, event_type, strategy_id, payload_json, created_at)`
—— **没有 actor 列**。actor 只能塞进 `payload_json`，**无法按人检索**。

### 1.3 后果（真金系统不可接受）

**出了事要能回答「谁在什么时候改了什么」。** 今晚我们靠 `audit_events` 还原了 21:49 的真相，
但**如果 actor 只能塞 payload，下次就还原不了**。

**更糟的是**：`system_settings` 里躺着今晚查的**大部分关键开关** ——
`kill_switch:*`、`strategies_paused:*`、`volatility_new_entries_paused:*`、`execution_mode`、
`strategy_enabled:*`、`real_account_config:*`、`risk_limits:*` —— **它们的每一次变更都无 actor、无历史**。

---

## 2. 现状盘点（逐键归类）

### 2.1 在 `system_settings`（**当前无留痕**）

| 族 | 代表键 | 读频率 |
| :--- | :--- | :--- |
| 启停开关 | `kill_switch` / `{tenant}` / `{account}`、`strategies_paused`、`volatility_new_entries_paused`、`connectivity_degraded` | **极高**（每次风控检查） |
| 策略开关 | `strategy_enabled:*`、`strategy_execution_mode:*`、`execution_mode` | 高 |
| 风控状态 | `strategy_reconciliation_disabled:*`、`symbol_blocked:*`、`recovery_blocked:*` | 高 |
| 账户/租户配置 | `real_account_config:*`、`risk_limits:*`、`ml_scoring:*` | 中 |
| 任务调度 | `last_run:*`、`task_backoff_until:*`、`task_failure_count:*` | **极高**（每轮） |
| 雷达状态 | `volatility_signal_*`、`volatility_scanner_*`、`volatility_cooldown_*` | 高 |
| 通知 | `FEISHU_*`、`REAL_ACCOUNT_ID` | 低 |
| 清理 | `campaign_state`（`state_key, value_json`） | 高 |

### 2.2 在 `runtime_config`（**已有留痕**）

`VOLATILITY_CAMPAIGNS`、`GAMMA_CAMPAIGNS`、`STRATEGY_BUDGETS`、`VOLATILITY_SYMBOLS`、
`VOLATILITY_RADAR_BUDGETS`、`ENV_SETTINGS`（及 `config_versions` 历史）。

---

## 3. 目标与**不做什么**（架构判断）

### 3.1 不做：把两张表**物理合并成一张**

Product 提议"收敛到一张表、一套 API"。**我不建议物理合并**，理由是实测的访问模式差异：

| | `system_settings` | `runtime_config` |
| :--- | :--- | :--- |
| 类型 | 纯字符串 KV | JSON（可嵌套结构） |
| 读频率 | **极高**（每次风控检查、每轮调度都读） | 低（配置级） |
| 写并发 | 最后写入者胜即可 | 需 `version` 原子读改写（`BEGIN IMMEDIATE`） |
| 合并代价 | **每一个 `get_setting` 调用点都要改**（数百处），且把热读路径压到带版本锁的表上 | — |

**物理合并是一次大爆破迁移，风险与收益不成比例**，而且会牺牲热读路径的性能。

### 3.2 做：**统一"写侧契约"与"留痕能力"**

**目标不是"一张表"，而是"任何一次配置变更，都能回答谁/何时/为什么/改了什么"。**

1. **`system_settings` 增加历史表 `setting_versions`**（镜像 `config_versions`）：
   `id, key, value, actor, reason, created_at`；
   **索引**：`idx_setting_versions_lookup (key, id DESC)` + **`idx_setting_versions_actor (actor, id DESC)`** ——
   **留痕必须可按人检索**，否则"按人查"在真金系统里等于没有；
2. **`set_setting(key, value, actor=None, reason=None)`** —— **传了 actor 才记 `setting_versions`**（opt-in）；
   **值相同则跳过**（省写入 + 天然去重）；
3. **`audit_events` 加 `actor` 列**（一次性幂等迁移）；
4. **新增配置一律写 `runtime_config`**（有留痕），除非是高频读的简单标志位；
5. **禁止 `actor="system"` 默认值** —— 现有 `mutate_runtime_config(actor="system")` 的默认值**必须去掉**。

**这样两张表的读路径各自优化（热读不被拖慢），但写侧留痕能力完全一致、可统一检索。**

### 3.3 🔴 两类写入必须分开 —— `set_setting` 不是"稀有写"

**实测调用点**：`set_setting` **76 处**（`src/` 内）、`store.audit` **124 处**（src 56 / scripts 33 / pages 35，AST 精确统计）。

**但更要紧的是写入频率**：`reconciliation.py:157` 在
`for symbol in current_symbols` 循环里写 `symbol_blocked:{symbol}` ——
**每轮、每标的都写**（13 个标的 × 每 60 秒）；`:135` 的清理循环同理。

**若 `set_setting` 无条件写历史 → 热路径写入量放大一个数量级**，正好违反 §3.2 的
"读路径各自优化"。

| 类型 | 例子 | 需要留痕？ |
| :--- | :--- | :--- |
| **配置写**（决策） | `kill_switch`、`strategies_paused`、`execution_mode`、`real_account_config:*`、`risk_limits:*`、`strategy_enabled:*`、`ml_*` | ✅ **必须** |
| **运行状态写**（机器） | `symbol_blocked:*`、`last_run:*`、`task_backoff_until:*`、`volatility_signal_*`、`order_push_connected` | ❌ **不需要** —— 记了是噪声，还放大写入 |

### 3.3.1 🔴 按前缀 WARN 会误报 —— 有一类键**人和机器都在写**

**反例（实测）**：`strategy_enabled:*` 是**配置**（人决定启用策略），
但 `reconciliation.py:50,53` 会在对账 MISMATCH 时**把它写成 `false`** —— **机器写配置键**。
按前缀 WARN → **每一次对账禁用都会触发一条 WARN** → 噪声淹没真缺口。

**正确的修法：`actor` 不是"人"，而是"谁/什么做的决定"。**

| 写入方 | actor 取值 | 例子 |
| :--- | :--- | :--- |
| 人（页面） | `streamlit_admin:<user>` | 手动停用策略 |
| 机器（组件） | `service:reconciliation` / `service:radar` / `scheduler:main` | 对账禁用策略、滚动冷却 |

**收益**：① **留痕完整** —— "谁改的"的答案可以是"对账服务因 MISMATCH 改的"，
**这恰恰是最需要可追溯的那类变更**；② **WARN 只在"真的没人负责"时触发**；
③ **不增加热路径负担** —— 热路径的**状态写**仍不传 actor、不记历史。

**落地**：
- `reconciliation` 的机器写**必须传 `actor="service:reconciliation"`** —— 否则它自己就会把 WARN 打满；
- **对已知配置键前缀未传 actor → 记 WARN `CONFIG_WRITE_WITHOUT_ACTOR`（带 key）**，不阻断，但可见。
  **⏳ 本条仅在 C2 过渡期有效 —— C3 把 `actor` 改必填后自动废止**：
  C3 之后"未传 actor"会在**签名层**直接报错，这条 WARN 的路径**不复存在**。
  **不要把它写进 C3 的实现里**（死规则）。

> **原则**：**静默的缺口必须变成可见的** —— 但"可见"不能靠制造噪声实现。
> **给机器写一个诚实的 actor，比给它一条 WARN 更好。**

### 3.3.2 actor 校验：**必填 + 格式 fail-closed，类型只 WARN**（Chief 裁定）

**两条规则，作用在不同层面**：

| 层面 | 规则 | 后果 |
| :--- | :--- | :--- |
| **必填 + 格式** | `actor` 为空 / 缺失 / **不符合 `<class>:<id>` 格式** | **拒写 + 抛错**（fail-closed） |
| **类型匹配** | 实际 class 与登记表声明的 `human`/`machine`/`both` 不符 | **只 WARN，不拒写** |

**为什么类型不拒写**：存量与历史调用里存在大量**自由格式** actor
（`long_term_validation.py:204`、`real_trading.py:195,204`、`store.py:1402` 的 payload
实际值如 `"broker_position_verified"`；`ui.py:665,902` 甚至是中文 UI 标签）。
**类型拒写会把存量与历史调用打成阻断** —— 那是拿可用性换规范性，不成比例。

**class 枚举（格式 `<class>:<id>`）**：

| class | 含义 | 示例 |
| :--- | :--- | :--- |
| `streamlit_admin` | 人在页面改 | `streamlit_admin:admin` |
| `service` | 服务组件 | `service:reconciliation` / `service:radar` |
| `scheduler` | 调度器 | `scheduler:main` |
| `cli` | 命令行 | `cli:migrate_risk_limits` |
| `test` | 测试 | `test:pytest` |

> **`scheduler` 也要带 id**（`scheduler:main`）—— 单段 `scheduler` 不符合
> `<class>:<id>` 格式。runbook 已同步修正。

**类型推导（用于 WARN 判定）** —— **前提：已通过格式校验（有冒号且 class:id 均非空）**：

| 实际 actor | 推导类型 |
| :--- | :--- |
| `streamlit_admin:<user>` | `human` |
| `service:<comp>` / `scheduler:<id>` / `cli:<id>` / `test:<id>` | `machine` |
| **有冒号但 class 不在枚举内**（如 `system:radar`、`weird:foo`） | **`unclassified`** |

> **`system:` 不在五类枚举内** —— 机器写统一用 **`service:<component>`**
> （`service:radar` / `service:reconciliation` / `service:execution` …）。
> **`system:radar` 这种写法属"class 不认识" → `unclassified` → 只 WARN**，
> 由守卫提示改用 `service:`。**不新增第六个 class** —— 枚举每加一个，格式校验就多一处分支。

> **注意**：`broker_position_verified` **无冒号 → 按 §3.3.3 是"格式非法 → 拒写"**，
> **不是 `unclassified`**。`unclassified` 只属于**有冒号但 class 不认识**的值。
> （`broker_position_verified` 是**存量 payload 的历史值**，本规则只作用于**新 `actor` 参数**。）

### 3.3.3 🔴 判定写死：「格式非法」与「未分类」是两种不同行为

**这是 Chief 指出的矛盾点**：`broker_position_verified` 这种值，
"格式非法 → 拒写"与"未分类 → 只 WARN"会给出**相反结论**。**必须写死**：

| 实际 actor | 判定 | 行为 |
| :--- | :--- | :--- |
| **空值 / 缺失** | 格式非法 | **拒写 + 抛错** |
| **无冒号**（如 `scheduler`、`broker_position_verified`） | **格式非法** | **拒写 + 抛错** |
| **有冒号，class 不认识**（如 `weird:foo`） | **`unclassified`** | **只 WARN，不拒写** |
| 有冒号，class 认识（`service:x` / `streamlit_admin:y`） | `human` / `machine` | 正常；类型不符时 WARN |

**为什么这样切**：
- **无冒号 = 连格式都不符合** → 拒写（fail-closed，这是 C1 的核心约束）；
- **有冒号但 class 不认识** = **格式合规、语义未知** → 只 WARN（存量与历史调用大量属于此类，拒写会打断它们）。

> **注意**：`broker_position_verified` **无冒号 → 按本表是"格式非法 → 拒写"**。
> 但它是**存量 payload 里的历史值**，**不是新参数** —— 本表**只约束新引入的 `actor` 参数**。
> **存量 payload 的自由文本不受约束**（那是数据，不是参数）。
> **PRD 验收 #5 的表述必须写明这一点**，否则 Tester 会拿存量 payload 来测拒写。

**三份文档（PRD / 设计 / runbook）本节必须逐字一致。**

**WARN 规则**：`both` 键接受任何 actor（含 `unclassified`）；
`human` / `machine` 键收到 `unclassified` → **WARN**（不阻断）。

**规则**：
- `both` 键 → 接受任何 actor（含 `unclassified`）；
- `human` / `machine` 键 → 收到 `unclassified` → **WARN（不是拒绝）**；
- **不强制改造存量自由格式 actor** —— 那是另一批工作，且**不该为了类型校验去改审计语义**。

### 3.4 配置键登记表（单一来源）

**形态：Python 模块，不是 DB 表** ——
`src/futu_quant/config_key_registry.py`：

```python
@dataclass(frozen=True)
class KeySpec:
    key_prefix: str          # 前缀匹配（含冒号形态）
    actor_type: str          # "human" | "machine" | "both"
    reason_required: bool = True
    note: str = ""

CONFIG_KEY_REGISTRY: tuple[KeySpec, ...] = (
    KeySpec("kill_switch", "both", note="急停开关，人可手动、服务可自动触发"),
    KeySpec("strategies_paused", "human", ...),
    KeySpec("execution_mode", "human", ...),
    KeySpec("real_account_config:", "human", ...),
    KeySpec("risk_limits:", "human", ...),
    KeySpec("strategy_enabled:", "both", note="人可启用；对账服务可自动禁用"),
    KeySpec("ml_", "human", ...),
)
```

**硬规则**：
1. **只有登记在表里的键才要求 actor**（不按前缀猜 —— 那会误报）；
2. **新增配置键必须登记，否则守卫测试直接红** —— "忘登记"也变成可见缺口；
3. **登记表是 Python 常量，随 PR 审查** —— 不做成 DB 表（DB 表本身又要一套写路径与 actor，递归）；
4. **守卫必须可做变异测试**：把某个键的 `actor_type` 从 `both` 改成 `human` → 该键用
   `service:` actor 写入时守卫**必须转红**。

---

## 4. `audit_events` 加 `actor` 列

```sql
ALTER TABLE audit_events ADD COLUMN actor TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor, id DESC);
```

- 迁移**幂等**（`initialize()` 里用 `ADD COLUMN` + 存在性检查）；
- `store.audit(event_type, payload, strategy_id, actor=...)` —— **`actor` 设为必填**（无默认值）；
- 存量行 `actor=''`，标注为"历史遗留，无法追溯" —— **不伪造**。

---

## 5. 迁移与落地

| 步 | 内容 | 风险 |
| :--- | :--- | :--- |
| **1** | `audit_events` 加 `actor` 列 + `store.audit` 必填 actor | 低（幂等迁移） |
| **2** | `setting_versions` 表 + `set_setting(..., actor, reason)` | 中（所有写点要传 actor） |
| **3** | 去掉 `mutate_runtime_config` 的 `actor="system"` 默认 | 低 |
| **4** | **存量盘点**：逐键给出"留在 `system_settings` / 迁 `runtime_config`"建议 | 低（只出报告） |
| **5** | 页面/脚本所有写配置处**显式传 actor** | 中（逐点核） |

**硬规则**：
- **不新增第三处配置存储**；
- **`actor` 与 `reason` 一律必填**，空值视为无效写入（拒绝 + ERROR）；
- **迁移只加能力，不改语义** —— 读路径行为完全不变。

---

## 6. 验收（交给 Tester）

1. `set_setting("x", "1", actor="tester", reason="验证")` → `setting_versions` 出现该条，
   **含 actor/reason/时间**；
2. `set_setting` **不传 actor → 拒绝写入 + ERROR**（禁止默认值）；
3. `store.audit(..., actor="tester")` → `audit_events.actor == "tester"`，**可按 actor 检索**；
3b. `setting_versions` 同样**可按 actor 检索**（索引 `idx_setting_versions_actor`）；
4. 存量 `audit_events` 行 `actor=''`，**不伪造**；
5. **读路径行为不变**：`get_setting` 语义、性能无回归（全量回归 + 关键查询耗时对比）；
6. **变异测试**：把 `set_setting` 的 actor 参数改回可选 → 守卫转红。

