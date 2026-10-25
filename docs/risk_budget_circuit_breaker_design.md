# 风险预算与回撤熔断 · 技术架构设计（草案 v0.1）

> 作者：Architect　日期：2026-10-25　分支：`docs/risk-budget-design`（worktree `wt-risk-budget-design`）
> 依据：用户裁定 **最大可接受回撤 10%**、敞口按**权利金全损口径**；Chief 定参 **单笔 1%≈$535 / 单日 2%≈$1,070**；两档熔断 **8% 停新开 / 10% 全停+人工复核**
> 状态：草案，待 Chief 签字；仓位公式的最终数字等 G3 成本归因数据

---

## 1. 不可协商的约束（先写死，后面所有设计不得违反）

1. **任何熔断等级都不得阻断退出路径**：止盈、硬止损、临期处置、保护性平仓（`side == SELL`）**一律放行**。这与已上线的 Reduce-Only 铁律同源（`execution.py` 只在 `side.upper()=="BUY"` 处拦）。
2. **熔断只停"开新仓"，不清仓、不强平存量**：8% 档让存量自然到期。
3. **10% 档"全停"的定义 = 停所有新开仓 + 冻结「纯开仓类任务」（见 §D5.1，绝不冻结止盈/止损/平仓任务）+ 必须人工复核解除**，**不等于清仓**。
4. **缺键 fail-closed**：风险预算配置缺失 → **拒绝开新仓 + 记 ERROR**，不回落更大值、不静默兜底。
5. **单源解析**：页面、Worker、ExecutionManager 三处必须调用同一个生产解析函数，禁止各自拼配置键。

---

## 2. 现状缺口（逐条实测，不是推测）

| # | 缺口 | 证据 |
|---|---|---|
| **N1（v2 重写，Reviewer R3 修正）** | **运行期已有「日内」回撤计算与硬停** —— 我初稿写"没有回撤计算"是**错的** | `risk.py:491-537`：`equity_since(ny_trading_day_start_utc())` 取当日快照 → `peak=max(snapshots+[assets])` → `drawdown=1-assets/peak` → `>= max_drawdown_pct` 直接返回 `RiskDecision(False)`。**真正缺的是「跨日 / 相对历史峰值」口径**（日内每天清零，累计亏损 10% 时日内可能从未触线） |
| **N2** | **`equity_snapshots` 没有 `tenant_id`**（只有 migration 6 加的 `env`） | `store.py:508-514` 建表 + `migrations.py:72-74` |
| **N3** | 已有 **4 套独立拦截**，再加一套就是"一个概念五份实现" | daily loss kill switch、`portfolio_stress`、对账锁（`recovery_blocked`）、`strategy_entries_paused` |
| **N4** | 实测最大回撤 **−9.55%**，距 10% 仅 0.45pp | Chief 报告（净值快照口径） |
| **N5** | **既有日内回撤硬停阈值当前是 5%（`RISK_MAX_DRAWDOWN_PCT=0.05`）**，比新熔断 8% 更早触发 | `.env` 实测 + `risk.py:65` 默认 `0.05` |

**N2 的架构裁定：不给 `equity_snapshots` 补 `tenant_id`。** 历史行没有租户归属，回填只能靠猜；而且该表按 `env` 分区、被多个租户共写。→ **新建独立状态表**（见 D1）。

**N1/N5 的架构裁定：不新增第 2 套回撤计算，且必须写清两个闸门的关系。**

| 闸门 | 口径 | 当前阈值 | 何时生效 | 本设计 |
|---|---|---|---|---|
| **既有「日内回撤」硬停** | `1 - assets/当日峰值`，**每天归零** | `max_drawdown_pct=0.05` | `risk.py:525` | **一字不改**（复用它，不重写） |
| **新「跨日累计」熔断** | `dd_since_reset` / `dd_vs_peak`，**跨交易日累计** | 8% / 10% + 绝对底线 | 新仲裁器 | 新增，**复用同一份回撤计算内核** |

