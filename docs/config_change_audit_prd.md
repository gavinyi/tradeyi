# 配置变更审计 产品需求规格说明书 (PRD)
> **文档版本**: 1.0 (PRD · 用户/运营视角)  
> **负责人**: Product (产品经理)  
> **制定日期**: 2026-10-23  
> **配套技术设计**: [config_storage_unification_design.md](config_storage_unification_design.md)  
> **状态**: 待实施  
> **产品红线**：**必须在「恢复实盘交易」之前完成**

---

## 一、背景与问题

### 1. 现状：配置变更无法回答「谁在什么时候改了什么」
| 写入路径 | 表 | 留痕能力 |
| :--- | :--- | :--- |
| `mutate_runtime_config(..., actor=...)` | `runtime_config` | ✅ `config_versions`（actor + reason + 时间） |
| `set_setting(key, value)` | `system_settings` | ❌ **无 actor、无历史** |
| `store.audit(...)` | `audit_events` | ⚠️ **无 actor 列**（actor 只能塞 payload，**无法按人检索**） |

### 2. 后果（实测）
`system_settings` 里躺着的**关键开关，每一次变更都无 actor、无历史**：
```
kill_switch / kill_switch:{tenant} / kill_switch:{account}
strategies_paused / strategies_paused:{tenant}
volatility_new_entries_paused:{tenant}
execution_mode、strategy_enabled:*、real_account_config:*、risk_limits:*
last_run:*、task_backoff_until:*、task_failure_count:*
```

### 3. 为什么这是红线
**今晚的证据**：我们靠 `audit_events` 还原了 21:49 的真相（成交 → 接管 → 合约号被改写 → 孤儿）。**但如果 actor 只能塞 payload、无法按人检索，下次就还原不了。**

**真金系统必须能回答「谁在什么时候改了什么」**——这是**可审计性底线**，不是可选项。

---

## 二、用户与场景

| 用户 | 场景 | 核心诉求 |
| :--- | :--- | :--- |
| **交易员/用户** | 发现某个开关被改了 | 知道**谁改的、什么时候、为什么** |
| **交易员/用户** | 怀疑某次异常与配置有关 | 能按**时间窗**查变更 |
| **审计/复盘** | 事后追溯 | 能按**人**查、按**键**查，导出 |
| **运维** | 交接班 | 知道**最近改了什么** |

---

## 三、查询能力（三种筛选维度）

### 1. 按人筛（actor）
- 输入 `actor`（支持前缀匹配，如 `streamlit_admin:` / `service:reconciliation`）
- 输出该 actor 的全部变更，**按时间倒序**
- **必须支持**：`streamlit_admin:admin`（人）与 `service:reconciliation`（机器）**可区分**

### 2. 按键筛（key）
- 输入配置键（支持前缀，如 `risk_limits:` / `kill_switch`）
- 输出该键的全部变更历史
- **必须支持**：查看**某一键的完整生命周期**（初值 → 每次变更 → 现值）

### 3. 按时间筛
- 输入时间窗（如「10-22 21:00–22:00」）
- 输出该窗口内**所有**配置变更
- **用途**：事故复盘时重建「事发前后改了什么」

### 4. 组合查询
- 人 + 键 + 时间窗任意组合
- 支持「只看真金相关键」快捷筛选（`real_account_config:*` / `risk_limits:*` / `kill_switch*`）

---

## 四、历史留痕展示口径

### 1. 展示字段（每条变更）
| 字段 | 说明 |
| :--- | :--- |
| **时间** | 变更时间（中国时区，精确到秒） |
| **配置键** | 完整键名（含租户后缀） |
| **变更前** | 旧值（敏感值脱敏） |
| **变更后** | 新值（敏感值脱敏） |
| **actor** | `streamlit_admin:admin` / `service:reconciliation` |
| **reason** | 变更原因（**必填**，空 reason 视为无效） |
| **来源** | 页面 / 调度器 / CLI / 对账服务 |

