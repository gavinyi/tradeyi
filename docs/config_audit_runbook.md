# 配置审计改造 —— 落地 Runbook

> 状态：runbook 已定稿，供 P1-C1 / C2 / C3 执行时使用
> 关联：`docs/config_storage_unification_design.md`（方案）、`docs/fail_closed_design_rules.md`
> 原则：**同批改完、可回退、不留半状态** —— 今晚已为半状态付过三次代价。

---

## ① C1：`audit_events.actor` 破坏性变更（**124 处调用点**：`src/` 56 + `scripts/` 33 + `pages/` 35，AST 精确统计；原写 55 是只数了 `src/` 的一部分）

### 1.1 切换顺序（**顺序不能反**）

**actor 格式（`<class>:<id>`，fail-closed）**：

| class | 含义 | 示例 |
| :--- | :--- | :--- |
| `streamlit_admin` | 人在页面改 | `streamlit_admin:admin` |
| `service` | 服务组件 | `service:reconciliation` |
| `scheduler` | 调度器 | `scheduler:main` |
| `cli` | 命令行 | `cli:migrate_risk_limits` |
| `test` | 测试 | `test:pytest` |

**校验规则**：**空值 / 缺失 / 非法格式 → 拒写 + 抛错**；
**类型匹配（human/machine/both）只 WARN，不拒写**（存量自由格式 actor 不阻断）。

**🔴 判定写死（与设计 §3.3.3 逐字一致）**：

| 实际 actor | 判定 | 行为 |
| :--- | :--- | :--- |
| **空值 / 缺失** | 格式非法 | **拒写 + 抛错** |
| **无冒号**（如 `scheduler`、`broker_position_verified`） | **格式非法** | **拒写 + 抛错** |
| **有冒号，class 不认识**（如 `weird:foo`） | **`unclassified`** | **只 WARN，不拒写** |
| 有冒号，class 认识 | `human` / `machine` | 正常；类型不符时 WARN |

> **本表只约束新引入的 `actor` 参数**；**存量 payload 的自由文本不受约束**
> （`broker_position_verified` 是数据不是参数）。**PRD 验收 #5 必须写明这一点。**

```
第 1 步  加列（幂等，向后兼容）
        ALTER TABLE audit_events ADD COLUMN actor TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor, id DESC);
        ← 旧代码不改也能跑（新列有 DEFAULT）

第 2 步  改 store.audit 签名（加 actor 必填）        ← 破坏性，这一步才打穿调用点

第 3 步  同批改完 124 处调用点（src 56 / scripts 33 / pages 35）

第 4 步  全量回归 + 静态巡检 + Reviewer 签字

第 5 步  部署
```

**为什么不能先改签名再加列**：中间状态会往**不存在的列**写 → 直接报错。
**为什么第 1 步安全**：`ADD COLUMN ... DEFAULT ''` 是**加法**，旧代码完全不受影响。

### 1.2 回滚

| 场景 | 回滚动作 | 数据影响 |
| :--- | :--- | :--- |
| 代码部署后发现问题 | **回滚代码即可** | ✅ **无影响** —— `actor` 列留着（DEFAULT `''`），旧代码不读它 |
| 加列失败 | 迁移幂等，**重跑**即可 | 无需回滚 |
| 加索引失败 | `IF NOT EXISTS`，**重跑** | 无需回滚 |

> **关键**：**列是加法，回滚代码不需要 DROP 列**。DROP 才是破坏性操作，**不做**。
> 回滚判据：`PRAGMA quick_check` 通过 + `audit_events` 行数与回滚前一致。

### 1.3 124 处调用点的 actor 取值（**同批交付**；原表只覆盖 src 一部分，pages/scripts 需按模块补全）

| 文件 | 处数 | actor |
| :--- | :--- | :--- |
| `execution.py` | 16 | `service:execution` |
| `scheduled_tasks.py` | 6 | `scheduler:main` |
| `volatility_scanner.py` | 4 | `service:radar` |
| `volatility_orphans.py` | 4 | `service:radar` |
| `core_validation.py` | 3 | `service:validation` |
| `store.py` | 3 | `service:store` |
| `campaign_cleanup.py` | 2 | `service:campaign_cleanup` |
| `shadow_matching.py` | 2 | `service:shadow_matching` |
| `risk.py` | 2 | `service:risk` |
| `risk_limits.py` | 2 | `service:risk` |
| `real_trading.py` | 2 | **透传已有 `actor` 参数**（人工解锁 → `streamlit_admin:<user>`） |
| `reconciliation.py` / `order_push.py` / `option_lifecycle.py` / `health.py` / `disaster_recovery.py` / `operational_readiness.py` / `long_term_validation.py` / `validation_loop.py` | 各 1 | `service:<模块名>` / `service:health` / `service:dr` / `service:allocation` / `service:validation` |

