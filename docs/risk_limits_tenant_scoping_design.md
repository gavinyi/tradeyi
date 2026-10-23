# 风险与资金参数租户化设计（P0-7）

> 状态：设计已定稿，待实现
> 关联：`docs/volatility_campaigns_tenant_partition_design.md`、`docs/dual_tenant_architecture_tech_spec.md`
> 背景事件：2026-10-22 夜间，模拟账户 `20170229` 当日亏 $10,351.89，撞上**全局** $10,000 急停线
> 被自动急停。用一个全局数字去管两个资金量完全不同的账户，必然一边太松、一边太紧。

## 1. 问题

`RiskLimits`（`risk.py:33-63`）有 **10 个资金/亏损参数**，**全部从 `os.getenv` 读，一个进程一套，所有租户共用**：

```
max_order_notional / max_daily_notional / max_reduction_order_notional /
max_daily_reduction_notional / max_pending_orders / max_symbol_weight /
max_gross_exposure / max_option_premium / max_daily_loss / max_drawdown_pct
```

其中 `max_daily_loss`（`risk.py:61`）是**急停线**，在 `risk.py:250-252` 使用：
`daily_pl <= -max_daily_loss` → `set_kill_switch(True, account_id=broker_acc)`。
**急停动作本身已是账户级的，但阈值是全局的** —— 这正是误杀的根因。

对比：`real_account_config:{account_id}` 已有单笔/单日额度，`strategy_enabled:{tenant}:...`
也已租户化。**风险参数是唯一还整体停留在全局的大块**。

## 2. 参数分类（技术分类；最终口径由 Product 定）

| 类别 | 参数 | 理由 |
| :--- | :--- | :--- |
| **必须按账户** | **`max_entry_notional`**、**`max_entry_daily_notional`**、**`max_daily_loss`**、`max_option_premium`、`max_symbol_weight`、`max_gross_exposure`、`max_drawdown_pct`、`max_reduction_order_notional`、`max_daily_reduction_notional`、`max_pending_orders` | 随**账户资金量**缩放；按账户才对 |
| **天然全局** | `correlation_clusters`（市场结构）、期权乘数 100、`OPTION_PATTERN`、合约解析规则、冷却期等市场微观结构常量 | 与账户无关；按账户反而错 |

> `correlation_clusters` 现已是 store 设置（`risk.py:241`），属"天然全局"一类，**保持全局**。

### 2.1 字段改名裁定（Chief，2026-10-23）

**`risk` 段的 `max_order_notional` / `max_daily_notional` 改名为
`max_entry_notional` / `max_entry_daily_notional`**，其余 8 个字段名与 `RiskLimits` 保持一致。

**理由**：这两个名字与 `real_account_config:{account_id}` **顶层**的
`max_order_notional` / `max_daily_notional` **同名不同义** ——
顶层那两个是**实盘门禁**（REAL 专用，由 `real_trading.check_real_trade` 读取，当前值 2500/5000），
`risk` 段这两个是**风控引擎**（全模式，由 `RiskEngine` 读取）。

| 位置 | 键 | 归属 | 谁读 |
| :--- | :--- | :--- | :--- |
| 顶层 | `max_order_notional` / `max_daily_notional` | **实盘门禁**（REAL 专用） | `real_trading.check_real_trade` |
| `risk` 段 | `max_entry_notional` / `max_entry_daily_notional` | **风控引擎**（全模式） | `RiskEngine` |

**改名时机**：`--apply` **尚未执行**，此时改名**零迁移成本**；顶层键是活的，改名才需要迁移。
"开仓额度"也比"名义额度"更准确。

> **Reviewer 审查口径**：**两个门禁、两套参数、一张显式映射表** ——
> 映射表必须让"哪个键归哪个门禁"一目了然，不允许同名不同义。

## 3. 三层解析链（单一入口，fail-closed）

新增 `resolve_risk_limits(store, broker) -> RiskLimits`，优先级**高 → 低**：

```
① real_account_config:{account_id} 的 risk 段      ← 账户级，最具体
② risk_limits:{tenant_id}                          ← 租户级
③ env 全局默认                                      ← 仅兜底
```

### 3.1 判定规则（核心）

- 只要账户级存在**任一**资金/亏损类参数，就必须**齐全**；缺任一项 → **拒绝开仓 + ERROR 审计**，
  **不得用 env 静默补齐**。
- 只有账户级**完全没有** risk 段时才回落 env，且**记 WARN 审计**（迁移前状态）。
- **不得静默回落到不同数值**：账户级部分配置时，缺的那项**不许**用 env 的值顶上。

