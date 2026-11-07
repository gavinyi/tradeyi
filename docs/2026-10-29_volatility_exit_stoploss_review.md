# 波动率期权策略 止损/止盈盘点报告

- **日期**：2026-10-29
- **范围**：`volatility_long_gamma`（波动率期权）策略的止损、止盈、退出与冷却链路
- **不在范围**：财报 Gamma（`GAMMA_CAMPAIGNS`，见 §8.4）、加密/期货策略族、组合级风控（`risk_limits`）
- **代码基线**：`scripts/run_gamma_strategy.py`（2221 行）、`src/futu_quant/volatility_optimizer.py`（1578 行）、`pages/10_volatility_options_strategy.py`
- **用途**：交接文档。§5 是待处理项，§7 是建议的处理顺序，§8 是接手需要的地图与验证手段

---

## 1. 结论摘要

1. 退出链路结构完整：**亏损侧 7 道闸门 + 盈利侧 6 道闸门**，每条 campaign 每轮独立判定；硬止损有**两层纵深防御**并**绕过退出冷却**（10-22 META 事故的直接修复）。
2. 但**盈利侧存在明显的"配置了但不生效"现象**：七档「阶梯锁盈」被「回吐 20%」完全遮蔽，实际出场时点由回吐阈值单一决定；「软启动保护」只在峰值 8%–10% 的窄带内生效。
3. **止损侧存在两套语义重叠的比例止损**（`stop_loss=0.35` 与 `max_position_loss_ratio=0.25`），前者已不可达；真正的止损线随持有时间从 35% 收紧到 12%，页面上的「硬止损比例」有误导性。
4. **发现 1 个可复现的功能性缺陷（P1）**：退出冷却时长靠中文字符串匹配，四类盈利退出文案（阶梯锁盈 / 软启动锁盈 / 软启动回吐 / 快速回吐）落到默认 24 小时，而不是止盈档 4 小时。
5. 另有 2 个 P2、3 个 P3，见 §5。所有结论均已用线上 `runtime_config` 实际数据 + 纯函数实测复核，不是纯静态阅读。

---

## 2. 判定顺序（每条 campaign 每轮）

### 2.1 亏损侧（先判定，命中即置 `SELL`）

| # | 闸门 | 条件（当前生效值） | 实现位置 |
|---|---|---|---|
| 1 | 单笔硬止损 | 相对亏损 ≥ 25% **或** 绝对亏损 ≥ $300，OR 取较紧 | `run_gamma_strategy.py:1140`（第 1 层）、`:1651`（第 2 层）→ `volatility_optimizer.py:586` |
| 2 | 时间衰减动态止损 | 阈值由 35% 线性收紧到 12%，15→90 分钟 | `volatility_optimizer.py:628`，调用点 `:1370` |
| 3 | 方向失效 / Delta 衰减 | 连续 2 轮方向相反；或 \|Delta\| < 0.18 且亏损 | `volatility_optimizer.py:1299-1311` |
| 4 | IV / Gamma-Theta 效率衰减 | IV 较实际入场 -20%，或效率 -40%，且需持有 ≥90 分钟 + 价格/方向不利 | `volatility_optimizer.py:1313-1330` |
| 5 | 波动收缩 | 连续 3 轮 `inactive + contraction` 确认 | `volatility_optimizer.py:1285-1294` |
| 6 | 临期 / 到期 | DTE ≤ 1 且价内（内在价值 ≥ $0.01）；或到期日 15:00 后 | `run_gamma_strategy.py:356`、`:1669` |
| 7 | 最长持有 / 周末避险 | 24 小时硬上限；周五收盘前 30 分钟内浮盈 < 20% 清仓 | `volatility_optimizer.py:1460-1472` |

### 2.2 盈利侧（触发即卖，含分批）

