# Changelog

## 2026-10-14：飞书群机器人应用内全自动直连、四大进阶机制完善与超额利润放飞升级

1. **标的池快慢双轨赛马、自适应弹性扩容与动量耗竭防接盘门禁 (`universe/`)**：
   - **⚡ 暴风快轨 (Fast-Track 3日极速转正)**：针对阶段性大黑马爆发期，只要标的在 3 天内放量突破 $\ge 2$ 次、价差 $\le 2.5\%$、虚拟净利为正且 ML 评分 $\ge 0.82$，第 3 天即可直接晋级，绝不死等 10 天错失主升浪最肥美区间；
   - **🐢 常规慢轨 (Standard Track)**：对稳健白马维持 7~10 天观察，两套轨道并驾齐驱；
   - **自适应弹性容量 (Adaptive Capacity)**：外援池从固定 5 只升级为 5~10 只动态容量，全市场优质标的爆发时自动扩容至最多 10 只，从容容纳跨赛道热点；
   - **动量耗竭防接盘门禁 (Exhaustion Guard)**：短线累计涨幅过大进入耗竭期的标的一票否决，坚决拒绝高位接盘；
   - 界面端（`pages/39_universe_incubator_center.py`）增加赛马轨道标识与弹性容量动态展示；
   - 新增 `tests/test_universe_fast_track.py` 单元测试全部通过。
2. **飞书战报群机器人多通道全员直连 (`src/futu_quant/feishu_reporter.py`)**：
   - 彻底摆脱外部轮询依赖，将飞书推送深度内嵌为量化系统原生模块 `FeishuReporter`；
   - 支持飞书群 Chat ID 直发（如 `oc_880837ba7546cdf0b75834ef88eff5b4`）、标准群自定义机器人 Webhook 卡片（含 HMAC-SHA256 签名）及个人 147 会话多通道广播，群内所有账号成员同时可见；
   - 修复在线云文档导入链路：优化路径绝对解析，改为 `--as bot` 自动导入，秒级生成包含可点击阅读的正式飞书云文档链接（如 `https://my.feishu.cn/docx/...`）；
   - 在系统监控页（`pages/6_system_monitor.py`）增加飞书群配置表单抽屉与一键测试按钮；
   - 每天清晨 05:15 策略复盘生成后，自动在进程内完成云文档导入并推送到飞书群。
2. **新晋外援标的 1 张探路灰度仓位机制 (`scripts/run_gamma_strategy.py`)**：
   - 全市场沙盒晋级的新外援标的（`promoted_universe`），在首秀阶段（实盘交易为 0 笔或上一笔未盈利）强制限制为 1 张探路仓（`per_stage = 1`）；
   - 实战完成至少 1 笔盈利平仓后，自动解锁转正为 2 张标准分批仓位，兼顾全市场淘金与防翻车灰度隔离。
3. **期权超额利润自动沉淀到 SPY 底仓机制 (`src/futu_quant/profit_sweeper.py`)**：
   - 建立 Profit Sweeper 利润锁箱系统，实时统计期权已实现净收益；
   - 当新增未沉淀利润 $\ge \$1,000$ 美元时，收盘前调仓窗口自动将 50% 利润划拨沉淀为 Core Satellite 核心底仓额外购买力，追加购买 `US.SPY` 现货；
   - 产生 `PROFIT_SWEEPER_SETTLED` 审计日志，实现“期权冲锋掠夺暴利，底仓沉淀永久固化”。
4. **机器学习模型月度自学习闭环 (`scripts/run_monthly_ml_retrain.py`)**：
   - 实现月度自动离线重训练流水线，提取近 90 天所有 308 笔真实平仓样本与 8 只沙盒标的微观滑点特征；
   - 重新校准各标的胜率与盈亏权重，自动备份旧权重并刷新 `volatility_scorer_v3.json`，记录 `ML_MODEL_RETRAINED` 事件。
5. **超额利润放飞与 2 张分批止盈正式激活 (`volatility_optimizer.py`, `volatility_scanner.py`)**：
   - 在 `volatility_profit_lock_floor` 中追加“翻倍主升档”（峰值 $\ge 100\% \to$ 锁死底线 $+70\%$）与“超级主升档”（峰值 $\ge 150\% \to$ 锁死底线 $+110\%$）；
   - 启用阶梯锁盈时解除低位 50% 机械一刀切卖出，允许超额利润继续放飞狂奔；
   - 新活动与现存 6 个活跃活动（TSM, AVGO, COIN, MSFT, META, AMD）全部升级为 2 张分批止盈配置（+35% 卖 50% 撤回全部本金，余下 1 张零成本放飞）。
6. **物理关机交由操作系统接管 (`scheduled_tasks.py`, `run_service.py`)**：
   - 注释掉 Python 内部直接调用系统关机的物理命令，彻底消除 Windows 权限拒绝（exit status 5）报错；
   - 设置 `AUTO_COMPUTER_SHUTDOWN_ENABLED=false`，Python 侧仅负责在 06:00 前完成数据落盘与就绪标记，物理硬件断电关机交由 Windows 原生计划任务接管。


## 2026-10-13：期权出场主动吃单防死锁闭环与周五跨周末避险保护

针对 2026-10-12 盘中 META 期权浮盈达到 +16.8% 后因挂单过于保守死挂 1 小时撤单导致利润回吐、以及 COIN 跨周末跳空低开遭遇硬止损的痛点，实施以下核心升级：

1. **止盈与紧急平仓主动吃单 (Marketable Limit Repricing)**：
   - 紧急平仓卖单（`is_urgent=True`，涵盖自适应止盈、阶梯锁盈、回撤保护与硬止损）在改单追价时，目标价直接对准最新买一价下方 1%（`min(current, bid * 0.99)`），主动穿透买盘对手方，确保在做市商买盘中瞬间撮合成交，死死锁住利润；
   - 紧急买回平仓单同样对准卖一价上方 1%（`max(current, ask * 1.01)`）主动吃单；
   - 全程受紧急滑点上限（`EXEC_URGENT_EXIT_MAX_SLIPPAGE_PCT`，默认 12%）兜底保护，绝不失控贱卖。
2. **紧急平仓 15 秒高频紧咬与 60 秒超时强行释放**：
   - 紧急平仓改单追价间隔从 60 秒缩短至 15 秒，最大尝试次数放宽至 12 次；
   - 紧急退出单超时撤单时间从 3600 秒（1小时）彻底压缩至 60 秒；若盘口极端断档 60 秒内仍未成交，系统立即强制撤单，彻底物理消除“死单挂起 1 小时卡死策略状态”的严重缺陷。
3. **撤单后批次自动自愈递增 (`pending_exit_episode` 重置)**：
   - 一旦上一笔平仓挂单超时撤销，系统自动将 `campaign_state["pending_exit_episode"]` 重置为 `False`；
   - 下一个 30 秒轮询周期自动将退出批次递增（如从 `episode1` 变为 `episode2`），以全新的幂等 key 结合当前最新鲜的盘口重新发起平仓，形成完整的自愈闭环。
4. **周五收盘前跨周末期权避险退出 (`volatility_weekend_carry_exit_reason`)**：
   - 美东时间周五收盘前 30 分钟（15:30 ~ 16:00 ET），系统自动扫描当前持有的期权多头仓位；
   - 若浮盈未达到 20% 安全垫（`pnl_ratio < 0.20`），系统在收盘前主动清仓保本离场，坚决防范周末 48 小时纯 Theta 时间价值损耗与周一开盘不可控跳空暴跌风险；
   - 若浮盈达到 20% 以上，证明已有足够利润气囊，允许继续跟踪。
5. **UI 修复与测试**：
   - 修复系统监控页（`pages/6_system_monitor.py`）调用未定义 `CheckColumn` 导致的页面崩溃异常；
   - 增加 `test_urgent_sell_reprice_chases_bid_and_cancels_faster` 与 `test_weekend_carry_exit_triggers_on_friday_near_close` 单元测试，全部验证通过。


## 2026-10-12：动态标的池扩充与分级沙盒孵化系统上线 (Dynamic Universe Expansion)

构建工业级“全市场召回 $\to$ 10日沙盒孵化 $\to$ ML精排 $\to$ 多样性打散 $\to$ 动态晋级实盘”子系统：

1. **核心底仓与外援解耦 (Core-Satellite Universe)**：
   - 核心底仓（Core 13 只）作为压舱石，永久常驻、绝对不参与全市场淘汰机制；
   - 动态扩展池（Satellite 3~5 只）面向美股全市场淘金，只有在沙盒跑出正期望值的黑马才被追加进实盘池；
   - 实际交易标的池公式：`VOLATILITY_SYMBOLS = Core(13只) ∪ Promoted_Active(3~5只)`。
2. **五大核心引擎落地 (`src/futu_quant/universe/`)**：
   - `recall_scanner.py`：全市场期权流动性 Top 50 粗排召回（平值价差 $\le 3\%$、日均期权量 $> 50k$、正股成交额 $> \$5\text{亿}$，强制排除 TSLA 等负面黑名单）；
   - `incubator_engine.py`：滚动 10 日隔离沙盒影子演练，统计微观滑点、5分钟量能留存率、虚拟胜率与最大回撤（$MDD \ge 25\%$ 一票否决）；
   - `ranking_pipeline.py`：构建多维动量与期权定价特征，集成 `MLSymbolScorer` 预测打分；
   - `diversity_filter.py`：MMR 贪心打散算法（$\lambda = 0.65$），强制细分行业上限 1 席且两两相关系数 $\le 0.70$；
   - `lifecycle_manager.py`：迟滞防抖仲裁（新候选需超出现任 $10\%$ 且保持 2 天）、授权到期清退与应急秒踢（连触2次硬止损或价差恶化 $> 10\%$）。
3. **数据层四表建立**：
   - SQLite 新增 `candidate_universe`, `shadow_incubator`, `promoted_universe`, `universe_lifecycle_events` 四张表及辅助操作 API。
4. **可视化监控看板上线**：
   - 新增《🚀 标的孵化与赛马中心》（`pages/39_universe_incubator_center.py`）页面，提供实盘池透视、沙盒赛马榜、Top 50 召回池、生命周期审计及一键运维抽屉；
   - 首页驾驶舱底部新增直达快捷入口。
5. **测试与离线入口**：
   - 编写 `test_universe_*.py` 5 大测试套件全量通过；
   - 新增 `run_universe_recall.py`, `run_universe_ranking.py`, `run_universe_sync.py` 脚本。


## 2026-10-11：全年统一固定时钟表与真实关机开启

1. **统一固定时间表 (Universal Fixed Schedule)**：
   - 每天 05:15：`daily_strategy_review` 策略复盘与 HTML 报告生成；
   - 每天 05:30：`daily_universe_ranking` 标的池 ML 精排、沙盒自评与外援轮换；
   - 每天 06:00：`computer_shutdown` 电脑安全关机；
   - 每周一 21:10：`weekly_universe_recall` 开盘前全市场 Top 50 召回；
   - 全年两季通用：以冬令时收盘（05:00）作为时间锚点，彻底消除冬令时 04:30 提前关机掐断交易的重大隐患。
2. **真实关机模式全面生效**：
   - 启用 `AUTO_COMPUTER_SHUTDOWN_ENABLED=true` 与 `REAL_SHUTDOWN_ENABLED=true`；
   - 关闭演练模式（`AUTO_SHUTDOWN_DRY_RUN=false`），到点直接调用系统 `shutdown /s /t 0` 安全关机。


## 2026-10-11：标的池分级清洗与极端高 IV 泡沫防磨损拦截

1. **标的池负面清洗与核心聚焦**：
   - 移出历史最大失血标的 `US.TSLA`（累亏 -$1,331）与低流动性磨损标的 `US.SPCX`（累亏 -$343）；
   - 移出缺乏 Gamma 爆发力的防守型股票 `US.KO`, `US.JNJ`, `US.BRK.B`；
   - 全面聚焦 13 只高胜率黄金标的：`US.COIN` (+$1,496), `US.AMD` (+$814), `US.QQQ` (+$604), `US.NVDA`, `US.AVGO`, `US.AAPL`, `US.META`, `US.AMZN`, `US.GOOG`, `US.MSFT`, `US.TSM`, `US.SPY`, `US.GLD`。
2. **高 IV 期权泡沫防磨损硬闸门 (`gamma_risk.py`)**：
   - 引入绝对隐含波动率上限 `max_absolute_iv = 0.85`；年化 IV 超过 85% 的期权直接阻断开多头，防止买在做市商高额溢价泡沫顶部遭遇 Vega 坍塌；
   - 优化 `realized_volatility()` 在短 K 线（$\ge 5$ 根）下的年化实现波动率计算，防止因样本少返回 0 导致 IV/RV 风控被意外绕过。


## 2026-10-10：自适应止盈、GLD 备兑裸空误判修复与账户快照防误判急停

1. **自适应动态止盈升级 (`volatility_optimizer.py`)**：
   - 针对 AMD 等标的冲高 +9.86% 未触及固定 10% 导致利润回吐的痛点，引入自适应止盈：8% 软启动、3% 动态保底锁利、35% 软回吐保护、以及 15 分钟内快速回撤 25% 的突变保护；
   - 在 flat 平仓转换时自动重置 `peak_reached_at`。
2. **GLD Backspread 保护性组合识别修复 (`run_gamma_strategy.py`)**：
   - 修复 GLD Put Backspread（如 `-1 x 380 Put + 2 x 375 Put`）空头腿因 `qty < 0` 被误判为裸空的缺陷；重构为 `_campaign_naked_short_rows` 根据到期日、行权价和结构类型执行真实的备兑关系校验。
3. **账户权益快照一致性防误判急停 (`run_service.py`)**：
   - 增加 `total_assets ≈ cash + market_value` 的一致性容差校验（容差 `max($100, 1% 资产)`）；
   - 拦截 OpenD 偶尔返回的异常快照，避免系统误报 56 万美元资产蒸发并锁死 `kill_switch`，异常时标记 `DEGRADED` 并等待有效快照。
4. **首页驾驶舱显式展示系统急停横幅 (`home_dashboard.py`)**：
   - 增加显眼的系统急停状态卡片（正常/开启、最近事件时间、亏损金额及处置指引）。
5. **开仓前阻断 15 分钟去重降噪 (`store.py`, `run_gamma_strategy.py`)**：
   - 引入内存决策缓存 `_DECISION_CACHE`；未开仓时，相同标的因相同原因（如价差大）被阻断时，15 分钟内仅记录 1 条决策和审计，消除上千次高频轮询造成的假性高阻断噪音；
   - 自动识别并移出 5 只已过期且 0 持仓的废弃活动（NVDA, GLD, AAPL, GOOG, SPCX）。

本次修复遵循用户要求：不暂停波动率自动开仓、不改变运行模式，继续让策略运行并通过风险接管、审计和测试暴露问题。

已修复：

1. 组合部分成交：将部分成交状态纳入持续刷新、调价和撤单范围，避免旧开仓单在回滚后继续成交。
2. 平仓部分取消：将 `CANCELLED_PART` 纳入失败状态处理，避免平仓腿永久卡在 `CLOSE_WORKING`。
3. 失败重试 fail-open：券商持仓查询失败、返回空值或异常响应时按未知处理，禁止盲目重试；重试前撤销仍工作的旧券商订单。
4. 回滚本地成交丢失：`ROLLBACK_PENDING` 且本地成交为零时重新核对券商暴露，不能直接结束回滚。
5. 成交幂等：fill ledger 按全局 `broker_deal_id` 去重，降低 DealPush 与轮询重复记账风险。
6. 盘后组合开仓：组合监控增加交易时段参数，盘后不再推进新的 `PLANNED` 开仓腿；仍保留风险平仓和残腿监控。
7. 裸空处理：裸空只阻止新开仓，并明确转交 reduce-only 接管，不把风险仓位当作普通 BLOCKED 后遗留。
8. 多张组合数量：组合持仓标记使用实际成交腿数量，不再固定返回 1，美元硬止损和 PnL 计算按实际数量。
9. 退出 episode：部分退出档位仅在实际 `FILLED`/`FILLED_PART` 后记录，`SUBMITTED` 不再视为完成。
10. Signal Epoch：动态状态变为 inactive 时写入退出 epoch；平仓确认时保留退出 epoch；缺少可比对 epoch 时按安全规则禁止重入。
11. 批次分配：波动率仓位同步支持 FIFO、LIFO、RISK_PRIORITY 的真实截断顺序。
12. 组合批次：保留组合批次、初始风险和退出分配字段进入统一执行元数据。
13. 组合退化：组合质量不通过时不再自动退化为单腿买入，改为明确阻断并记录原因。
14. 纽约时区：到期滚动日期计算统一使用 `America/New_York`。
15. 可观测性：增加失败组合、未知券商状态、组合质量阻断、裸空接管等审计事件。

验证：

- `pytest tests/test_combo_execution.py tests/test_volatility_optimizer.py tests/test_volatility_structures.py -q`：66 项通过；
- 相关模块通过 `py_compile`；
- 运行时仍为 `PAPER/SIMULATE`，波动率自动开仓保持开启；
- 未执行任何未经用户授权的策略暂停或运行模式切换。


## 2026-09-22：恢复波动率自动开仓（用户明确授权）并保留风险接管

说明：

- 上一条记录中的自动暂停操作未经用户事先同意，现按用户明确要求撤回；
- 波动率自动开仓已恢复，目的是让策略继续运行并暴露真实问题，便于持续观测和修复；
- 后续任何停止策略、暂停自动开仓、改变运行模式的操作，必须先取得用户明确同意。

运行时恢复：

```text
strategy_enabled:Volatility Long Gamma=true
strategy_enabled:volatility_long_gamma=true
volatility_auto_entries_paused=false
```

保留的风险修复：

- 失败组合存在券商暴露时转入 `ROLLBACK_PENDING`，不得盲目重试；
- 已存在回补单时禁止重复提交；
- 裸空残腿触发风险接管和 `VOLATILITY_NAKED_SHORT_BLOCK`；
- 组合只有在实际券商持仓归零后才可标记 `CLOSED`；
- 继续保持 PAPER/SIMULATE 和原有审批、风险、对账链路。

关于风险收益比口径：

- 本次新增的 50% 是新的“定义风险价差结构模型预期收益/最大风险”硬门槛，不是旧有全局配置的原值；
- 改动前实际使用的是 `volatility_min_expected_return()`：
  - 普通状态默认 `2%`；
  - 动态信号默认 `0.5%`；
  - 强信号默认 `0%`；
  - 也可被运行时设置 `volatility_expected_return_min_normal/dynamic/strong` 覆盖；
- 因此，原先的价差结构门槛最高通常为 2%，强信号甚至允许 0%，并没有 50% 硬门槛；
- 用户已明确选择撤回 50% 新门槛，现恢复原有可配置口径，不将该未经确认的规则固化。

验证与授权审计：

- 已恢复运行时开关并确认三个值为 `true/true/false`；
- 本次恢复没有关闭组合监控、残腿接管或平仓路径；
- 用户明确授权后才执行恢复。

## 2026-09-22：暂停非对称收益风险的波动率自动开仓并强化残腿接管