- **必须复用同一份计算内核**：把 `risk.py:491-499` 的"取快照 + 算峰值 + 算回撤"抽成一个共享函数（`compute_equity_drawdowns()`），日内口径与新跨日口径**都从它取数**；**禁止在 `entry_gate.py` 里另写一遍取快照逻辑**（那就是 D3 自己明令禁止的第 5 套实现）。
- **一个必须让 👤@itsyi 知道的现实**：日内 5% 硬停**比新熔断 8% 更紧**，所以"8% 停新开"在**日内口径下几乎不会先触发**；新熔断的增量价值**只在跨日累计**（连续多日小亏、日内从未到 5%，但累计已 8~10%）。若不改 `max_drawdown_pct`，用户实际体感仍是"日内跌 5% 就停"。**这是参数层面的选择，本设计不动它**，但要写进 PRD 让用户知道两个数字并存。

---

## 3. 架构决策

### D1 新增租户级风控状态表 `risk_state`

```sql
CREATE TABLE IF NOT EXISTS risk_state (
    tenant_id   TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'equity',   -- 预留：equity / margin
    -- 两个基准各自的跟踪峰值（Reviewer C1：分母只涨不跌，回撤恒在 [0,1)）
    peak_hist            REAL NOT NULL,           -- 历史峰值，永不因重置变小
    peak_hist_at         TEXT NOT NULL,
    peak_hist_source     TEXT NOT NULL DEFAULT 'live',   -- live | backfill
    baseline_equity      REAL NOT NULL,           -- 重置基准（当前权益或 backfill）
    baseline_at          TEXT NOT NULL,
    baseline_source      TEXT NOT NULL DEFAULT 'live',   -- live | backfill | reset
    peak_reset           REAL NOT NULL,           -- 自重置以来的跟踪峰值，起点 = baseline_equity
    -- 熔断状态
    breaker_level   INTEGER NOT NULL DEFAULT 0,   -- 0 正常 / 1 = warn 档 / 2 = stop 档
    breaker_set_at  TEXT,
    breaker_reason  TEXT NOT NULL DEFAULT '',
    -- 2 档人工解除的锁存（§D2.3）
    ack_dd_vs_peak  REAL,                         -- NULL = 未锁存
    ack_at          TEXT,
    de_risk_scale   REAL NOT NULL DEFAULT 1.0,    -- 解除后 0.5，dd_since_reset<8% 后回 1.0
    reset_count_30d INTEGER NOT NULL DEFAULT 0,
    last_reset_at   TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope)
);
```
- 单行 upsert，key = `(tenant_id, scope)`；**不回填、不复用 `equity_snapshots`**；
- 只有持锁 Worker 写，页面只读；
- **建表幂等**（`IF NOT EXISTS` + 迁移版本号），且只在 04:00 后窗口落库（开盘期不建表）。

### D2 回撤口径与峰值更新规则（Reviewer C1 修正：分段用**各自的跟踪峰值**，不出现负回撤）

```
equity_t = 券商 total_assets（含未实现，与净值快照同口径）

# 每个基准各自维护一个"只涨不跌"的跟踪峰值，分母永远 >= 当前权益
peak_hist  = max(historical_peak, equity_t)      # 历史峰值，永不因重置变小
peak_reset = max(baseline_equity, peak_reset_prev, equity_t)   # 重置基线的跟踪峰值，起点 = baseline_equity

dd_vs_peak    = 1 - equity_t / peak_hist          # ∈ [0,1)，创新高时恒为 0，不会变负
dd_since_reset= 1 - equity_t / peak_reset         # ∈ [0,1)，创新高时恒为 0，不会变负

更新规则: 仅当「本轮采样成功」时才更新两个峰值（取大）；异常采样不改峰值、不参与判定
异常采样: 券商查询失败 / 返回 0 / 离群（|Δ|>40%）→ 记 ERROR，本轮跳过
```

> Reviewer C1 的原话是「`dd_since_reset` 在创新高时会变负，应除以 `max(baseline, 期间峰值)`」—— 这里采纳并推广为**两个基准各用各的跟踪峰值**，这样两个口径都恒在 `[0,1)`，也不会出现"创新高后回撤为负、把之前亏的抵掉"的诡异读数。

> 🔴 **peak 初始化是"假绿"高发点**：若首次启动把 peak 设成"当前权益"，那么当前已处于 −9.55% 的位置会被抹成 0% 回撤，熔断**永远不触发**（`peak_hist` 必须 backfill，见 D2.2）。

### D2.1 双基准并存（采纳 Product 的「重置基准」，但必须配一条不可重置的绝对底线）