**硬约束**：
1. **禁止 `actor="system"`**（无信息量的占位）；
2. **格式必须 `<class>:<id>`**：机器写用 `service:<component>` / `scheduler:main`，
   人写用 `streamlit_admin:<user>`；**单段 `scheduler` 不合法**；
3. **空值 / 缺失 / 非法格式 → 拒写 + 抛错**（fail-closed，见 §1.1）；
4. **类型匹配（human/machine/both）只 WARN，不拒写** —— 存量自由格式 actor 不应被阻断。
3. **124 处必须同批改完** —— 留一处不改就是编译错误（必填参数），**这正是"同批"的保证**。

### 1.4 C1 验收（两条，缺一不可）

1. **空/非法 actor → 拒写并抛错**（fail-closed），守卫 + **变异证红**；
2. **存量 `actor=''` 仍可正常读取** —— 不伪造、不改写。

---

## ② C3：热路径写入量前后对比方法

### 2.1 为什么必须测

`reconciliation.py:157` 在 `for symbol in current_symbols` 循环里写 `symbol_blocked:{symbol}`
—— **每轮 × 每标的**（13 标的 × 60 秒）。**若 `set_setting` 无条件记历史，写入量放大一个数量级。**

### 2.2 测法（三步，全部只读）

**第一步：基线（改造前）**
```
跑一个模拟对账周期（桩 broker，13 个标的），统计：
  - set_setting 调用次数
  - system_settings 的 updated_at 变更行数
  - SQLite 写入事务数（可通过 PRAGMA data_version 变化或日志）
```

**第二步：改造后**
```
同样跑一个周期，额外统计：
  - setting_versions 新增行数
  - set_setting 调用次数（应不变）
```

**第三步：判据**

| 判据 | 阈值 |
| :--- | :--- |
| `setting_versions` 新增行数 | **≤ 配置写次数**（不是 `set_setting` 总次数） |
| `symbol_blocked:*` / `last_run:*` / `volatility_signal_*` **出现在 `setting_versions`** | **0 次**（出现即失败） |
| `set_setting` 调用次数 | **与基线一致**（"值相同则跳过"只减不增） |
| 单轮对账耗时 | **劣化 < 5%** |

> **最简实现**：在测试里 **monkeypatch `set_setting` 与 `setting_versions` 插入**，
> 跑一个对账周期，**断言状态写没有进历史表** —— 这条断言本身就是守卫，且不依赖生产测量。

### 2.3 "值相同则跳过"的验收

`set_setting(k, v)` 传**相同的值两次** → `setting_versions` **只新增 1 条**（或 0 条，若首次也不记）。
**变异测试**：去掉"值相同跳过" → 断言转红。

---

## ③ 迁移失败的回滚点与判据

### 3.1 回滚点

| 阶段 | 失败形态 | 回滚动作 |
| :--- | :--- | :--- |
| **加列** | `ALTER TABLE` 报错（极少见） | **不回滚，重跑**（幂等） |
| **加索引** | 同上 | **重跑**（`IF NOT EXISTS`） |
| **建 `setting_versions` 表** | 同上 | **重跑** |
| **代码部署** | 全量回归红 / 运行时报错 | **回滚代码**（列留着无害，见 §1.2） |
| **`set_setting` 签名变更** | 有调用点漏改 → 编译/运行报错 | **回滚代码**（同批改完是这条的预防） |

### 3.2 判据（迁移前后各测一次）

| 项 | 判据 |
| :--- | :--- |
| **完整性** | `PRAGMA quick_check` 通过 |
| **行数** | `audit_events` / `system_settings` 行数与迁移前**一致**（迁移只加列/加表，不动数据） |
| **存量** | `audit_events.actor` **全为 `''`**（未被伪造） |
| **性能** | 关键查询耗时劣化 **< 20%**（新增索引会改变计划，需实测） |
| **可回滚性** | **回滚代码后服务正常启动**（这是"列是加法"的验证） |

### 3.3 迁移前必做

1. **备份**：`data/qa/` + `manifest-<时间>.json`（含 sha256）—— 沿用既有备份流程；
2. **备份必须在迁移开始前完成**，不是迁移后；
3. **磁盘余量检查**（< 4 GB 直接拒）—— 沿用 `migrate_volatility_campaigns_tenant_scope.py:511` 的现成实现。

---

## 4. 执行纪律（照 Chief 定的）

1. **C1 → C2 → C3 串行**，前一批**合并并回归完**才开下一批；
2. **交易时段只写不部署** —— 部署统一放**收盘后 / 明早开盘前**，且必须**全量绿 + Reviewer 签字**；
3. **同批改完、禁止半状态**；
4. **回滚永远是"回滚代码"，不是"改数据"** —— 列是加法，留着无害。

