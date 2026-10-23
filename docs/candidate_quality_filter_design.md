# 候选质量过滤 —— 技术设计（P0-5）

> 状态：设计待签字（门禁第 3 步）
> 上游：[candidate_quality_filter_prd.md](candidate_quality_filter_prd.md)（Product PRD，已落盘）
> 关联：`docs/fail_closed_design_rules.md` §4（静默是最大的敌人）
> 代码冻结中：**本文档不授权写代码**

## 1. 现状：三层，各管一段（实测）

| 层 | 代码 | 管什么 | 粒度 |
| :--- | :--- | :--- | :--- |
| **① 雷达扫描层** | `volatility_scanner.py` `scan_market_layer` / `fan_out_to_tenant` | 扫**哪些正股**、建 campaign | **正股** |
| **② 候选构建层** | `src/futu_quant/volatility_roll.py:150` `rank_long_option_candidates` → `:161` `rank_volatility_contracts` → `:196` `_apply_ml_ranking` | 构建**期权合约候选**、评分、选结构 | **期权合约** |
| **③ 入场门禁层** | `data_quality.py:31-56` `option_quote_freshness` | 下单前的新鲜度 + 盘口检查 | **单笔** |

**门禁实测参数**（`data_quality.py:32-33`）：`fresh_seconds=180`、`maximum_seconds=900`、
**`maximum_spread_pct=20.0`**。
降级逻辑（`:44-48`）：`180 < age ≤ 900` 且盘口可用（`bid>0 && ask>0 && ask>=bid && 价差≤20%`）→ **降级通过**。

---

## 2. 过滤点落点（Chief 点名要写清的）

### 2.1 硬排除 → **候选构建层**（`src/futu_quant/volatility_roll.py`）

**理由**：
1. **雷达层只看得见正股，看不见合约盘口** —— 合约级判据（到期、价差、盘口）在雷达层**拿不到数据**，硬要放过去要么拿不到、要么在雷达层加一次合约行情调用（**那才是真正的性能坑**）；
2. 候选构建层**已经在遍历合约候选**（`rank_long_option_candidates`），加判据是**同一趟循环**，无额外遍历；
3. **在评分之前排除** → 被排除的合约**不参与排序、不进结构、不写 campaign**，源头干净。

**插入点**：`rank_long_option_candidates`（`:150`）**之后**、`rank_volatility_contracts`（`:161`）**之前**。

### 2.2 软评分 → **也在候选构建层**，但作为**降权**而非排除

在 `rank_volatility_contracts`（`:161`）的评分里加**惩罚项**（价差越大、成交量越低 → 分数越低），
**不改变候选集合**，只改变排序。

**为什么软评分不放入场层**：入场层是**单笔**视角，**没有"和其他候选比较"的上下文** ——
**"排末尾"这个语义只在候选层成立**。

### 2.3 雷达层**只加正股级过滤**

雷达层可加的（有数据支撑）：**正股本身无行情 / 正股 `update_time` 停滞** → 跳过该正股。
**不碰合约**。

### 2.4 入场门禁层**保持不变**

`data_quality.py` 的 900 秒硬上限**不动**（Product PRD ⑤ 已裁定），
它是**最后一道防线** —— 候选层没拦住的，这里仍拦。

### 2.5 🔴 不变量：过滤器**只作用于新入场候选**，已持仓的退出管理不受影响

**这是 META 那次的失效模式**：止损评估**只遍历 campaign 列表**，
一旦"被排除"被实现成"跳过该 symbol"，**已持仓仓位会同时失去止盈/止损/平仓** ——
**META 从 $300 跑到 $366 就是这么来的**。

**硬规则**：
1. **过滤发生在"构建候选"这一步**，**不触碰已有 campaign 与其持仓**；
2. **被排除的合约若已有持仓 → 保留其 campaign（`exit_only`）**，退出管理照常；
3. **验收**：构造"持仓合约落在排除名单里" → **仍能正常止盈/止损/平仓**（Reviewer 阻断项）。

> **实现提示**：**最自然的写法（"跳过该 symbol"）就是错的** ——
> **过滤必须作用在"候选列表"，不是"symbol 是否存在"**。

---

## 3. 阈值单源（Chief 硬要求 ①）

**问题**：Ops 建议"价差 >50% 排除"，而入场层是 **20%** —— 两套阈值会让 20%–50% 的合约
**"入选后被拦"**，产生**不可解释的 BLOCK**。

