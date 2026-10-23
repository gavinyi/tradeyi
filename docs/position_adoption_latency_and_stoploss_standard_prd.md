# 持仓接管时延与止损口径标准化 产品需求规格说明书 (PRD)
> **文档版本**: 1.0 (Production PRD)  
> **负责人**: Product (产品经理)  
> **制定日期**: 2026-10-22  
> **触发事件**: `US.META261030P660000` 实盘开仓后 **10 分 18 秒**才进入止损管理，期间无任何保护  
> **适用范围**: Tradeyi 量化交易系统（持仓接管时效、止损口径、告警触达）

---

## 一、 事件复盘（数据库实查证据）

| 时间（中国） | 事实 | 证据 |
| :--- | :--- | :--- |
| **21:39:02** | 挂单 BUY 限价 `$21.40` | `order_intents` id=862 |
| **21:39:21** | 成交 @ `$20.76`，1 张，券商单 `FH1D53D24E3F87D000` | 同上，`filled_quantity=1` |
| **21:39:22** | 系统自检告警 `OPTION_LIFECYCLE_RISK`（价内、交割缺口 100 股）**未推送** | `alerts` 表 `delivered=0` |
| **21:49:39** | 🔴 孤儿检测器才发现该持仓**未被接管**，ADOPT 为 `exit_only` campaign | `audit_events` payload：`campaign_id=orphan:US.META261030P660000`，`orphan_adopted_at=2026-10-22T13:49:39Z`，当时 `pnl_ratio=+5.97%` |
| **22:01:18** | 硬止损触发平仓，亏损 `$366` ≥ `$300` | `order_intents` id=866 |

**关键事实**：该笔入场合约 `US.META261030P660000` **不是雷达原始信号选出的**，而是由**自动滚动逻辑**选出——campaign JSON 中 `_roll_diagnostics.message` = 「**已自动滚动到 US.META261030P660000（单腿买方期权）；强波动按日内方向选择 PUT**」。

---

## 二、 根因分析（产品视角）

### 1. 接管是「轮询发现」，不是「成交即登记」（核心缺陷）
- 孤儿检测器 `manage_volatility_orphans()` 在**每个策略周期开头**运行（`run_volatility_strategy.py:154`），将券商持仓与**当周期加载的 campaign 列表**比对；
- 成交推送（`order_push.py`）只更新本地账本，**不会把新成交登记进任何 campaign**；
- 因此新成交仓位与 campaign 归属之间**天然存在一个窗口**，窗口长度 = 检测器下一次运行的时刻。

### 1b. 🔴 结构性根因：真金成交根本不进推送（实测，Architect 核出）
- `order_push.py:45`：`if ... trd_env ... == "SIMULATE"` —— **REAL 环境的成交在这一行被直接过滤**，`record_fill` 从不执行（heartbeat 文案也写着「模拟盘订单推送」）；
- 后果：**所有真金成交只能靠轮询兜底**（`executor.reconcile()` / `order_reconciliation` 任务），延迟是**轮询周期级**；
- 这不是「优化项」——**意味着实盘侧的成交登记一直是「有窗口」的**，历史上每一次实盘开仓都存在无保护窗口。

### 2. 自动滚动会「打断」持仓归属（放大器）
- `should_replace_campaign` + `roll_campaign` 会用新合约**替换**原 campaign（`upsert_campaign("VOLATILITY_CAMPAIGNS", rolled)`）；
- 若滚动发生在成交之后，被替换掉的 campaign 所对应的**已成交仓位会瞬间失去归属** → 变成孤儿；
- **两条滚动路径护栏不一致**：`run_volatility_strategy.py:397-412` 的滚动**有**护栏（旧合约若有券商持仓/挂单 → 保留为 `exit_only`），但**雷达的滚动**（`volatility_scanner.py:360-366`）直接 `upsert_campaign(rolled)`，**没有这道护栏**；
- 雷达每 300–900 秒滚一次，可把已成交合约顶出 campaign 列表 → 仓位瞬间无归属 → 止损评估（只遍历 `campaigns`）看不见它；
- 本例中入场合约正是由滚动选出（`_roll_diagnostics.message` = 「已自动滚动到 US.META261030P660000」），滚动与成交的时序交错直接制造了 10 分钟无保护窗口。

### 3. 止损是「周期评估」，不是「实时触发」
- 止损按策略周期（`VOLATILITY_INTERVAL_SECONDS`，默认 30 秒）评估；
- **仓位不在 campaign 内 → 完全不参与评估**。本例 21:39:21→21:49:39 期间，该仓位对止损系统**不可见**。