### 2. 展示规则
- **按时间倒序**，默认显示最近 20 条，可分页；
- **值相同则不产生记录**（去重，天然省写入）；
- **敏感键脱敏**：`real_account_config:*` 的账户号、`risk_limits:*` 的数值**明文显示**（这是运维需要的），但**密钥类键**（如有）脱敏；
- **存量 `actor=''` 的行**：显示为「历史遗留 · 无法追溯」，**不伪造、不回填**。

### 3. 两类写入的区分（展示层也要体现）
| 类型 | 例子 | 是否留痕 |
| :--- | :--- | :--- |
| **配置写**（人/机器决策） | `kill_switch`、`strategies_paused`、`execution_mode`、`real_account_config:*`、`risk_limits:*`、`strategy_enabled:*`、`ml_*` | ✅ **必须** |
| **运行状态写**（机器产生） | `symbol_blocked:*`、`last_run:*`、`task_backoff_until:*`、`volatility_signal_*` | ❌ 不留痕（噪声） |

**理由**：`reconciliation.py:157` 在 `for symbol` 循环里**每轮每标的写** `symbol_blocked:`（13 标的 × 60 秒）——无条件留痕会**把热路径写入放大一个数量级**。

---

## 五、页面信息架构（两个入口）

### 入口 A：配置审计页（新页面）
- **顶部筛选栏**：人 / 键 / 时间窗 / 快捷筛选（真金相关键）
- **主表**：变更历史（字段见 §4.1）
- **侧栏**：最近 5 条变更（**任何配置页保存后即时显示**）
- **导出**：CSV（用于审计归档）

### 入口 B：各配置页内嵌
- 每个配置页（风险中心、策略控制、ML 管理…）保存后，**该页底部显示最近 5 条变更**
- **让用户在「改的地方」就能看到「改了什么」**

---

## 六、状态语义

| 状态 | 判据 | 展示 |
| :--- | :--- | :--- |
| 🟢 **可追溯** | 变更有 actor + reason | 正常展示 |
| 🟠 **缺 reason** | 有 actor 无 reason | 显示「原因缺失」标记（**写入时应拒绝**） |
| 🔴 **无 actor** | 登记表内的键未传 actor | **WARN 可见**（不阻断），展示「无责任人」<br>⚠️ **仅 C2 过渡期有效**——C3 把 `actor` 改必填后，此路径不存在，**本条自动废止**（见 §9.1） |
| ⚫ **历史遗留** | 存量 `actor=''` | 「历史遗留 · 无法追溯」 |

---

## 七、文案规范

| 场景 | 文案 |
| :--- | :--- |
| 正常 | `2026-10-22 21:49 · risk_limits:futu_real_master · max_daily_loss 10000 → 100000 · service:reconciliation · 原因：租户级隔离` |
| 无责任人 | `⚠️ 该变更无责任人 · 请确认是否遗漏传 actor` |
| 历史遗留 | `历史遗留 · 无法追溯（该变更发生在本功能上线前）` |
| 空态 | 「所选条件下无变更记录」 |

---

## 八、空态与异常态

| 情形 | 展示 |
| :--- | :--- |
| 无记录 | 「所选条件下无变更记录」 |
| 存量数据 | 「历史遗留 · 无法追溯」+ 说明 |
| 查询失败 | 「审计查询暂不可用」+ 重试，**不显示堆栈** |
| 写入被拒 | 「actor 缺失或格式非法，写入被拒」+ 说明正确格式 |

---

## 九、验收（5 条，可演示）