| # | 闸门 | 条件（当前生效值） | 实现位置 |
|---|---|---|---|
| 1 | 分批止盈 | +18% 卖 50%；+35% 再卖 25%（2 张以上才拆） | `volatility_optimizer.py:199`，调用点 `run_gamma_strategy.py:1510` |
| 2 | 软启动保护 | 峰值 ≥ 8%（=0.8×启动阈值），锁盈线 3%、回吐 35%、15 分钟回吐 25% | `volatility_optimizer.py:1400-1420` |
| 3 | 阶梯锁盈 | 峰值 10%→锁 6%，18%→12%，30%→18%，50%→30%，80%→50%，100%→70%，150%→110% | `volatility_optimizer.py:110`、`:1423` |
| 4 | 时间收紧止盈 | 持有 ≥60 分钟且峰值 ≥10%，回吐 ≥12% | `volatility_optimizer.py:1432` |
| 5 | 盈利回吐保护 | 峰值盈利回吐 ≥ 20% | `volatility_optimizer.py:1443` |
| 6 | 最低锁定盈利 | 峰值 ≥10% 后，浮盈跌回 ≤5% | `volatility_optimizer.py:1450` |

> 单张持仓例外：`single_contract_take_profit`（18%）**不卖出**，只把 reason 变成「启动移动止盈」并继续 HOLD（`volatility_optimizer.py:1475`）。页面文案「1 张达到主动落袋线会先兑现原合约」与此不一致，见 P3-3。

### 2.3 硬止损的两层纵深防御（不要当成冗余删掉）

- 第 1 层 `run_gamma_strategy.py:1139-1155`：在决策分支**之前**判定，防止「分批止盈 / 常规决策」把已置的 `SELL` 覆盖掉。
- 第 2 层 `run_gamma_strategy.py:1650-1670`：在决策分支**之后**独立重算一次，覆盖到期/临期等下游分支。
- 结构守卫：`tests/test_hard_stop_end_to_end.py:290`（`test_no_branch_downgrades_sell_after_hard_loss_layer`）钉住「第 2 层之后不得把 SELL 改回非 SELL」。
- 变异验证：`tests/test_volatility_p0_mutations.py` 已证明「删任一层行为不变、删两层失效」。

### 2.4 开仓与退出的不对称（有意设计，勿"统一"）

| 场景 | 限额缺失/非法时 | 位置 |
|---|---|---|
| **开仓** | 拒绝开仓 + ERROR 告警（禁止静默兜底） | `run_gamma_strategy.py:489`、`:1796-1832` |
| **退出** | 回落 0.25 / 300 并推 ERROR 告警（**绝不能因配置错误锁死退出**） | `run_gamma_strategy.py:525-565` |

设计理由写在注释里：配置错误不该把已有持仓的退出管理一起冻结（`strategies_paused` 踩过这个坑）。**改动时不要为了"一致性"把退出侧也改成拒绝**。

---

## 3. 当前线上生效参数（实测，非文档值）

读取 `data/trading.db` 的 `runtime_config`，`futu_real_master` 租户 3 条 campaign（AMD / TSM / GOOG）**参数完全一致**：

| 参数 | 值 | 备注 |
|---|---|---|
| `max_position_loss_ratio` | 0.25 | 硬止损相对线 |
| `max_position_loss_usd` | 300.0 | 硬止损绝对线 |
| `stop_loss` | 0.35 | **不可达**（见 P2-1），仅作为动态止损的起点值被复用 |
| `profit_target` | 2.0 | **不可达**（见 P2-2） |
| `trailing_stop` | 0.25 | 仅在 profile 缺键时作为回吐兜底，实际被 `profit_protection_giveback_normal=0.20` 覆盖 |
| `max_hold_hours` | 24 | |
| `minimum_hold_minutes` | 90 | 只约束 IV/效率衰减类退出 |
| `partial_profit_tier1 / fraction` | 0.18 / 0.50 | |
| `partial_profit_tier2 / fraction` | 0.35 / 0.25 | |
| `single_contract_take_profit` | 0.18 | 1 张时不卖，转移动止盈 |
| `tiered_profit_lock_enabled` | true | 但被回吐遮蔽，见 P2-3 |
| `profit_time_tighten_minutes / giveback` | 60 / 0.12 | 60 分钟后实际最紧的止盈线 |
| `dynamic_time_stop` | 12% / 15–90 分钟 | 长持后实际止损线 |
| `profit_protection_start_normal` | 0.10 | 决定 `protection_active` |
| `profit_protection_giveback_normal` | 0.20 | **实际主闸门** |
| `minimum_locked_profit_normal` | 0.05 | |
| `iv_decline_exit_ratio` | 0.20 | |
| `efficiency_decline_exit_ratio` | 0.40 | |
| `contraction_confirmations_required` | 3 | |
| `adverse_signal_confirmations_required` | 2 | |
| `cooldown_*` | 止盈 4h / 保护 8h / 止损 24h / 衰减 8h / 收缩 6h | |
| `cooldown_bypass_enabled` | false | |