Product 的判断成立：**当前已 −9.55%，若直接以历史峰值为基准，S2 上线第一天必然触发 8% 告警并停开仓**，会被误判为"上线即故障"。但**单纯重置为当前净值有一个后门：反复重置可以把亏损无限洗白**（每次把 9.5% 洗成 0，再按新增 8% 继续亏，累计可到 17%+）。所以必须**双基准并存，各自职责不同**：

| 口径 | 定义 | 用途 | 可否重置 |
|---|---|---|---|
| **`dd_since_reset`** | `1 - equity_t / peak_reset`（`peak_reset` 为自重置以来的跟踪峰值，起点 = `baseline_equity`） | 驱动 **8% / 10% 熔断档**（防"继续失血"） | 可，但受 §D2.2 治理 |
| **`dd_vs_peak`** | `1 - equity_t / peak_hist`（历史最高权益的跟踪峰值） | 驱动 **绝对底线**：`dd_vs_peak >= 10%` → **停新开仓 + 必须人工复核**，不自动解除 | **永不可重置** |

- **判定取并集**（任一触发即拦），但**原因分开记录**，页面**双口径同时展示**（Product 要求）；
- 用户裁定的「最大可接受回撤 10%」对应的是 **`dd_vs_peak >= 10%`** 这条绝对底线——这是真正不可越过的线；
- 8% 这个数字作为「新增回撤」的早期预警，含义是**自上次重置/上次峰值以来**再亏 8%。

**上线时的效果（正好避免误报）**：当前 `dd_vs_peak = 9.55%` < 10%（不触发绝对底线），`dd_since_reset = 0%`（不触发 8% 档）→ **S2 上线第一天不会告警、不会停开仓**，同时**从 9.55% 再跌到 10% 时会被绝对底线拦住**。

### D2.2 基准重置治理（防"越亏越能亏"）

- 重置**只允许两个触发点**：① S2/S3 首次启用；② 2 档熔断**人工复核解除**时；
- 必须有 `admin` 门禁 + 审计事件 `RISK_PEAK_BASELINE_RESET`（记录 old/new baseline、操作人、`dd_vs_peak` 快照）；
- **频率限制：30 天内最多 1 次**，超限 → 拒绝并要求更高授权（防止用重置当解套手段）；
- `risk_state` 增列：`baseline_equity`、`baseline_at`、`baseline_source` ∈ `backfill/reset/live`、`reset_count_30d`；
- 首次启用仍按 `backfill` 初始化 historical peak（近 30 天 `equity_snapshots` 最高值与当前权益取大），**再单独记录 baseline = 当前权益**。

### D2.3 2 档人工解除的锁存与重新武装（补 Product 指出的漏洞）

**漏洞**：`dd_vs_peak` 永不重置且当前已 9.55%。一旦触及 2 档并人工解除，**下一轮并集判定仍然成立 → 立刻再拦**，解除按钮形同虚设。
**另一个更隐蔽的漏洞（Product 未提，我核出来）**：人工解除时 `dd_since_reset` 通常也已 ≥10% > 8% ⇒ **1 档同样成立**。所以**只锁存绝对底线是不够的，解除必须三件事同时生效，缺一则解除无效**。

**人工解除（`RISK_BREAKER_MANUAL_RESET`）的原子语义 = 三件套**：

| # | 动作 | 作用 |
|---|---|---|
| 1 | `baseline_equity := 当前权益`（`baseline_source='reset'`） | 把 `dd_since_reset` 归零，**否则 1 档仍会拦**（受 §D2.2「30 天 ≤1 次」约束） |
| 2 | `ack_dd_vs_peak := 当前 dd_vs_peak`（**锁存**） | 让绝对底线不再用 10% 正交判定，改用 `max(10%, ack + 2pp)` |
| 3 | `de_risk_scale := 0.5`（预算减半，$535/$1,070 → $268/$535） | 解除后仍处深水区，只允许小额验证 |

**重新武装（re-arm）规则**：

```
if ack 存在:
    floor_effective = max(10%, ack_dd_vs_peak + 0.02)     # 2pp 间隔，防抖动反复拦
    if dd_vs_peak >= floor_effective: 再次进入 2 档
else:
    floor_effective = 10%

# ack 的清除（否则 10% 底线会永久失效 → 又变回"越亏越能亏"）
if dd_vs_peak 连续 1 个交易日 < 10%:
    clear ack; floor_effective 回到 10%
if dd_since_reset < 8%:  de_risk_scale 恢复 1.0
```