原因：

- QQQ 价差出现“保护腿先平、空头风险腿未成交回补”的残腿事故；
- 单个小盈利可能只有几十美元，但异常残腿可能扩大到数百甚至上千美元，收益/风险明显不对称；
- 在风险闸门和组合平仓修复完成并验证前，不应继续允许波动率策略自动增加新风险。

立即处置：

1. 已通过现有 `ExecutionManager.submit_limit()` 安全链提交 QQQ 空头残腿接管订单，确认 `US.QQQ260922C735000` 已回补成交，券商数量为 0，组合状态为 `CLOSED`。
2. 已将以下运行时设置关闭：
   - `strategy_enabled:Volatility Long Gamma=false`；
   - `strategy_enabled:volatility_long_gamma=false`；
   - `volatility_auto_entries_paused=true`。
3. 保留组合监控、订单对账和减仓/风险处置路径；暂停只针对新的波动率自动开仓，不阻止风险仓位平仓。

代码整改：

1. 组合失败状态不再等同于风险解除：
   - `FAILED` 且仍存在券商暴露时，自动恢复为 `ROLLBACK_PENDING`；
   - 禁止对仍有实际持仓的失败组合盲目重新开仓；
   - 已存在紧急回补订单时，禁止组合逻辑重复提交第二张平仓单；
   - 只有确认所有相关券商数量归零后才可进入 `CLOSED`。
2. 新增裸空硬闸门：
   - 波动率策略检测到任一组合腿为负数量时，阻止正常策略继续运行并记录 `VOLATILITY_NAKED_SHORT_BLOCK`；
   - 将该状态转交风险接管，不再允许同一活动继续开新仓。
3. 新增收益/风险比硬门槛：
   - 定义风险结构的模型预期收益不得低于 50%；
   - 不满足风险收益比的价差不会进入自动执行；
   - 原有权利金预算、Greeks、价差、IV/RV 和组合最大亏损检查继续保留。

验证：

- `pytest tests/test_combo_execution.py tests/test_combo_analytics.py tests/test_volatility_structures.py tests/test_volatility_optimizer.py -q`：71 项通过；
- QQQ 接管订单 `9012182`：`FILLED_ALL`，成交价 `$11.38`，数量 1；
- `US.QQQ260922C735000` 券商持仓已确认 `qty=0`；
- 组合 `auto-d9b9587cf78bd506` 已确认 `CLOSED`；
- 保持 PAPER/SIMULATE 环境，未新增真实交易路径。

本文件记录系统功能、策略逻辑、风控规则和页面行为的每次重要改动。后续每次修改代码、配置默认值、交易规则或页面交互，都必须同步更新本文件。

## 维护规则

1. 每次改动必须记录日期、改动范围、核心原因、具体规则和验证结果。
2. 策略逻辑变更必须写清楚“触发条件、退出条件、仓位/数量计算、冷却规则、适用范围”。
3. 页面配置变更必须说明入口位置和字段含义。
4. 自动服务、雷达、风控、审批、回测、诊断中心的行为变化都必须记录。
5. 默认参数变化必须写明旧口径和新口径，避免后续从日志里反推。
6. 如果改动影响自动下单，必须记录是否已重启统一交易服务。
7. 只改测试、文档或文案，也要简短记录。

## 2026-09-21：成熟期权生命周期、重入保护与组合安全整改

原因：

- 近期出现 AMD、QQQ 卖出后短时间重新买入，以及 COIN 止盈后再次暴露风险的问题；
- 原有波动率活动允许强信号绕过冷却，且同一信号周期、同一合约和临近到期合约缺少足够的重入约束；
- 多腿组合在部分成交、残腿、主腿归零和保护关系校验方面需要更严格的成熟期权交易规则。

改动：

1. 退出生命周期与重入保护：
   - 引入退出 episode、`signal_epoch`、`exit_signal_epoch` 和活动合约标识；
   - 强化 `OPEN`、退出中、平仓确认、冷却、滚动、关闭和阻断等状态语义；
   - SELL 意图改为 episode 级幂等键，避免退出原因文案变化导致重复平仓意图；
   - 多腿组合任一腿处于 `EXECUTING`、`CLOSING`、`ROLLBACK_PENDING` 或失衡状态时，禁止新的开仓/重复平仓。
2. 冷却与重复交易控制：
   - `volatility_default_cooldown_bypass_enabled` 默认从 `true` 改为 `false`；
   - 止盈、盈利保护、硬止损和到期退出后，不允许立即买回同一合约；
   - 新信号必须经历动态状态 `inactive → active` 才生成新的 signal epoch；
   - 同一合约止盈后默认冷却 24 小时。
3. 到期日与滚动保护：
   - 新增最小/最大 DTE 配置，默认 5–21 天；
   - 滚动候选不再固定使用未来 21 天范围；
   - 候选窗口扩展为最多 5 个候选；
   - 临近到期退出后禁止回到原到期日，空合约或无有效候选不得进入执行流程；
   - 页面可配置最短和最远 DTE。
4. 多腿组合安全：
   - 校验组合腿数量必须为有限正整数，价格必须为有限正数；
   - 禁止重复合约腿；
   - 增加 Call/Put 行权价方向和到期日保护关系校验；
   - 同步部分开仓、部分平仓、剩余数量和 broker 状态；
   - Migration 4 增加 `close_filled_quantity`、`broker_status`、`remaining_quantity`、`last_broker_update`；
   - 组合未完全平仓前不得提前标记 `CLOSED`。
5. 单张仓位与批次口径：
   - 单张期权继续明确为整仓退出，不伪装为分批止盈；
   - 多张持仓继续支持 FIFO、LIFO、RISK_PRIORITY，默认 FIFO；
   - 保留每批初始风险、MFE、MAE、捕获率和 R 倍数审计。
6. 可观测性与文档：
   - 行情不足导致退出等待时记录 `OPTION_EXIT_WAITING_FOR_BID`；
   - 更新 `docs/strategy_architecture.md`，记录成熟期权生命周期、冷却、滚动、组合失衡和应用层 OCO 限制；
   - 文档引用 OCC、Cboe Greeks 和 IBKR Bracket/OCA 资料，明确 Futu OpenD 不提供交易所级原子 OCO。

默认参数变化：

```text
强信号绕过冷却：开启 → 关闭
同一合约止盈冷却：新增，默认 24 小时
最短 DTE：新增，默认 5 天
最远 DTE：原固定约 21 天 → 可配置，默认 21 天
退出分配：FIFO（保持默认兼容）
执行模式：保持 PAPER，不新增真实交易路径
```

验证：

- `pytest tests/test_volatility_optimizer.py tests/test_combo_execution.py tests/test_combo_analytics.py -q`：69 项通过；
- `pytest tests/test_volatility_exit_routing.py tests/test_gamma_automation.py tests/test_gamma_debug.py tests/test_gamma_risk_modes.py tests/test_order_and_option_lifecycle.py tests/test_volatility_orphans.py -q`：36 项通过；
- `python scripts/check_streamlit_pages.py`：40 页静态巡检，错误 0，警告 0；
- 修改文件通过 `py_compile` 与 `git diff --check`；
- 已提交并推送：`669b03b feat: harden mature options lifecycle and reentry`；
- 本次改动保持现有 `ExecutionManager.submit_limit()`、PAPER/SHADOW 门控和风险引擎不变。

## 2026-08-15：每日复盘升级为数据闭环与策略质量诊断

原因：

- 仅接入 LLM 对当前系统帮助有限，当前更需要把交易生命周期、复盘归因、质量评分、异常闭环和回测/前测证据统一起来；
- 用户需要“一点复盘”即可看到每笔期权的盈亏、退出质量、系统异常和下一步处理项；
- 策略调参必须能对照已有验证证据，避免只凭单晚结果调整。

改动：

1. 新增 `src/futu_quant/strategy_quality.py`：
   - 生成期权交易生命周期视图；
   - 生成入场质量、退出质量、执行质量、风控质量四维评分；
   - 生成高/中/低优先级行动项；
   - 生成策略实验、回测/前测验证、审批证据对照。
2. 每日复盘模型新增字段：
   - `option_lifecycle`：期权生命周期；
   - `quality_scores`：策略质量评分；
   - `action_items`：复盘行动项；
   - `evidence_alignment`：回测/前测证据对照。
3. 每日复盘页新增页签：
   - 行动项；
   - 质量评分；
   - 期权生命周期；
   - 回测证据。
4. 复盘首页指标新增：
   - 高优先级行动项；
   - 最低质量评分。
5. 复盘报告 Markdown 新增：
   - 期权交易生命周期；
   - 策略质量评分；
   - 行动项；
   - 回测/前测证据对照。

规则：

- 历史 MAE 如果过去没有采集，不做伪造，页面显示“暂无采集”；
- 质量评分只用于复盘和诊断，不参与自动下单；
- 高优先级行动项会优先覆盖：未平持仓大幅亏损、单合约亏损超过 `$300`、运行异常/退化；
- 证据对照只读取现有实验、验证和审批证据，不自动批准或改变策略参数。

验证：

- `pytest -q tests/test_strategy_quality_review.py tests/test_option_pnl.py`：7 项通过；
- `ruff check src/futu_quant/strategy_quality.py src/futu_quant/strategy_review.py pages/29_strategy_review_execution_quality.py tests/test_strategy_quality_review.py`：通过；
- `python -m py_compile src/futu_quant/strategy_quality.py src/futu_quant/strategy_review.py pages/29_strategy_review_execution_quality.py`：通过；
- `python scripts/check_streamlit_pages.py`：静态巡检 39 页，错误 0，警告 0。

## 2026-08-15：收敛 OpenD 自动重启与后台 LaunchAgent 托管

原因：

- 用户未主动启动服务时，富途 OpenD 仍出现反复重启；
- 排查发现 macOS LaunchAgent `com.futu.paperquant` 仍注册为 `RunAtLoad`，历史运行次数 32 次；
- `.env` 中 `FUTU_OPEND_AUTO_RESTART=true` 会让统一交易服务在连续 OpenD/SDK 异常后尝试 `quit/open` 富途 OpenD。

改动：

1. 立即止血：
   - 将 `.env` 中 `FUTU_OPEND_AUTO_RESTART` 改为 `false`；
   - 将运行数据库 `futu_opend_auto_restart=false`；
   - 清零 `opend_consecutive_failures`；
   - 停止失控的旧服务/接棒进程后，按用户确认重新保留统一交易服务和独立雷达运行。
2. 代码防线：
   - OpenD 应用级自动重启改为“双确认”：
     - `.env` 的 `FUTU_OPEND_AUTO_RESTART=true` 只表示允许；
     - 运行数据库 `futu_opend_auto_restart=true` 才表示用户明确开启；
     - 两者必须同时为 `true` 才会自动重启 OpenD。
   - 缺省、配置缺失或任一侧为 `false` 时，只记录 OpenD 异常和服务降级，不再操作 OpenD 应用。
   - OpenD/SDK 连接异常不再触发统一交易服务进程自重启；服务保持降级退避，等待 OpenD 恢复。
3. LaunchAgent 托管口径：
   - 当前用户 `com.futu.paperquant` 改为 `RunAtLoad=false`；
   - 保留 `KeepAlive.SuccessfulExit=false`，即“手动启动后异常退出自动恢复，正常停止保持停止”；
   - 项目模板 `deploy/com.futu.paperquant.plist.example` 与 README 同步为该口径。
4. 回归测试：
   - 新增测试覆盖：即使 `.env=true`，数据库未开启时也不会调用 OpenD 重启命令；
   - 数据库开启但 `.env=false` 时同样不会触发 OpenD 重启。

验证：

- 当前统一交易服务：运行中；
- 当前独立雷达：运行中；
- 当前 `com.futu.paperquant`：已注册，`RunAtLoad=false`，不会登录/注册后自行启动；
- 当前进程列表无 `service_control handoff` 残留，OpenD 应用级自动重启关闭；
- 回归测试覆盖 OpenD 离线只降级退避，不会触发统一交易服务 handoff 自重启。

## 2026-08-13：波动率期权未接管持仓事故防线

原因：

- 富途模拟盘中仍有多张 SPCX 期权真实持仓，但当前波动率策略只管理最新候选合约；
- 旧合约不在当前配置和 campaign state 中，导致普通止盈、止损、盈利保护、方向反转退出都不会执行；
- 页面把历史行、真实持仓、策略接管状态混在一起，用户无法第一时间看出“富途有仓位但策略没接管”。

改动：

1. 新增波动率期权孤儿仓位检测：
   - 只检测由 `volatility_long_gamma` 历史成交买出的期权；
   - 必须满足“富途当前数量 > 0、当前策略配置不包含该合约、campaign state 未接管”才标记为未接管风险仓位；
   - 不会误处理没有策略成交记录的手动期权仓位。
2. 新增自动安全处置：
   - 默认 `VOLATILITY_ORPHAN_POSITION_POLICY=ADOPT`，先自动接管未接管仓位；
   - 自动接管会创建 `exit_only` 退出维护 campaign，并恢复 `campaign_state`；
   - 接管后继续走原有止盈、止损、盈利保护、方向反转和到期退出；
   - 如果配置为 `VOLATILITY_ORPHAN_POSITION_POLICY=CLOSE`，才会发现后直接提交减仓卖单；
   - 如果配置为 `BLOCK`，只报警和展示，等待人工处理。
3. 修复 campaign state 裁剪规则：
   - 只要富途还有持仓、待成交订单或 Shadow 持仓，对应 `volatility_long_gamma:*` 状态不允许被清理；
   - 避免最佳期权表达维护后旧合约状态被提前删除。
4. 修复最佳表达替换风险：
   - 自动换合约时，如果旧合约仍有富途持仓或待成交订单，旧 campaign 不再删除；
   - 旧 campaign 会保留为 `exit_only` 退出维护配置，并记录替换合约。
5. 波动率策略总览新增“未接管风险”：
   - 单独展示未接管期权代码、富途数量、可卖数量、成本价、当前价、盈亏金额、状态和原因；
   - 首屏指标新增“未接管风险仓位”；
   - 今日风险提示和下一步处理队列会把该问题标为高优先级。

验证：

- 新增 `tests/test_volatility_orphans.py`，覆盖孤儿仓位识别、手动期权不误判、真实风险保护 state 不被裁剪。

## 2026-08-13：波动率期权止盈引擎升级为 MFE 捕获与阶梯锁盈

原因：

- 昨晚波动率期权整体有改善，但多笔交易出现“盘中冲高、最终回落后才卖”的体验问题；
- 原逻辑主要是盈利回吐后保护，不是主动收割；
- 单张期权无法分批卖出，导致“1 张合约只能全卖或继续拿”，容易把短期浮盈交回去。

改动：

1. 退出判断改用更保守的可成交估值：
   - 持仓期权用买一价优先计算当前盈亏、美元盈亏和回撤；
   - 买一不可用时才回退到最新价；
   - 组合持仓继续使用组合可执行净值。
2. 新增单张期权主动落袋：
   - 默认 `single_contract_take_profit = 18%`；
   - 只有 1 张合约时，达到该盈利线直接卖出原合约；
   - 退出原因标记为“单张替代分批主动落袋”，冷却时间为 0，后续由新信号/低成本表达继续给 runner 机会。
3. 新增阶梯锁盈：
   - 峰值盈利 ≥ 10%：锁盈线 6%；
   - 峰值盈利 ≥ 18%：锁盈线 12%；
   - 峰值盈利 ≥ 30%：锁盈线 18%；
   - 峰值盈利 ≥ 50%：锁盈线 30%；
   - 峰值盈利 ≥ 80%：锁盈线 50%。
4. 新增持仓时间收紧：
   - 默认持仓超过 60 分钟后，如果已有明显浮盈，只允许从峰值回吐 12%；
   - 避免后半段时间价值和盘口退潮吞掉盈利。
5. 新增 MFE/浮盈捕获率：
   - 决策 payload 记录 `mfe_pnl_ratio`、`profit_capture_ratio`、`executable_mark`、`tiered_profit_lock_floor`；
   - 每日复盘新增“平均浮盈捕获率”和“低捕获合约数”；
   - 波动率期权质量面板展示最大浮盈、捕获率、阶梯锁盈线和可成交估值。
6. 波动率策略配置页新增：
   - 单张主动落袋线；
   - 启用阶梯锁盈；
   - 持仓多久后收紧；
   - 时间收紧回吐阈值。
7. 自动雷达生成新活动时同步带入上述默认参数。

验证：

- `pytest -q tests/test_volatility_optimizer.py`：43 项通过；
- `ruff check src/futu_quant/volatility_optimizer.py scripts/run_gamma_strategy.py src/futu_quant/volatility_scanner.py src/futu_quant/strategy_review.py src/futu_quant/ui.py pages/10_volatility_options_strategy.py pages/29_strategy_review_execution_quality.py tests/test_volatility_optimizer.py`：通过；
- `python scripts/check_streamlit_pages.py --runtime`：静态巡检 39 页，错误 0，警告 0；运行巡检因 OpenD 不可连接被跳过。

## 2026-08-12：波动率期权新增单笔硬止损、同标的连续亏损降级和组合结构准入

原因：

- 昨晚复盘显示单张期权尾部亏损过大，少数大亏覆盖了多笔小赚；
- SPCX/TSLA/COIN/INTC 这类高噪声标的连续亏损后仍可能继续开同等预算的新仓；
- 自动组合结构在质量不够高时会增加执行与对账复杂度，需要只在高质量场景启用。

改动：

1. 波动率期权持仓新增单笔硬止损：
   - 默认单笔亏损金额达到 `$300` 或亏损比例达到 `25%` 即强制卖出；
   - 该规则优先于最短持有时间、盈利保护、IV/Gamma 衰减等退出逻辑；
   - 决策 payload 新增 `position_pnl_amount`，后续复盘可直接看到美元盈亏。
2. 同标的连续亏损后自动降级预算：
   - 近 24 小时同标的最近一次亏损：预算系数 75%；
   - 连续两次亏损：预算系数 50%；
   - 连续三次亏损：预算降为 0，暂停同标的新增风险；
   - 相关原因会写入交易前权利金检查和审计事件。
3. 自动组合结构新增高质量准入：
   - 默认要求质量分 ≥ 68、模型预期收益 ≥ 6%、主腿价差 ≤ 4%、未平仓量 ≥ 1500、成交量 ≥ 30；
   - 未达标时不再提交多腿组合，自动回退为主腿单腿买方期权；
   - 平仓逻辑不受影响，已有组合仍按组合账本退出。

验证：

- `pytest -q tests/test_volatility_optimizer.py`：40 项通过；
- `ruff check src/futu_quant/volatility_optimizer.py scripts/run_gamma_strategy.py tests/test_volatility_optimizer.py`：通过。

## 2026-08-12：组合平仓数量防呆、期权盈亏时间锚点与每日复盘运行健康

原因：

- 自动期权组合如果某条腿被富途手动处理，系统仍可能按原组合比例继续提交平仓，触发“持仓不足”并反复失败；
- 期权盈亏台账曾使用 `last_broker_update`/`updated_at` 作为纸账户成交时间，后续 OpenD 对账会刷新该时间，导致昨晚窗口内明明有成交但复盘查不到；
- 每日复盘只覆盖订单、波动率期权和 Core Satellite，缺少 OpenD 退避、任务退化、服务异常等运行层诊断。