> 复现方式见 §8.5。

---

## 4. 关键数值行为（容易误读的三处）

### 4.1 止损线随持有时间收紧，不是固定的 -25%

`volatility_time_decay_stop` 把阈值从 `stop_loss=0.35` 线性压到 `dynamic_stop_floor=0.12`（15→90 分钟）。实测：

| 持有 | 0–15 分钟 | 30 分钟 | 48 分钟 | 60 分钟 | ≥90 分钟 |
|---|---|---|---|---|---|
| 动态止损线 | 35.0% | 30.4% | 24.9% | 21.2% | **12.0%** |

结论：**持有超过约 48 分钟后，动态止损比硬止损 25% 更紧**；长持仓位的真实止损线是 12%。所以「硬止损 -25%」只在开仓后 48 分钟内是有效约束。

### 4.2 $300 与 -25% 谁先到，取决于单笔权利金规模

`volatility_hard_loss_exit_reason` 是 OR：`-25%` 与 `-$300` 谁先满足谁触发。两者交汇于**单笔权利金 $1,200**：

- 权利金 **< $1,200** → -25% 更紧，先触发（例：$600 权利金，-25% = $150，够不到 $300）
- 权利金 **> $1,200** → $300 更紧，先触发（例：$3,000 权利金，$300 ≈ -10%）

注意：三条 campaign 的 `max_premium_per_stage` 分别是 **45,000 / 6,000 / 3,000**（不是页面默认的 1,000），所以「哪一层更紧」不能从预算上限推断，要看**实际建仓权利金**。可用 `decision_logs` 里每轮的 `position_pnl_amount` 与 `pnl_ratio` 反推：若某轮 `pnl_ratio ≈ -10%` 就触发 SELL，说明 $300 在起作用；若 ≈ -25% 触发，则是比例层。

### 4.3 出场点的真实决定因素（按峰值分档）

| 峰值盈利 | 实际出场线 | 由哪条规则决定 |
|---|---|---|
| 8%–10% | 峰值的 65%（0.65×peak），且不低于 3% | 软启动回吐 35% |
| ≥10%，持有 <60 分钟 | 峰值的 80% | 盈利回吐 20% |
| ≥10%，持有 ≥60 分钟 | 峰值的 88% | 时间收紧回吐 12% |

即：**峰值 20% 的仓位，持有满 60 分钟后跌到 17.6% 就会走**。阶梯锁盈表里的 12%/18%/30% 等档位不会改变这个时点（见 P2-3）。

---

## 5. 问题清单

### P1-1 冷却时长靠中文字符串匹配，四类盈利退出误判为 24 小时

- **位置**：`src/futu_quant/volatility_optimizer.py:827-851`（`volatility_cooldown_hours`），调用点 `run_gamma_strategy.py:857`
- **机理**：函数按 reason 文本里的关键词（`分批止盈` / `到期` / `行权` / `硬止损` / `盈利保护` / `最低锁定盈利` / `止盈`）分流，逐条 `if` 匹配，未命中即 `default`。
- **实测结果**（已跑纯函数，输出见附录 A）：

  | reason 文案 | 实际冷却 | 应有冷却 |
  |---|---|---|
  | `阶梯锁盈触发：…` | **24h** | 4h（止盈类） |
  | `软启动锁盈触发：…` | **24h** | 4h |
  | `软启动回吐保护触发：…` | **24h** | 8h（保护类） |
  | `快速回吐保护触发：…` | **24h** | 8h |
  | `分批止盈第一档：…` | 0h | 0h ✅ |
  | `持仓时间收紧止盈：…` | 4h | 4h ✅ |
  | `盈利保护触发：…` | 8h | 8h ✅ |
  | `最低锁定盈利触发：…` | 8h | 8h ✅ |