1. **按人可查**：改 `kill_switch` → 用 actor `streamlit_admin:admin` 筛选 → **能查到该条变更**（含前后值、reason、时间）。
2. **按键可查**：查 `risk_limits:` 前缀 → **列出该前缀下所有键的完整变更历史**。
3. **按时间可查**：查「10-22 21:00–22:00」→ **能重建事发前后的变更序列**。
4. **两类写入区分**：构造对账禁用策略（机器写 `strategy_enabled:*`）→ **有留痕且 actor = `service:reconciliation`**；构造 `symbol_blocked:*` 状态写 → **不产生留痕**，且**改造前后写入量对比不放大**。
5. **格式 fail-closed**：传空 actor 或非法格式 → **写入被拒 + 明确报错**；存量 `actor=''` 行**仍可正常读取**（不被伪造）。

> **第 5 条的适用范围（重要，采纳 Architect 补充）**：
> **「非法格式拒写」仅适用于新引入的 `actor` 参数**（C1 新增，要求 `<class>:<id>` 格式）。
> **存量 payload 里的 `actor` 字段是历史自由文本**（实际值形如 `broker_position_verified`），**那是数据不是参数，不动它**——否则旧代码全挂。

### 9.1 `actor` 校验的三种情形（判定写死，三份文档一致）

| 情形 | 示例 | 判定 | 行为 |
| :--- | :--- | :--- | :--- |
| **空值 / 缺失** | `actor=""` / 未传 | **格式非法** | 🛑 **拒写**（fail-closed） |
| **无冒号** | `broker_position_verified`、`scheduler` | **格式非法** | 🛑 **拒写**（fail-closed） |
| **空 class / 空 id** | `:foo`、`service:` | **格式非法** | 🛑 **拒写**（fail-closed） |
| **有 `class:` 但 class 不认识** | `weird:foo` | **`unclassified`** | ⚠️ **只 WARN，不拒写** |
| **class 认识但类型不匹配** | 机器写传 `streamlit_admin:admin` | **类型错配** | ⚠️ **只 WARN，不拒写** |
| **完全合法** | `service:reconciliation` | 正常 | ✅ 写入 |

**完整判定链（与设计 §3.3.3 / runbook §1.1 同源）**：
```
空值 / 缺失 / 无冒号 / 空 class / 空 id  → 格式非法 → 拒写（fail-closed）
有 class: 但 class 不在五个枚举内        → unclassified → 只 WARN
class 在枚举内但与声明类型不符           → 只 WARN
（全部只作用于新 actor 参数；存量 payload 不校验）
```

**关键区分（Chief 裁定，必须写死）**：
- **「格式非法」= 结构上无法解析**（空、无冒号）→ **拒写**；
- **「未分类」= 结构合法但语义不认识**（class 不在枚举内、或类型不匹配）→ **只 WARN**。

**理由**：拒写是**阻断**，只该用于「结构无法解析」；**语义不认识**不该阻断业务——否则存量与历史调用会被打成阻断。

**合法 class 枚举**：`streamlit_admin` / `service` / `scheduler` / `cli` / `test`。

**格式只接受 `<class>:<id>`，没有单段例外**——`scheduler` 这类裸单段**属格式非法 → 拒写**；调度器应写 **`scheduler:main`**（或带具体组件名）。
**理由**：若给单段开例外，`broker_position_verified` 这类存量自由文本就会被当成「合法单段」放行——**例外会把格式校验蛀空**。

> **判据**：新参数格式非法 → 拒写；存量 payload 自由文本 → **不受本规则约束**（是数据不是参数）。
> **§六「无 actor → WARN」与本节的过渡关系**：§六那条**仅 C2 过渡期有效**；**C3 把 `actor` 改必填后，「未传 actor」这条路径不存在，§六那条自动废止**——**不留死规则**。

---

## 十、非目标（本期不做）
- 不做配置变更的**审批流**（改前需批准）——本期只做**事后可追溯**；
- 不做配置回滚 UI（回滚走 CLI/运维流程）；
- 不做配置版本 diff 可视化（表格展示前后值即可）。

---
*本 PRD 为用户/运营视角；技术实现见配套设计文档。*