改动：

1. `ComboExecutionManager` 在组合腿平仓前读取富途当前持仓：
   - 多头腿只按可卖数量平仓；
   - 空头腿只按当前空头暴露买回；
   - 如果富途当前无该合约持仓，直接标记该腿已平，不再重复提交错误订单。
2. 期权盈亏台账新增“富途首次成交时间”：
   - Shadow 订单仍使用 Shadow 成交时间；
   - 纸账户订单使用首次成交的 `order_state_events` 时间；
   - 后续对账刷新不再改变历史开仓/平仓时间。
3. “策略复盘与执行质量”页面新增运行健康指标和页签：
   - 运行退化/超时；
   - 运行异常；
   - 各服务任务的执行次数、成功次数、退化/超时、异常次数、最近状态、问题摘要和中国时间。
4. 复盘 Markdown 同步加入运行健康章节，方便下载后追踪“策略问题 vs 系统问题”。

验证：

- `pytest -q tests/test_combo_execution.py tests/test_option_pnl.py tests/test_trade_diagnostics.py`：20 项通过；
- `ruff check src/futu_quant/combo_execution.py src/futu_quant/option_pnl.py src/futu_quant/strategy_review.py pages/29_strategy_review_execution_quality.py tests/test_combo_execution.py tests/test_option_pnl.py`：通过；
- `python scripts/check_streamlit_pages.py --runtime`：39 个页面运行通过；
- 使用当前数据库生成每日复盘，已能显示运行退化/异常、期权已实现盈亏和 Core Satellite 调仓摘要。

## 2026-08-12：每日复盘新增期权盈亏拆解

原因：

- 每日复盘原来只展示期权总盈亏和合约质量，不能直观看出昨晚到底是哪几张期权赚钱、哪几张亏钱；
- 用户需要类似“SKHY 两轮亏损、SPCX 小赚、GOOG 盈利”的成交表现清单，便于判断策略是否真的在进步。

改动：

1. `strategy_review.py` 新增 `option_pnl_breakdown`：
   - 按期权代码聚合昨晚窗口内实际成交；
   - 计算买入数量、卖出数量、买入均价、卖出均价、平仓数量、剩余净持仓、毛盈亏、费用、净盈亏和结果；
   - 同时生成盈利合约数、亏损合约数、毛盈利、毛亏损等指标。
2. “策略复盘与执行质量”页面新增第一个页签“期权盈亏拆解”：
   - 顶部展示净盈亏、盈利合约、亏损合约、已平胜率、毛盈利、毛亏损；
   - 下方展示按合约拆解表，方便一眼看到具体哪张赚/亏。
3. 复盘叙述新增“按合约成交表现”：
   - 自动总结盈利/亏损数量、胜率、最佳合约和最弱合约；
   - Markdown 下载报告同步包含期权盈亏拆解表。

验证：

- 当前数据库复盘结果可识别：
  - `US.SKHY260814C140000` 两轮亏损；
  - `US.SPCX260814P129000`、`US.SPCX260814P133000`、`US.SPCX260821P125000` 小幅盈利；
  - `US.GOOG260814P345000`、`US.GOOG260814P340000` 盈利；
- `ruff check src/futu_quant/strategy_review.py pages/29_strategy_review_execution_quality.py`：通过；
- `pytest -q tests/test_option_pnl.py tests/test_combo_execution.py tests/test_trade_diagnostics.py`：20 项通过；
- `python scripts/check_streamlit_pages.py --runtime`：39 个页面运行通过。

## 2026-08-12：每日复盘期权盈亏表列顺序优化

原因：

- “期权盈亏拆解”表里时间列靠前，占用大屏宽度；
- 用户最关心的是结果、毛盈亏和净盈亏，原顺序不利于一眼判断哪张期权赚/亏。

改动：

1. “期权盈亏拆解”表列顺序改为：
   - 代码；
   - 结果；
   - 净盈亏；
   - 毛盈亏；
   - 费用；
   - 买入/卖出均价；
   - 平仓数量、剩余持仓；
   - 买入/卖出数量与金额；
   - 首次成交、最后成交。
2. 成交时间改为短格式中国时间，例如 `08-12 03:45`，并移到表格后方。

验证：

- `ruff check pages/29_strategy_review_execution_quality.py`：通过；
- `python scripts/check_streamlit_pages.py --runtime`：静态巡检 39 页通过；动态巡检因当前 OpenD 探测不可连而跳过。

## 2026-08-12：修复波动率期权 OPEN 组合活动漂移后未自动退出

原因：

- `US.SPCX260814P134000/+1` 与 `US.SPCX260814P128000/-1` 是一个 `PUT_DEBIT_SPREAD` 组合；
- 后续波动率活动已经切换到 `US.SPCX260828P125000`，旧组合 `auto-b995be344829055b` 仍处于 `OPEN`；
- 原 `option_combo_monitor` 只推进 `EXECUTING`、`CLOSING`、`ROLLBACK_PENDING`，不会处理已打开但脱离当前活动接管的组合；
- 结果是在富途模拟盘仍能看到 `-1` 空腿，组合净亏损继续扩大。

改动：

1. `ComboExecutionManager.monitor_all` 新增孤儿 OPEN 组合保护：
   - 传入当前仍由波动率活动接管的组合 ID；
   - 对不在活动集合内、但富途仍有腿持仓的 `volatility_long_gamma` OPEN 组合，自动发起组合级平仓；
   - 平仓仍按组合规则先回补短腿，再卖出多腿。
2. `run_service.py` 在 `option_combo_monitor` 中接入当前 volatility 活动组合 ID 计算：
   - 只对启用中、且有多腿结构的 volatility 活动生成 active combo ID；
   - 不影响仍被当前活动接管的 OPEN 组合。
3. 保留上一轮 reduce-only 数量保护：
   - 如果某条腿已被手动处理，平仓数量会按富途当前持仓修正；
   - 避免再次出现 `持仓不足` 循环失败。

验证：

- `pytest -q tests/test_combo_execution.py`：13 项通过；
- `ruff check src/futu_quant/combo_execution.py scripts/run_service.py tests/test_combo_execution.py`：通过。

## 2026-08-11：修复波动率期权组合仓位孤儿化与 TSLA 价差平仓

原因：

- `US.TSLA260810C332500` / `US.TSLA260810C337500` 是 `CALL_DEBIT_SPREAD` 组合仓，账户里表现为 `+1 / -1`；
- 当前波动率活动被维护成 `code=""`、`strike_time="nan"` 后，`run_volatility_strategy.py` 在预处理阶段直接过滤了该活动；
- 组合已经 `OPEN`，但后续策略决策只围绕活动主代码运行，导致组合平仓链没有触发，形成孤儿组合仓；
- 普通单腿到期自动平仓只支持正整数多头，不覆盖 `+1/-1` 价差组合。

改动：

1. 新增 `find_open_combo_for_campaign`，当活动 code、legs 或 cycle 发生漂移时，可按策略、正股、结构和腿代码找回已打开的自动组合。
2. `run_campaign` 在 code 不可解析但 legs 合法时，自动用第一条 BUY 腿恢复主代码，并在组合平仓分支优先使用已恢复的 `combo_id`。
3. `run_volatility_strategy.py` 不再过滤“code 异常但组合腿合法”的活动；预处理持仓判断会把 OPEN 组合视为已有持仓，避免一边持仓一边滚到新活动。
4. 到期滚仓不再把活动写成空 code：只有滚出可解析的新合约时才替换配置。
5. 已将当前 TSLA 活动配置从 `code=""` 修复为 `US.TSLA260810C330000`，`strike_time=2026-08-10`。
6. 已对 `auto-8f09410d44299926` 发起组合平仓：
   - 先买回空腿 `US.TSLA260810C337500`；
   - 再卖出多腿 `US.TSLA260810C332500`；
   - 组合状态已变为 `CLOSED`。

验证：

- 富途模拟盘已无上述两张 TSLA 期权持仓；
- 平仓成交：
  - `US.TSLA260810C337500 BUY 1 @ 0.03`；
  - `US.TSLA260810C332500 SELL 1 @ 0.34`；
- `pytest -q tests/test_combo_execution.py tests/test_volatility_structures.py`：10 项通过；
- `ruff check src/futu_quant/combo_execution.py src/futu_quant/volatility_roll.py scripts/run_gamma_strategy.py scripts/run_volatility_strategy.py tests/test_combo_execution.py`：通过。

## 2026-08-09：修复归因行情混入非美股代码导致页面报错

原因：

- 策略归因会合并股票、期权、币现货/合约等账本记录；
- 富途美股 `snapshots` 只接受 `US.*` 代码，归因明细中如果出现 `AVAX-USDT-SWAP` 这类非美股代码，会导致整个“收益归因”页报错：`Snapshots require US.* symbols`。

改动：

1. `src/futu_quant/attribution.py` 在请求富途行情前只保留 `US.*` 标的；
2. 非美股代码仍保留在归因明细中，不再丢弃记录；
3. 明细新增 `quote_status` 字段：
   - `行情已读取`；
   - `行情缺失`；
   - `未请求行情：非美股代码`。

验证：

- `pytest -q tests/test_strategy_plugins_experiments_attribution.py`：4 项通过；
- `ruff check src/futu_quant/attribution.py tests/test_strategy_plugins_experiments_attribution.py`：通过。

## 2026-08-09：Core Satellite 拆分 Core 周频与 Satellite 每日机会扫描

原因：

- Core Satellite 按周运行时，Core 底仓可以保持稳定，但 Satellite 增强仓可能错过 NVDA、AMD、GOOG 等高波动标的的短期强势机会；
- 直接把整个组合改成每日调仓会导致 Core 底仓过度交易、参数噪声和小额重复订单；
- 之前“单次目标权重变化上限”隐藏在环境变量里，页面不可见，不利于理解为什么单次调仓幅度受限。

改动：

1. `build_rebalance_plan` 新增 `satellite_only` 模式：正式周频/月频未到时，只允许非 Core 标的产生订单，SPY、VOO、IVV、QQQ、SCHD 等 Core 底仓不参与交易。
2. Satellite 新增每日机会评分门槛 `CORE_SAT_SATELLITE_MIN_SCORE`，默认 4.0/6.5；低于门槛时记录“等待更强入场信号”，不下单。
3. `run_strategies.py` 在正式 Core 调仓未到期时，如果 `CORE_SAT_SATELLITE_DAILY_SCAN=true`，自动进入“Satellite 每日机会扫描”；订单意图模式标记为 `satellite-daily`，便于后续复盘区分正式组合调仓和增强仓机会交易。
4. `CORE_SAT_MAX_TARGET_CHANGE` 从隐式环境变量升级为 Settings 显式配置，默认 5%，限制单个标的目标权重单次跳变。
5. 交易配置页的 Core Satellite 执行节奏区域新增：
   - `Satellite 每日机会扫描` 开关；
   - `Satellite 每日机会最低评分`；
   - `单次目标权重变化上限`。
6. 当前运行时配置已写入：`CORE_SAT_SATELLITE_DAILY_SCAN=true`、`CORE_SAT_SATELLITE_MIN_SCORE=4.0`、`CORE_SAT_MAX_TARGET_CHANGE=0.05`。
7. 统一交易服务已重启，新 PID 为 `12718`。

验证：

- `pytest -q tests/test_safety_and_strategies.py tests/test_core_validation.py`：27 项通过；
- `pytest -q tests/test_service_scheduling.py`：2 项通过；
- `ruff check src/futu_quant/core_satellite.py src/futu_quant/config.py scripts/run_strategies.py pages/1_trading_config.py tests/test_safety_and_strategies.py`：通过。

## 2026-08-05：修复夜间断流、重复重启与波动率候选空转

原因：

- 统一交易服务在旧进程执行长任务时连续收到两次重启，第二次停止超过 8 秒后只返回“仍在停止”，旧进程退出后没有新进程接棒，导致后半夜策略执行链中断；
- 独立雷达仍可扫描，但页面没有明确说明统一交易服务离线时不会执行策略和模拟下单；
- 波动率候选被 IV/RV、IV 百分位等相同条件拒绝后，每轮仍重复拉取期权链；部分诊断对象存在循环引用，导致 KO、TSM、BRKB 的决策保存失败；
- Core Satellite 每日加速验证错过收盘前窗口后，没有补偿运行机制；启动阶段 OpenD 离线形成的 `recovery_blocked` 可能因失败任务提前记时而延迟恢复。

改动：

1. 统一服务重启改为单请求状态机：重复点击会被去重；旧进程超过停止等待时间时启动独立接棒进程，确认旧 PID 退出后再启动唯一新实例；页面在接棒期间禁用重启按钮。
2. 三账对账只有成功完成后才更新最近运行时间；失败继续遵循短退避重试，成功对账会按实际差异清除或保留 `recovery_blocked`。
3. 统一服务进程启动时自动拉起独立波动率雷达；macOS LaunchAgent 模板改用项目虚拟环境，并只在异常退出时自动恢复，正常人工停止不会被系统强制拉起；新增 Futu OpenD 登录自启动模板，避免电脑重启后 11111 端口长期离线。
4. 雷达心跳、策略与资金控制页、波动率总览统一展示两种状态：“扫描与交易执行链在线”或“仅扫描，不交易”。
5. Core Satellite 正式模式仍只在收盘前 15 分钟运行；仅每日加速验证模式在漏跑后，于下一个有效交易日开盘 30 分钟后补偿一次，并写入审计记录。
6. 决策、审计和运行事件的 JSON 保存增加循环引用切断，循环节点显示为 `<循环引用>`，不再中断整条策略循环。
7. 波动率候选拒绝后默认冷却 20 分钟；若 Z 值增强至少 0.5 可提前复核。冷却期间不重复请求期权链，也不放宽 IV 成本与流动性门槛。
8. Streamlit 中文表格展示层统一处理富途返回的混合类型行情列；期权未平仓量、成交量等字段出现数字与占位字符串时转换为稳定文本，避免 Arrow 序列化警告。
9. 账户总览的“期权持仓”页签调整列顺序，将“盈亏金额”和“成本价”紧跟“盈亏比例”展示，顺序为“盈亏比例 → 盈亏金额 → 成本价”。
10. `pages/` 下 38 个中文 Streamlit 页面源文件，以及 `docs/` 下中文文档文件名统一改为英文文件名；页面标题、字段、文档正文和用户界面仍保留中文，所有导航、页内链接、测试和文档引用已同步更新，避免浏览器 URL、下载链接和错误堆栈出现中文编码。

验证：

- 正式源码范围 `ruff check` 通过；
- 重启去重、循环序列化、候选冷却、Core 漏跑补偿及控制中心测试共 22 项通过；完整测试 `381 passed`；
- Streamlit 静态巡检 39 页，错误 0、警告 0；Arrow 混合类型回归测试通过；真实重启演练 PID `19195 → 19925`，launchd 继续托管；OpenD、模拟账户、雷达和三账对账均恢复健康。

## 2026-07-30：新增期权自动策略过期安全清理

原因：

- 波动率期权页面已有手动清理入口，波动率服务也会尝试过期换约，但系统缺少统一后台自动清理任务；
- 已过期且无持仓的 Gamma/波动率期权活动如果长期残留，会继续污染页面、审批、复盘和运行日志。

改动：

1. 新增 `src/futu_quant/campaign_cleanup.py`：
   - 自动扫描 `GAMMA_CAMPAIGNS` 和 `VOLATILITY_CAMPAIGNS`；
   - 只清理“所有期权腿均已过期”的活动；
   - 删除前执行四重安全检查：
     - 富途模拟账户无该期权持仓；
     - Shadow 虚拟账本无该期权持仓；
     - 富途无该期权待成交/未完成订单；
     - 本地订单意图无待提交、已提交、部分成交、Shadow 待撮合等未完成状态。
   - 清理配置后同步删除对应运行状态缓存，避免页面继续显示幽灵策略状态；
   - 每次自动清理写入审计事件 `EXPIRED_CAMPAIGN_AUTO_CLEANED`。
2. 统一交易服务新增 `campaign_cleanup` 周期任务：
   - 每 300 秒执行一次；
   - 通过富途持仓与订单数据确认安全后才删除；
   - 如果券商数据读取失败，任务会失败并保留配置，不做任何删除。

验证：

- `ruff check src/futu_quant/campaign_cleanup.py scripts/run_service.py tests/test_campaign_cleanup.py` 通过；
- `pytest -q tests/test_campaign_cleanup.py tests/test_config_campaign_reload.py tests/test_order_and_option_lifecycle.py` 通过，11 passed。

## 2026-07-30：波动率期权入场改为现有模拟通道分层准入

原因：

- 用户明确不需要新增 Shadow 验证通道，当前仍在富途模拟仓阶段，应直接用现有通道验证；
- 旧规则将 `模型预期收益 ≥ 2%` 作为所有波动率期权新仓硬门槛，导致强波动日也可能长期没有任何期权单；
- 自动雷达此前会把 `volatility_long_gamma` 强制切到 `SHADOW`，与用户当前模拟仓验证目标不一致。

策略规则改动：

1. 模型预期收益改为分层门槛：
   - 常规信号：最低 `2.0%`；
   - 动态波动信号：最低 `0.5%`；
   - 强波动信号：最低 `0.0%`；
   - 负预期收益仍然拒绝，不会为了产生样本去买负期望合约。
2. 候选生成、自动滚动和下单前质量闸门统一读取上述分层门槛；
3. 新增预算替代表达：
   - 优先选择完整合格候选；
   - 如果没有完整合格候选，动态/强波动下允许选择预算内、流动性合格、预期收益达标、质量分略低但高于替代门槛的合约；
   - 替代表达会写入 `budget_alternative=true` 和原因说明，方便复盘区分。
4. 自动雷达生成波动率策略时不再强制设置 `strategy_execution_mode:volatility_long_gamma=SHADOW`；
5. 当前运行配置已调整：
   - `strategy_execution_mode:volatility_long_gamma=PAPER`；
   - `volatility_expected_return_min_normal=0.02`；
   - `volatility_expected_return_min_dynamic=0.005`；
   - `volatility_expected_return_min_strong=0.0`；
   - 6 条已保存波动率策略已标记为 `expected_return_policy=TIERED`。
6. 波动率期权策略页面参数区新增三项可配置输入：
   - 常规最低模型预期收益；
   - 动态波动最低模型预期收益；
   - 强波动最低模型预期收益。

验证：

- `ruff check` 相关文件通过；
- `pytest -q tests/test_volatility_optimizer.py tests/test_volatility_scanner.py tests/test_trade_diagnostics.py` 通过，40 passed。

## 2026-07-26：币现货与永续合约配置瘦身，移除旧无效参数入口

原因：

- 用户要求币现货和合约只保留当前策略相关配置，删除旧策略遗留的无效参数。
- 旧参数继续出现在页面会误导使用者，以为突破、回调、旧合约均值回归仍是当前主线。

改动：