> **两个方向都必须防**：① 不锁存 → 解除无效（Product 指出）；② 只锁存不清除 → 10% 绝对底线被永久放宽（我的补充）。上面两条同时写死。
> `de_risk_scale` 是**参数**不是架构常量（默认 0.5，可配 `risk_budget_post_release_scale:{tenant_id}`）。**✅ 已拍板 2026-10-25（用户回复「都采纳」）**：三件套原子解除、`ack` 双向清除、解除后预算减半 $535/$1,070 → **$268/$535** 全部生效，`MANUAL_RESET` 仍 30 天 ≤1 次。

### D3 门禁仲裁器：不新增第 5 套拦截，改为统一出口

新增 `src/futu_quant/entry_gate.py`：

```python
def resolve_entry_block_reasons(store, broker, strategy_id, tenant_id) -> list[BlockReason]:
    """返回**全部**阻止开新仓的原因（空列表=放行）。ExecutionManager 只调这一个函数。"""
```
内部按固定顺序聚合现有 4 套 + 新增回撤熔断：
`对账锁 → 租户/账户急停 → 策略只减仓 → 回撤熔断(1/2档) → 日亏损 → 风险预算`。
- `ExecutionManager.submit_limit` 里 `side=="BUY"` 分支**只读这个函数的结果**，有多条原因时**全量落 audit**（不是只报第一条）；
- 好处：以后再加拦截点只改这个文件，`submit_limit` 的门禁顺序不再被反复改动（红区稳定性）。

### D4 生效值统一解析（**拆两层，不新建第 4 个限额源**）

> 🔴 Reviewer B1/B2 修正：初稿新建 `risk_max_order_notional:{tenant}` 是**第 4 个限额源**，且"默认 535"与"缺键 fail-closed"自相矛盾。改为 **复用** + **分层**。

**现有唯一入口**：`risk_limits.py:10-24` 三层链 `real_account_config:{account_id}.risk → risk_limits:{tenant_id} → env 兜底(记 WARN + 计数)`，语义已含「部分配置即拒绝」。本设计**不新增任何限额键**。

#### D4.1 限额（S1 改的就是这一层，写死）

**Product 核出的关键事实（必须写进设计，否则 S1 会改错层）**：
- **今天真正卡住单子的是「实盘门禁」**：`real_trading.check_real_trade` 的 `$2,500 / $5,000`，来自 **env**（`runtime_config` 无 `real_account_config` 键），REAL-only；
- **策略层风险限额**（`risk.py` → `risk_limits.py` 三层解析）当前 `RISK_MAX_ORDER_NOTIONAL=100000`，**值太松 → 从未真正生效**；
- **两者同名不同层、不同归属、不同数值**（`risk_limits.py:57-60` 已显式说明）。

**S1 落点（唯一正确解，字段名已按代码实证）**：
1. **改策略层**：`risk_limits:{tenant_id}` 的 `risk` 段。**配置键名不是 `max_order_notional`**（那是 `RiskLimits` 的**属性名**），配置键是 **`max_entry_notional` / `max_entry_daily_notional`**（`risk_limits.py:65-74` 的 `ACCOUNT_RISK_FIELDS`）。**写错名 → 被判 unknown → `present` 为空 → `return None` → 回落 env 兜底 → S1 静默不生效**（我实测验证过，见下方实证）；
2. **必须一次写满 10 个字段**（`_risk_section_to_limits` 铁律：部分配置即抛 `RiskLimitsConfigError`，`risk.py:283-286` 捕获后**直接拒绝开仓**）。S1 的完整 JSON（未标"改"的沿用当前 `.env` 值）：

```json
{"max_entry_notional": 535,            "max_entry_daily_notional": 1070,
 "max_reduction_order_notional": 150000, "max_daily_reduction_notional": 500000,
 "max_pending_orders": 20,              "max_symbol_weight": 0.25,
 "max_gross_exposure": 0.85,            "max_option_premium": 30000,
 "max_daily_loss": 10000,               "max_drawdown_pct": 0.05}
```
3. **实盘门禁 `$2,500/$5,000` 原样不动**，作为"单笔绝对上限兜底"；
4. **铁律：门禁 ≥ 预算**。配反了（预算 > 门禁）**必须告警**，**不允许门禁静默盖住预算**；
5. **不新建第 4 个来源键**（`risk_max_order_notional:{tenant}` 那套撤回），走现有三层链；
6. **UI 生效值必须来自 `resolve_risk_limits()` 解析结果**，不得页面自算/直读 env；
7. **不动 SELL 减仓额度**（$150,000/$500,000）。

