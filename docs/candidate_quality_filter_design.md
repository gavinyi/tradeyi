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

**结构性保障（Reviewer 核出，实测）**：`volatility_roll.py:172-175` 在候选为空时
**原样返回原 campaign + diagnostics**：
```python
if candidates.empty or "volatility_eligible" not in candidates:
    return _with_diagnostics(campaign, candidates, regime, "基础流动性筛选后没有可评分的新合约")
```
→ **过滤到空 = 保留原 campaign = 退出管理照常**。**A0 不需要新代码，只要一条回归测试钉住它。**

**🔴 禁止把过滤器加到遍历层**（否则就是 META 的失效形态）：
- ❌ `load_volatility_campaigns`（campaign 加载/遍历）
- ❌ `reclaim_exit_only_orphans`（退出接管遍历）
- ❌ 止盈/止损遍历
- ✅ **只加在 `roll_campaign` 的候选构建段**（`:150`–`:161` 之间）

---

## 3. 阈值单源（Chief 硬要求 ①）

**问题**：Ops 建议"价差 >50% 排除"，而入场层是 **20%** —— 两套阈值会让 20%–50% 的合约
**"入选后被拦"**，产生**不可解释的 BLOCK**。

**设计**：**一个配置源，两层读同一个解析器**。

```
candidate_quality:{tenant_id}   （runtime_config）
  ├─ hard_stale_seconds         硬排除：报价停滞下限（默认 180）
  ├─ soft_spread_pct            软降权：价差阈值（默认 **实盘 5% / 模拟 10%**）
  ├─ soft_volume_min            软降权：成交量下限
  ├─ entry_max_spread_pct       入场层盘口上限（默认 20，与 data_quality 现值一致）
  ├─ hard_oi_min                既有硬筛接管：未平仓量下限（默认 500 = 现值，行为不变）
  ├─ hard_spread_pct            既有硬筛接管：价差硬筛（默认 12 = 现值，行为不变）
  └─ hard_moneyness_max         既有硬筛接管：moneyness 上限（默认 10 = 现值，行为不变）
```

> 🔴 **剩余到期（T-0）不在此配置里 —— 已有一道，复用即可**（Chief 裁定 ③）：
> `src/futu_quant/volatility_roll.py:129` 已有
> `min_dte = max(1, int(float(campaign.get("maturity_min_dte", 5) or 5)))`，
> **`max(1, ...)` 保证 DTE ≥ 1，T-0 与已到期已被排除**。
> **新加一道 = "一个概念两份实现"** —— **本设计只复用/收紧这一处**，
> 且**按 America/New_York 判定**（见 §6.1）。

> **`runtime_config` 的 actor 完整性依赖 P1-C（配置留痕批）** ——
> 本配置写 `runtime_config` 可拿到 `config_versions` 的 actor/reason，
> **但 `set_setting` 的 actor 化在 P1-C**；**在 P1-C 落地前，本配置的 actor 留痕不完整**。

> **价差阈值取 5%/10%（Product 已采纳 Chief 建议，非原建议的 8%/15%）**。
> **前提**：**必须真正实现为软降权** —— **若实现成硬排除，5% 会砍掉大量合约、
> 反而回到「没有候选」的老问题**。**这条进实现验收**（见 §8 验收 3）。

> 🔴 **既有字面量本批接管（Chief 最终裁定）**：`earnings_gamma.py:147` 传入的
> **`min_open_interest=500` / `max_spread_pct=12` / `max_moneyness_pct=10` 三项本批由
> `candidate_quality:{tenant}` 接管**，**默认值 = 现有字面量（500/12/10），行为不变**。
> **除这三项外，`screen_gamma_contracts` 其余逻辑本批不动**。
> **理由**：**若只把新的 5% 软线进配置、而 12% 硬线仍写死，就是"一个概念多份实现"**；
> **接管后链条可校验（见下方硬规则 1），否则租户可把硬筛设成 >入场层上限，重新制造不可解释 BLOCK**。

**硬规则**：
1. **链式校验（fail-closed）**：**`soft_spread_pct ≤ hard_spread_pct ≤ entry_max_spread_pct`**
   —— **候选层软线 ≤ 硬线 ≤ 入场层上限**，任一违反 → **拒绝解析 + ERROR**；
   否则会出现"候选层放行、入场层拦住"的**不可解释 BLOCK**（配置错误路径重新引入）；