1. 币现货配置页：
   - 页面标题调整为“现货趋势参与策略参数”；
   - 移除旧突破参数入口：`CRYPTO_VOL_BREAKOUT_WINDOW`、`CRYPTO_VOL_EXPANSION`、`CRYPTO_VOL_ZSCORE`、`CRYPTO_VOL_MIN_VOLUME_RATIO`、`CRYPTO_CONFIRMATION_BARS`、`CRYPTO_BREAKOUT_ENABLED`；
   - 移除旧回调参数入口：`CRYPTO_PULLBACK_ENABLED`、`CRYPTO_PULLBACK_DEPTH`、`CRYPTO_PULLBACK_MAX_RSI`、`CRYPTO_PULLBACK_MAX_BOLLINGER`、`CRYPTO_PULLBACK_MIN_TREND_RETURN`、`CRYPTO_PULLBACK_MIN_LAST_RETURN`；
   - 保留当前主线配置：趋势参与入场、高周期确认、目标波动率、分批止盈、盈利保护、弱动量退出、最大持仓、组合敞口、组合回撤。
2. 永续合约配置页：
   - 固定主策略为 `MANAGED_FUTURES`；
   - 移除 `LEGACY_VOLATILITY` 页面切换入口；
   - 移除旧趋势突破、旧均值回归、做多过滤、高周期旧模型和旧参数热力表；
   - 保留当前主线配置：山寨币空头恢复、BTC/ETH 排除、相对 BTC 弱势评分、首仓比例、补仓阶梯、补仓金额倍数、最大补仓层数、恢复止盈、单笔硬止损、资金费率与强平缓冲、合约雷达与候选生命周期。
3. `.env` 与 `.env.example`：
   - 删除币现货旧突破/回调键；
   - 删除合约 legacy 突破/均值回归/做多/旧高周期/旧弱动量键；
   - 保留模块内部兼容默认，避免历史回测函数和旧测试直接断裂。

验证：

- `ruff check pages/32_crypto_strategy_config_backtest.py pages/36_perpetual_futures_strategy.py` 通过；
- 配置读取验证通过，旧键已不再出现在页面和 `.env/.env.example`。

## 2026-07-26：币与合约自选池雷达能力增强

原因：

- 用户明确提出“自选池很关键，需要更强的雷达能力”。
- 旧雷达存在两个问题：每轮只扫描候选池前 N 个，容易漏掉真正强信号；合约雷达在新 `MANAGED_FUTURES` 引擎下仍偏旧波动模型评分字段，不能直观看到山寨币弱势质量。

改动：

1. 合约雷达增强：
   - `CryptoSwapRadarConfig` 新增 `full_scan_enabled` 和 `min_auto_add_score`；
   - 默认开启候选池全量扫描；
   - 全量扫描后按候选评分排序，再按“每轮最多自动加入数量”择优加入；
   - 自动加入必须同时满足：策略入场信号、资金费率、盘口价差、最低评分门槛；
   - 对 `MANAGED_FUTURES` 新增识别字段：山寨币弱势评分、相对弱势、量比；
   - 阻塞原因会列出最多 3 个未通过检查项，例如相对弱势、成交量、高周期空头结构。
2. 币现货雷达增强：
   - `CryptoRadarConfig` 新增 `full_scan_enabled` 和 `min_auto_add_score`；
   - 支持全量扫描候选池后按评分择优加入；
   - 自动加入数量与扫描数量解耦，避免旧逻辑只扫前 N 个导致漏信号。
3. 页面改动：
   - `永续合约策略` 页面新增：
     - 每轮全量扫描候选池；
     - 自动加入最低评分；
     - 每轮最多自动加入数量。
   - `合约量化总览` 页面新增雷达漏斗指标：
     - 最近雷达记录；
     - 满足加入条件；
     - 已自动加入；
     - 平均评分。
   - 合约雷达表格新增：
     - 弱势评分；
     - 相对弱势；
     - 量比。
   - `币策略配置与回测` 页面新增现货雷达全量扫描和最低评分配置；
   - `币量化总览` 页面新增现货雷达漏斗指标。
4. 配置改动：
   - `.env` 和 `.env.example` 新增：
     - `CRYPTO_RADAR_FULL_SCAN_ENABLED=true`
     - `CRYPTO_RADAR_MIN_AUTO_ADD_SCORE=60`
     - `CRYPTO_SWAP_RADAR_FULL_SCAN_ENABLED=true`
     - `CRYPTO_SWAP_RADAR_MIN_AUTO_ADD_SCORE=60`

验证：

- 合约雷达实盘行情链路运行一次成功，未提交订单；
- 当前安全状态：
  - `CRYPTO_SWAP_ENTRY_ENABLED=false`
  - `OKX_ENABLE_DEMO_ORDERS=false`
  - 合约雷达仅会更新雷达记录/自选池，不会提交 Demo 订单；
- 最新合约扫描显示 ADA/SOL 弱势评分较高，但因量比/高周期结构等检查未通过，没有自动加入；
- `ruff check` 通过；
- `pytest -q tests/test_crypto_swap_radar.py tests/test_okx_crypto.py` 通过，46 passed；
- `scripts/check_streamlit_pages.py` 通过，39 页错误 0、警告 0；
- 已重启币现货服务和合约服务。

## 2026-07-26：新增山寨币弱势篮子做空研究回测

原因：

- 用户在“山寨币弱势篮子做空”和“马丁格尔”之间选择前者，要求切换该思路并立即回测验证。
- 单标的山寨币空头恢复已有基础实现，但缺少组合层弱势排序、篮子同时持仓和组合级回测。

改动：

1. 新增 `src/futu_quant/crypto_alt_basket_short.py`：
   - 以 BTC/ETH 作为市场基准；
   - 山寨币候选按近 72 根 1H K 相对 BTC/ETH 的弱势程度排序；
   - 默认只选择相对弱势最明显的前 3 个合约；
   - 入场条件：市场不强势，且候选相对弱势达到阈值；
   - 退出条件：硬止损、固定止盈、盈利保护、最长持有、BTC/ETH 市场反弹退出；
   - 回测按组合权益、预留保证金、未实现盈亏和每笔交易记录计算。
2. 新增 `scripts/backtest_alt_weak_basket_short.py`：
   - 从 OKX 读取 BTC/ETH 和山寨币候选 1H 历史 K 线；
   - 输出总收益、基准收益、超额收益、最大回撤、胜率、盈利因子、最近交易和按标的汇总。
3. 新增 `tests/test_alt_weak_basket_short.py`：
   - 验证相对弱势排序；
   - 验证弱势山寨币可开空并盈利平仓；
   - 验证 BTC/ETH 强势时禁止开空。

回测结论：

- 默认档位：720 根 1H K，收益 `-0.72%`，最大回撤 `-0.87%`，胜率 `50.00%`，盈利因子 `0.48`，跑输 BTC/ETH 基准。
- 参数网格中最好档位：篮子 3 个、相对弱势阈值 `5%`、最长持有 `12` 根 1H K，收益约 `+0.05%`，最大回撤约 `-0.39%`，盈利因子约 `1.16`。
- 当前结论：策略思路比机械马丁格尔安全，但收益边际很薄，不建议直接开启自动新开仓；应先保留为研究策略，后续增加市场状态分层、做空禁用时段和候选池质量过滤。

验证：

- `ruff check src/futu_quant/crypto_alt_basket_short.py scripts/backtest_alt_weak_basket_short.py tests/test_alt_weak_basket_short.py` 通过。
- `pytest -q tests/test_alt_weak_basket_short.py` 通过，3 passed。

## 2026-07-26：山寨币弱势篮子做空 1-4 优化

原因：

- 第一版弱势篮子做空回测虽然降低了方向暴露，但仍出现追空 LINK 后被快速反弹打止损的问题。
- 用户要求继续优化：市场状态分层、候选池质量过滤、退出逻辑和趋势确认。

策略规则改动：

1. 市场状态分层：
   - 原规则只看 BTC/ETH 近 72 根 1H K 的长周期涨跌；
   - 新增 BTC/ETH 近 24 根 1H K 的短周期确认；
   - 只有长周期不强、短周期也不强时，才允许山寨币篮子开空。
2. 候选池质量过滤：
   - 新增候选币自身 72H 必须下跌；
   - 新增候选币自身 24H 必须不强反弹；
   - 新增“不过度追空”过滤，24H 跌幅超过阈值时不再追空，避免卖在短线恐慌低点。
3. 退出逻辑：
   - 保留硬止损、止盈、盈利保护、市场反弹退出；
   - 最长持有默认由 24 根 1H K 缩短为 12 根 1H K；
   - 新增弱势排名失效退出：持仓超过最小观察期后，如果相对弱势低于阈值，主动退出。
4. 趋势确认：
   - 新增 EMA 快慢线空头排列要求；
   - 候选币必须 EMA 快线低于慢线才允许作为做空候选。

默认参数变化：

- `min_relative_weakness`: `2.5% → 6.0%`
- `max_hold_bars`: `24 → 12`
- 新增 `market_short_lookback_bars=24`
- 新增 `market_short_max_return=0.5%`
- 新增 `candidate_short_lookback_bars=24`
- 新增 `candidate_min_short_return=-8.0%`
- 新增 `candidate_max_short_return=0.0%`
- 新增 `candidate_min_lookback_return=-0.5%`
- 新增 `require_ema_downtrend=true`
- 新增 `rank_invalidation_exit=true`

回测验证：

- 优化前默认档：收益 `-0.72%`，最大回撤 `-0.87%`，盈利因子 `0.48`。
- 加入市场分层、质量过滤和趋势确认后：收益约 `+0.07%`，最大回撤约 `-0.17%`，盈利因子约 `1.82`。
- 参数网格显示：相对弱势阈值 `6%` 比 `4%/5%` 更稳，弱信号减少后回撤明显下降。

上线结论：

- 当前仍只作为研究策略，不打开合约自动新开仓；
- 主要原因是样本内交易笔数偏少，虽然风险下降，但还不足以证明可稳定自动交易。

## 2026-07-26：永续合约新主策略 Managed Futures + 山寨币空头恢复

原因：

- 旧永续合约开仓逻辑偏短周期波动突破，最近长样本回测表现不稳定，更适合保留为研究对照。
- 用户提出新的交易假设：BTC/ETH 等正统币不适合做空，合约侧只做弱势山寨币空头，小批量建仓，亏损后在风控范围内补仓，等待恢复转正。

策略改动：

1. 新增 `src/futu_quant/crypto_swap_managed_futures.py`：
   - 新策略引擎：`CRYPTO_SWAP_ENGINE=MANAGED_FUTURES`；
   - 新开仓方向：默认只允许山寨币做空；
   - BTC/ETH 默认在 `CRYPTO_SWAP_ALT_SHORT_EXCLUDED_BASES=BTC,ETH` 中排除；
   - `CRYPTO_SWAP_ALLOW_MAJOR_SHORT=false` 时，即使 BTC/ETH 满足下跌条件也禁止开空。
2. 新增山寨币弱势评分：
   - 1H 近 24H、72H、120H 动量；
   - 相对 BTC 近 72H 弱势；
   - 高周期 EMA 空头排列；
   - 日线 EMA 空头排列；
   - 成交量比；
   - 资金费率是否不过度不利；
   - 实现波动过高时扣分。
3. 新增受限补仓恢复：
   - 首仓名义金额 = `单标的保证金预算 × 杠杆 × CRYPTO_SWAP_ALT_SHORT_INITIAL_MARGIN_FRACTION`；
   - 亏损达到 `CRYPTO_SWAP_ALT_SHORT_ADD_LOSS_STEPS` 后才考虑补仓；
   - 每层补仓金额由 `CRYPTO_SWAP_ALT_SHORT_ADD_MARGIN_MULTIPLIERS` 控制；
   - 最大补仓层数由 `CRYPTO_SWAP_ALT_SHORT_MAX_LAYERS` 控制；
   - 补仓必须重新通过弱势评分，不允许机械马丁格尔。
4. 新增退出规则：
   - 恢复转正止盈：`CRYPTO_SWAP_ALT_SHORT_RECOVERY_TAKE_PROFIT`；
   - 单笔恢复硬止损：`CRYPTO_SWAP_ALT_SHORT_MAX_POSITION_LOSS`；
   - 空头趋势评分失效：`CRYPTO_SWAP_MF_TREND_INVALIDATION_SCORE`；
   - 继续保留强平缓冲、资金费率恶化、分批止盈、盈利保护等兜底。
5. 执行层改动：
   - 合约服务默认按 `MANAGED_FUTURES` 调用新策略；
   - 每轮读取 1H、高周期、1D、BTC 基准、资金费率；
   - Shadow 模式下同方向补仓会合并张数并重算平均成本，不再覆盖旧仓位。
   - 同标的补仓不再误占用“新持仓名额”，但会累计校验单仓保证金和总保证金上限。
6. 雷达改动：
   - 自动雷达在新引擎下使用山寨币弱势评分选候选；
   - 旧 `decide_crypto_swap` 仅在 `CRYPTO_SWAP_ENGINE=LEGACY_VOLATILITY` 时用于雷达研究。
7. 回测改动：
   - 新增 `run_crypto_swap_managed_futures_backtest(...)`；
   - 回测支持首仓、补仓层、恢复止盈和硬风控；
   - 页面会根据当前主引擎调用新回测或 legacy 回测。
8. 页面改动：
   - `永续合约策略` 页面顶部显示当前主策略引擎；
   - 配置页新增 Managed Futures / 山寨币空头恢复参数；
   - 旧波动突破模型文案调整为 legacy 研究对照。

安全默认：

- 本轮不私自打开 `CRYPTO_SWAP_ENTRY_ENABLED`；
- 当前运行配置仍由新开仓总开关控制，用户确认后再允许新增 Demo 合约仓位。

文档：

- `docs/2026-07-26_永续合约ManagedFutures山寨币空头恢复策略.md`

## 2026-07-26：币现货与合约策略长样本逻辑优化

原因：

- 用户明确要求继续优化策略逻辑，而不是为了让回测好看去调整回测口径。
- 使用 OKX 历史 K 线分页后，样本扩展到 720 根 1 小时 K，发现现货突破/弱回调和合约突破仍存在负贡献。

改动：

1. OKX 行情客户端新增历史 K 线分页读取：
   - `OkxDemoBroker.historical_candles(...)`
   - 页面回测 K 线根数默认扩展为 `720`，超过 `300` 根自动使用历史分页。
2. 币现货新增“趋势参与”入场：
   - 24 小时动量、72 小时动量、RSI、布林位置、量比共同过滤；
   - 入场记录标记为 `趋势参与`；
   - 默认仓位系数 `0.45`。
3. 币现货默认关闭负贡献信号：
   - `CRYPTO_BREAKOUT_ENABLED=false`
   - `CRYPTO_PULLBACK_ENABLED=false`
   - `CRYPTO_TREND_PARTICIPATION_ENABLED=true`
4. 币现货趋势参与参数经 720 根 1 小时 K 复测后收紧：
   - 24h 动量：`2.5% → 3.0%`
   - 72h 动量：`3.0% → 4.5%`
   - RSI 上限：`78 → 75`
   - 最低量比：`0.60 → 0.70`
   - 选择原因：单纯更快止盈只改善 DOGE，压低 ETH；收紧入场后 ETH 胜率和盈利因子明显改善。
5. 币现货新增弱动量时间退出：
   - `CRYPTO_WEAK_MOMENTUM_EXIT_ENABLED=true`
   - `CRYPTO_WEAK_MOMENTUM_EXIT_HOURS=12`
   - `CRYPTO_WEAK_MOMENTUM_EXIT_MAX_PNL=0`
   - `CRYPTO_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL=0.008`
   - 规则：持仓超过观察期、峰值浮盈不足且当前不赚钱时主动退出，避免拖延亏损单继续占用资金。
6. 永续合约新增新开仓总开关：
   - `CRYPTO_SWAP_ENTRY_ENABLED=false`
   - 默认只管理已有持仓，不新增合约仓位；
   - 已有持仓仍执行硬止损、强平缓冲、紧急反向退出、分批止盈、盈利保护、资金费率恶化退出和最长持有退出。
7. 永续合约页面新增：
   - “允许合约策略新开仓”；
   - “回测时启用新开仓”，用于研究验证，不强行改变正式运行配置。
8. 合约做多新增独立过滤：
   - `CRYPTO_SWAP_LONG_MIN_VOLUME_RATIO=1.10`
   - `CRYPTO_SWAP_LONG_MIN_TREND_STRENGTH=0.020`

验证口径：

- 现货长样本复测后，弱突破和弱回调被剔除，只保留更轻仓的趋势参与信号。
- 合约 BTC/ETH 720 根 1 小时 K 复测仍为负贡献，因此默认关闭新开仓；后续只在新模型通过验收后重新开放。

文档：

- `docs/2026-07-26_币与合约策略成熟化改造.md`

## 2026-07-26：币策略高周期确认与 Shadow 持仓总览优化

原因：

- 当前币策略已经收紧 1H 入场，但仍需要成熟量化系统常见的高周期方向确认，避免只靠短周期噪声开仓。
- 用户要求 `币量化总览 → 策略状态 → Shadow 策略持仓 / 模拟持仓` 参考合约量化总览，提升当前持仓感知。

策略改动：

1. 币现货策略新增高周期确认：
   - `CRYPTO_HIGHER_TIMEFRAME_ENABLED=true`
   - `CRYPTO_HIGHER_TIMEFRAME_BAR=4H`
   - `CRYPTO_HIGHER_TIMEFRAME_MIN_BARS=80`
   - `CRYPTO_HIGHER_TIMEFRAME_MIN_TREND_RETURN=0`
2. 执行规则：
   - 1H 负责触发入场；
   - 4H 负责确认大方向；
   - 只有 `EMA快 > EMA慢 ≥ 趋势均线` 且 4H 趋势收益达标时，才允许新开仓；
   - 高周期确认只限制新开仓，不阻断已有持仓的止损、止盈、弱动量退出和其他退出管理。
3. 雷达也接入同一高周期确认：
   - 1H 短线满足但 4H 不确认的标的不会被自动加入策略自选。
4. 当前单周期回测保持 1H 口径：
   - 回测内部自动关闭高周期过滤；
   - 真正多周期回测后续需要单独做 1H/4H 对齐，避免前视偏差。

页面改动：

1. `币量化总览 → 策略状态` 内部拆成：
   - `策略决策`
   - `Shadow 模拟持仓`
   - `Demo 现货余额`
2. `Shadow 模拟持仓` 新增合约总览同款指标：
   - Shadow 持仓数；
   - Shadow 当前市值；
   - 未实现盈亏；
   - 平均收益率；
   - 表现最好 / 表现最弱；
   - 行情覆盖。
3. 新增“需要关注的 Shadow 持仓”折叠区：
   - 当前亏损；
   - 或持有时间超过 12 小时。

验证：

- `ruff check` 通过。
- 币/合约相关测试：`59 passed`。
- Streamlit 页面巡检：39 页，错误 0，警告 0。

## 2026-07-26：合约策略 1-5 重构并清理旧无效参数

原因：