> ✅ **实证（Reviewer 要求，我在生产 venv 直接跑 `_risk_section_to_limits`，未连库、未写任何东西）**：
> - 正确键名 JSON → `RiskLimits.max_order_notional=535.0, max_daily_notional=1070.0` ✅
> - **错误键名**（`{"max_order_notional":535,"max_daily_notional":1070}`）→ 返回 **`None`**，并 WARN `risk 段含无法识别的字段：['max_daily_notional','max_order_notional']` → **证实 Reviewer R1：写错名 = S1 静默不生效**；
> - 只写 1 个字段 → 抛 **`RiskLimitsConfigError`** → **证实 R2：必须写满 10 个**；
> - 当前 `.env` 实测：`RISK_MAX_ORDER_NOTIONAL=100000`、`RISK_MAX_DAILY_NOTIONAL=100000`、`RISK_MAX_DAILY_LOSS=10000`、`RISK_MAX_DRAWDOWN_PCT=0.05`（与 Product 的"策略层太松从未生效"一致）。

#### D4.2 熔断阈值（新键，fail-closed）

`risk_drawdown_warn_pct:{tenant_id}`（0.08）、`risk_drawdown_stop_pct:{tenant_id}`（0.10）、`risk_drawdown_resume_pct:{tenant_id}`（0.06）、`risk_drawdown_rearm_gap_pp:{tenant_id}`（0.02）、`risk_breaker_mode:{tenant_id}`（`off`）。

- **缺任一阈值键 → 拒绝开新仓 + ERROR**（不回落更大值）——**只针对阈值类**；
- 限额类沿用 `risk_limits` 既有语义（允许 env 兜底但**必须记 WARN + 计数**），**不再额外 fail-closed** → B2 矛盾消除；
- 链式校验：`0 < resume < warn < stop <= 1` 且 `rearm_gap_pp > 0`，违反 → ERROR + 拒绝解析。

### D5 熔断状态机（含滞回，防 flapping）

| 进入条件 | 等级 | 动作 | 解除方式 |
|---|---|---|---|
| `dd_since_reset >= warn` 或 `dd_vs_peak >= floor_effective` | 1/2 | 见下 | 见下 |
| 1 档 | 1 | 停该租户**新开仓** + 飞书告警 | `dd_since_reset <= resume(6%)` **自动**解除 |
| 2 档 | 2 | 停新开仓 + **冻结开仓类任务**（见 D5.1）+ 飞书 CRITICAL | **只能人工**解除（`RISK_BREAKER_MANUAL_RESET` 三件套，§D2.3） |

#### D5.1 「全停」的边界（Reviewer B4 修正）

**禁止冻结整个调度**——那会把止盈、硬止损、临期处置一起冻掉，等于把持仓扔下不管（META 教训）。2 档只冻结**开仓类任务白名单**：

- **冻结**：`volatility_new_entry` / `gamma_entry` / 现货开仓类任务（凡会调 `submit_limit(side=BUY)` 的任务）；
- **继续跑**：`close_*` / `exit_*` / 止盈止损巡检 / 对账 / 心跳 / 告警；
- 实现方式：**不按任务名硬编码黑名单**，而是冻结后由仲裁器在 `side=="BUY"` 处拦——**调度层只跳过"纯开仓任务"**，退出类任务一律照跑。

### D6 三阶段上线（与 Chief 分步一致）

| 阶段 | 内容 | 生效条件 |
|---|---|---|
| **S1** | 只改限额配置（$2,500→$535、$5,000→$1,070），**只对新开仓生效**，存量不动 | 全量回归绿 + Reviewer 签 |
| **S2** | 熔断 **shadow 模式**：只计算 + 记 `would_block` + 告警，**不真拦** | S1 观察 ≥ 2 周无异常 |
| **S3** | 打开硬拦 | S2 期间 `would_block` 无误报 + 用户确认 |

- 阶段开关：`risk_breaker_mode:{tenant_id}` ∈ `off / shadow / enforce`（默认 `off`，逐租户推进）；
- **shadow 与 enforce 走同一份判定代码**，只有最后一步是否真的返回 block 不同 —— 禁止两套实现。