- **影响**：这四类退出后要多等约 20 小时才允许重入，实际压低了资金周转与信号复用率；且"文案改了冷却就变"这种耦合会在后续任何文案调整时静默重现。
- **建议修复**：把退出类型从决策层显式传出（如 `exit_kind ∈ {partial, take_profit, profit_protection, stop_loss, decay, contraction, expiry, other}`），冷却改为按枚举查表；**不要继续补关键词**（治标，且下一次改文案还会踩）。
- **验收**：新增测试对 8 类退出 reason 断言冷却值；`volatility_cooldown_hours` 保留旧签名做兼容包装（内部改为先判 kind 再回落关键词），避免一次改爆既有调用点。

### P2-1 `stop_loss=0.35` 不可达，页面「硬止损比例」有误导性

- **位置**：`volatility_optimizer.py:1368`（`pnl_ratio <= -stop_loss`）
- **机理**：`pnl ≤ -35%` 必然已满足 `pnl ≤ -25%`，硬止损在两层中任一层已先行置 `SELL`（`:1140` / `:1651`），该分支永远走不到。
- **影响**：运维看到页面填 0.40 会以为止损在 -40%，实际最严一层是 -12%（长持）或 -25%（短持）。这属于**参数语义误导**，不是计算错误。
- **建议**：页面把 `stop_loss` 改名为「动态止损起始线（仅作衰减起点）」，并在旁边显式标注当前生效止损线；或与 `max_position_loss_ratio` 合并为单一入口。

### P2-2 `profit_target=2.0`（200%）不可达

- **位置**：`volatility_optimizer.py:1382`（条件含 `not tiered_profit_lock_enabled`）
- **机理**：三条 campaign 的 `tiered_profit_lock_enabled=true`，条件恒假；多张持仓时 `full_profit_target_enabled` 还会被 `partial_profit_enabled` 再关一次（`run_gamma_strategy.py:1571`）。
- **影响**：整单止盈是一条死分支；页面上「止盈比例 2.0」不会发生任何事。
- **建议**：二选一 —— 若保留阶梯体系，则从界面移除该输入或改为只读展示；若要保留整单止盈语义，则把条件改为 `tiered off OR 到达 profit_target` 并做档位冲突校验。

### P2-3 七档「阶梯锁盈」不影响出场时点，只影响日志文案

- **位置**：`volatility_optimizer.py:110-125`（档位表）、`:1423`（阶梯分支）、`:1443`（回吐分支）
- **机理**：阶梯锁盈的 `floor/peak` 比值最高只有 **73.3%**（150%→110%），其余档位为 60%–70%；而回吐 20% 的出场线恒为峰值的 **80%**。由于 `floor < 0.8×peak` 恒成立，价格路径必然先穿过 `0.8×peak`，此时阶梯条件（`pnl ≤ floor`）尚未成立 → 先触发的是回吐保护。
- **唯一例外**：单轮跳空直接从 `>0.8×peak` 跳到 `<floor`，两者同时为真，阶梯分支在前、拿到 reason，但**成交时点与回吐分支相同**。
- **影响**：`tiered_profit_lock_enabled` 与七档表的调参对结果无影响，容易让调参者误判「已经收紧锁盈」。
- **待决策（需业务定）**：
  - 方案 A：让阶梯真正生效 —— 回吐阈值必须 < `1 - floor/peak`，即峰值 18% 档要求回吐 < 33%。当前 20% 恰好越过这条线。
  - 方案 B：承认「回吐 20%」是唯一闸门，删掉七档表与开关，减少认知负担。
- **不建议**：继续在两条规则上各自调参而不做冲突校验 —— 这类"配置了但不生效"的问题会在下一次调参时重复发生。

### P3-1 「软启动保护」只覆盖峰值 8%–10% 的窄带

- **位置**：`volatility_optimizer.py:1400`（`if soft_active and not protection_active`）
- **机理**：`soft_active = peak ≥ 0.8×start`，`protection_active = peak ≥ start`；两者同时成立时走 `not protection_active` 分支为假，软启动规则全部跳过。峰值单调不减，一旦 ≥10% 就再也不回到软启动。
- **影响**：`soft_profit_lock_floor=3%`、`soft_profit_giveback=35%`、`rapid_giveback=25%/15min` 三个参数只在 2 个百分点区间内起作用，与它们的命名暗示不符。
- **建议**：要么把软启动改为「始终与硬保护并行取较紧者」，要么在页面明示该组参数的适用区间。