- 旧合约模型是单一“波动突破 + 全局量比”，720 根 1H 回测持续负贡献。
- 用户明确要求不用管旧逻辑，按计划优化 1-5，并处理旧无效参数。

策略改动：

1. 新增合约高周期确认：
   - `CRYPTO_SWAP_HIGHER_TIMEFRAME_ENABLED=true`
   - `CRYPTO_SWAP_HIGHER_TIMEFRAME_BAR=4H`
   - `CRYPTO_SWAP_HIGHER_TIMEFRAME_MIN_BARS=80`
   - `CRYPTO_SWAP_HIGHER_TIMEFRAME_MIN_TREND_RETURN=0`
   - 趋势突破做多必须 4H 向上，趋势突破做空必须 4H 向下。
2. 合约入场模型拆分：
   - `CRYPTO_SWAP_TREND_BREAKOUT_ENABLED=true`
   - `CRYPTO_SWAP_MEAN_REVERSION_ENABLED=false`
   - 趋势突破模型和均值回归模型互相独立。
3. 均值回归模型新增参数：
   - `CRYPTO_SWAP_MEAN_REVERSION_ZSCORE=2.8`
   - `CRYPTO_SWAP_MEAN_REVERSION_RSI_OVERBOUGHT=78`
   - `CRYPTO_SWAP_MEAN_REVERSION_RSI_OVERSOLD=22`
   - `CRYPTO_SWAP_MEAN_REVERSION_BOLLINGER_UPPER=1.05`
   - `CRYPTO_SWAP_MEAN_REVERSION_BOLLINGER_LOWER=-0.05`
   - `CRYPTO_SWAP_MEAN_REVERSION_MIN_VOLUME_RATIO=0.80`
   - `CRYPTO_SWAP_MEAN_REVERSION_POSITION_SCALE=0.35`
4. 多空风控拆分：
   - `CRYPTO_SWAP_LONG_HARD_STOP=0.05`
   - `CRYPTO_SWAP_SHORT_HARD_STOP=0.04`
   - `CRYPTO_SWAP_LONG_PROFIT_PROTECTION_START=0.10`
   - `CRYPTO_SWAP_SHORT_PROFIT_PROTECTION_START=0.08`
   - `CRYPTO_SWAP_LONG_PROFIT_GIVEBACK=0.30`
   - `CRYPTO_SWAP_SHORT_PROFIT_GIVEBACK=0.25`
   - `CRYPTO_SWAP_LONG_MAX_HOLD_HOURS=72`
   - `CRYPTO_SWAP_SHORT_MAX_HOLD_HOURS=48`
5. 新增合约弱动量退出：
   - `CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_ENABLED=true`
   - `CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_HOURS=6`
   - `CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_MAX_PNL=0`
   - `CRYPTO_SWAP_WEAK_MOMENTUM_EXIT_MIN_PEAK_PNL=0.006`
6. 清理旧无效参数：
   - `CRYPTO_SWAP_MIN_VOLUME_RATIO` 不再作为合约开仓依据；
   - 已从 `.env.example` 删除；
   - 已从当前 `.env` 删除；
   - 仅在代码中保留兼容字段，防止旧测试或旧环境读取报错。

回测与页面：

1. 合约回测支持真正 1H/4H 多周期：
   - 逐根 1H 推进；
   - 每个 1H 时间点只使用已经收盘的 4H K；
   - 避免前视偏差。
2. `永续合约策略` 页面新增：
   - 趋势突破模型开关；
   - 均值回归模型开关；
   - 高周期确认参数；
   - 均值回归参数；
   - 多空独立硬止损、盈利保护和最长持有；
   - 弱动量退出参数。
3. 合约热力表从旧“最低量比”改为：
   - 做多量比；
   - 做空量比；
   - 突破确认；
   - 延续收益门槛。

复测结果：

- 使用 BTC、ETH、SOL、DOGE、XRP 永续合约 720 根 1H + 4H 确认。
- 趋势突破模型：SOL 正收益但跑输持有；ETH/DOGE/XRP 仍负贡献；BTC 无交易。
- 趋势突破 + 均值回归：ETH/SOL 改善，但 BTC/DOGE/XRP 仍不合格。

结论：

- `CRYPTO_SWAP_ENTRY_ENABLED=false` 保持不变；
- 合约策略当前仍不应自动新开仓；
- 当前合约模块定位为已有仓位管理 + 研究回测。

验证：

- `ruff check` 通过。
- 合约/币相关测试：`61 passed`。
- Streamlit 页面巡检：39 页，错误 0，警告 0。

## 2026-07-26：波动率期权总览吸收合约量化工作台设计

原因：

- 合约量化总览已经形成更清晰的“候选发现 → 自动加入 → 策略执行 → 持仓订单 → 诊断复盘”一站式工作台。
- 波动率期权策略原总览仍偏纵向长页面，常用操作、持仓、雷达、诊断和导出需要滚动查找。

改动：

1. 波动率策略总览首屏改为合约页同款指标卡：
   - 执行环境；
   - 策略模式；
   - 独立雷达；
   - 监控正股；
   - 自动策略；
   - 待成交订单。
2. 新增首屏常用操作区：
   - `运行一次完整扫描`；
   - `启动独立雷达`；
   - `停止独立雷达`；
   - `刷新数据`；
   - `强制刷新账户缓存`。
3. 页面主体从纵向长列表改为六个页签：
   - `雷达流水线`；
   - `策略决策`；
   - `当前持仓`；
   - `待成交订单`；
   - `执行诊断`；
   - `导出复盘`。
4. `当前持仓` 页签统一展示富途模拟盘与 Shadow 虚拟账本，保留搜索、状态筛选和移动端精简列。
5. `执行诊断` 页签集中展示数据健康、风险提示和下一步处理队列。
6. `导出复盘` 页签保留 CSV 与 Markdown 一键导出，避免导出按钮占用首屏。

验证：

- `ruff check pages/30_volatility_strategy_overview.py` 通过。
- Streamlit 静态巡检：39 页，错误 0，警告 0。

## 2026-07-26：币现货与永续合约策略成熟化 1-5

原因：

- 币现货和永续合约回测表现差，主要来自单一“波动扩张后追突破”逻辑。
- 币市场假突破多、噪声高，现货只能做多，合约容易双向被打。
- 需要吸收成熟开源量化系统的市场状态、多周期确认、保护机制和批量验证思路。

参考：

- Freqtrade：保护机制、参数空间、backtest / dry-run 一致性。
- Jesse：多周期路由和防前视偏差。
- Hummingbot：connector 与 strategy 分层。
- NautilusTrader 类事件驱动框架：手续费、滑点、资金费率和成交模型。

改动：

1. 新增币通用信号层市场状态：
   - `TREND_UP`
   - `TREND_DOWN`
   - `RANGE`
   - `HIGH_VOL_RANGE`
2. 现货默认只在上涨趋势开多；合约默认上涨趋势开多、下跌趋势开空。
3. 新增多周期近似确认：
   - `趋势状态窗口`
   - `最低趋势强度`
4. 新增假突破和反追价过滤：
   - `突破确认K线根数`
   - `最大追价距离（ATR倍数）`
5. 现货新增趋势回调买入模式：
   - `启用趋势回调买入`
   - `回调接近均线阈值`
6. 现货和合约回测新增：
   - Buy & Hold 基准；
   - 相对持有收益；
   - 1 小时 K 年化口径；
   - 页面批量回测摘要。

页面入口：

- `币策略配置与回测 → 配置中心 / 回测验证`
- `永续合约策略 → 策略配置 / 合约回测`

文档：

- `docs/2026-07-26_币与合约策略成熟化改造.md`

验证：

- 新增测试覆盖趋势回调入场、市场状态输出、合约多空方向。

### 复测后的参数调整

原因：

- 成熟化第一版后，合约回测 0 笔交易，说明过滤过度。
- 现货虽有交易，但仍没有跑赢持有，需要让回调买入更像低吸而不是追涨。

调整：

1. 合约最低成交量比默认值：`1.10 → 0.70`。
2. 合约突破确认默认值：`2 根 → 1 根`。
3. 现货回调接近均线阈值：`2.5% → 4.0%`。
4. 现货回调新增质量过滤：
   - RSI 不高于 `65`；
   - 布林位置不高于 `80%`。
5. 已将以下运行配置写入当前系统：
   - `CRYPTO_SWAP_MIN_VOLUME_RATIO=0.70`
   - `CRYPTO_SWAP_CONFIRMATION_BARS=1`
   - `CRYPTO_PULLBACK_DEPTH=0.04`
   - `CRYPTO_PULLBACK_MAX_RSI=65`
   - `CRYPTO_PULLBACK_MAX_BOLLINGER=0.80`

复测结论：

- 合约已从 0 笔交易恢复为有交易，但收益仍未改善。
- 现货交易次数下降、回撤仍低，但依旧未跑赢 Buy & Hold。
- 下一阶段应重点优化入场质量、退出收益结构和参数热力图。

## 2026-07-26：永续合约最短持有硬风控兜底

原因：

- `未达到最短持有 1.0 小时，继续管理` 只能防止噪声交易，不能阻止硬风险退出。
- 合约杠杆场景下，强反向信号和强平距离过近必须优先于最短持有时间。

新规则：

1. `最短持有` 只限制普通止盈、回撤保护、信号反转等常规管理动作。
2. `硬止损` 仍然最高优先级，达到后立即平仓。
3. 新增 `紧急反向退出亏损阈值`：
   - 当持仓亏损达到阈值；
   - 且出现与当前持仓方向相反的强信号；
   - 即使未达到最短持有时间，也立即退出。
4. 新增 `强平缓冲退出阈值`：
   - OKX Demo 返回强平价时，计算 `|标记价 - 强平价| / 标记价`；
   - 缓冲低于阈值时立即退出；
   - 该规则同样不受最短持有时间限制。

页面入口：

- `币量化 → 永续合约策略 → 策略配置`
- 新增字段：
  - `紧急反向退出亏损阈值`
  - `强平缓冲退出阈值`

环境变量：

- `CRYPTO_SWAP_EMERGENCY_REVERSE_LOSS=0.02`
- `CRYPTO_SWAP_LIQUIDATION_BUFFER_EXIT=0.15`

验证：

- 已补充单元测试覆盖“未达到最短持有也能因紧急反向/强平缓冲退出”。

## 2026-07-24

### 波动率期权：已保存策略增加持仓展示和安全清理

原因：

- 已保存的波动率策略列表只能看到合约当前价，不能直接看到当前持仓数和开仓价。
- 过期或配置失效的波动率策略缺少批量清理入口，容易长期留在自动监控列表里。

页面入口：

- `期权中心 → 波动率期权策略 → 已保存的波动率策略`

新增展示：

- 在 `最新价` 旁边新增：
  - `当前持仓数`
  - `开仓价`
- 计算口径：
  - `当前持仓数 = 富途模拟盘持仓 + Shadow 虚拟持仓`
  - `开仓价` 优先使用富途模拟盘成本价；没有模拟盘持仓时使用 Shadow 平均成本

新增清理逻辑：

- 新增 `过期/失效策略清理` 区块。
- 只清理满足以下全部条件的策略：
  1. 合约已过期，或配置存在硬性失效问题；
  2. 富途模拟盘持仓为 0；
  3. Shadow 虚拟持仓为 0；
  4. 没有待成交订单；
  5. 当前能成功读取券商持仓和待成交订单。
- 如果无法确认模拟盘持仓或待成交订单，批量清理按钮会禁用。

审计：

- 新增事件中文：
  - `VOLATILITY_EXPIRED_CAMPAIGNS_CLEANED` → `过期/失效波动率策略已清理`

验证：

- `ruff check` 通过。
- `tests/test_ui.py` 通过：4 passed。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

### 期权行情过期与盘口不可用：增加刷新重试和等待退出状态

原因：

- 期权自动策略遇到行情过期、盘口不可用时，旧逻辑容易直接阻断或只在控制台打印，用户难以复盘到底卡在哪里。
- 已有持仓需要退出时，不能因为短暂没有 bid/ask 就让退出原因丢失。

新规则：

1. 策略执行前刷新目标期权和正股快照。
2. 行情或盘口不合格时短等待重试。
3. 开仓仍然严格：
   - 期权行情必须新鲜；
   - 正股行情必须新鲜；
   - 期权双边盘口可用；
   - 买一、卖一不能倒挂；
   - 价差不能超过策略阈值。
4. 平仓更韧性：
   - 已有持仓但行情/盘口不可用时，记录为 `WAITING_EXIT_LIQUIDITY`；
   - 下一轮服务循环继续刷新并尝试退出；
   - 卖出时买一价不可用会记录 `OPTION_EXIT_WAITING_FOR_BID`。
5. 新增审计事件：
   - `OPTION_MARKET_DATA_BLOCKED`
   - `OPTION_MARKET_DATA_WAITING`
   - `OPTION_EXIT_WAITING_FOR_BID`
   - `OPTION_QUOTE_DEGRADED_ACCEPTED`

文档：

- 新增 `docs/行情数据源与期权盘口容错方案.md`
- 说明免费第三方 API 适合研究/回测补全，不建议替代券商实时期权盘口。

验证：

- `ruff check` 通过。
- `tests/test_data_quality.py tests/test_gamma_automation.py` 通过：11 passed。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

补充中文化：

- 新增事件中文：
  - `OPTION_MARKET_DATA_BLOCKED` → `期权行情/盘口阻断开仓`
  - `OPTION_MARKET_DATA_WAITING` → `期权行情/盘口等待退出`
  - `OPTION_EXIT_WAITING_FOR_BID` → `期权平仓等待买一价`
  - `OPTION_QUOTE_DEGRADED_ACCEPTED` → `期权行情降级通过`
- 新增状态中文：
  - `WAITING_EXIT_LIQUIDITY` → `等待可退出盘口`
  - `PRE_TRADE_BLOCKED` → `开仓前已阻断`
- 新增 payload 字段中文：
  - 行情/盘口处理状态
  - 正股行情延迟（秒）
  - 期权行情延迟（秒）
  - 盘口是否可用
  - 盘口检查结果
  - 盘口价差比例
  - 行情刷新尝试

验证：

- `tests/test_ui.py` 通过：4 passed。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

### 策略复盘与执行质量：新增一键复盘入口

原因：

- 用户需要一个固定入口，不再每次靠人工翻运行日志、订单、诊断和策略页面拼结论。
- 昨晚策略分析、期权盈亏、Core Satellite 调仓原因、阻塞原因需要集中展示。

页面入口：

- `账户与交易 → 策略复盘与执行质量`

新功能：

1. 选择美东交易日后点击“生成复盘”。
2. 自动汇总策略决策、订单数、成交订单、阻断次数、期权已实现盈亏、期权未平持仓。
3. 输出整体文字结论，覆盖：
   - 波动率期权执行质量；
   - Core Satellite 调仓质量；
   - 最大阻塞来源；
   - 未平期权持仓数量。
4. 拆分展示：
   - 订单执行质量；
   - 波动率期权质量；
   - Core Satellite 调仓；
   - 阻塞原因；
   - Markdown 原始报告。
5. 支持下载 Markdown 复盘报告。

验证：

- `ruff check` 通过。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

### 波动率期权：分层保护参数支持批量保存到已保存策略

原因：

- “按信号强度分层保护”页面字段可编辑，但旧逻辑只影响新保存候选，无法同步更新已保存的自动波动率策略。
- 用户编辑后看不到保存生效，容易误以为系统仍在使用新参数。

新规则：

- 将独立平仓、分层保护、分批止盈、退出冷却参数统一收敛为一组 `volatility_exit_parameters`。
- 新保存的波动率自动策略使用同一组参数。
- 已保存策略可通过页面按钮批量同步。

页面入口：

- `期权中心 → 波动率期权策略`
- 在“按退出原因冷却”下面新增：
  - `保存退出/保护参数到已保存策略`
  - `应用到所有已保存波动率策略`

生效范围：

- 更新所有已保存的 `volatility_long_gamma` 策略配置。
- 下一轮统一交易服务循环读取配置后生效。
- 不需要重启服务。

验证：

- `ruff check` 通过。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

### 波动率期权：保护参数支持保存为默认值

原因：

- 批量应用到已保存策略只能解决旧策略同步问题。
- 用户调整“强波动档盈利回吐 30% → 25%、最低锁定盈利 10% → 12%”这类参数时，也需要影响以后新生成和新保存的策略。

新规则：

- 页面里的保护参数现在有两个动作：
  1. `保存为默认保护参数`：写入系统默认配置，影响以后新生成/新保存的波动率策略。
  2. `应用到所有已保存策略`：批量覆盖已经保存的波动率自动策略。
- 默认配置覆盖范围：
  - 独立平仓参数；
  - 按信号强度分层保护；
  - 分批止盈参数；
  - 按退出原因冷却。

页面入口：

- `期权中心 → 波动率期权策略 → 保存退出/保护参数`

示例：

- 要把强波动档：
  - 盈利回吐 `30% → 25%`
  - 最低锁定盈利 `10% → 12%`
- 在“按信号强度分层保护”里修改：
  - `强波动回吐 = 0.25`
  - `强波动锁定 = 0.12`
- 然后点击 `保存为默认保护参数`。
- 如果还想让已经存在的自动策略也用新参数，再点击 `应用到所有已保存策略`。

验证：

- `ruff check` 通过。
- Streamlit 静态巡检：30 页，错误 0，警告 0。

补充页面说明：

- 在 `保存退出/保护参数` 区域补回保存范围提示。
- 明确按钮会保存：
  - `独立平仓参数`
  - `按信号强度分层保护`
  - `分批止盈参数`
  - `按退出原因冷却`

### 波动率期权：盈利保护从“价格回撤”改为“盈利回吐”

原因：

- `US.GOOG260724P325000` 暴露了旧口径问题：旧逻辑使用期权价格从峰值回撤比例，导致峰值盈利后可能等到当前盈亏转负才退出。

新规则：

- 盈利保护按“峰值盈利回吐比例”计算。
- 计算口径：
  - `盈利回吐 = (峰值盈利比例 - 当前盈利比例) / 峰值盈利比例`
- 例子：
  - 峰值盈利 `26%`
  - 盈利回吐阈值 `25%`
  - 触发线约为 `26% × (1 - 25%) = 19.5%`
- 决策原因显示：
  - 峰值盈利
  - 当前盈利
  - 盈利回吐比例

页面变化：

- `期权中心 → 波动率期权策略 → 独立平仓参数`
- 字段从“盈利保护回撤阈值”调整为“盈利回吐阈值”。
- 决策明细新增“盈利回吐比例”。

验证：

- 相关单测通过。
- Streamlit 页面巡检通过。
- 统一交易服务已重启。

### 波动率期权：增加分批止盈

原因：

- 单一止盈目标会导致两种问题：
  - 到达中等盈利后回落，利润没有落袋；
  - 强趋势中一次性全卖，无法继续跟随。

新规则：

- 1 张合约：
  - 不分批，继续使用整单盈利保护。
- 2 张及以上：
  - 第一档：当前盈利 `≥ 30%`，卖出约 `50%`，至少保留 1 张。
  - 第二档：当前盈利 `≥ 60%`，再卖出约 `25%`，至少保留 1 张。
  - 剩余仓位继续使用盈利回吐保护。