> 这条"部分配置即拒绝"是本设计的中枢。否则会出现"账户配了 `max_daily_loss` 却没配
> `max_order_notional` → 后者静默回落 env" —— 与 P1-3 为止损限额定下的规矩同源，
> 这里推广到全部 **10 个**参数。
>
> 计数口径：`RiskLimits` 共 **10** 个字段；`correlation_clusters` **不是** `RiskLimits` 的字段，
> 属"天然全局"（见 §2），**不计入**。文档与代码差一个数，就是下一次误会的起点。

## 4. 下单前校验与缓存纪律

- `RiskEngine` 仍**构造时注入** limits（保持现有接口，测试替身不受影响）；但**解析动作**
  只有一个入口：`resolve_risk_limits`。
- `execution.submit_limit` 在风控门前，用**目标 broker 的账户**解析一次再传入。
- **缓存键必须含 (账户, 交易日)**；跨账户或跨日**必须重解析**。严禁进程级长缓存 ——
  否则 A 账户的 limits 会被 B 账户的下单读到。

### 4.1 调用点清单（Reviewer 硬审查项）

至少三处必须都走 `resolve_risk_limits`：

1. `RiskEngine` 的构造注入点；
2. `execution.submit_limit` 的风控门；
3. `real_trading.check_real_trade`。

> 教训：2026-10-22 已栽过一次"抽了共享护栏、雷达那条路径漏调"。同一个坑不能踩第二次。

## 5. 可观测性

- 账户级 risk 段全缺 → 回落 env **记 WARN**；
- **"回落发生了几次"必须能在界面上看见**（计数器或审计列表）。
  **静默回落就是今晚一切问题的母题。**
- 账户参数在界面上被改动后，运行中的进程必须**立即重解析**，不等下次重启
  （缓存以 `updated_at` 或显式失效信号驱动）。

## 6. 迁移

- `real_account_config:{account_id}` 增加 `risk` 段（JSON 对象），把当前 env 里的**账户相关项**
  显式写进去。
- env 的 `RISK_*` **保留为兜底默认**，但不再是唯一来源。
- 脚本 `scripts/migrate_risk_limits_to_accounts.py`：**默认 dry-run**，把当前 env 值写进每个
  `is_active=True` 的账户，**显式化**（消除"靠 env 隐式生效"）。
- 收益：迁移后模拟账户与实盘账户**各自显式一套**参数 —— 这是今晚误杀的根治。

## 7. 红线

1. **fail-closed**：资金/亏损类参数缺失 → 拒绝开仓 + ERROR 审计。
2. **不得静默回落到不同数值**。
3. **解析失败抛异常**，不返回默认值继续下单。
4. **缓存键含账户 + 交易日**。
5. **天然全局参数不强行租户化**（过度分区反而制造不一致）。

## 8. 验收（交给 Tester）

1. 账户 A 配 `max_daily_loss=100`、账户 B 未配 → A 按 100 触发急停，B 按自己的（或 fail-closed）；
2. 账户级 risk 段**缺一项** → 拒绝开仓 + ERROR 审计（不静默用 env）；
3. **跨账户不串用**（缓存键含账户）；
4. **跨日重解析**（次日生效）；
5. 迁移 dry-run 输出两账户各自参数集；`--apply` 后 env 值不变（仍作兜底）；
6. 三处调用点均走 `resolve_risk_limits`（静态守卫）。

## 9. 落地顺序

按 Chief 排期：**A（`fill_ledger` 双计）→ B/D（清单与开关租户化）→ P0-7 → P1/P2**。
**恢复实盘前 P0-7 必须完成。**

## 10. 纸面账户的急停语义：告警线与硬停线是**两个字段**（2026-10-23，B 方案）

### 10.1 事故与根因

2026-10-22 夜，模拟账户 `20170229` 当日亏 **$10,351.89**，撞上**全局**
`RISK_MAX_DAILY_LOSS=10000` 被自动急停。**根因不是数值太小，而是语义错了**：

> 自动急停是**保护真金**的机制。用在纸面账户上，它**中断的是正在进行的策略验证** ——
> 而模拟盘正是验证修复的地方，它被误杀，验证就断了。

### 10.2 B 方案：两个字段，各管一件事（Chief 10-23 裁定，Product 确认）

| 字段 | 纸面值 | 语义 | 作用域 |
| :--- | :--- | :--- | :--- |
| `risk_limits:{tenant}.max_daily_loss` | **$100,000** | **硬停线，拦单**（与实盘同一语义） | 全账户统一 |
| `paper_daily_loss_warn[:{tenant}]` | **$25,000** | **告警线，只告警不拦单** | **仅纸面账户** |

> **作用域铁律（Product 10-23）**：`paper_daily_loss_warn` **仅对 `trading_env=SIMULATE`
> 的租户生效，禁止配到实盘账户上**。真金账户的亏损上限只有 `max_daily_loss` 一条，
> 不存在"只告警"的口径 —— 真金账户的每一条亏损线都必须拦单。
> （代码里 `risk.py` 也只在 `_paper_warn_only()` 内读它，而那个函数第一步就问
> `_is_paper_account()`；实盘账户永远走不到这个键。）

