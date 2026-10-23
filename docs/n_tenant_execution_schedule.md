# Tradeyi 项目：通用多租户架构（N-Tenant）三阶段实施排期计划

## 一、 项目背景与演进目标
针对实盘真金交易（REAL）与前向推演模拟盘（SIMULATE）在运行过程中的解耦需求，以及未来横向扩展至 N 个实盘/模拟子账户（如主备实盘号、跨券商账户、专项赛马模拟仓）的机构级诉求，系统正式启动由“二元伴生模型”向“通用多租户架构（N-Tenant Architecture）”的全面演进。

---

## 二、 三阶段实施排期矩阵

| 实施阶段 | 阶段主题 | 核心交付物 | 责任角色 | 预计耗时 | 验收标准 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **阶段一** | **数据契约与账本联合主键强隔离** | `TradingStore` 联合主键支持、`ExecutionManager` 环境守卫、租户依赖注入、跨租户拦截单测套件 | Coder / Reviewer / Tester | 1 工作日 | 39 项隔离用例与安全测试全绿，实盘零停机兼容 | **已完成** |
| **阶段二** | **多租户独立守护进程与服务参数化** | `run_service.py --tenant <id>`、独立 pid 与文件锁、独立状态机追价/超时撤改补单、平滑灰度启动 | Coder / Architect / Ops | 1 工作日 | 模拟盘拥有对等自闭环状态机，双进程无资源争用 | **已完成** |
| **阶段三** | **全局多租户视窗与端到端闭环验收** | Streamlit 顶部多租户动态切换器、孤儿持仓侦测看门狗、全量 520+ 自动化回归报告 | Product / Coder / Tester | 0.5 工作日 | UI 严格按租户切片过滤，全量用例 100% 绿灯验收 | **已完成** |

---

## 三、 阶段二（进行中）详细任务拆解与分工

### 1. 服务启动参数化（@Coder）
- 重构 `scripts/run_service.py`，新增 `--tenant <tenant_id>` 命令行参数；
- 解析指定的租户 Profile，按租户身份初始化 `FutuPaperBroker`、`ExecutionManager` 与专属 `TradingStore` 实例；
- 独立管理服务单例锁文件：`data/trading-service-<tenant_id>.lock` 与 `.pid`。

### 2. 状态机对等与补单闭环（@Coder）
- 在模拟盘专属 Service Worker 中挂载完整的 `reprice_open_orders`（追价改单）与 `cancel_stale_orders`（超时撤改补单）轮询周期；
- 彻底根除模拟单超时撤单后无人管理的“孤儿单”技术隐患。

### 3. 运维平滑上线与进程托管（@Ops）
- 在保持生产实盘进程（PID 20692）不停机的前提下，以金丝雀方式拉起 `run_service.py --tenant sim_primary`；
- 巡检脚本自动识别并纳管多进程心跳与健康状态。

### 4. 质量与安全把关（@Reviewer & @Tester）
- 审查参数化改造代码，确保实盘默认参数向下兼容；
- 验证双进程并发读写 SQLite WAL 时的事务安全与锁竞争。

---

## 四、 阶段三（排期启动）详细任务拆解与分工

### 1. 全局多租户切换视窗（@Coder，主责 UI 实现）
- Streamlit 顶部导航栏部署**动态租户切换器**：从 `tenants` 注册表实时拉取列表，展示 `[真金·主账户]`、`[模拟·沙盒]` 等标签，切换即全局刷新；
- 所有功能页面（资产总览、持仓、订单、风控中心、驾驶舱）严格按当前选定租户切片过滤数据；
- 历史遗留的硬编码账户展示逻辑全面收敛到租户上下文。

### 2. 孤儿持仓侦测看门狗（Watchdog）常态化（@Ops）
- 在定时巡检脚本中固化「孤儿持仓雷达」：券商有持仓但无租户管理动作超阈值即飞书告警；
- 叠加「止损触发却无平仓成交」的出口堵塞专项告警（今晚 QQQ 病例的机制化沉淀）；
- 报价新鲜度门控降级路径纳入监控：期权报价冻结 > 900 秒且双边盘口健康时自动提示。