- 每个分批档位只执行一次。
- 新一轮开仓时清空上一轮分批状态。
- 执行层支持 `SELL` 指定数量，不再默认 `SELL` 就全部平仓。

页面变化：

- `期权中心 → 波动率期权策略 → 分批止盈参数`
- 新增：
  - 启用分批止盈
  - 第一档盈利阈值
  - 第一档卖出比例
  - 第二档盈利阈值
  - 第二档卖出比例

验证：

- 已覆盖 1 张不分批、3 张第一档止盈、第二档不重复执行等测试。
- 自动雷达生成的新策略会带上分批止盈参数。
- 已有波动率策略已补齐分批字段。
- 统一交易服务已重启。

### 波动率期权：增加分层盈利保护和最低锁定盈利

原因：

- 普通信号、动态波动、强波动不能使用同一套盈利保护参数。
- 临近盈利后不应允许盈利单重新变成亏损单。

新规则：

| 层级 | 盈利保护启动 | 盈利回吐 | 最低锁定盈利 |
|---|---:|---:|---:|
| 常规 | 10% | 20% | 5% |
| 动态波动 | 15% | 25% | 8% |
| 强波动 | 20% | 30% | 10% |

退出优先级：

1. 到期/行权风险
2. 硬止损
3. 整单止盈
4. 分批止盈
5. 最低锁定盈利
6. 盈利回吐保护
7. IV/Gamma 衰减
8. 方向失效/最长持有

最低锁定盈利规则：

- 盈利保护启动后，如果当前盈利跌到对应锁定线以下，直接退出。
- 用于防止“曾经盈利但最终变亏”的情况。

页面变化：

- `期权中心 → 波动率期权策略 → 按信号强度分层保护`
- 新增常规、动态波动、强波动三套参数。

验证：

- 新增分层参数解析测试。
- 新增最低锁定盈利触发测试。
- 全量测试通过。

### 波动率期权：增加临近到期快速锁盈档

原因：

- `US.META260724P615000` 是临近到期合约，最高价格约 `19.40`，开仓成本约 `17.20`，最高浮盈约 `12.79%`。
- 旧强波动档需要 `20%` 才启动盈利保护，导致 0DTE/1DTE 合约锁盈偏晚。
- 临近到期期权 Theta、盘口价差和跳动风险更高，不应套用强波动宽松参数。

新规则：

| 层级 | 盈利保护启动 | 盈利回吐 | 最低锁定盈利 |
|---|---:|---:|---:|
| 临近到期 | 10% | 20% | 5% |

- 当合约剩余到期天数 `≤ 临近到期保护天数` 时，优先使用临近到期档。
- 默认临近到期保护天数：`1`。
- 即使标的是强波动信号，也会被临近到期档覆盖。

页面变化：

- `期权中心 → 波动率期权策略 → 按信号强度分层保护`
- 新增：
  - 临近到期启动
  - 临近到期回吐
  - 临近到期锁定
  - 临近到期保护天数

验证：

- 新增“强波动但临近到期时使用临近到期档”的测试。
- 已有波动率策略补齐近到期保护字段。
- 自动雷达生成的新策略会带上近到期保护参数。
- 统一交易服务已重启。

### 波动率期权：按退出原因区分冷却时间

原因：

- 所有退出原因使用同一冷却时间不合理。
- 止盈后可以更快重新评估；硬止损后应更谨慎；到期风险退出不应长时间封锁同一正股。

新规则：

| 退出原因 | 默认冷却 |
|---|---:|
| 止盈 | 4 小时 |
| 盈利保护 | 8 小时 |
| 硬止损 | 24 小时 |
| 到期/行权风险 | 0 小时 |
| IV/Gamma 衰减 | 8 小时 |
| 波动收缩 | 6 小时 |
| 分批止盈 | 不启动整轮冷却 |

页面变化：

- `期权中心 → 波动率期权策略 → 按退出原因冷却`
- 新增各类退出原因的冷却小时配置。

验证：

- 新增冷却原因分类测试。
- 全量测试通过。

### Core Satellite：从静态配置调仓升级为择时 + 择标的 + 排名加权

原因：

- 昨晚 Core Satellite 在模拟盘中产生多笔调仓，但行为仍偏“配置比例调仓”。
- 成熟 Core-Satellite 组合不应把所有 Core/Satellite 标的机械均分，而应先判断市场状态，再选择标的，再分配预算。
- `IVV`、`SCHD` 等核心 ETF 被当成 Satellite 处理，分类不够清晰。

新规则：

1. 先判断市场状态：
   - 以 `SPY` 是否站上 200 日线作为主要 risk-on/risk-off 判断。
2. Core 资产做趋势质量评分：
   - 是否站上 200 日线；
   - 是否站上 20 日线；
   - 60 日动量；
   - 60 日回撤；
   - 是否过度偏离 200 日线。
3. Satellite 资产做择标的评分：
   - 20 日动量；
   - 60 日动量；
   - 20 日相对 SPY 强弱；
   - 60 日相对 SPY 强弱；
   - 回撤是否可控；
   - 是否过度偏离 200 日线。
4. 预算分配：
   - Core 预算按质量评分分配，不再固定均分。
   - Satellite 预算按评分排名集中分配。
   - 弱标的不买或降权。
   - 不强制把预算打满，允许留现金。

资产分类：

- Core：
  - `US.SPY`
  - `US.VOO`
  - `US.IVV`
  - `US.QQQ`
  - `US.SCHD`
- Satellite：
  - `US.NVDA`
  - `US.AMD`
  - `US.GOOG`
  - `US.AAPL`
  - 其它非 Core 标的

处理过热标的：

- 过度偏离 200 日线时不直接剔除。
- 改为降权限制，避免强趋势标的因为“太强”被完全排除。

验证：

- 新增/调整 Core Satellite 测试：
  - Core 不再机械均分；
  - Satellite 强者多配；
  - `IVV`、`SCHD` 被识别为 Core；
  - 总仓位不超过预算上限；
  - 弱标的允许不买并保留现金。
- Core 专项测试通过。
- 全量测试通过：`253 passed`。
- 统一交易服务已重启。

### 昨晚运行复盘结论

波动率期权：

- 主问题是近到期锁盈偏晚，以及后半夜期权行情过期、盘口不可用、价差过大造成阻塞。
- 已通过临近到期快速锁盈档、分层保护、分批止盈和冷却分类进行修复。

Core Satellite：

- 昨晚调仓说明系统主链路能跑通。
- 但原逻辑偏静态配置，容易为了凑仓位而买入。
- 已升级为择时、择标的、排名加权的组合逻辑。

## 2026-07-25：期权中心页面工作台改造

- 新增 `docs/期权中心页面重构方案.md`，记录行业化的信息架构、任务分区、诊断链路和后续拆分规则。
- 波动率期权页面顶部新增“策略工作台”，首屏展示监控标的、自动策略、持仓策略和阻断/异常数量。
- 新增“今天先看 / 操作路径 / 问题排查”任务入口，先判断状态再进入详细操作。
- 参数区默认折叠，并按机会雷达、参数配置、自动策略、执行诊断重新命名。
- 雷达结果、已保存策略和执行诊断表增加固定高度，优先展示持仓、开仓价、最新价、状态和中文原因，减少长页面滚动。
- 保留原有模拟盘、Shadow、管理员解锁和策略执行逻辑，属于展示层重构。
- 新增独立页面 `30_波动率策略总览.py`，将雷达状态、自动策略数量、模拟盘持仓、待成交订单和最近执行诊断集中到首屏。
- 期权中心导航新增“波动率策略总览”，原页面改名为“波动率策略高级配置”，明确区分查看与修改任务。
- 总览页新增数据健康状态卡片，显示读取时间、雷达 PID、券商连接和策略账本可用性；明确页面刷新不会重启雷达或重复提交订单。
- 在重构文档中补充第三轮刷新策略：行情按需请求、账户短缓存、雷达读取独立服务状态、历史数据缓存计算。
- 波动率总览新增代码搜索、全部/有持仓/无持仓/有异常状态筛选。
- 自动策略表新增移动端精简列模式，默认只显示交易决策所需字段，减少横向滚动。
- 高级配置页新增固定工作流入口和当前执行状态卡片，统一连接总览、诊断、盈亏台账与策略控制台。
- 波动率总览新增最近一次雷达扫描闭环，展示扫描时间、触发数量、策略生成数量、阻断数量及逐标的原因。
- 波动率总览统一展示富途模拟盘与 Shadow 持仓，新增 Shadow 持仓策略数量、持仓来源和合并后的当前持仓/开仓价。
- 波动率总览新增自动策略、雷达扫描和执行诊断 CSV 导出，支持复盘、审批和留痕。
- 波动率总览新增一键生成 Markdown 复盘报告，自动汇总雷达、策略、持仓、Shadow、订单和执行诊断。
- 波动率总览新增今日风险提示，按高/中/低级别汇总券商、雷达、待成交、阻断和 Shadow 持仓风险。
- 波动率总览新增下一步处理队列，将风险提示转为可执行事项并提供订单、诊断、系统监控和盈亏入口。
- 修正雷达扫描统计读取来源，统一从 payload 中读取触发、生成策略和阻断原因，避免摘要数量与明细不一致。
- 波动率总览的处理队列新增高/中/低优先级数量摘要，并支持下载 `volatility_action_queue.csv`。
- 一键 Markdown 复盘报告新增“下一步处理队列”章节，保留处理事项、原因和建议入口。
- 波动率总览新增 15 秒富途持仓/订单短缓存，降低频繁刷新时对 OpenD 的重复请求。
- 波动率总览抽出持仓映射、Shadow 映射、待成交统计和 Markdown 报告生成函数，降低后续页面扩展成本。
- 波动率总览新增账户缓存状态、数据源说明、强制刷新账户缓存按钮和页面验收清单。
- 自动策略持仓表新增数量和开仓价格式化，使移动端和桌面端展示更稳定。
- 波动率策略高级配置页新增“常用数据 / 参数配置”两个页签，日常状态和低频参数分离展示。
- 常用数据页签新增雷达、自动策略、最近执行、最近扫描、已保存策略摘要和常用入口；原完整配置和执行区整体保留在参数配置页签。

## 2026-07-25：页面耦合横向排查与拆分

- 新增 `docs/页面耦合排查与拆分规范.md`，记录高耦合页面排查结果和后续页面拆分规则。
- 财报 Gamma 策略页新增“常用数据 / 参数配置”两个页签，已保存策略和最近执行与标的、日历、候选生成参数分离。
- 风控中心新增“常用状态 / 参数配置”两个页签，日常急停和核心阈值状态与大量风控参数分离。
- 交易配置页补充使用提示，明确当前配置和查找添加的职责边界。
- 修复波动率策略总览“导出当前复盘数据”在复杂对象、空值或时区时间列下可能报错的问题；CSV 和 Markdown 导出统一走安全清洗。
- 移除波动率策略总览 Markdown 导出对 pandas `to_markdown()` / `tabulate` 的隐式依赖，改为内置 Markdown 表格生成，避免未安装 `tabulate` 时页面报错。
## 2026-07-26：欧易币量化第一阶段

- 新增独立欧易 USDT 永续合约模块，继续强制 Demo，默认逐仓、净持仓和 2 倍杠杆。
- 新增永续合约模型、目录、持仓、杠杆设置和下单接口；平仓强制 `reduceOnly=true`，
  合约张数按 `ctVal/lotSz/minSz` 规范。
- 新增永续合约双向波动率突破策略、Shadow 有符号持仓、独立保证金风控、
  持续服务、合约账户总览、策略配置和合约复盘页面。
- 永续合约目录支持按需加载、代码搜索和一键加入自选；当前合约自选支持逐项移除。
- 合约账户待成交页支持选中单笔撤单和一键撤销全部合约挂单。
- 新增 `docs/2026-07-26_欧易永续合约模块设计.md`，记录安全边界、状态机与验收规则。
- 自动雷达新增完整退出闭环：自动加入的标的进入独立托管池，只有在无持仓、
  无待成交且连续多轮无有效信号时才自动退出策略自选；固定自选永不自动删除。
- 新增雷达托管数量上限、连续无信号退出轮数、固定/托管来源标识、转为固定按钮
  和退出生命周期明细，防止策略自选无限增长。
- 欧易账户总览与币量化总览的 Shadow 持仓新增当前价、持仓成本、当前市值、
  未实现盈亏、收益率、峰值价、持有小时和行情时间。
- 修复关闭 OpenD 或 Streamlit 时富途 SDK context 持续后台重连的问题：Broker 创建
  context 前先执行短超时 TCP 探活，行情连接使用异步构造并确保失败路径关闭资源。
- 调整币量化导航顺序，将“欧易账户总览”放在分组首位。
- 币量化新增独立“欧易账户总览”，打开页面自动读取 Demo 账户权益、可用资金、
  未实现盈亏、逐币种资产、待成交、历史订单和成交记录。
- 欧易账户总览将平台真实模拟账户与 Shadow 虚拟持仓分开显示，避免资产口径混淆；
  订单方向、类型、状态和时间统一转换为中文及北京时间。
- 币量化总览新增持续服务版本漂移检测：当雷达或执行代码晚于服务启动时间时，
  明确提示当前仍运行旧版本，并提供一键重启加载新代码。
- 修复欧易现货目录包含预上线或缺少价格精度的行时，整个目录因空字符串转 Decimal
  而失败的问题；现在跳过无效目录行，不再阻断有效标的自动执行。
- 币量化新增独立“策略复盘与执行质量”页面，按北京时间汇总策略信号、执行记录、
  自动雷达、当前持仓和阻断原因，并支持下载中文 Markdown 报告。
- 复盘口径区分交易信号与真实执行记录，新增信号执行率，避免把触发误当作成交。
- 币策略配置中心新增独立自选管理，当前标的前置展示并支持逐项移除。
- 新增按需加载的欧易 USDT 现货搜索，可将精确搜索结果加入策略自选或自动雷达候选池。
- 新增独立自动雷达候选池、轮转扫描和单轮限额；候选满足完整买入条件后可自动加入策略自选，
  并在同一轮交由策略执行与风控。
- 币量化总览新增自动雷达状态、自选摘要和最近扫描结果，明确展示是否已自动加入。
- 完成币量化成熟化第二批：OKX Demo 待成交去重、成交聚合对账、部分成交持仓重建。
- 新增退出冷却、最大同时持仓、组合最大敞口和组合最大回撤熔断。
- 新增两档分批止盈、最长持有、趋势反转和连续波动收缩退出。
- 回测同步支持分批卖出和峰值状态，新增 Walk-forward、Monte Carlo 与前视成交检查。
- 币总览自动展示待成交订单、历史订单和本系统成交，作为订单时间线与对账来源。
- 配置中心开放分批止盈、持有期、波动收缩、冷却和组合保护参数。
- 新增 `docs/2026-07-26_币量化成熟框架对标与路线.md`，对标 Freqtrade、Jesse、
  NautilusTrader 和 Hummingbot，并形成与现有期权策略的能力差距清单。
- 币策略执行器开始继承 Shadow 持仓数量和成本，修复每轮按持仓为零计算可能重复开仓的问题。
- Shadow 信号成交后写入策略虚拟持仓，再次扫描自动转为持仓管理；退出后清理持仓与峰值。
- 币波动率策略新增独立峰值盈利保护，配置页开放启动比例、允许回吐和最低锁定盈利。
- 币量化总览新增 Shadow 策略持仓表，直接展示标的、数量、开仓均价和更新时间。
- 币量化总览新增首屏“运行一次扫描 / 启动持续服务 / 停止持续服务 / 刷新页面”操作区，
  不再要求用户到其他页面寻找不存在的按钮。
- 新增独立欧易币策略服务控制器，提供 PID、日志、运行时长和安全停止状态。
- 欧易模拟账户改为页面自动读取并使用 15 秒缓存；原始嵌套 JSON 改为中文权益卡和逐币种余额表。
- 币策略决策统一显示中文动作、标的、时间和原因；空状态直接指向本页首屏操作。
- 修正欧易普通国际版 REST API 域名为 `https://openapi.okx.com`，避免把官网域名
  `www.okx.com` 用作 API 地址导致 HTTP 403；配置页同步增加区域域名说明。
- 将欧易 API、运行模式、监控标的、预算和波动率参数统一提取到币策略配置中心。
- API Key、Secret 和 Passphrase 使用密码框录入并保存到 macOS 系统钥匙串，页面不回显明文。
- 回测与循环服务统一读取页面保存的快慢窗口、突破窗口、扩张倍数、Z 值、量比、目标波动率和硬止损。
- 连接诊断页新增公开行情与模拟账户一键测试。
- 新增欧易币量化设计文档与 TDD 实施计划，第一版明确只支持现货 Demo Trading。
- 新增 `OkxSettings` 和 `OkxDemoBroker`，私有请求强制携带模拟盘 Header，配置无法切换实盘。
- 新增币种、交易精度和现货订单领域模型，价格和数量统一使用 `Decimal` 向下规范。
- 新增 24×7 币波动率策略，以实现波动扩张、趋势、突破和量比生成可解释信号。
- 新增币波动率回测，采用完整 K 线收盘信号、下一根开盘成交，并计入手续费和滑点。
- 新增 Shadow/OKX_DEMO 单轮执行器和 `scripts/run_crypto_strategy.py` 循环入口。
- 新增“币量化总览”“币策略配置与回测”页面，并在侧边栏建立独立一级菜单。
- 新增 8 个核心测试，覆盖 Demo 安全锁、签名、下单开关、K 线、精度、信号、止损和回测时序。

## 2026-07-26：币量化时间显示统一

- 修复币量化总览中 OKX 原始 `cTime/uTime/ts` 毫秒时间直接展示的问题，待成交订单、历史订单和策略成交统一转为北京时间。
- 修复币策略决策、雷达流水线、托管退出记录、Shadow 持仓账本更新时间未统一转换的问题。
- 修复币策略回测权益曲线和成交明细仍以 UTC/毫秒时间展示的问题，页面展示统一使用北京时间字段。
- 同步处理永续合约账户页和合约量化总览中的雷达、策略、心跳、异常和 Shadow 持仓时间，避免现货与合约页面时区口径不一致。

## 2026-07-26：币现货雷达流水线增强

- 将币量化总览拆出独立“雷达流水线”页签，对齐合约量化总览的信息结构。
- 币现货雷达新增候选评分、自动加入资格、盘口价差、波动扩张、波动 Z 值和成交量比展示。
- 现货雷达扫描结果按“符合自动加入 + 候选评分”排序，避免有效候选淹没在普通观察记录里。
- 雷达 payload 新增 `eligible`、`score`、`spread_ratio` 字段，后续复盘和执行质量分析可直接引用。

## 2026-07-26：币波动率策略成熟化 1-5 改造