2. **两层读同一个 `resolve_candidate_quality(store, tenant_id)`** —— **不各读各的**；
3. 参数**按租户可配**（实盘严、模拟松），走与 P0-7 同构的三层解析链；
4. 🔴 **租户必须显式传入，禁止从 broker 解析**（Chief 裁定 ④）：
   `roll_campaign(broker, campaign, store)`（`:93`）**当前没有租户参数** ——
   **新增显式参数 `tenant_id`，或从 `campaign["tenant_id"]` 取**（P0-9 后 campaign 已带）。
   **从 broker 反解是今晚定死的 fail-closed 红线**（`risk.py:105-112` 的三级反解），
   **此处不得复用** —— 租户上下文必须由调用方显式给出。

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
| **停滞判据需要 `update_time`** | ✅ **无需新增行情调用** —— `src/futu_quant/volatility_roll.py:145` **已有** `quotes = broker.snapshots(chain["code"].tolist())`（**一次取全部候选合约的批量调用**），插入点 `:150`–`:161` **正好在它之后** | **直接用这份 `quotes`** 判停滞/盘口缺失 |
| 每轮候选数 | 量级：每标的几十个合约 × 13 个标的 | 已被 `:145` 的批量调用覆盖，**O(1) 次网络往返** |
| 排除记录写入 | 每轮几十条 INSERT | 可接受（一行一条）；**监控指标见下** |
| 软降权计算 | 纯内存 | 无影响 |

> **修正**：原设计把"必须加一次批量 `snapshots`"列为**本设计唯一真正的性能风险点** ——
> **那是重复**。`:145` 的批量调用**已经存在**，过滤**直接用**即可。
> **本设计不新增任何行情调用**；**若确需新增，必须批量**（`broker.snapshots(list_of_codes)`），
> **严禁逐合约调用**。

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

## 6. 判据：**大部分已在盘上** —— 真正缺的只有三样（Reviewer 核出）

### 6.0 既有实现（`earnings_gamma.py:16-34` `screen_gamma_contracts`，调用点 `volatility_roll.py:147`）

```python
contracts = contracts[
    (contracts["bid_price"] > 0)
    & (contracts["ask_price"] > 0)
    & (contracts["option_open_interest"] >= min_open_interest)      # 500
    & (contracts["spread_pct"] <= max_spread_pct)                   # 12
    & (contracts["moneyness_pct"].abs() <= max_moneyness_pct)        # 10
    & (~contracts["suspension"])
].copy()
contracts["atm_distance"] = (contracts["strike_price"] - spot).abs()
return contracts.sort_values(["atm_distance", "spread_pct"]).reset_index(drop=True)
```

| P0-5 判据 | 是否已有 | 位置 |
| :--- | :---: | :--- |
| **盘口缺失**（bid/ask>0） | ✅ **已有** | `earnings_gamma.py:26-27` |
| **OI 下限**（≥500） | ✅ **已有** | `:28` |
| **价差硬筛**（≤12%） | ✅ **已有** | `:29` |
| **T-0 / 到期**（`min_dte ≥ 1`） | ✅ **已有** | `volatility_roll.py:129`（**且按 NY**，`:131 today = ny_today`） |
| **报价停滞 >180s** | ❌ **缺** | **Ops 的根因，现在没有任何一道在判** |
| **阈值按租户可配** | ❌ **缺** | 现写死 `500/12/10` + campaign 级 `maturity_*_dte` |
| **`exclusion_reason` 留痕** | ❌ **缺** | `screen_gamma_contracts` **静默丢弃行**，**无任何原因留痕** |

### 6.0.1 🔴 硬约束：**不能"只加不并"**（Reviewer 提出）

**若直接加新过滤 → 三道价差阈值并存**：既有 **12% 硬筛** + 新 **5% 软降权** + 入场层 **20%** ——
**正是"一个概念多份实现"**。

**必须写明三件事**：
1. **既有 12% 硬筛怎么处置** —— **保留为硬筛，但阈值由 `candidate_quality:{tenant}` 接管**
   （`hard_spread_pct`，默认 12 = 现值，行为不变）；
   **它与新 5% 软降权的关系**：**12% 是"能不能交易"的硬线，5% 是"值不值得优先"的软线** ——
   **两者不冲突，但必须在文档里分清**，否则实现者会以为要合并；**链式校验见 §3 硬规则 1**；
2. **软降权与既有主排序的关系** —— `:34` 是 `sort_values(["atm_distance", "spread_pct"])`，
   **价差只是 tiebreaker** → **"排末尾"现在无法判定**；
   **软降权必须进入主排序键**（如 `sort_values(["soft_demoted", "atm_distance", "spread_pct"])`），
   **否则 5% 阈值形同虚设**；
3. **`500/12/10` 本批由配置接管**（§3，默认值 = 现值，行为不变）—— **接管后不得再留字面量**。