**设计**：**一个配置源，两层读同一个解析器**。

```
candidate_quality:{tenant_id}   （runtime_config）
  ├─ hard_expiry_min_days       硬排除：剩余到期下限（默认 1）
  ├─ hard_stale_seconds         硬排除：报价停滞下限（默认 180）
  ├─ soft_spread_pct            软降权：价差阈值（默认 **实盘 5% / 模拟 10%**）
  ├─ soft_volume_min            软降权：成交量下限
  └─ entry_max_spread_pct       入场层盘口上限（默认 20，与 data_quality 现值一致）
```

> **价差阈值取 5%/10%（Product 已采纳 Chief 建议，非原建议的 8%/15%）**。
> **前提**：**必须真正实现为软降权** —— **若实现成硬排除，5% 会砍掉大量合约、
> 反而回到「没有候选」的老问题**。**这条进实现验收**（见 §8 验收 3）。

**硬规则**：
1. **`soft_spread_pct ≤ entry_max_spread_pct`** —— **候选层必须严于或等于入场层**，
   否则会出现"候选层放行、入场层拦住"的**不可解释 BLOCK**；
   **解析时校验**，违反 → **拒绝解析 + ERROR**（fail-closed）；
2. **两层读同一个 `resolve_candidate_quality(store, broker)`** —— **不各读各的**；
3. 参数**按租户可配**（实盘严、模拟松），走与 P0-7 同构的三层解析链。

---

## 4. `exclusion_reason` 存哪（Chief 点名要答的）

**新增表 `candidate_exclusions`**（幂等建，纳入 `store.initialize()`）：

```
id, tenant_id, strategy_id, underlying, code,
reason,              -- EXPIRED_T0 / QUOTE_STALE / BOOK_MISSING / SPREAD_TOO_WIDE / VOLUME_TOO_LOW
age_seconds,         -- 实测报价停滞秒数（可空）
spread_pct,          -- 实测价差（可空）
volume,              -- 实测成交量（可空）
phase,               -- HARD_EXCLUDE / SOFT_DEMOTE
created_at
```

**为什么不复用 `decision_logs`**：
1. `decision_logs` 按 `(strategy_id, symbol)` 去重、且会被 prune —— **排除记录需要完整留痕**；
2. 排除是**每轮 × 每候选**的高频事件，混进 `decision_logs` 会**把它冲垮**；
3. 监控页要**按原因聚合**（"硬排除 M / 软降权 K"）—— 需要**独立可查可聚**的表。

**索引**：`(tenant_id, created_at DESC)` + `(reason)` —— 监控页按租户 + 时间窗查。

### 4.1 四条硬约束（Chief 裁定）

| # | 约束 | 理由 |
| :--- | :--- | :--- |
| **a** | **必须带 `tenant_id`** | 不带租户 → 跨租户又混（今天已发生过） |
| **b** | **必须带实测值** `age_seconds` / `spread_pct` / `volume` | **否则"被排除"和"猜被排除"分不清** |
| **c** | 🔴 **绝不写 `runtime_config`，也绝不写任何无前缀全局键** | **今天已踩过"测试写穿生产 `runtime_config`"**；本过滤器**每轮每标的都写**，**是最容易复发的形态** |
| **d** | **必须有 prune / 保留策略 + 表体积监控**（Ops） | 高频写入，无保留策略就是下一个 `reconciliation_reports`（129 万行） |

**保留策略（本期）**：
- `candidate_exclusions` **按天保留 7 天**（`prune_candidate_exclusions(before=now-7d)`，纳入统一调度）；
- **监控指标**：表行数 + 每日增量 + 最长保留期 —— **Ops 巡检项**；
- **表结构预留降采样**：`created_at` 精确到秒，便于后续按天归档。

---

## 5. 性能影响（Chief 点名要答的）

| 项 | 影响 | 处置 |
| :--- | :--- | :--- |
| **停滞判据需要 `update_time`** | **候选构建层现在没有逐合约行情** —— 要判 180s 停滞**必须取一次行情** | 用 **OpenD 批量 `snapshots`**（一次调用取全部候选合约），**严禁逐合约调用** |
| 每轮候选数 | 量级：每标的几十个合约 × 13 个标的 | 一次批量调用覆盖，**O(1) 次网络往返** |
| 排除记录写入 | 每轮几十条 INSERT | 可接受（一行一条）；**监控指标见下** |
| 软降权计算 | 纯内存 | 无影响 |