---

## 4. 与既有机制的优先级与去重

| 机制 | 层级 | 是否阻断退出 | 与熔断关系 |
|---|---|---|---|
| 对账锁 `recovery_blocked` | 最高（数据不可信） | **现状：只拦 BUY（实测 `risk.py:297-326`）** | 熔断排在它之后（数据不对时先修数据）；**本设计不改它的语义**（见 V16） |
| 账户急停 / 租户暂停 | 账户级 | 否（租户暂停会停调度，需人工） | 独立 |
| 策略只减仓 `strategy_entries_paused` | 策略级 | 否 | 独立，原因并列展示 |
| **回撤熔断（本设计）** | 租户/账户级 | **否** | 新增，接入仲裁器 |
| 日亏损急停 | 日级 | 否 | 与回撤**互补**（日是当天，回撤是相对峰值），两条都保留 |

---

## 5. 风险与回滚

| 风险 | 处置 |
|---|---|
| **上线即触发**（当前 −9.55%，距 10% 仅 0.45pp） | 必须走 S1→S2→S3；S2 先只告警，确认不误报再硬拦 |
| peak 初始化错误 → 熔断永不触发（假绿） | §D2 规定 backfill 口径 + V2 守卫断言 |
| 熔断误伤退出路径 | 仲裁器只被 `side=="BUY"` 分支调用；V1 守卫：SELL 永不被任何 reason 拦 |
| 与 4 套既有拦截各自为政 | D3 统一仲裁器，`submit_limit` 只读一处 |
| 配置缺失导致静默放行 | D4 缺键 fail-closed + ERROR |
| **重置基准反复使用洗白亏损** | §D2.2：只有 2 个触发点 + admin 门禁 + `dd_vs_peak>=10%` 绝对底线不可重置 + 30 天最多 1 次 |
| 双基准被混读 | 页面与 audit **分两行展示**，字段名不共用（`dd_since_reset` / `dd_vs_peak`） |

**回滚**：`risk_state` 新表（可不删，保留只读）、`entry_gate.py` 新文件、限额配置回写原值；`risk_breaker_mode` 置 `off` 即恢复现行为，**不需要回滚代码**。

---

## 6. 验收（数字，可断言）