### P3-2 `cooldown_bypass_enabled` 缺键时默认值左右相反

- **位置**：代码 `run_gamma_strategy.py:1372` 缺键回落 **True**；页面/scanner 默认 **False**（`pages/10:832-836`、`volatility_scanner.py:508`）
- **影响**：只在「老 campaign 没有这个键」时出现，行为是**允许强信号绕过冷却**，与页面语义相反。当前 3 条 campaign 都显式写了 `false`，暂未触发。
- **建议**：把代码回落改成与页面一致的 `False`（fail-closed），并加一次性迁移把缺失键补齐。

### P3-3 页面文案与实际实现不符（两处）

1. `pages/10:622-625` 声称退出优先级为「到期/行权风险 → 硬止损 → …」，实际到期判定在**最后**（`:1669`），硬止损在最前。
2. `pages/10:767-769` 声称「1 张合约达到主动落袋线会先兑现原合约」，实际 `decide_volatility_action:1475` 只返回 `HOLD` 并转移动止盈，不卖出。
3. `pages/10:852-855` 帮助文案称 `$300` 上限「且不超过账户净值的 0.5%」，代码中**未找到该约束的实现**（`0.005` 出现在 `volatility_min_expected_return`，与止损无关）。即 $300 是绝对上限，与净值无关。

---

## 6. 页面未暴露的隐含参数（只能改 JSON）

这些键在代码里有默认值，但 `pages/10` 没有输入框，运维无法从界面感知：

| 键 | 默认值 | 作用 |
|---|---|---|
| `soft_profit_protection_start` | 0.08 | 软启动触发（=0.8×start） |
| `soft_profit_lock_floor` | 0.03 | 软启动锁盈线 |
| `soft_profit_giveback` | 0.35 | 软启动回吐阈值 |
| `rapid_giveback_threshold` / `_window_minutes` | 0.25 / 15 | 快速回吐保护 |
| `weekend_protection_enabled` / `_min_profit` / `_minutes` | true / 0.20 / 30 | 周五尾盘避险 |
| `near_expiry_itm_exit_days` / `_min_intrinsic` | 1 / 0.01 | 临期价内强平 |
| `partial_exit_mode` / `exit_allocation_policy` | FIFO / FIFO | 分批出场选批次方式（FIFO/LIFO/RISK_PRIORITY） |
| `cooldown_bypass_stop_loss` / `_min_zscore` / `_min_intraday_move` / `_min_atr_ratio` / `_min_volume_ratio` | false / 3.0 / 3.5% / 2.0 / 1.5 | 强信号豁免冷却细则 |
| `urgent_exit_price_improvement_pct` | 0.02 | 紧急退出的主动让价 |

建议：至少在页面加一个「高级退出参数（只读展示 + JSON 编辑）」折叠块，避免这些参数成为"只有读代码才知道"的暗配置。

---

## 7. 建议处理顺序

| 顺序 | 事项 | 理由 | 风险 |
|---|---|---|---|
| 1 | **P1-1 冷却映射** | 唯一直接影响成交频率的缺陷，改动面小（纯函数 + 一个调用点） | 低。需保留旧签名兼容，并补 8 类 reason 的断言 |
| 2 | **P2-3 阶梯锁盈取舍** | 需要业务先定 A/B 方案，否则后续所有止盈调参都无意义 | 中。改阈值会直接改变出场点 |
| 3 | **P2-1 / P2-2 死参数清理** | 纯展示层与条件清理，先消除误导 | 低。移除输入框前需确认无外部脚本依赖 |
| 4 | **P3-2 默认值对齐** | fail-closed 对齐，避免老配置静默放行 | 低。需一次性数据迁移 |
| 5 | **P3-1 / P3-3 文档与语义对齐** | 改注释、改文案、补说明 | 极低 |

**不建议**在本轮改动中调整 `max_position_loss_*` 的实际阈值（0.25 / $300）：它是 10-22 事故后的既定结论，且刚完成显式化治理（缺键即拒绝开仓）。要动数值请单独走一次评审。