**新增监控指标**（Ops 要求的"被排除候选数 + 原因分布"）：
- `candidate_total` / `candidate_hard_excluded` / `candidate_soft_demoted`（每轮）；
- **按原因分布**（`EXPIRED_T0` / `QUOTE_STALE` / …）—— **监控页可见**。

### 5.1 🔴 300s 收紧的埋点与复审（Chief 裁定 ③）

**背景**：Reviewer 指出「900s 降级几乎不可达、所以 300s 无影响」是**断言不是实测** ——
**候选层与入场层在时间上分离**（扫描 → 建 campaign → 下一轮评估 → 选结构 → 下单），
**候选在扫描时新鲜，不代表下单时新鲜**（中间可能隔一个周期）。

**裁定**：**300s 先上线，但必须带埋点**：

```
quote_age_at_submit   -- 下单尝试时刻的报价年龄（秒）
```

- **记录点**：入场门禁层判定**通过**（含降级通过）时，把当次 `age` 写入
  `candidate_exclusions`（`phase=ADMITTED`）**或**独立的埋点表；
- **统计**：每日出 **P50 / P95 / P99**（按租户分开）；
- **复审**：**一周后按数据复审** —— **若 P95 逼近 300s，说明拦单过多，回退到 600s**；
- 🔴 **文档里不许再写「影响几乎为零」这类断言** —— **红区结论必须有数据**。

**监控页**：`quote_age_at_submit` 的 P50/P95 曲线 —— **让"代价"可见，而不是靠断言**。

---

## 6. 硬排除的四条判据（对齐 PRD）

| 判据 | 阈值 | 理由 |
| :--- | :--- | :--- |
| **剩余到期 < 1 天** | `hard_expiry_min_days=1` | 今晚 **65 次**被"合约已到期"挡下 —— **雷达在选 T-0 合约**，是选品问题 |
| **报价停滞** | `hard_stale_seconds=180` | 180s 与入场层的 `fresh_seconds` **同值**（同源） |
| **盘口缺失** | `bid<=0 or ask<=0 or ask<bid` | 无法报价 = 无法交易 |
| **价差过硬上限** | 见 §3（`entry_max_spread_pct=20`） | **硬排除只用于"数据不可信/无法交易"** |

**软降权两条**：价差（`soft_spread_pct`）、成交量（`soft_volume_min`）。

---

## 7. 迁移与回滚

| 步骤 | 内容 | 回滚 |
| :--- | :--- | :--- |
| 1 | 建 `candidate_exclusions` 表（幂等） | **无需回滚**（空表无害） |
| 2 | `candidate_quality:{tenant}` 配置写入 `runtime_config`（**已有 `config_versions` 留痕**） | 恢复配置值 |
| 3 | 候选层加过滤 + 门禁层读同源阈值 | **回滚代码** |

**回滚判据**：服务正常启动 + 候选数**不低于回滚前**（过滤误伤时能立刻发现）。

---

## 8. 验收（**11 条**：PRD 7 条 + 阈值校验 + 变异 + 退出管理不变量 + 300s 埋点）

1. 剩余到期 <1 天的合约 → **不进候选池**，`candidate_exclusions.reason = EXPIRED_T0`；
2. 报价停滞 >180s → **不进候选池**，`reason = QUOTE_STALE`，**且 `age_seconds` 记录实测值**；
3. **价差 5%–20% 的合约 → 不排除，只降权**（仍在候选池、排序末尾）——
   **这条同时验证「软降权必须真正实现」**（实现成硬排除即失败）；
4. 候选偏少时 → 监控页显示 **「候选 N / 硬排除 M / 软降权 K」+ 排除明细**；
5. **无候选时必须显示排除明细** —— 不能只显示"没机会"；
6. 阈值**按租户可配**（实盘严、模拟松，同一份配置）；
7. **阈值缺失 → fail-closed**（拒绝解析 + ERROR），**不静默用默认值**；
8. **`soft_spread_pct > entry_max_spread_pct` → 拒绝解析**（防"不可解释 BLOCK"）；
9. **变异测试**：把硬排除判据关掉 → 守卫**必须转红**；
10. 🔴 **退出管理不受影响**（Reviewer 阻断项）：构造"**持仓合约落在排除名单里**" →
    **仍能正常止盈/止损/平仓**，**且其 campaign 被保留为 `exit_only`**；
11. **300s 埋点**：`quote_age_at_submit` **可查**（含降级通过的那一笔），
    **P50/P95 可出** —— **一周后按数据复审**。