### 6.0.2 真正要新增的三样

| # | 内容 | 落点 |
| :--- | :--- | :--- |
| **①** | **报价停滞 >180s → 硬排除**（用 `:145` 的 `quotes`，判 `update_time`） | 候选层，`screen_gamma_contracts` 之后 |
| **②** | **阈值租户化**：`500/12/10` + `maturity_*_dte` → `candidate_quality:{tenant}` | 配置层（§3） |
| **③** | **`exclusion_reason` 留痕 + 可见**（**现在静默丢行**） | 新表 `candidate_exclusions`（§4） |

> 🔴 **③ 才是"静默"的确切位置** —— **`screen_gamma_contracts` 现在把不达标的行直接丢掉，
> 不留任何原因**。**今晚所有事故的共同点就是静默，而这里已经有一处。**

### 6.1 🔴 那 65 次「合约已到期」**不属本需求** —— 且来源是一条真 bug

**来源（实测）**：`gamma_debug.py:47-48`
```python
if expiry < date.today():          # ← 本机日期（北京），不是 America/New_York
    issues.append(f"合约已于 {expiry} 到期")
```
- 走 **`run_gamma_strategy.py:449-454`** 判**已有 campaign**，命中即在**任何动作前 `return`**；
- 该字符串被 `strategy_review.py:192/:471`、`trade_diagnostics.py:213` 归类为 BLOCKED。

**所以**：
1. **这 65 次是「已有 campaign 的到期校验」，不是候选选择** —— **本设计的候选层过滤器消不掉它**；
2. **根因是 TZ bug**：`date.today()` 用**北京日期**，比 NY 快 12 小时 ——
   **实测现在 `local=2026-10-24 / NY=2026-10-23`** →
   **`US.COIN261023P170000`（NY 今日到期）被判「已到期」**；
3. **真实风险（⏳ 仅修 TZ 前成立）**：若持有当日到期合约，**北京 00:00 之后（ET 12:00–16:00，最高 gamma 时段）
   它的止盈/止损/退出会被这个 BLOCK 全部拦住**。今晚恰好 `qty=0`，**只损失了 65 次假 BLOCK，没伤到仓位**。

> 🔴 **时态修正（Chief 裁定）**：**TZ 修好之后，第 3 条的现象消失** ——
> 当日到期时 `expiry == NY today`，**不满足 `expiry < today`**，**不会被判已到期**。
> **所以"T-0 冻结"是「修 TZ 前」的现象，修完即消失**，**不得当作分家的理由**。

**处置**：**单列 P0 缺陷（不属 P0-5）** —— **四处 + 一处分家**，**逐处修法写死**：

| 站点 | 修法（关键词） | 理由 |
| :--- | :--- | :--- |
| **`gamma_debug.py:47`** | `date.today()` → **America/New_York** | **65 次假 BLOCK 直接来源**；`diagnose_campaign` 对**波动率 campaign 同样生效** |
| **`option_ranking.py`** | 🔴 **在 `volatility_roll.py:150` 调用处传 `as_of=ny_today`，不改 `option_ranking.py:58` 兜底默认** | **`:58` 兜底默认影响所有调用方，影响面不可控**；`:150` 传参只影响 roll 路径。**`rank_long_option_candidates` 已接受 `as_of`**（`:58` 在用），**`volatility_roll.py:99/:131` 已有 `ny_today`**。**这条决定候选 DTE / probability / 排序正确性** —— `:72` 的 `dte` 直接喂给 `:73 expiry_profit_probability(...)` |
| **`run_volatility_strategy.py:141`** | `datetime.now().astimezone().date()` → **NY 日期** | 🔴 **平仓路径** —— 本地日期会让 **NY 当日到期 combo 提前 ≤4h 被平仓**（与 65 次同病、相反方向） |
| **`volatility_optimizer.py:932`** | 兜底默认改 NY（**非必须** —— 调用处已传 NY） | 避免下次再漂 |
| **`hard_campaign_issues` return 分家** | 命中 hard issue 时**不得阻断退出动作**（止盈/止损/平仓），只拦新开仓 | 见 §6.1.1 |

**守卫测试**：
1. mock「北京 00:30 / NY 前一日」→ T-0 合约**不得**进 `hard_issues`；
2. mock 非到期类 hard issue（如"财报时间未人工确认"）+ 有持仓 → **退出动作不被阻断**；
3. **`option_ranking` 的判据是"排序不变"，不只是"日期正确"** ——
   构造一个 DTE 在北京/纽约日期下差 1 天的合约 → 断言传入 `as_of=ny_today` 后其
   **probability / rank 与预期一致**（跨日期边界排序稳定）；