---

## 8. 交接信息

### 8.1 文件地图

| 关注点 | 文件 | 关键行 |
|---|---|---|
| 退出决策总入口 | `src/futu_quant/volatility_optimizer.py` | `decide_volatility_action:1334` |
| 硬止损纯函数 | 同上 | `volatility_hard_loss_exit_reason:586` |
| 动态止损 | 同上 | `volatility_time_decay_stop:628` |
| 盈利保护档位 | 同上 | `PROFIT_PROTECTION_DEFAULTS:102`、`volatility_profit_protection_profile:237` |
| 阶梯锁盈 | 同上 | `volatility_profit_lock_floor:110` |
| 分批止盈 | 同上 | `volatility_partial_exit_plan:199` |
| 冷却 | 同上 | `volatility_cooldown_hours:827` |
| 信号类退出（IV/效率/方向/收缩） | 同上 | `volatility_exit_reason:1273` |
| 周末避险 | 同上 | `volatility_weekend_carry_exit_reason:668` |
| 策略主流程 / 两层硬止损 | `scripts/run_gamma_strategy.py` | `:1139`（第 1 层）、`:1650`（第 2 层） |
| 限额解析（退出侧兜底） | 同上 | `_hard_stop_limits:525` |
| 限额校验（开仓侧拒绝） | 同上 | `_stop_limit_problems:489` |
| 临期价内强平 | 同上 | `volatility_near_expiry_itm_exit_reason:356` |
| 参数录入与落库 | `pages/10_volatility_options_strategy.py` | `:596-620`、`:621-733`、`:766-800`、`:837-869`、`:921-924`、`:982-1032`、`:1311-1312` |

### 8.2 相关测试

| 测试 | 保护什么 |
|---|---|
| `tests/test_stop_limit_explicit.py` | 缺键不得静默兜底（开仓拒绝） |
| `tests/test_stop_limit_ui_explicit.py` | 页面必须显式写出两个限额 |
| `tests/test_gamma_page_stop_limits.py` | 财报页的限额输入 |
| `tests/test_hard_stop_end_to_end.py` | 硬止损端到端；`:290` 是 SELL 不得降级的结构守卫 |
| `tests/test_hard_stop_bypasses_cooldown.py` | 硬止损绕过冷却（META 事故回归） |
| `tests/test_volatility_p0_mutations.py` | 两层纵深防御的变异验证 |
| `tests/test_volatility_exit_routing.py` | 退出路由 |

> 目前**没有**覆盖冷却按原因分档的测试 —— 这正是 P1-1 能长期存在的原因，修复时应一并补上。

### 8.3 数据位置

- 权威配置：`data/trading.db` → `runtime_config` 表，键 `VOLATILITY_CAMPAIGNS`（无租户旧键）/ `VOLATILITY_CAMPAIGNS:{tenant_id}`
- 镜像：`.env` 同名键（SQLite 是权威，`.env` 仅供兼容读取）
- 审计：`data/trading.db` → `audit_events`，事件 `VOLATILITY_EXIT_PARAMETERS_UPDATED`（含改动前后限额对比）
- 决策留痕：`data/trading.db` → `decision_logs` 表（`SELL`/`BLOCKED` 均带 `reason` 与 `pnl_ratio`）
- 运行状态：`data/futu-logs/`、`data/service-futu_*.log`

### 8.4 与财报 Gamma（`GAMMA_CAMPAIGNS`）的差异

财报 Gamma 是**另一套**退出逻辑（`src/futu_quant/gamma_automation.py:decide_gamma_action`），只有 4 条：硬止损（`stop_loss` 比例）、止盈（`profit_target`）、峰值回撤（`trailing_stop`）、事件后最长持有（默认 8h），**没有**分层保护、分批止盈、冷却、临期价内强平。两套共用 `_hard_stop_limits` 的限额字段，但校验与冷却互不影响。改波动率侧时不要把财报侧的参数当作同一套。

### 8.5 复现命令