**B 方案的要点**：`max_daily_loss` 的既有急停逻辑**一行不动**，只在它前面加一条
「纸面账户过告警线 → 发 WARNING → 继续放行」的旁路。
**风控引擎里不得出现"这个数字是告警还是硬停"的语义分叉** —— 告警线有自己的键，
硬停线就是 `max_daily_loss`，两者互不复用。

> **为什么不能复用 `max_daily_loss` 当告警线**：同一个数字承载两种语义（纸面=只告警、
> 实盘=真急停）时，"这个数字是告警还是硬停"必然在某处被分叉，而分叉的那一处就是
> 下一次事故的地方。分两个键，语义写在键名上，不需要任何分支去解释。

**为什么硬停线仍必需**：纸面账户若只有告警、完全没有硬停，策略失控时可以无限亏下去 ——
假钱虽假，但**污染验证数据、还会打出海量废单**。即「**放宽但不失控**」。

### 10.3 落点

- 实现：`src/futu_quant/risk.py`
  - `_paper_warn_line()` —— 读 `paper_daily_loss_warn`（租户键优先、无前缀全局键兜底，
    走共享入口 `tenant.tenant_config_key_candidates`，不手拼）；**没配就不旁路**
    （fail-closed：宁可少一条告警，也不能因为读不到配置把急停整个放过）。
  - `_paper_warn_only()` —— **告警带**旁路，**每个分支各用各的阈值、量纲必须对得上**：
    | 分支 | `metric_value` | 告警阈值 | 硬停上限 |
    | :--- | :--- | :--- | :--- |
    | 当日亏损 | 美元额 | `paper_daily_loss_warn`（$25,000） | `max_daily_loss`（$100,000） |
    | 日内回撤 | **比例**（`metric_is_ratio=True`） | `max_drawdown_pct`（10%） | `max_daily_loss` 折美元 |
    「日内权益损失」分支**不旁路**（它本质是回撤的一种表达，若也走日亏损线，等于从侧门对
    3% 的回撤发告警，与"回撤 10% 才告警"自相矛盾）。
  - `_loss_guard()` —— **只有"硬停"一种语义**，实盘与纸面共用：写急停 + 拦单；
    文案按 `metric_label` 定量纲（"当日亏损" / "日内权益损失金额" / "日内回撤金额"），
    **不一律写"当日亏损"**。告警事件名按账户性质区分（实盘 `DAILY_LOSS_KILL_SWITCH`，
    纸面 `PAPER_DAILY_LOSS_HARD_STOP`）。
  - 三处触发点：当日亏损与日内回撤各是 `if _paper_warn_only(...): pass  # 纸面只告警`
    `elif <既有硬停条件>: <既有逻辑>`；**实盘的硬停条件一字未改**。
  - 运行事件载荷是**量纲感知**的：`metric` / `metric_value`（回撤为比例）/
    `metric_amount_usd` / `warn_line` / `hard_stop_line` / `assets`。
- **一个数值上的事实**（由 Product 的口径推出，非缺陷）：纸面账户净值 ≈ $1M，
  `max_drawdown_pct=10%` 与 `max_daily_loss=$100,000` **同时落在 10%**，
  所以纸面的**回撤告警带是空的** —— 回撤路径对纸面只会"过硬停线即拦单"，不会先发 WARN。
  若将来纸面净值或两条线的数值变了，告警带会自然出现。
- 告警：`PAPER_DAILY_LOSS_WARN` 文案**带账户 + 该分支实际数值 + 触发线**（Product 要求）；
  金额进文案后 900s 精确去重不再跨金额生效，但本告警只在 **BUY 下单检查**时触发，量级有界。
- 判定纸面账户走共享入口 `broker_scope`（`SIMULATE`），**不新写第二套租户判定**；
  拿不到 `trading_env` 时**不旁路**（按更严的一侧处理）。
- 验收守卫：`tests/test_paper_kill_switch_warns_not_halts.py`
  （含 Product 指定场景：当日亏 $12,000 —— 超旧线、低于告警线 —— 断言**不被急停**；
  以及 $26,000 —— 过告警线、未过硬停线 —— 断言**只告警不拦单**）。

### 10.4 未落地的那一半（提醒）

本条只改**语义**。数值仍由 `risk_limits:{tenant}` 承载；迁移脚本需按上表把纸面租户的
`max_daily_loss` 落成 **$100,000**（硬停线），并新增 `paper_daily_loss_warn:{tenant}=25000`。
**三件同批**（代码 / 守卫 / 数值），缺一件就是"数值已改、语义未改"的半状态。