### 3b. 🔴 决定性根因：接管时「合约号被改写」，持仓从未真正被管理（Coder 实测）
- 接管写入的 campaign：`campaign_id = orphan:US.META261030P660000`（**正确**，对应实际持仓），但 `code = US.META261030P670000`（**错误**，另一个行权价）；
- AMZN 同样错位：`campaign_id=orphan:...P252500` 而 `code=...P250000`；
- **改写来源**：被接管的 campaign 是 `trigger_mode=VOLATILITY`、`exit_only=True`，而**波动率扫描器的「表达复核」不跳过 `exit_only`** —— 它照常跑 `roll_campaign()`，按「当前最佳期权表达」重选行权价，把 `code` 覆写；
- **后果**：campaign 名义上「接管了」持仓，但指向的是**另一个合约** → **两边对不上，仓位等于没人管**；
- 最终那笔止损是走 **orphan recovery（未接管仓位自动平仓）** 通道出去的（`intent_key` 前缀正是 `orphan:`），亏损 $366；
- **修正**：Product 此前的「10 分钟窗口」框架**不完整**——真实空窗是「**接管后 → 平仓前**」整整 12 分钟（21:49 接管 → 22:01 平仓），且**只要表达复核再跑一次，错位就会复发**。

### 3c. 🔴 决定性根因：实盘门禁的「当日累计」**不分买卖方向**（代码实据）
**代码位置**：`src/futu_quant/real_trading.py:239-247`

```python
used = submitted_real_notional_since(
    store, day_start.isoformat(), exclude_intent_id, account_id=target_account,
)                                              # ← 没有传 side
if used + notional > max_daily:                 # max_daily = REAL_MAX_DAILY_NOTIONAL = 5000
    return RealTradeDecision(False, f"实盘账户 {target_account} 当日累计名义金额将超过 ${max_daily:,.2f}", ...)
```

**而 `submitted_real_notional_since`（`real_trading.py:122-147`）根本没有 `side` 参数**——它对**所有**处于 SUBMITTING/SUBMITTED/UNKNOWN/FILLED/FILLED_PART/CANCEL_REQUESTED 状态的意图，累加 `abs(quantity × limit_price × multiplier)`，**完全不区分 BUY / SELL**。

**所以：卖出被拿去和买入共用同一个 $5,000 额度桶。** 当天买入消耗完后，任何卖出都会撞上这条线。

**注意 `check_real_trade` 是有 `side` 参数的（`:166`）——但它只用于白名单/急停判断，从未用于日累计额度。**

**为什么会写成这样（产品归因）**：
1. 日累计额度最初是作为一个**粗糙的「当日委托流水总量」节流阀**（防止单日过度交易）设计的，**从未区分方向的经济含义**；
2. 后来 `risk.py` 的通用风控引擎**已经**做了正确分离（`risk_reducing_sell` → `max_daily_reduction_notional`），但**实盘门禁自己另写了一套并行的日累计检查，没有复用**；
3. 结果就是**同一个概念两套实现，一套对、一套错，且错的那套（实盘门禁）先触发**——这是典型的「平行实现导致语义漂移」缺陷。

**修正要求**：
- `submitted_real_notional_since` 增加 `side` 参数（过滤 `order_intents.side`）；
- `check_real_trade` 按方向分流：**BUY** → 计入 `max_daily_notional`（只累加 BUY）；**风险减仓 SELL** → 计入 `max_daily_reduction_notional`（只累加 SELL）；
- 实盘门禁的日累计检查**与 `risk.py` 的口径必须一致**（建议抽成共享函数，杜绝第三套实现）；
- **验收**：当日开仓额度已用尽时，**卖出平仓必须仍能通过**（回归守卫）。

### 3d. 风险告警未触达（并列缺陷）
- 成交后 1 秒系统即检出「交割缺口 100 股」并记 ERROR，但 `delivered=0`——**从未推送飞书**；
- `OPTION_LIFECYCLE_RISK` 不在飞书白名单内，且 `alerts.py` 对已 opened 的非白名单事件**直接 return，连库都不写**。

---

## 三、 详细业务需求规格

### 1. 成交即登记（Fill-Triggered Adoption）—— P0
- **强约束**：任何策略成交（FILLED）后，**必须在同一执行路径内**将该仓位登记进对应 campaign，**不得依赖下一轮周期巡检**；
- **实现方向**（Architect 方案，采纳）：
  - `order_push.py` 的 `DealPushHandler` **去掉 SIMULATE 过滤**，改为按 `intent` 的账户/租户归属处理（推送本就带 `order_id` → `intent_by_broker_order` 可反查到 intent 与租户）；
  - `record_fill` 之后立即调用 `register_fill_to_campaign(store, broker, intent, qty, price)`：按 `code` 找所属 campaign → 幂等标记已登记；**找不到归属 → 当场 ADOPT**（复用 `adopt_volatility_orphan_position`），不等下一周期；
  - 真金侧兜底：`executor.reconcile()` 每轮同样跑一次登记（幂等），保证轮询路径也不留窗口。
- **验收**：构造一笔成交，断言**同一周期内**该仓位已出现在 campaign 列表中（`previous_position_quantity > 0`）。