```bash
# 1) 查线上实际参数（只读）
cd E:/Thisyi/trade && .venv/Scripts/python.exe -c "
import sqlite3, json
c = sqlite3.connect('file:data/trading.db?mode=ro', uri=True)
for key in ('VOLATILITY_CAMPAIGNS', 'VOLATILITY_CAMPAIGNS:futu_real_master',
            'VOLATILITY_CAMPAIGNS:futu_paper_sandbox'):
    row = c.execute('SELECT value_json, version, updated_at FROM runtime_config WHERE config_key=?', (key,)).fetchone()
    if not row: continue
    print(key, 'ver', row[1], row[2])
    for r in json.loads(row[0]):
        print('  ', r.get('code'), r.get('max_position_loss_ratio'), r.get('max_position_loss_usd'),
              r.get('stop_loss'), r.get('profit_target'))
"

# 2) 复现冷却映射（P1-1）与动态止损曲线（§4.1）
cd E:/Thisyi/trade && .venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'src')
from datetime import datetime, timedelta
from futu_quant.volatility_optimizer import volatility_cooldown_hours as cd, volatility_time_decay_stop as td
for r in ['阶梯锁盈触发：标准盈利档峰值盈利18.0%', '软启动锁盈触发：峰值盈利9.0%',
          '快速回吐保护触发：15分钟内回吐25.0%', '盈利保护触发：回吐20.0%']:
    print(f'{cd(r, 24):>5.1f}h  <-  {r}')
o = datetime(2026, 1, 1, 9, 30)
for m in (0, 15, 30, 48, 60, 90):
    th, _, _ = td(pnl_ratio=0.0, opened_at=o, now=o + timedelta(minutes=m),
                  hard_stop_loss=0.35, floor_stop_loss=0.12,
                  start_minutes=15, full_decay_minutes=90, enabled=True)
    print(f'持有{m:>3}分钟 动态止损线={th:.1%}')
"
```

### 8.6 接手注意事项

1. **不要**为了"消除重复"删掉硬止损两层中的任一层 —— 那是纵深防御，有变异测试钉住。
2. **不要**把退出侧的限额兜底改成拒绝 —— 配置错误不能连退出管理一起冻结（见 §2.4）。
3. 改退出 reason 文案时，**必须**同时检查冷却映射（P1-1 就是文案与映射脱钩的产物）。
4. 动 `PROFIT_PROTECTION_DEFAULTS` 会同时影响四档 regime（常规/动态/强波动/临期），改动前先确认 `profit_protection_*_normal/_dynamic/_strong/_near_expiry` 这些 per-campaign 覆盖键的分布。
5. 所有验证优先用**只读**连接 `file:data/trading.db?mode=ro`，避免与运行中的服务抢锁。

---

## 附录 A：冷却映射实测输出（2026-10-29）

```
  0.0h  <-  分批止盈第一档：当前盈利18.0% >= 18.0%
 24.0h  <-  阶梯锁盈触发：标准盈利档峰值盈利18.0%          ← 应为 4h
 24.0h  <-  软启动锁盈触发：峰值盈利9.0%                  ← 应为 4h
 24.0h  <-  软启动回吐保护触发：盈利回吐40.0%              ← 应为 8h
 24.0h  <-  快速回吐保护触发：15分钟内回吐25.0%            ← 应为 8h
  4.0h  <-  持仓时间收紧止盈：已持有61分钟
  8.0h  <-  盈利保护触发：回吐20.0%
  8.0h  <-  最低锁定盈利触发：当前盈利5.0%
 24.0h  <-  单笔硬止损触发：亏损 310 >= 300
 24.0h  <-  时间衰减动态止损触发：已持有95分钟
 24.0h  <-  波动率仓位已持有24小时，触发最长持有退出
  0.0h  <-  合约到期日临近收盘，强制退出以避免自动行权
  6.0h  <-  实现波动开始收缩，退出波动率仓位
  8.0h  <-  隐含波动率较实际入场下降20%
  8.0h  <-  Gamma/Theta效率较实际入场下降40%
  0.0h  <-  临近到期价内合约强制提前退出
```

## 附录 B：动态止损曲线实测（§4.1 数据来源）

```
持有   0 分钟  35.0%
持有  15 分钟  35.0%
持有  30 分钟  30.4%
持有  48 分钟  24.9%   ← 与硬止损 25% 交汇
持有  60 分钟  21.2%
持有  90 分钟  12.0%
持有 120 分钟  12.0%
```