### 3. 端到端验收与全量回归（@Tester）
- 全量 507+ 用例（含租户参数化四件套）跑 100% 绿灯；
- 多租户双进程并行下的端到端演练：双租户同时开仓→管理→平仓全链路无串轨；
- 出具最终验收报告并归档飞书云盘 03_测试基线文件夹。

### 4. 产品验收（@Product）
- 按 PRD 逐项核验多租户视窗、数据切片、告警体系的体验完整性；
- 组织用户 UAT 验收，收集反馈并整理迭代 backlog。

### 阶段三启动前置条件
阶段二代码通过 Reviewer 审查 + Tester 回归绿灯后即刻进入；预计耗时 0.5 个工作日。

---

## 五、 全局待改造事项跟踪表（Backlog，持续滚动更新）

| # | 事项 | 来源 | 责任人 | 优先级 | 状态 | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 期权止损通道加固（A 类缺陷：彻底根治 QQQ 病例） | 2026-10-20 QQQ 病例复盘 | Coder / Reviewer / Tester | **P1** | **已完成** | 四条安全护栏闭环：取消 abs 防单向暴跌、偏离锚放宽至 40% 保护止盈止损、旁路审计告警留痕、10/10 专项单测全绿，已合入 Commit 47301e2 |
| 2 | 租户急停「假刹车」安全漏洞（UI 写入与风控读路径断链） | Reviewer 阶段三审查审查发现 | Coder / Reviewer / Tester | **P0** | **已完成** | `set_kill_switch` 升级严格双写（tenant + account 双键），四段断言全过，风控引擎 BUY 闸门毫秒级阻断，已闭环 |
| 3 | 阶段三：全局动态多租户切换视窗与多账户切片展示 | N-Tenant 三阶段计划 | Coder / Reviewer / Tester | P0 | **已完成** | 顶部导航租户切换器 + 41 页面切片改造 + `moomoo_real` 注册同源对齐，静态巡检 0 错误 |
| 4 | 独立模拟 Worker 验证（`--tenant futu_paper_sandbox`） | 生产运维验证 | Ops | P0 | **已完成** | 盘后 `--once` 实测 Exit 0，独立单例锁隔离生效，主服务（13356）心跳 HEALTHY 零受扰 |
| 5 | 复盘任务 27 轮重试与 `service.log` 行缓冲加固 | Ops 运维排查 | Coder / Ops / Reviewer | P0.5 | **已完成** | KeyError 彻底修复，08:18 报告完成并送达 IM；run_service 注入行刷新与 -u 参数 |
| 6 | Tester 全量回归 520 例（507 基线 + 13 租户与安全守卫） | 质量门禁基线 | Tester | P0 | **已完成** | 全量 520/520 全绿（2分47秒，exit 0） |
| 7 | QQQ 模拟仓孤儿单结案归档 | 用户指令 | Chief | - | **已完成** | 市价单 9118273 于 $3.72 成交结清，实现盈亏约 -$131，审计已落库 |
| 8 | DB 备份恢复点 `pre-stage2-20261020T190942Z` | Reviewer 阶段二放行清单 | Ops | - | **已完成** | 1.5GB 完整校验（SHA-256 一致 + integrity_check ok + 52 表 837 意图） |
| 9 | DR 灾备校验环境适配（`verify_latest_backup.py` / `create_recovery_point` 支持 `TMPDIR` 重定向） | Ops 运维需求 | Coder | P2 | 待排期 | 官方恢复点本体在沙箱外服务不受影响，此项只惠及受限会话内自检 |
| 10 | 租户切换器默认租户产品定性（无 session 时默认落 REAL 还是 SIMULATE） | Product 需求把关 | Product | P2 | 待对齐 | 当前代码默认回退落 REAL，待与 Product 最终对齐 |
| 11 | 自适应多租户一键启停（`start.bat`/`stop.bat`/`launch_workers.py`） | 用户需求（配置驱动零心智负担） | Coder / Product / Ops | P0 | **已完成** | 动态发现 `is_active=True` 租户、防重拉、定向级联停止；CREATE_NO_WINDOW 消除弹窗，Commit af8ab5e/fb1bbe5 |
| 12 | 全局 UTF-8 统一固化（根治 Windows GBK 乱码） | 用户需求 | Coder / Ops | P0 | **已完成** | sitecustomize.py 全局钩子 + PYTHONUTF8 环境注入，Commit a821f2c |
| 13 | 风控对账隔离精细化（全局连坐误伤根治）+ 历史虚构账本出清 | 2026-10-22 AMD 误伤病例 | Coder / Reviewer / Tester | **P0** | **已完成** | 标的级+策略级+租户级三级隔离，SELL 通道永不阻断，实盘/模拟历史残留账本全部校准归零，Commit 3af3384 |
| 14 | $0.01 极低价下单与撤单测试用例 + 实机探针工具 | 用户需求 | Coder / Reviewer / Tester / Ops | P0 | **已完成** | 模拟沙盒实测闭环（订单 9123615/9123623 毫秒撤单零成交）；实盘探针经全套门禁验证被富途境内 IP 监管拦截（待 Proxifier 启动后复跑），Commit 75168a4 |
| 15 | 三账对账异常穿透告警与防静默熔断 | 用户需求（杜绝静默拦截） | Product / Coder / Ops / Reviewer | **P0** | **已完成** | 飞书红卡秒级穿透 + 绿色自愈闭环 + 30分钟巡检置顶 + 41页全局Banner + 一键平账，告警吞噬 Bug 根治，84/84 测试全绿，Commit 5dead60，PRD 已归档飞书 |
| 16 | 实盘出海网络恢复（Proxifier 客户端启动）并复跑实盘 $0.01 探针 | 实盘探针被富途境内 IP 拦截 | 用户 / Ops | **P0** | **等待用户操作** | 需用户在桌面双击启动 Proxifier（C:\Program Files (x86)\Proxifier\），OpenD 流量经 7900 SOCKS5 出海后实盘报单即恢复；启动后 Ops 复跑探针拿回真实券商回执 |
| 17 | 对账误报复发：实盘账本未计入租户内原生底仓偏移，core_satellite 被反复熔断 | 2026-10-22 用户二次告警（实盘租户） | Coder / Reviewer / Tester | **P0** | **已完成（线上闭环）** | 根因：`store.virtual_positions()` 实盘分支（store.py:2238-2279）只汇总 `order_intents`+`virtual_allocations`，未计入 `reconciliation_adjustments`（模拟分支 :2281-2304 计入）；叠加 `reconciliation_adjustments` 表无租户维度（仅 strategy_id+symbol 唯一），导致实盘 NVDA -1/GLD -2 对券商 7/2.4344 差异 8/4.4344 被误判丢单，且模拟租户 20170229 GLD 被反向污染。豁免条件（reconciliation.py:107）要求账本≈0 才生效，账本非 0 时永远不触发 |
| 18 | 对账恢复路径持久修：恢复扫描对称清全部前缀变体 + 租户启用键名口径统一 + restore 快照覆盖带前缀键 | 幽灵锁与键名口径根因（Reviewer/Tester 守卫实测为红） | Coder / Reviewer / Tester | **P0** | **待门禁 + 重启窗口** | 代码已落盘、幽灵锁/键名口径守卫已转绿；剩 restore 快照缺口（`reconciliation.py:26-29` 快照只记无前缀键、`:49` 读带前缀键必落空 → 用户主动停用的策略会被对账**静默改回启用**）1 条红守卫，修完复跑 578 例。上线后实盘 core_satellite **首次真正进入调度**（实盘真金首次自动开仓），需人工盯第一轮。"定时炸弹"（无前缀 `strategy_enabled` 两枚为 false）已由读侧租户感知拆除，全局键保持原值 |
| 19 | 模拟 Worker 挂死进程清理（PID 16136） | Ops 巡检 | Ops / 用户 | **P1** | **待用户操作** | 阻塞在 OpenD 调用、不响应停止信号、心跳与日志长期未刷新；`start.bat` 因"看着活着"会跳过它；需 `stop.bat` 的 taskkill 收掉后再 `start.bat` |
| 20 | P2：`recovery_blocked` 写侧偏宽——`run_service.py:907`、`:1312`、`:1316`、`recovery.py:18` 四处**只写无前缀全局键**，而 `reconciliation.py:197` 写的是账户级键 | Architect 对照表复核（第二轮） | Coder / Reviewer | P2 | **待排期** | 影响有界：`risk.py:126` 的无前缀读仅在 `strategy_id is None` 时生效，带策略的正常下单路径不读它；建议四处改为 `f"recovery_blocked:{account_id}"` 与 :197 对齐。**更正**：本族不能表述为"完全对称"，准确说法是"读侧覆盖写侧，但写侧偏宽" |
| 21 | P2：`ui.py:444-445` 用 `tid in k or str(acc) in k` **子串**过滤展示挂起策略，非精确前缀 | Architect 对照表复核（第二轮） | Coder / Reviewer | P2 | **待排期** | 纯展示、不影响下单；若某租户 ID 是另一个的子串，侧边栏可能显示别家租户的挂起策略。建议改精确前缀匹配 |
| 22 | 更正记录：`symbol_blocked` 的"账户未知时用 `= NULL` 语义、legacy 键不命中"表述**不成立** | Architect 对照表复核（第二轮） | Chief | — | **已更正** | `risk.py:98` 明确读无前缀 `symbol_blocked:{symbol}`，`tests/test_execution.py:126` 正是靠无前缀键阻断——**无前缀 legacy 键仍会命中，这是有意的回落档，不是缺陷**。禁止据此清理 legacy 行 |
| 23 | 模拟侧 legacy 数据清理（`tenant_id=''` 调整行 15 条 + 加密币陈旧仓位） | 模拟 Worker 复活后首次对账暴露的真实 MISMATCH（SPY 162 vs −263） | Ops / Coder / 用户 | **P1** | **已完成（线上闭环）** | 未删任何历史成交（审计轨迹保留）；按纸面券商**实际持仓重算**写入 14 行账户级调整（SPY +425 / QQQ +277 / NVDA **+267** + 11 只期权 ±1），非照搬旧值（旧值 +211 已不准，会差 56 股）。备份 `data/qa/backup_paper_legacy_20261022T185801.json` 可回滚。结果：纸面 9 个标的全 MATCHED、4 枚策略锁（两种前缀）+ `recovery_blocked` + `symbol_blocked` 全 false、侧边栏两侧均不显示 |
| 24 | 侧边栏对账警示横幅两处缺陷：策略名重复（账户前缀+租户前缀各扫一次）、策略名为空时仍弹空横幅 | 18:42 用户实报（模拟侧重复、实盘侧空名） | Coder / Tester | **P1** | **修复中** | `ui.py:447-449`：`has_blocked_symbol` 为真但 `disabled_strats` 为空时仍渲染。修：去重 + 空名不渲染（或改为明确的"存在阻断标的"文案） |
| 25 | `stop.bat` 加固后真实验证（自提权 + `[FAILED]` + `exit /b 1` 路径） | Coder v4 加固（GBK/CRLF 已修） | Ops / 用户 | **P1** | **待验证（今晚收盘后）** | 开盘前不拿刚上线的 Worker 做实验；收盘后双击 stop.bat 验证能真正级联停干净。建议改为**条件提权**：先按普通权限杀，仅在有 Worker 存活时才自提权重试（避免每次停止都弹 UAC） |
| 26 | 悬案排查：订单 9113511 在券商列表消失 + 环境不一致 | 10-19 起 OPEN，10-22 清理提示时一并标记已解决 | Coder / Reviewer | **P2** | **已完成（误报，无残留风险）** | 结论：该单是**纸面沙盒单**（`intent_key` 尾部 `PAPER`、走 SIMULATE 通道），旧代码告警前**无环境过滤**，实盘执行器拿 REAL 环境去比 PAPER 单 → 误报；`incidents` 52 次实为**三笔单 × 多轮巡检**合计（9112627/9113079/9113511）。该缺陷已于 `1da274b`（10-20 20:23）修复，当前代码无产生路径。终态：`CANCELLED_ALL`、成交 0、无挂单、实盘无期权仓。安全断言独立验证成立：三个事件字符串仅 `execution.py:421/423` 写入，**无任何读取点** |
| 27 | **红区已拍板**：`volatility_long_gamma` 实盘免人工确认自动下真金期权单 | Coder 排查 9113511 时在**同一标的同一秒**发现实盘真金单 840 | 用户 | **P0** | **用户拍板：A 保持现状（10-22 19:31）** | 用户明确选择维持 `require_manual_confirm: false`，即**期权策略 + 真金 + 免人工确认**维持现状，风险由用户自行承担。**不做任何代码改动、不需要重启**。保留的既有安全网：单笔 2500 / 单日 5000 额度上限、账户白名单、三账对账熔断与禁静静默拦截。后续如要改回人工确认，改 `real_account_config:281756481476169109` 的 `require_manual_confirm` 并重启实盘 Worker |
| 28 | `data/service-futu_*.log` 中文乱码（GBK 编码），全局 UTF-8 固化漏了这条写入路径 | 21:33 用户反馈"雷达没日志"时顺带发现 | Coder / Ops | **P2** | **待修** | `service-futu_real_master.log` 正文为 GBK 乱码（如"琛屾儏/鐩樺彛妫€鏌ユ湭閫氳繃"），该文件由 `launch_workers.py` 重定向产生，未走 `PYTHONUTF8` 体系；影响日志可读性，不影响交易。修法：该重定向路径显式指定 UTF-8 |
| 29 | **实盘首笔自动交易已成交 + 价内期权行权风险（用户拍板 A：不干预）** | 21:39 `volatility_long_gamma` 自动买入 `US.META261030P660000` 看跌 1 张 @ $20.76（真实券商单 `FH1D53D24E3F87D000`，面值约 $2,076） | 用户 / Ops | **P0 实盘** | **用户拍板 A：不干预（10-22 21:5x）** | 该单是用户选 A（免人工确认）后的**第一笔实金自动交易**，额度在限内（单笔 2500 / 单日 5000），对账已对齐（1.0/1.0 MATCHED）。系统同时报 `OPTION_LIFECYCLE_RISK`（654 次）：**剩余 8 天、价内、内在价值 $9.40、预计交割正股缺口 100 股**——即被行权会变做空 100 股 META。用户明确选择让策略自身的止损/生命周期管理处理，**团队不自行平仓或移仓**。持续监控该事件，若临近到期风险升级立即回报用户 |
| 30 | **跨租户串扰**：波动率机会链是单租户形状，实盘成交导致模拟侧"以为已做过"而跳过 | Architect 逐行核实（`volatility_scanner.py:366`、`run_volatility_strategy.py:100`、`run_service.py:615`） | Architect / Coder / Reviewer / Tester | **P2** | **已立项，待 03:45 窗口后动手** | 事实：`VOLATILITY_CAMPAIGNS` 与 `volatility_signal_active:{symbol}` / `volatility_scanner_notified:{symbol}` 均为**全局键、无租户前缀**；两侧 Worker 读**同一份机会清单**，`campaign_state` 键为 `strategy:code` 也是全局。故实盘开 META 后全局状态标"已处理"，模拟 Worker 读同一份即跳过。**不是"模拟缺一条雷达"**。改法（Architect 方案）：一次扫描、**按租户扇出**——机会清单命名空间化为 `VOLATILITY_CAMPAIGNS:{tenant_id}`，读侧租户键优先、无前缀全局键兜底；`campaign_state` 同样加租户维度；雷达对每个启用租户各写一份。**明确否掉"给模拟再配一条雷达"**（两条雷达会同时写同一全局清单、互相覆盖，更糟）。**只影响模拟盘、不涉及真金**，等 `core_satellite` 首轮调仓（03:45）稳定后再动 |
| 31 | `volatility-orphan-broker-positions` 孤儿持仓告警 | 21:49 起出现、22:06/22:11 再响 | Ops / Reviewer | **P1 实盘** | **已查明：误报，事件已自动恢复（22:11:41）** | 三次告警分别对应 META（21:49，已平）、AMZN P250（22:06）、AMZN P252.5（22:11），**每次都紧跟一笔新期权成交**。原因：检测器在"新成交的下一轮"运行，那一刻新仓位尚未登记进 campaign → 判成孤儿 → **当场 ADOPT**（建 `orphan:US.xxx` 的 exit-only campaign）。**每笔新期权成交都会响一次，不是漏管**。真金敞口已接管：两张 AMZN 看跌在实盘账户、各 1 张、`exit_only=true`（纳入止盈止损、不再加仓）；当前浮亏 -9.9% / -12.9%，**10-28 到期（6 天），价内被行权会分派成做空 100 股 AMZN**，持续监控 |
| 32 | 告警文案不带合约名；"当轮已 ADOPT"的孤儿不该记 ERROR | Ops 查孤儿告警时提出 | Coder / Reviewer | **P3** | **待排期** | `volatility_orphans.py` 文案只写"发现 N 个"，用户看不到是哪个合约；且同轮已接管的孤儿按 ERROR 记会持续制造噪音（今晚 3 次）。建议：文案写入具体合约名；同轮已 ADOPT 时降级为 INFO |
| 33 | Ops 会话沙箱拦掉全部进程执行（python/cmd 均 Access denied），监控探针降级为扫 DB 原始字节 | 22:20 Ops 反馈 | Chief / 平台 | **P2** | **待处理** | Ops 无法跑常规只读探针（Python 执行被沙箱拒绝），只能直接扫库，监控能力受限。**Chief 会话可正常执行 Python，暂由 Chief 兜底跑探针**；长期应把该会话切到「完全权限」 |
| 34 | **"为什么下这一单"对用户不可见**：决策依据只写进 DB 元数据，界面无可读呈现；且信号告警晚于成交 | 22:36 用户质疑 META 那笔"都没见到数据" | Product / Architect / Coder | **P1** | **待立项** | 事实：21:39 那笔 META 买入的入场理由（`reason=动态波动与方向确认后首次入场`、`entry_tier=强波动`、`reference_price=21.75`、`initial_risk=761.25`、预算 4500/9000）**只存在于 `order_intents.metadata_json`**；界面无任何对应呈现。更严重的是**时序倒挂**：雷达的 `VOLATILITY_SIGNAL_DETECTED`（US.META）告警产生于 **21:43:50**，**比 21:39:21 的成交晚了约 4 分钟**——先下单、后出信号记录，用户必然"见不到数据"。要求：① 把决策依据做成用户可读界面（入场理由/风险档位/参考价/当时市场条件）；② 保证信号记录先于或同步于下单 |
| 35 | **单笔硬止损额度与接管时延**：META 实际按 $300 触发（非代码默认 $100），且新成交要等下一轮才被接管 → 止损空窗 | 22:4x 用户质疑"最高亏 100 美元的限制呢" | Coder / Reviewer / Product | **P1** | **待修** | 事实：平仓单（intent 866）理由原文 **"单笔硬止损触发：亏损 $366.00 ≥ $300.00"**——**止损确实触发了**，但触发线是 **$300**。来源：`run_gamma_strategy.py:1098` `max_loss_usd=float(campaign.get("max_position_loss_usd", 300))`，而 `volatility_optimizer.py:450` 的函数默认是 `100.0`；META 这个 campaign **未设 `max_position_loss_usd`** → 落到 300。**且**：该仓位 21:39:21 成交、**21:49 才被 ADOPT 成 exit-only campaign**（即"孤儿持仓"告警），**接管前不在止损管理范围内**，接管后到下次评估亏损已跑到 $366。改进：① 所有波动率 campaign 显式设 `max_position_loss_usd`（按用户意图，勿依赖默认值）；② **新成交同轮接管**，消除止损空窗。**22:5x 用户拍板：止损口径采纳 Product 双层方案** —— 相对止损 **-25% 权利金**（主防线，随仓位缩放）+ 绝对止损 **$300**（兜底，且 ≤ 净值 0.5%），组合层 $1,500 / $3,000 不动；三条硬要求：限额必须显式写入每个 campaign 且下单前强制校验、UI 可见可改留痕可审计、**任何缺省都视为配置错误并告警、绝不静默兜底**。**接管时延根因（用户问的"21:49 为什么才接管"）**：接管是**轮询式**而非事件式——`run_volatility_strategy.py:147` 在每个 `run_once` 周期才调 `manage_volatility_orphans`，成交路径本身不把"已持有"写进 campaign 状态，故成交（21:39:21）到接管（21:49）相差整整一个扫描周期，**该周期内仓位不在任何止损管理范围内** |
| 36 | **🔴 P0：风险告警被静默**——`OPTION_LIFECYCLE_RISK` 成交后 1 秒即检出，但 `delivered=0` 从未推送飞书 | Product 查 `alerts` 表发现（21:39:22 告警、`delivered=0`；`VOLATILITY_SIGNAL_DETECTED` 同样 `delivered=0`） | Coder / Ops / Reviewer | **P0** | **修复中** | 系统检出"剩余 8 天、价内、内在价值 $6.59、**预计交割正股缺口 100 股**"后**未推送任何通知**——正是用户明确要求"严禁静默"的那类风险。Architect 定位根因：`alerts.py:32-35` ERROR/CRITICAL 先 `record_incident`，若事件已 opened 且不在白名单则**直接 return，连告警行都不写**；Feishu 只推白名单/CRITICAL。修法（Architect P1-a）：白名单只控制"推送"、**不控制"落库"**；两个事件加白名单；新增**触达失败升级**（ERROR 落库后 N 分钟未送达 → 再发一条 CRITICAL `ALERT_DELIVERY_FAILED`） |
| 37 | 实盘自动交易暂停（用户指令） | 22:48 用户："先停了，马上修复吧"；22:5x 补充："接管不要停，持仓要继续跟进" | 用户 / Chief | **P0** | **已按用户要求调整** | **已改为精确模式**：`volatility_new_entries_paused = true`（只停开新仓）+ `strategies_paused:futu_real_master = false`（恢复策略运行 → 持仓退出/止损管理在跑）。**不再是整租户暂停**，`core_satellite` 亦恢复调度 |
| 38 | **🔴 P0：卖出（平仓）被"当日开仓额度"误拦** | 22:56 两笔 AMZN 平仓卖出被拒 | Coder / Reviewer / Product | **P0** | **根因已定位，待修** | 机制：`real_trading.py:146`（累加）与 `:230`（取值）均为 `abs(quantity × price × multiplier)`，**两处都不看 `side`** → SELL 与 BUY 扣同一份额度；`.env` 已有 `RISK_MAX_REDUCTION_ORDER_NOTIONAL=150000` 但此闸从未接入。后果：预算被**双向消耗**，白天开仓用掉额度、晚上盈利的平仓单被自己的买入额度挡住。修法：开仓额度只计 BUY，SELL 走独立减仓额度。**23:1x 临时调高账户级 `max_daily_notional`（5000→20000）已回滚**（它没派上用场，且副作用是放宽了同账户 `core_satellite` 的买入额度） |
| 39 | **🔴 P0：$366 止损被"退出冷却"挡住 4 轮** + 接管合约号被表达复核改写 | Coder 从 `decision_logs`/`audit_events` 取到原始记录，推翻了此前"成交后 10 分钟无人管"的说法 | Coder / Reviewer / Product | **P0** | **根因已定位，待修** | **更正**：仓位在成交后 **5 秒**（21:39:26）即被识别并管理（21:40–21:56 跑盈利保护，浮盈 6.7%/5.5%/12.5%…），21:49:39 那次是**第二次接管**。真机制两条：① **表达复核不跳过 `exit_only`**，照常重选行权价，把 `code` 从 P660000 覆写为 **P670000**（`audit_events` 留痕 `14:13:00 VOLATILITY_CONTRACT_ROLLED old=P660000 → new=P670000`）→ campaign 指向不存在的合约，实际持仓等于没人管；② **冷却分支（`run_gamma_strategy.py:805-863`）在硬止损（`:1095`）之前**且命中即 `return` → 21:57–22:00 每轮都是"同一标的退出冷却中，剩余 24 小时"、`position_quantity` 全空 → **正常退出判定被冷却吃掉**，硬止损直到 22:01:17 才等到机会。修法：`exit_only` 跳过表达复核；**硬止损绕过冷却**；接管时校验 `campaign.code == 实际持仓合约` |
| 40 | **P0-1 前置：`fill_ledger` 已存在 13 个 intent 双计** | Reviewer 实测（intent 818：intent_qty=1.0 但 ledger_rows=2 / ledger_qty=2.0） | Coder / Reviewer | **P0** | **待修（P0-1 落地前必须先做）** | 机制：`store.py:1126-1138` 的 `update_by_broker_order` 按 `delta = 券商回报成交量 − order_intents.filled_quantity` 插 `fill_ledger`，**既不带 `broker_deal_id`、也无唯一约束**；`filled_quantity` 被清零过（备份恢复/迁移回填）后再报一次就再插一行。今天没爆是因为 `virtual_positions` 读 `order_intents` 而非 `fill_ledger`，但 `fill_ledger` 驱动 **P&L / `position_batch` 批次 / `allocate_exit` 平仓成本分配**，都是双计的。要求：先去重（按 intent 保留最早一行）；去掉 SIMULATE 过滤后同一笔 REAL 成交将有**三个写入者**，必须统一到一个写入者（`record_fill` 有 deal_id 去重；`update_by_broker_order` 的 delta 行必须同样带 `broker_deal_id`） |
| 41 | **止损口径定案（用户拍板：按 Product 专业意见）** | 23:2x 用户："按 product 的专业意见来" | Product / Coder / Reviewer | **P0** | **已定案，待落地** | **废除单一固定美元额，改双层**：① **相对止损（主防线）** = 权利金 **-25%**，随仓位规模自动缩放；② **绝对止损（兜底）** = 单笔 **$300** 且 ≤ 账户净值 0.5%，防极端行情失守；③ 组合层红线不变（单笔 $1,500 / 单日 $3,000）。**三条硬要求**：限额**必须显式写入每个 campaign** 且**下单前强制校验**；**UI 可见、可改、留痕可审计**；**任何缺省都视为配置错误并告警、绝不静默兜底**（消除 `volatility_optimizer.py:450` 的 100 与 `run_gamma_strategy.py:1098` 的 300 两个默认值打架）。**并修时效**：硬止损绕过冷却（第 39 项②）+ 成交即登记（P0-1），否则限额设得再准、仓位不在管理范围内也白搭。存量 campaign 必须先补值，否则会立刻进入"拒绝开仓" |