### 2. 滚动不得孤立已成交仓位 —— P0
- `roll_campaign` 执行前必须检查：**被替换 campaign 是否持有未平仓券商仓位**；
- 若有 → **禁止直接替换**，改为「保留原 campaign 至仓位了结」或「先了结再滚动」，并写审计 `ROLL_DEFERRED_OPEN_POSITION`；
- 绝不允许出现「仓位已成交、campaign 已被替换」的中间态。
- **护栏必须抽成共享不变量**：`retain_campaign_if_at_risk(store, campaign, positions, orders)`，**worker 与雷达两条滚动路径都走它**；
- **不变量（产品级）**：持有券商持仓或挂单的 campaign **永不被删除，只降级为 `exit_only`**。

### 3. 止损口径标准化 —— P1
**结论：采用双层限额，废除单一固定美元额。**

| 层级 | 口径 | 建议值 | 理由 |
| :--- | :--- | :--- | :--- |
| **相对止损（主防线）** | 权利金百分比 | **-25%** | 随仓位规模自动缩放；期权标准止损位 |
| **绝对止损（兜底）** | 单笔最大亏损 | **$300**（且 ≤ 账户净值 0.5%） | 防极端行情下相对止损失守 |
| **组合层（现有红线不变）** | 单笔名义 / 日累计 | `$2,500` / `$5,000` | 保持不动 |

**为什么不是 $100**：$100 对 `$2,076` 的仓位仅 **4.8%**（会被正常波动频繁打掉），对 `$300` 的仓位却是 **33%**（形同虚设）。**固定美元额不随规模缩放，不是工业级做法。**

**Coder 已核实**：`volatility_optimizer.py:450` 的 `max_loss_usd=100.0` 只是**函数默认值**；`run_gamma_strategy.py:1098` 实际取 `campaign.get("max_position_loss_usd", 300)`，campaign 缺该键即回落 **300**。**所以 $300 就是当前生效值**，`-25% / $300` 不是新限额；要动的是「**显式写入 + 强制校验**」，不是改数。

**硬要求**：
- 限额必须**显式写入每个 campaign**，下单前强制校验；
- **禁止任何默认值静默兜底**——缺省即视为配置错误并**记 ERROR、拒绝开仓**；
- UI 可见、可改、改动留痕可审计。

### 3a. 减仓与开仓额度必须分离 —— P0
- **产品口径**：额度必须按**交易方向的经济含义**分别计量：
  - **开仓额度**（entry quota）：只计 **BUY 开仓**；
  - **减仓额度**（reduction quota）：**SELL 平仓单独计量**，不占开仓额度；
- 系统已有 `RISK_MAX_REDUCTION_ORDER_NOTIONAL` 概念，**必须真正接入卖出单的校验路径**；
- **验收**：在开仓额度已用尽的情况下，**卖出平仓必须能通过**（回归守卫）。

### 3b. `exit_only` campaign 必须跳过表达复核 —— P0
- **强约束**：`exit_only=True` 的 campaign **只管理退出，不得重选合约**；
- 表达复核 / 滚动逻辑必须显式跳过 `exit_only`；
- 接管时必须**用同一来源**写入 `campaign_id` 与 `code`（均取 `item.code`），并在接管后**校验 `campaign.code == 实际持仓合约`**，不一致即告警。

### 4. 止损评估时效 —— P1
- 止损评估频率应与策略周期一致（≤30 秒）；
- **仓位一旦登记，即刻纳入评估**，不得等到下一周期；
- 新增「保护覆盖率」指标：`受管理仓位数 / 券商实际持仓数`，**必须恒为 1.0**，否则告警。

### 5. 告警触达完整性 —— P0（与既有 PRD 合并）
- `OPTION_LIFECYCLE_RISK` 加入飞书群白名单；
- `alerts.py` 改为**始终落库**，白名单只决定是否推送；
- ERROR 级告警 `delivered=0` 持续 N 分钟 → 升级为 CRITICAL（`ALERT_DELIVERY_FAILED`）。

#### 5.1 白名单方向必须反转（产品决策，Architect 支持）
- **现状（错）**：默认静默，只有显式列入白名单的事件才推 → `DAILY_LOSS_KILL_SWITCH`（模拟账户当日亏 $10,351 触发急停）、`REAL_TRADE_REJECTED`、`ORDER_ENV_MISMATCH` 等**真金关键事件全被静默**；
- **改为（对）**：**默认推送 ERROR 及以上**，防疲劳靠**显式静音清单**（mute-list），而不是靠「默认静默」——后者会吃掉真金信号；
- **产品原则**：**凡涉及真金敞口、资金、风控门禁的 ERROR/CRITICAL 事件，一律必须推送**，不因「怕吵」而静默。

---

## 四、 验收标准
1. 任意策略成交后，**同一周期内**该仓位已归属 campaign（`previous_position_quantity > 0`）；
2. 构造「滚动 + 已成交仓位」场景，断言**不得**出现无归属仓位；
3. 「保护覆盖率」恒为 1.0，出现 <1.0 时产生告警；
4. `OPTION_LIFECYCLE_RISK` 在「已 opened」状态下仍写入 `alerts` 行并推送飞书；
5. 缺省限额配置触发告警而非静默兜底。

---
*本文档为需求基线，归档至 docs/ 目录。*