4. **return 分家验收：一条测试两个方向** —— **同一 hard issue 下 → 开仓被拦 + 平仓不被拦**
   （**只测"平仓能走"会漏掉"把门禁整没了"**）。

> **本设计的 T-0 验收口径因此改为「**按 NY 判定**」，不是"新增一道排除"。**

#### 6.1.1 🔴 修时区 bug 时必须同时审 `hard_campaign_issues` 的 return 范围（Reviewer 提出）

**实测**（`run_gamma_strategy.py:448-454`）：
```python
issues = diagnose_campaign(campaign, executor.store.earnings_events())
hard_issues = hard_campaign_issues(campaign, issues)
if hard_issues:
    reason = "；".join(hard_issues)
    executor.store.decision(strategy_id, code, "BLOCKED", reason)
    return          # ← 拦住**全部**后续动作
```

**`return` 在函数早期** —— **止盈/止损/退出逻辑全在它之后**。
所以**一个"诊断问题"会连带冻结退出管理**。

**这与 P0-5 的 A0 不变量是同一形态**（§2.5），**只不过它是既有的真实缺陷**：
- **A0**：新过滤器若写成"跳过 symbol" → 已持仓失去退出管理；
- **6.1.1**：**已存在的** `hard_campaign_issues` → **诊断出问题就冻结全部动作（含平仓）**。

**裁定**：修 `date.today()` 时**必须同时**做 **`hard_campaign_issues` 的 return 范围分家** ——
**判据形如 `if hard_issues and not is_exit_action: return`**。

**🔴 分家的真实理由（Chief 修正）**：**不是 T-0**（TZ 修好后 T-0 冻结就消失了），
而是**其它 hard issue**：
- `财报时间未人工确认`、`期权代码无法识别`、`早于财报日`、`财报日期格式无效`
  （`gamma_debug.py:66,70` 的 hard markers）——
  **这些诊断命中时同样会在任何动作前 `return`，把已持仓的止盈/止损/平仓一起冻结**；
- **且以后每新增一条诊断，都会自动落进这个冻结范围** —— **分家是对未来诊断的长期保护**。

**否则**：修掉时区 bug 之后，一个"财报时间未确认"的诊断仍能在高点冻结退出管理（META 的失效模式）。

**守卫测试两条**：
1. mock「北京 00:30 / NY 前一日」→ T-0 合约**不得**进 `hard_issues`；
2. **mock 一个非到期类 hard issue（如"财报时间未人工确认"）+ 有持仓** →
   **退出动作不被阻断**（**这条比"真实已到期合约仍可平仓"可测**）。

**这条并入那个 P0 时区缺陷批**（同一处代码、同一次改动）。

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

### 8.0 🔴 前置条件：验收环境必须可证「跑的是哪份代码」（阻断级）

**本节全部验收（尤其 A0 / D 两条结构性测试）必须先满足此条件，否则验收无效。**

**机制**：在 `tests/conftest.py` 的会话守卫里加一条**代码来源断言**（与既有生产库指纹守卫同处）：

```python
import futu_quant
from pathlib import Path

def _assert_code_origin() -> None:
    worktree_root = Path(__file__).resolve().parents[1]     # 本 worktree 根
    module_path = Path(futu_quant.__file__).resolve()
    if worktree_root not in module_path.parents:
        raise AssertionError(
            f"跑错代码：futu_quant 解析到 {module_path}，"
            f"不在本 worktree {worktree_root} 下 —— 验收无效"
        )
```

**为什么这条能抓住陷阱**：
- **正确配置**（`--no-install-project` + pytest `pythonpath=["src"]`）→
  `futu_quant.__file__` = `<本worktree>/src/futu_quant/__init__.py` → **通过**；
- **共享 editable venv** → `__editable__.futu_paper_quant-0.1.0.pth` 指向
  **`E:\Thisyi\trade\src`** → **断言失败，验收当场变红**。

**硬规则**：
1. **跑测试前先断言 `futu_quant.__file__` 属于本 worktree** —— 不满足则**整轮验收无效**；
2. **变异测试尤其要在正确环境里做** —— **否则「把过滤器关掉 → 守卫转红」可能跑的是生产代码，给出假绿**；
3. **该守卫与生产库指纹守卫同批落地**（都在 `tests/conftest.py` 会话守卫里）；
4. 🔴 **seed 脚本自身必须带"不许写生产"守卫**（与上面合并为**一个环境守卫**）：
   断言 **CWD 不是生产根目录**、**DB 路径落在本 worktree 内**。
   `TradingStore()` 的默认路径是 **CWD 相对的 `data/trading.db`** ——
   **从生产目录跑一次 seed，写的就是生产库**（与今天那次事故同一形态的高频操作）。
   **未带守卫的 seed 脚本不得执行**（见 §8.1）。