- **V1**：`side=="SELL"` 在 breaker_level=2 时**必须放行**（变异：让仲裁器作用于 SELL → 此用例必红）；
- **V2**：首次初始化时 `historical_peak` 取近 30 天快照最高值与当前权益的**较大者**（`peak_source='backfill'`），同时 `baseline_equity=当前权益`；**不得出现两个基准都=当前权益且都无来源标注**；
- **V3**：双基准——构造 `dd_vs_peak=9.6%` 且 `dd_since_reset=0%` → **不触发 8% 档、不停开仓**（上线首日口径）；再跌到 `dd_vs_peak=10.1%` → **触发绝对底线，停新开仓**（即使 `dd_since_reset` 仍 <8%）；
- **V4**：滞回——`dd_since_reset` 8.2% 触发 1 档后，回落到 7.0% **不解除**（resume=6%），落到 5.8% 才自动解除；
- **V5**：2 档**不被任何自动逻辑解除**，只有 `RISK_BREAKER_MANUAL_RESET` 审计存在时才解除；
- **V6**：重置治理——30 天内第 2 次重置请求被**拒绝**；每次重置必须落 `RISK_PEAK_BASELINE_RESET` 审计（含 old/new baseline 与 `dd_vs_peak` 快照）；**`historical_peak` 不受重置影响**（变异：重置时把 historical_peak 一并改写 → 用例必红）；
- **V7**：缺任一风险预算键 → BUY 被拒 + ERROR 落盘 + SELL 不受影响；
- **V8**：shadow 模式下 `would_block=True` 但**订单照常提交**（断言 intent 状态不是 REJECTED）；
- **V9**：仲裁器多原因时 audit 里**原因列表完整**（不是只落第一条）；
- **V10**：`0 < resume < warn < stop <= 1` 违反 → 拒绝解析 + ERROR（不采用非法值）。
- **V11**（锁存）人工解除后**第一轮判定必须放行**：构造 `dd_since_reset=10.5%`、`dd_vs_peak=11.2%` → 触发 2 档；执行 `RISK_BREAKER_MANUAL_RESET` 后 → **1 档与 2 档都不再触发**（三件套生效）。
- **V12**（三件套完整性）**只做其中一件 → 必须仍被拦**：① 只锁存 ack、不重置 baseline → 1 档仍拦（变异必红）；② 只重置 baseline、不锁存 ack → 2 档仍拦（变异必红）；③ 两者都做但 `de_risk_scale` 未生效 → 预算未缩半（必红）。
- **V13**（重新武装）`ack=11.2%` 时：`dd_vs_peak=12.9%` 不拦；`13.3%` **必须再拦**；且 `dd_vs_peak` 连续 1 个交易日 `<10%` 后 ack 被清除、`floor_effective` 回到 10%。
- **V14**（解除频率）30 天内第 2 次 `MANUAL_RESET` 被拒；`de_risk_scale` 在 `dd_since_reset < 8%` 后自动恢复 1.0。
- **V15**（B4 冻结边界）2 档冻结期间：**纯开仓任务被跳过**，但**止盈/硬止损/临期平仓任务必须照常执行**，且其 `side=="SELL"` 订单正常提交（变异：把冻结扩到全调度 → 此用例必红）。
- **V16**（C2 行为冻结）迁移到仲裁器**前**先钉现状：用例断言「对账锁当前只拦 BUY、不拦 SELL」（实测 `risk.py:297-326`）。若断言不成立则**先修描述再动手**；**本批不改变对账锁语义**（CLAUDE.md 写的 "locks SELL" 与实现不一致，登记为待澄清项，不在本批改行为）。
- **V17**（限额层单源）UI 显示的「单笔/单日限额」必须等于 `resolve_risk_limits()` 解析结果，且 **S1 生效后 BUY 按 535 被拦、SELL 不受影响**；变异：只改 `.env` 不改 `risk_limits:{tenant}` → 必须出现"界面 535、实际仍按旧值放行"的失败，从而证明该用例能抓到 B1 那类错层。
- **V18**（门禁 ≥ 预算）`risk_limits` 预算 > 实盘门禁时 → **必须告警**（不允许门禁静默盖住预算）；且 S1 后 `runtime_config` 键集合快照对比，**出现第 4 个限额键即变异必红**；SELL 减仓额度 $150,000/$500,000 不变。
- **V19**（R1 名字写错即静默不生效）用错误键名 `{"max_order_notional":535,...}` 写 `risk_limits:{tenant}` → 解析必须**落回 env 且 WARN**，并断言「**不得被当作已生效**」（界面必须显示来源层级 = env，不是 tenant）。变异：把配置键名写回 `max_order_notional` → 此用例必红。
- **V20**（R2 部分配置即拒单）只写 9/10 个字段 → `RiskLimitsConfigError` → BUY 被拒 + ERROR；SELL 不受影响。**写满 10 个**才放行。
- **V21**（N1/N5 双闸门不重复实现）日内回撤硬停与跨日熔断**共用同一个 `compute_equity_drawdowns()`**：变异——在 `entry_gate.py` 里另写一遍取快照逻辑 → 单源守卫必红；并断言日内 5% 触发时走**既有**分支（`risk.py:525`），不是新熔断分支（两者报文不同，可区分）。

---

## 7. 待补（不阻塞本草案签字）

1. 仓位公式的最终参数依赖 **G3 成本归因**——`$535/$1,070` 是先按 1%/2% 定的**上限**；
2. 🔴 **成本必须进仓位公式（Chief/Ops 定，硬要求）**：`单笔期望净收益 = 毛期望 − $2.44/笔`（$1,815.34 ÷ 743 笔，**全费用估算、非已扣款**）。
   **理由**：小仓位单子毛期望为正、扣完成本其实为负；不把成本写进公式，等于系统性地低估"该不该开这一单"。
   同时记录可控杠杆：当前 **24.8 笔/日**，`$60.5/日 ≈ 噪声 ±$200 的 30%`；**笔数减半 ≈ 省 $370/月（估算）**，且减少 reprice/撤单噪声 —— 这比调 ML 阈值确定得多。
   → 仓位公式中 `$2.44/笔` 必须作为**可配常量**（`fee_per_trade_estimate:{tenant_id}`），季度人工校准后更新（P1-16 巡检规范），**不得写死在代码里**；
3. `risk_state` 是否需要跨进程可见性（当前设计：每租户一个 Worker，单写者；若将来多 Worker 共享租户需加行锁）。