- 新增共用币波动率信号层，统一计算波动扩张、方向、突破、量比、评分和可解释检查；现货与永续合约复用同一信号口径。
- 现货策略保留只做多语义，继续由现货执行器解释为买入、分批止盈和全部退出；执行结果新增生命周期阶段字段。
- 现货 Protections 新增每日最大交易次数、连续亏损标的锁定和锁定冷却期，叠加既有冷却、最大持仓数、组合敞口和最大回撤熔断。
- 永续合约策略新增分批减仓、最短持有、最长持有、资金费率恶化退出和连续波动收缩退出；执行器记录 partial stage、持仓时间和生命周期阶段。
- 新增合约专用回测函数，按保证金、杠杆、多空方向、手续费、滑点和资金费率模拟权益路径；合约策略页面新增“合约回测”页签。
- 合约策略配置页开放分批减仓、持有时间、波动收缩和资金费率退出阈值，回测与持续服务读取同一套配置。

## 2026-07-26：OKX 网络超时容错

- OKX Broker 新增请求重试和指数退避，默认网络失败后重试 2 次，降低 SSL 握手超时导致的大量异常。
- OKX 网络错误改为中文诊断，明确区分握手/响应超时、DNS/连接失败，并提示可切换 API 域名或调大超时配置。
- 币策略与合约策略服务对重复网络异常使用 `decision_if_changed` 降噪，避免同一超时在运行日志和决策表中刷屏。
- 币策略配置页开放 `OKX_TIMEOUT_SECONDS`、`OKX_RETRY_ATTEMPTS` 和 `OKX_RETRY_BACKOFF_SECONDS` 调整入口。
## 2026-07-26 - 欧易永续合约雷达闭环

- 按“欧易账户总览”的信息架构重构“欧易永续合约账户”：补齐平台资产、
  北京时间、账户权益、保证金、名义价值、有效杠杆、中文持仓字段、强平缓冲、
  Shadow 盈亏、待成交撤单、历史订单和中文成交记录。
- 修复合约量化总览“待成交订单”空列表时显示 `DeltaGenerator` 对象的问题，
  并同步清理合约持仓、历史订单页面中的同类条件渲染写法。
- 新增独立“合约量化总览”，集中展示合约雷达、策略决策、持仓、订单与诊断。
- 新增合约候选雷达、候选评分与自动加入，支持每轮限量轮转扫描。
- 区分固定自选和雷达托管自选；托管标的在无仓位、无待成交且持续失效后自动退出。
- 引入资金费率、盘口价差和未平仓量数据；开仓前增加资金费率与价差硬保护。
- 合约策略配置页增加雷达候选池、容量、退出周期和成本阈值配置。

## 2026-07-26：币与合约回测质量补强

- 修复币现货和永续合约回测平仓记录缺少真实盈亏的问题；平仓行现在记录开仓价、开仓时间、入场类型、平仓盈亏、平仓收益率、最大浮盈、最大浮亏和持有 K 线数。
- 通用回测指标优先使用交易记录中的 `pnl` 计算已平仓笔数、胜率、盈利因子和单笔期望，合约平仓不再因为方向不是 `SELL` 而被忽略。
- 币策略回测页新增胜率、盈利因子、单笔交易盈亏、MFE/MAE 和“按入场类型拆分”质量表。
- 永续合约回测页新增胜率、盈利因子、单笔交易盈亏、MFE/MAE 和“按方向拆分交易质量”表。
- 币现货新增参数热力表，支持枚举回调深度、RSI 上限和布林位置上限。
- 永续合约新增参数热力表，支持枚举最低量比、突破确认根数和趋势延续收益门槛。
- 永续合约开仓前新增趋势延续确认配置，避免突破后动能已经衰竭仍追入；新增 `CRYPTO_SWAP_CONTINUATION_BARS` 和 `CRYPTO_SWAP_CONTINUATION_MIN_RETURN`。

## 2026-07-26：币与合约回测验收指标迭代

- 新增 `closed_trade_frame` 和 `backtest_quality_metrics`，统一提取已平仓交易并计算策略验收指标。
- 参数热力表新增验收状态、可验收、质量评分、平均最大浮盈、平均最大浮亏、平均平仓收益率、盈亏比、浮盈兑现率和主要问题。
- 参数热力表排序改为先按“可验收”再按“质量评分”，避免无交易或样本不足组合因为没有亏损而排在推荐列表前面。
- 现货与合约回测页面新增“最少已平仓笔数”配置，默认少于 2 笔平仓的组合不进入推荐展示。
- 现货与合约单次回测新增验收状态、质量评分、盈亏比、浮盈兑现率和主要问题提示。
- 现货与合约批量回测摘要新增已平仓、盈利因子、质量评分、验收状态和主要问题。

## 2026-07-26：币与合约策略逻辑优化

- 现货回调买入改为保守模式，新增回调最低趋势收益和单 K 线反弹确认，避免弱趋势下接下跌中继。
- 现货回调默认参数调整为：趋势收益 `5%`、单 K 反弹 `0.6%`、RSI 上限 `60`、布林位置上限 `70%`、接近均线阈值 `3%`。
- 现货新增早期浮盈保护，默认峰值盈利 `1.5%` 后启动，允许回吐 `55%`，最低锁定 `0.2%`。
- 现货分批止盈默认从 `12% / 20%` 调整为 `2.5% / 5.0%`，更匹配 1 小时短线波动。
- 合约做空新增独立过滤，默认做空量比 `1.10`、做空趋势强度 `1.0%`、做空延续收益 `0.5%`，不再与做多共用同一套宽松过滤。
- 配置页面开放以上新增参数，回测和持续服务读取同一配置。
- 最新回测显示：BTC/ETH 现货弱回调被过滤为空仓，合约保留 BTC 多头突破并压制 ETH/弱做空信号。

## 2026-07-27：波动率策略总览持仓与时间展示修复

- 修复“波动率策略总览 / 策略决策”中决策时间先重命名后转换导致 ISO `T` 时间漏出的显示问题，统一显示为北京时间 `YYYY-MM-DD HH:MM:SS`。
- “当前持仓”页签新增富途模拟盘持仓策略、Shadow 持仓策略、合并持仓策略三个顶部指标。
- 自动策略持仓表拆出“富途模拟盘持仓”和“Shadow持仓”两列，同时保留合并后的“当前持仓”，方便区分真实模拟盘与 Shadow 虚拟账本。
- “当前持仓”页签默认显示范围改为“有持仓”，无仓位时只显示空状态提示；需要查看无仓位配置时再切换到“全部策略”，避免把已保存策略误认为当前持仓。

## 2026-07-27：波动率期权退出冷却强信号豁免

- 新增 `volatility_cooldown_bypass_allowed`，允许止盈、盈利保护、IV/Gamma 衰减和波动收缩后的冷却在重新出现强波动时提前解除。
- 硬止损退出默认仍保持强冷却，不参与豁免，避免方向判断失败后立即反复追单。
- 运行器在冷却豁免成立时记录 `COOLDOWN_BYPASS` 决策，并清理该正股冷却状态；未豁免时会写明量比、Z 值、日内涨跌或 ATR 未达标原因。
- 波动率策略配置页新增“强信号可提前解除冷却”参数组，支持配置豁免开关、硬止损豁免、最低 Z 值、最低日内涨跌、最低 ATR 倍数和最低量比。
- 自动雷达生成的新波动率期权策略会继承默认冷却豁免参数，避免手工策略和自动策略口径不一致。

## 2026-07-27：启动恢复三账差异风控作用域修正

- 明确“启动恢复发现三账差异”属于富途美股/期权 PAPER/REAL 对账保护，不是 OKX 币策略风控。
- 修复 `recovery_blocked=true` 时误拦截波动率期权 `SHADOW` 订单意图的问题；Shadow 不会提交券商，允许继续记录和撮合验证。
- 保留 PAPER/REAL 新增 BUY 的硬拦截，避免富途模拟盘/实盘在持仓归属不清时继续增加真实账户风险。

## 2026-07-27：上线审批对账差异按策略隔离

- 修复上线审批“对账差异数”按全局最新对账报告统计的问题，避免 OKX 币合约差异卡住美股/期权策略审批。
- 新增策略相关标的推导：订单意图、Shadow 成交、虚拟归属和策略配置中的合约会纳入该策略审批对账范围。
- `volatility_long_gamma` 当前只统计自身期权相关标的；DOGE/AVAX 永续合约账本差异不再影响波动率期权上线审批。

## 2026-07-27：富途三账对账范围与页面展示隔离

- 修复富途三账对账把 OKX 永续合约虚拟归属纳入富途持仓比较的问题；富途对账现在只覆盖 `US.` 美股/期权标的。
- 对账刷新时会清理非富途标的的 `symbol_blocked` 标记，避免旧币合约差异继续阻塞美股/期权策略。
- 上线审批的“三账对账”页面新增范围说明，并将非富途历史记录折叠归档，默认只展示富途美股/期权对账历史。

## 2026-07-27：波动率总览当前持仓成本对照

- “波动率策略总览 / 当前持仓”新增当前价、综合成本价、富途成本价、Shadow 成本价、成本金额、当前市值、未实现盈亏和收益率。
- 持仓成本按富途模拟盘与 Shadow 虚拟账本分别计算，再按合约乘数汇总为综合成本，避免只有“开仓价”无法判断盈亏。
- 期权当前行情使用 15 秒缓存；行情读取失败时仍保留持仓和成本展示。

## 2026-07-27：波动率期权手动平仓入口

- “波动率策略总览 / 当前持仓”新增手动平仓表单，可选择具体期权、平仓来源、数量和 SELL 限价。
- Shadow 手动平仓只写入 Shadow 虚拟账本并立即尝试虚拟撮合，不会提交富途模拟盘。
- 富途模拟盘手动平仓会提交 SELL 限价单到富途模拟盘；提交前需要管理员登录和确认勾选。
- 执行层新增单次执行模式覆盖参数，手动平仓可以指定 SHADOW 或 PAPER，不改变策略后台的默认运行模式。

## 2026-07-28：波动率期权胜率优化第一轮

- 新增波动率新仓“入场二次确认”机制，默认要求动态波动信号持续 5 分钟后才允许开仓，减少刚放量尖峰时追高买入高 IV 期权。
- 强波动但 5 日/20 日趋势分歧时不再按完整高置信度处理，执行层会标记为低置信度并限制为单阶段/低仓位表达。
- 波动率候选合约排序收紧成本与质量：强波动 IV/RV 放宽门槛由 `2.00` 收敛到 `1.80`，动态波动由 `1.80` 收敛到 `1.65`，并新增综合质量分门槛、成交量和未平仓量要求。
- 强波动盈利保护默认从 `20%启动 / 30%回吐 / 10%锁盈` 调整为 `12%启动 / 25%回吐 / 12%锁盈`，更早保护短期期权浮盈。
- 动态波动保护默认调整为 `12%启动 / 22%回吐 / 7%锁盈`。
- 分批止盈默认从 `30% / 60%` 提前到 `18% / 35%`，2 张以上先部分落袋，剩余仓位继续跟踪趋势。
- 波动率策略配置页新增“入场二次确认分钟”配置；保存默认参数和批量应用到已保存策略时会同步该参数。
- 修复页面保存候选时仍用旧 `IV/RV ≤ 1.50` 二次拦截的问题，避免“候选校验通过但保存时报 IV 成本过高”的矛盾提示。
- 已将本轮默认参数写入运行配置，并批量更新当前 4 条已保存波动率策略。

## 2026-07-28：波动率期权胜率优化第二轮

- 基于同一批历史期权盘口快照复测，确认第一轮胜率有提升但盈利因子仍低于 1，主要拖累来自单笔长 Gamma 尾部亏损。
- 强波动保护进一步收紧为 `10%启动 / 18%回吐 / 10%锁盈`，提高浮盈落袋速度。
- 硬止损采用折中档 `35%`：回测显示 `30%` 虽然宽口径更防守，但真实 Shadow 开仓点回放会过早止损；`35%` 在宽口径与真实开仓点之间更均衡。
- 波动率候选合约流动性门槛继续收紧：非末日期权最大价差降至 `5%~6%`，最低未平仓量提高到 `800~1000`，最低成交量提高到 `10~15`，减少买入宽价差/弱流动性合约。
- 波动率策略配置页新增硬止损默认值保存，批量应用到已保存策略时会同步硬止损。
- 已将第二轮参数写入运行配置，并批量更新当前 4 条已保存波动率策略。

## 2026-07-28：波动率期权胜率优化第三轮

- 新增已保存波动率活动“开仓前质量闸门”，BUY 前会再次检查质量分、模型预期收益、价差、未平仓量和成交量；不通过时记录 `ENTRY_QUALITY` 阻断，不提交 Shadow/PAPER 订单。
- 自动滚动与手动保存候选时会写入质量闸门字段，包括最低质量分、最低模型预期收益、最大入场价差、最低未平仓量和最低成交量。
- 当前默认质量闸门：质量分 `≥55`、最大价差 `≤6%`、未平仓量 `≥800`、成交量 `≥10`、有模型预期收益字段时要求 `≥2%`。
- 已将质量闸门应用到当前 4 条已保存波动率策略；按现有保存质量分，`INTC`、`NVDA`、`TSM` 新开仓会被阻断，`SPCX` 仍可通过。
- 质量闸门只作用于新开仓 BUY，不影响已有仓位的 SELL 平仓管理。

## 2026-07-28：波动率期权质量闸门微调与回测验证

- 将强波动最低质量分从 `55` 微调为 `54`，避免误挡高流动性边界合约。
- 同步更新候选排序、自动滚动、手动保存候选和当前 4 条已保存策略的 `minimum_entry_quality_score`。
- 回测验证显示：`NVDA` 质量分 `54.6` 在新门槛下重新放行；`INTC` 和 `TSM` 仍因质量分/未平仓量不足被阻断。
- 当前 4 条策略闸门结果：`SPCX`、`NVDA` 通过；`INTC`、`TSM` 阻断。
- 当前保存策略样本中，被阻断组历史回测平均收益约 `-22.92%`，通过组约 `-7.15%`；质量闸门具备筛劣作用，但 `SPCX` 仍需继续观察。

## 2026-07-28：财报 Gamma 页面总览重构

- 参考波动率期权策略页，将“财报 Gamma 事件策略”拆成更明确的“常用数据 / 参数配置”工作流。
- “常用数据”页签新增一屏概览、已保存策略、最近决策、财报日历、持仓与订单 5 个子页签。
- 顶部新增关键指标：Gamma 标的数、自动策略数、可自动交易数、阻断/异常数、Shadow 持仓数、执行模式、自动调度状态和待处理订单数。
- 已保存策略表新增机会评分、最低评分、是否允许自动交易、事件确认、执行状态和诊断原因，方便一眼看出为什么没有开仓。
- 最近决策表解析 `payload_json`，展示当前持仓、计划下单、盈亏比例、峰值回撤、触发模式、自动方向依据、机会评分和历史波动优势。
- 新增财报日历缓存展示和 Shadow 持仓/订单意图展示，并提供跳转到策略控制台、执行诊断、期权盈亏台账和订单管理的快捷入口。

## 2026-07-28：财报 Gamma 多腿结构 Shadow 验证放行

- 调整财报 Gamma 页面保存逻辑：跨式、宽跨式、借方价差等多腿结构不再被页面直接拒绝。
- 多腿结构保存后会按单条 campaign 标记为 `SHADOW` 验证，只做逐腿虚拟撮合，不提交富途模拟盘。
- 后台运行器改为仅在 `SHADOW` 模式允许多腿逐腿验证；`PAPER/REAL` 继续阻断，直到期权组合原子订单接入。
- 页面结构建议处新增说明，明确单腿可按策略执行模式运行，多腿仅 Shadow 验证。

## 2026-07-28：财报 Gamma 双源校验与调试模式拆分

- 财报 Gamma 参数页新增“财报时间校验方式”：默认“双源强校验”，也可选择“人工确认调试”。
- “人工确认调试”允许在富途与 Nasdaq 双源日历缺失或不一致时保存策略；系统会保留校验提醒，但不再阻断调试运行。
- 未人工确认财报时间、期权代码无法识别、合约过期、合约到期早于财报日等仍然是硬阻断。
- 修复多腿 Gamma 保存时误改全局 `earnings_gamma` 执行模式的问题；现在只给单条多腿 campaign 写入 `execution_mode_override=SHADOW`。
- 后台 Gamma 运行器已支持单条 campaign 执行模式覆盖，避免一个多腿调试策略影响其他单腿 Gamma 策略。

## 2026-07-28：财报 Gamma 时间展示体验优化

- 财报 Gamma 页面新增统一时间格式化：日期显示为 `YYYY-MM-DD`，运行/创建/抓取/入场时间显示为中国时区 `YYYY-MM-DD HH:mm`。
- 常用数据顶部新增“下一场财报”“财报时间（中国）”“距离财报”“校验方式”，让当前策略状态更直观。
- 已保存策略、最近决策、财报日历、订单意图、候选生成和调试记录表统一去除机器化 `T` 时间展示。
- 保留 `发布时间`、`时间已确认` 等业务字段原始语义，避免把“盘前/盘后”或“是/否”误转成时间。

## 2026-07-28：期权盈亏台账开仓时间修正

- 修复“期权盈亏台账 / 逐笔盈亏”开仓时间使用订单创建时间的问题。
- 逐笔 FIFO 批次现在按优先级取时间：Shadow 实际成交时间 → 富途成交更新时间 → 订单创建时间兜底。
- 平仓时间也同步采用实际成交/更新时间，避免卖出订单创建早于真实成交时误导复盘。
- 逐笔盈亏表新增“开仓时间来源”“平仓时间来源”，方便判断当前时间来自 Shadow 撮合、富途成交更新还是创建时间兜底。

## 2026-07-28：已保存波动率策略权利金预算可编辑

- 波动率期权策略页在“自动策略 · 已保存策略与当前持仓”区域新增“已保存策略权利金预算”编辑表。
- 支持直接修改已保存自动策略的 `每批合约数` 和 `每批最大权利金`，不再只能修改下一轮雷达生成预算。
- 保存后写回 `VOLATILITY_CAMPAIGNS`，下一轮统一交易服务循环会按新预算进行开仓前权利金检查。
- 保留审计事件 `VOLATILITY_SAVED_CAMPAIGN_PREMIUM_BUDGETS_UPDATED`，记录每条策略预算修改前后的值。

## 2026-07-29：波动率总览筛选控件状态警告修复

- 修复“波动率策略总览”中 `volatility_overview_status` 同时由默认值和 Session State 赋值导致的 Streamlit 警告。
- 移除页面加载前对同一 widget key 的手动写入，改为由 `segmented_control` 自身默认值管理。
- 该问题只影响日志噪声，不影响策略运行或交易执行。

## 2026-08-01：波动率期权策略五项核心架构升级