> **一句话**：**验收如果跑错代码，等于给自己发合格证** —— 今晚已经反复栽在这上面。
> **§8.0 防"跑错代码"，§8.1 防"根本没跑起来"，§8.0-4 防"写穿生产"** —— **三者都是前置。**

### 8.1 🔴 前置条件：手动冒烟测试前必须 seed 最小数据集

**问题**：**worktree 的 `data/` 是空的**（gitignored）—— **pytest 用 tmp DB 无影响，
但手动冒烟测试没有 DB / 配置，跑不起来**。

**后果**：**不 seed 的话，「验收通过」可能只是「没跑起来」** —— **又一种"合格证"**。

**要求**：
1. **脚本随代码版本化**：`scripts/seed_test_env.py`（含 campaign / 候选合约 / 配置键）；
2. **写明 seed 来源与内容**（哪些 campaign、哪些配置键、是否脱敏）；
3. **验收前必须 seed** —— **未 seed 的冒烟测试不计入验收**；
4. 🔴 **seed 脚本自身必须带"不许写生产"守卫**（Reviewer 提出）：
   断言 **CWD 不是生产根目录**、**DB 路径落在本 worktree 内**。
   `TradingStore()` 的默认路径是 **CWD 相对的 `data/trading.db`** ——
   **从生产目录跑一次 seed，写的就是生产库**（与今天那次事故同一形态的高频操作）。
   **与 §8.0 的 `futu_quant.__file__` 守卫合并成"一个环境守卫"**（同时断言代码来源 + DB 路径）。

> **与 §8.0 并列**：**§8.0 防"跑错代码"，§8.1 防"根本没跑起来"，§8.1-4 防"写穿生产"** ——
> **三者都是"验收无效 / 事故"的前置。**

1. 剩余到期（T-0）**按 America/New_York 判定** —— **复用 `volatility_roll.py:129` 的 `min_dte`**，
   **不新增排除**；**且该处按 NY 重算后不得把"NY 当日到期"误判为已到期**；
2. 报价停滞 >180s → **不进候选池**，`reason = QUOTE_STALE`，**且 `age_seconds` 记录实测值**；
   **🔴 注入点（阻断级，必须指定，否则本条不可实现）**：
   `volatility_roll.py:145` 的 `quotes = broker.snapshots(...)` 是**实时行情** ——
   **真实券商构造不出"1250 秒前"的报价**。
   **指定注入方式**：
   - **pytest**：fixture `stub_quotes_with_age(monkeypatch, age_seconds)`，
     **patch 目标 `volatility_roll.broker.snapshots`**（monkeypatch 替换 `broker` 的 `snapshots` 方法），
     返回带**可控 `update_time`** 的 DataFrame（`update_time = now - age_seconds`）；
     断言被排除且 `age_seconds ≈ 1250`；
   - **手动冒烟**：用 **seed**（`scripts/seed_test_env.py`，见 §8.1）。
   **不指定注入点，本条就是"写出来但跑不了"的条目 —— 而它在报告里和"假绿"长得一模一样。**
3. **价差 5%–20% 的合约 → 不排除，只降权**（仍在候选池、排序末尾）——
   **这条同时验证「软降权必须真正实现」**（实现成硬排除即失败）；
4. 候选偏少时 → 监控页显示 **「候选 N / 硬排除 M / 软降权 K」+ 排除明细**；
5. **无候选时必须显示排除明细** —— 不能只显示"没机会"；
6. 阈值**按租户可配**（实盘严、模拟松，同一份配置）；
7. **阈值缺失 → fail-closed**（拒绝解析 + ERROR），**不静默用默认值**；
8. **链式校验**：`soft_spread_pct ≤ hard_spread_pct ≤ entry_max_spread_pct`，**任一违反 → 拒绝解析**（防"不可解释 BLOCK"）；
9. **变异测试**：把硬排除判据关掉 → 守卫**必须转红**；
10. 🔴 **退出管理不受影响**（Reviewer 阻断项）：构造"**持仓合约落在排除名单里**" →
    **仍能正常止盈/止损/平仓**，**且其 campaign 被保留为 `exit_only`**；
11. **300s 埋点**：`quote_age_at_submit` **可查**（含降级通过的那一笔），
    **P50/P95 可出** —— **一周后按数据复审**。