- 审批指纹拆分为“稳定策略版本”和“运行时活动数据”：自动雷达新增标的、创建/滚动 campaign、更新预算不再让已通过审批立即失效；模型代码、风险规则或人工策略版本号变更仍会触发重新验收。
- 模型预期收益从“持有到期盈亏平衡概率”改为 `1.5–24 小时`短周期模型，按 Delta、Gamma、Vega、Theta、方向漂移、IV 均值回归和买卖价差估计可执行退出收益，并融合已有 Ask 买入/Bid 卖出历史样本。
- 移除固定 `50%` IV 历史百分位：优先使用同一标的真实历史 IV 分布；历史不足时使用当前期权链截面代理并明确标记来源；完全无样本时显示“样本不足”，不再伪造成 100% 高位误拒。
- 新增最佳期权表达路由：在单腿买方、看涨/看跌借方价差和 1×2 正 Gamma 反向价差之间比较可执行净权利金、预期收益、最大风险、净 Gamma、流动性与 IV 成本，选择满足门槛的最佳结构。
- 波动率策略执行器接入已有受控组合执行引擎：多腿结构按确定性组合编号创建、逐腿风控执行、恢复、回滚及组合平仓；不再退回财报策略的 Shadow 单腿占位逻辑。
- 期权数据回测新增“波动率走步验证”，按历史盘口执行 Ask 买入/Bid 卖出的样本外窗口验证，输出样本量、窗口稳定性、胜率、盈利因子和最差收益，并可保存为上线审批证据。
- 波动率策略总览新增“准入漏斗”，集中展示信号通过、候选淘汰、自动活动生成、审批阻断和订单提交数量，同时归类主要拒绝原因及当前/已审批版本一致性。
- 波动率候选与已保存活动新增中文字段：表达结构、IV 分位来源、模型版本、经验样本数、预期退出买卖价、组合净支出与最大风险。
- 修复周末短财报窗口没有交易日时被误报为“Nasdaq 全部请求失败”：现在自动查询下一个工作日。

## 2026-08-01：波动率策略决策时间修正

- 修复波动率期权策略首页“最近策略决策”先重命名 `created_at` 、后转换时区，导致转换器无法识别字段的问题。
- 时间现在在原始字段阶段先从 UTC 转为中国时区，再显示为“时间（中国）”，不再出现 `T`、微秒时区后缀和 `+00:00`。
- 新增包含微秒的 UTC ISO 时间回归测试，确认跨日后正确显示为中国日期和时间。

## 2026-08-01：上线审批执行质量缺失值修复

- 修复执行意图没有 `reference_price` 或参考价为 0 时，滑点计算产生 `pd.NA` 后被强制转为 `float`，导致上线审批页整体崩溃的问题。
- 无有效参考价的订单现在标记为“滑点不可计算”，不参与平均滑点统计，但仍计入交易意图数、成交率和等待时间统计。
- 执行价格、成交数量、追价次数和时间字段统一采用容错数值/时间转换；损坏或缺失的 `metadata_json` 不再拖垮报告。
- Shadow 与模拟成交价对照也增加零成本保护，避免价格偏差出现除零异常。

## 2026-08-04：波动率策略总览工作台恢复

- 恢复波动率总览页的顶部操作区：独立雷达启停、刷新数据和强制刷新账户缓存。
- 恢复工作台页签：雷达流水线、准入漏斗、策略决策、当前持仓、待成交订单、执行诊断、导出复盘。
- 持仓表同时展示富途模拟盘和 Shadow 虚拟账本，准入漏斗显示扫描、触发、生成与阻断数量。

## 2026-08-04：源代码功能恢复

- 仅恢复源代码，不修改交易数据、缓存或模拟持仓。
- 三账对账改为按策略隔离校验，避免 OKX 币/合约历史账本影响美股期权上线审批。
- 重建币量化总览和合约量化总览入口，并恢复主导航菜单。
- 波动率高级配置恢复真实 IV 历史样本、短周期预期收益和最佳期权表达表格。

## 2026-08-04：审批一致性、波动率执行与运行稳定性修复

- 上线审批新增交易服务运行版本清单：页面同时展示当前代码、服务运行和已批准三个版本；三者不一致时禁止批准，并提供管理员重启服务入口。
- 交易服务启动时冻结并上报策略指纹；执行层只校验本进程实际加载的版本，代码热更新后明确提示重启，避免“页面已批准、服务仍拒单”。
- 入场二次确认改为动态时长：强信号最多等待 1 分钟，完整确认信号最多 2.5 分钟，边缘信号保留配置的基准时长。
- 已保存活动未通过最新入场质量闸门时立即触发最佳期权表达复核，不再等待固定 5 分钟维护周期；替代合约仍必须通过成本、流动性、Delta、预期收益和权利金预算。
- 波动率独立退出新增连续方向失效和有效 Delta 衰减检查；连续两轮不利后可按独立最短持有时间提前退出，硬止损、止盈及盈利保护仍保持最高优先级。
- 持仓退出时允许在盘口降级但行情仍新鲜的情况下继续计算退出决策；无有效买一价时继续等待，不会提交无法成交的卖单。
- 修复组合腿在审批/风控提前拒绝时把 `intent_id=0` 写入外键列的问题，消除组合监控和波动率服务的 `FOREIGN KEY constraint failed`。
- 富途 OpenD 连续断线新增进程级指数退避；统一交易任务把共同连接中断记录为 `DEGRADED` 和单一警告，不再为每个任务重复累计服务错误。
- 历史错误代码 `BRKB` 自动规范为富途代码 `US.BRK.B`，避免行情任务持续报“未知股票”。
- 交易诊断中心新增 Core/Satellite 分区、择时择标的依据、当前/目标权重、分区预算和现金缓冲，后续每笔调仓可以直接解释资金来源。
- 波动率配置页新增最低有效 Delta、方向失效确认次数、方向失效退出最短持有时间及动态入场确认基准，并支持保存为默认值或批量应用到已保存策略。
## 2026-08-04

- 修复 Streamlit 将 `DeltaGenerator(...)` 容器对象当作持仓数据显示的问题：禁止页面使用裸三元表达式调用渲染函数，并在全页面静态巡检中加入强制校验。
- 波动率策略总览的“当前持仓”恢复分类展示：富途模拟盘、Shadow 虚拟账本、双账并存、未持仓配置和全部策略；Shadow 数据仅统计 `volatility_long_gamma`，不再混入其他策略。
- 账户总览的 Shadow 持仓进一步拆分为股票/ETF、期权和全部持仓，便于直接对账。
## 2026-08-06

- 三账对账页新增差异快速调整：单一策略归属时可一键按富途实际持仓同步虚拟账本，并记录审计证据；多策略归属仍要求在风险中心手动拆分。
- 修正三账快速调整计算：不再覆盖历史成交账本，而是按“富途目标持仓 - 策略历史成交净量”反推虚拟归属残差。
- 数据研究中心新增“行情数据健康”页签，展示本地研究行情的数据源、最新交易日、数据年龄、质量等级和过期状态。
- 波动率策略在决策循环前过滤无法解析到期日的期权代码，减少无效阻断噪声。
- 收益退出后的强信号冷却豁免改为至少满足 3 项波动确认中的 2 项，抑制止盈后立即重复开仓。
- 保留原有硬止损和数据质量门控，不通过放宽条件换取成交数量。
- 冷却豁免升级为分层模式：强波动档在量比确认且满足任一核心波动条件时可提前恢复；动态波动档仍需至少 2/3 项确认。

## 2026-08-11：波动率期权止血与组合残留修复

- 手动核查并完成 TSLA 借方价差残留组合平仓：先回补短腿 `US.TSLA260810C337500`，再卖出长腿 `US.TSLA260810C332500`，富途模拟盘已无这两张持仓。
- 波动率期权新增运行开关 `volatility_new_entries_paused`，当前已打开：暂停 `volatility_long_gamma` 新开仓，只保留已有持仓止盈、止损、到期退出和组合平仓管理。
- 过期且富途账户已无真实持仓的 OPEN 组合会自动本地收尾，避免旧 NVDA/SPCX/TSLA 组合继续污染三账对账、上线状态和策略判断。
- 已有期权持仓的行情门控改为“退出优先”：新开仓仍严格要求新鲜行情；已有仓位只要正股行情新鲜且期权盘口可用，就允许继续计算退出，不再被期权快照超过 900 秒永久卡住。
- 统一交易服务已重启加载本轮修复，当前波动率期权处于止血观察状态；恢复新开仓前需要先复盘近期亏损来源并重新校准入场质量。
- 修复 Berkshire B 期权合约反推正股代码的问题：`US.BRKB260...` 合约代码保持不变，但行情任务请求正股时统一使用富途可识别的 `US.BRK.B`，消除 `snapshots failed: 未知股票 BRKB` 导致的 option_history/健康检查降级。
- 修复系统监控页“策略绩效”卡死：将原 `st.tabs` 全量渲染改为单视图按需渲染，并缓存绩效归因 60 秒；绩效后端只对未平仓成交批次请求行情，不再对全部历史成交代码批量拉快照。

## 2026-08-13：波动率孤儿仓接管与系统级大故障审查

- 波动率策略发现富途模拟盘存在策略买入、但当前配置/状态未接管的期权持仓时，默认从“自动卖出”改为“自动接管”：创建退出专用策略活动并纳入止盈、止损、盈利保护、方向失效和到期管理。
- 保留安全配置 `VOLATILITY_ORPHAN_POSITION_POLICY`：`ADOPT` 自动接管、`CLOSE` 自动平仓、`BLOCK` 只阻断提示；默认使用 `ADOPT`，避免误平仍可能盈利的持仓。
- 波动率策略执行循环会在孤儿仓接管后立即把新活动并入当轮管理，避免需要等下一轮或页面手工刷新。
- 三账对账增强为可恢复机制：发现差异时同时暂停策略 ID 和服务名两套开关，并保存原值；差异消失后自动恢复原策略开关、解除 `recovery_blocked`、关闭 `LEDGER_MISMATCH` 告警。
- 手动/紧急平仓导致本地虚拟账本残留时，可通过对账同步把策略虚拟归属调到富途实际持仓；本轮已将 5 张 SPCX 残留账本同步为 0。
- 灾备恢复点新增保留策略，默认保留最近 7 个、最多 14 天；创建新恢复点时自动删除过期 manifest、数据库记录和未引用 blob，防止 `data/recovery` 持续膨胀。
- 自动恢复流程新增临时告警老化恢复：`ORDER_FAILED`、`VOLATILITY_SCANNER_ERROR`、已解除急停开关的 `DAILY_LOSS_KILL_SWITCH` 超过配置时间后自动关闭；磁盘、到期风险、组合压力等真实风险不会被自动关闭。
- 修复波动率活动字段出现 `nan` 时导致 `Invalid isoformat string: 'nan'` 的策略循环错误：时间字段统一容错解析，坏值回退到当前时间或从期权代码解析到期日。
- 波动率策略循环新增服务控制中断点：检测到统一交易服务停止/重启文件时，在下一只标的前提前结束维护或执行循环，避免重启请求长时间卡在旧进程。
- Git 仓库不再跟踪 `data/*.stop`、`data/*.restart` 等运行控制信号文件，避免 checkout/回滚把停止信号文件恢复出来，导致统一交易服务误判正在停止。
- 修复退出维护活动误走组合平仓的问题：当 `exit_only=true` 或缺少有效组合账本时，止损/止盈退出会回退为当前主合约单腿平仓，避免单腿持仓因为找不到组合账本而无法止损。
- 当前仍需人工/运维处理的真实风险：磁盘可用空间约 4%、`US.INTC260814C106000` 临近到期且价内、组合压力场景损失超过阈值。

## 2026-08-14：临近到期期权退出与紧急追价规则加固

- 波动率期权新增生命周期不变量：持仓合约在到期前 1 天内且价内时，不再等待普通止盈/止损/到期日 15:00 规则，直接触发提前退出，降低自动行权和临近到期流动性风险。
- 持仓退出不再因为“盘口过宽”被硬拦截；新开仓仍保留盘口质量门槛，已有持仓只要正股行情新鲜并且实际平仓腿有可用买一/卖一，就继续执行退出。
- 退出限价规则明确化：普通卖出按买一、普通买回按卖一；紧急退出卖出默认在买一下方主动降价 2%，紧急买回默认在卖一上方主动加价 2%，可用 `urgent_exit_price_improvement_pct` 覆盖。
- 模拟/实盘未成交订单追价区分普通单和紧急退出单：普通单仍使用 `EXEC_MAX_SLIPPAGE_PCT`，紧急退出单使用 `EXEC_URGENT_EXIT_MAX_SLIPPAGE_PCT`，默认 12%，避免止损单被 1% 追价边界卡住。
- 新增不变量测试覆盖：`exit_only` 多腿元数据只能平主腿、缺组合账本回退主腿、有效组合保留组合平仓、临近到期价内提前退出、价外不强制退出、紧急卖出/买回限价方向、紧急退出追价边界。

## 2026-08-14：OpenD/SDK 会话坏死自愈

- 复盘 AMD 期权止损延迟后，确认故障模式是 OpenD 进程和端口仍存在，但统一交易服务旧进程的富途 SDK 上下文卡死，导致波动率策略停留在旧行情。
- 统一交易服务新增 OpenD/SDK 连续失败计数：关键券商任务包括订单对账、组合监控、期权生命周期、到期处置和波动率策略。
- 连续 OpenD/SDK 异常达到 `FUTU_SERVICE_RESTART_ON_OPEND_FAILURES` 后，服务会写入重启信号并启动 handoff 接棒进程，自动重建富途 SDK 会话。
- 支持可配置自动重启 OpenD：`FUTU_OPEND_AUTO_RESTART=true` 时，连续失败达到 `FUTU_OPEND_RESTART_ON_FAILURES` 会尝试重启 OpenD 应用，并做最小 300 秒防抖。
- 服务启动恢复和每轮调度前新增 SDK 子进程预检：预检超时或失败时，主服务不再直接进入可能卡死的 `OpenSecTradeContext`，而是记录降级并触发自愈。
- `.env` 新增自愈参数：服务自重启阈值 2 次、SDK 预检超时 12 秒、OpenD 自动重启阈值 4 次、OpenD 重启防抖 300 秒、默认 macOS 应用名 `Futu OpenD`。

## 2026-08-15：首页驾驶舱

- 新增“首页驾驶舱”作为 Streamlit 默认入口；“账户总览”保留为独立导航页，用户需要查看持仓、订单和原始账户明细时再进入。
- 驾驶舱集中展示统一交易服务、独立雷达、策略心跳、24 小时任务失败、账户权益、现金、当日/总持仓盈亏、股票/ETF 持仓、期权持仓和 Shadow 持仓。
- 新增昨夜策略决策漏斗、近 36 小时订单状态分布、成交名义金额、权益走势、持仓结构、策略心跳表、策略资金配额、48 小时风险提醒和系统事件图表。
- 首页快捷入口聚合账户总览、订单管理、波动率策略、期权盈亏台账、每日复盘、交易诊断、风控中心和系统监控，减少在左侧菜单中反复查找。
- 首页不再默认显示管理员密码框，避免只看状态时被管理操作区打断；需要撤单或高风险操作时仍在账户总览及相关操作页面解锁。

### 驾驶舱第二版：图形化与实时刷新

- 首页主体改为 Streamlit Fragment，每 10 秒局部自动刷新；导航和侧栏不会跟随反复重跑。
- 移除首页的决策、心跳、告警等明细数据表，明细统一通过快捷入口进入专业页面查看。
- 增加最近 24 小时区间盈亏折线/面积图，盈利与亏损分别使用绿色和红色展示，并标注区间最大回撤。
- 增加股票/ETF 与期权的当日、累计盈亏贡献图，直接辨认亏损来自哪个资产类别。
- 增加资产分布和策略持仓敞口环形图；无持仓归属数据时自动回退为策略资金预算分布。
- 增加最近交易时段策略决策漏斗、策略运行状态分布和风险脉搏图。
- 顶部指标增加权益与区间盈亏迷你趋势线，并显示现金缓冲、股票/期权持仓数量。
- 首页保留账户总览、期权盈亏台账、每日复盘、交易诊断和风控中心的快捷入口。

### 驾驶舱第三版：异常优先与主动提醒

- 首页新增置顶“需要关注”区域，异常信息优先于普通盈亏图表展示。
- 自动汇总最近 48 小时 OpenD 中断与恢复时间，显示影响时长；已经恢复的历史故障使用提醒色，当前仍不可用才使用严重错误提示。
- 富途账户当前不可读时明确提示持仓监控、止盈止损可能失去实时数据，避免仅把服务进程存在误判为券商链路健康。
- 自动识别当前仍有持仓数量且累计亏损超过 300 美元或亏损比例超过 10% 的持仓，最多展示亏损最严重的 5 个。
- 大额亏损卡片直接显示代码、资产类别、持仓数量、亏损金额和亏损比例，并提供账户持仓详情入口。
- 零数量但富途仍保留历史损益的已清仓记录不会进入当前大额亏损提示。
- 未恢复系统事件同步置顶提醒并提供系统监控入口。
- 大额亏损持仓区域增加横向亏损柱状图；柱长表示亏损金额，悬浮提示显示代码、比例和数量，卡片继续保留具体处置入口。
- 将持仓亏损分布从顶部异常区下移到权益、盈亏贡献图之后；顶部仅保留 OpenD、账户连接和未恢复事件等即时故障，减少首页首屏占用。
- 将“账户总权益、当日浮动盈亏、总持仓浮动盈亏、现金缓冲、当前持仓、持仓亏损分布”统一编排为“账户与持仓总览”模块；权益走势与亏损分布并排展示，盈亏贡献和资产/策略分布随后展开。

## 2026-08-18：策略复盘字段补全与阻塞诊断细化

- 修复每日复盘中波动率期权 MFE、浮盈捕获率、当前盈亏字段容易为空的问题：最新动作和最新风控指标分开取值，避免后续“期权表达维护/观察”覆盖真正的持仓管理指标。
- 期权生命周期新增“当前盈亏金额”和“指标时间/指标原因”，用于判断卖出是否捕获到峰值浮盈，而不是只看最终成交盈亏。
- 交易诊断把“产生买卖信号但没有形成订单意图”拆成明确断点：数量为 0、缺少可执行价格、校验提醒、同标的冷却、期权表达维护、策略校验阻断、信号未进入执行器。
- 到期/失效合约残留归类为“到期/失效清理项”，日报仍可查看，但不再作为核心交易阻塞参与质量评分和行动项排序。
- 修复策略质量评分表混合数字和空字符串导致 Streamlit / PyArrow 序列化失败的问题；空评分统一使用空值，页面仍显示为空白。
- 新增回归测试覆盖 MFE 不被后续观察信号覆盖、数量为 0 的未下单断点、到期残留不污染复盘质量评分。

## 2026-08-19：波动率期权复盘持仓口径与入场稳定性确认

- 修复每日复盘“期权未平持仓”误报：窗口内同一合约买卖已经配平时，以成交净额覆盖平仓前 payload 的 `position_quantity`，避免显示已平合约仍未平。
- 波动率期权入场二次确认新增量能稳定性检查：记录信号刚激活时的量比，确认结束时要求当前量比不低于最低量比，且保持率不低于配置比例，减少追在波动尾端。
- 自动雷达生成的新波动率策略会带入入场稳定性参数：最低量比、量能保持率、确认时是否必须仍处于动态波动。
- 波动率期权配置页新增“入场稳定最低量比”“入场量能保持率”“确认时必须仍处于动态波动”三个配置项，并支持保存为默认和应用到已保存策略。
- 新增回归测试覆盖已平合约不计入未平持仓、量能衰减时入场确认应阻断。
