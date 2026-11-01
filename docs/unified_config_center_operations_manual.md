# 统一配置中心操作与运维手册 (生产归档版)

**文档版本**：v1.2 (2026-11-01)  
**适用系统**：Tradeyi 量化系统统一配置中心  
**核心代码路径**：`src/futu_quant/config_center/`、`pages/43_unified_config_center.py`、`pages/18_infrastructure_disaster_recovery.py`  
**归档位置**：`E:\Thisyi\trade\docs\unified_config_center_operations_manual.md`

---

## 一、配置中心使用说明

### 1. 权限与角色准入
- **谁能修改**：系统严格限制仅管理员（持有效 `admin_token` 会话）可操作配置变更；
- **Actor 派生规范**：所有写入操作必须携带合法 Actor（格式形如 `streamlit_admin:<会话标识>` 或 `service:<组件标识>`），严格杜绝无身份或静态匿名调用；
- **准入键白名单**：目前低风险可受控编辑键严格收敛为 `alert_mute_list`；CRITICAL 级别核心门禁（资金/风控/实盘环境）坚决排除在页面编辑之外。

### 2. 怎么修改配置（草稿发布审批流）
1. **新建草稿**：在配置管理页面选择待改配置项，输入新值与**必填的修改原因 (reason)**，系统校验后生成 `DRAFT` 状态发布单，此时运行态仍保持原值，草稿绝对物理隔离；
2. **基线校验**：发布时底层在单个 `BEGIN IMMEDIATE` 事务内先取得写锁，实时核对当前数据库版本是否严格等于草稿创建时的 `base_version`；
3. **原子发布**：校验通过后，在同一事务内完成运行值更新、版本历史新增、发布单置为 `PUBLISHED` 并自填 8 项对称审计字段（`before/after/base_version/result_version/release_id/config_key/actor/reason`）。

### 3. 怎么回滚配置
1. **一键回滚**：当新配置运行异常时，管理员点击回滚按钮并输入**必填回滚原因**；
2. **单事务逆向恢复**：系统在 `BEGIN IMMEDIATE` 事务内实时读取当下运行值作为审计 `before`，将已归档的历史快照恢复至 `runtime_config`，生成新递增版本 `result_version`，发布单置为 `ROLLED_BACK`，并在 `audit_events` 原子写入 `CONFIG_RELEASE_ROLLED_BACK` 事件，零半状态风险。

### 4. 怎么拒绝与作废草稿
1. **过期草稿出口**：当并发发布导致草稿基线过期时，界面显式提示警告黄条并禁用发布按钮；
2. **审计归档作废**：管理员点击拒绝并输入非空原因，系统单事务校验 Actor 并将状态更新为 `REJECTED`，同时在 `audit_events` 记录 `CONFIG_RELEASE_REJECTED` 审计。

### 5. 变更审计怎么查询
- **数据源**：`audit_events` 表；
- **查询事件名**：
  - `CONFIG_RELEASE_PUBLISHED`：配置发布成功；
  - `CONFIG_RELEASE_ROLLED_BACK`：配置已回滚；
  - `CONFIG_RELEASE_REJECTED`：草稿已作废拒绝；
  - `INCIDENT_RECOVERED`：告警恢复联动审计；
- **审计完整性**：所有 payload 100% 具备 `actor/reason/before/after/base_version/result_version/release_id/config_key`，全程可追溯。

---

## 二、配置生效时机与“热生效 / 需重启”清单（逐项绑定读取源码）

作为下一次重启窗口（桌面 `stop.bat` → `start.bat`）的客观验收依据，明确划分两类配置项：

### 1. 动态热生效项（无需重启生产 Worker 服务）
修改或发布后，运行中服务/页面每轮调度或下次事件触发时自动重新从 SQLite 读取生效：

| 配置项 | 读取消费者函数与源码位置 | 热生效原理与周期说明 |
|---|---|---|
| `alert_mute_list` | `AlertManager._mute_list` (`src/futu_quant/alerts.py:211`) | 每次触发告警调用 `send()` 时实时重读 SQLite，即刻生效 |
| `VOLATILITY_CAMPAIGNS:{tenant}` | `store.get_campaigns` (`src/futu_quant/store.py:1675`) | 雷达与波动率执行器每轮调度循环（30s）重新读取 SQLite，下周期生效 |
| `volatility_new_entries_paused:{tenant}` | `volatility_new_entries_paused_for_tenant` (`src/futu_quant/volatility_pause.py:33`) | 策略开仓门禁执行时直接查询 SQLite，1-2 分钟内开仓调度重读生效 |
| `strategy_enabled:{tenant}:{strategy}` | `resolve_configured_strategies` (`scripts/run_service.py:125`) | 调度器每个周期分发任务前实时重读 SQLite，下轮任务分发生效 |
| `real_account_config:{account}` 账户限额 | `resolve_real_account_config` (`src/futu_quant/real_trading.py:72`) | **账户级日额度与单笔限额**在 `submit_limit()` 门禁下单前实时重读 SQLite 生效 |
| `risk_breaker_mode:{tenant}` | `resolve_risk_breaker_config` (`src/futu_quant/risk_breaker.py:105`) | 风控引擎每轮计算风险敞口时从 SQLite 实时拉取最新熔断档位 |

### 2. “改了不重启不生效”清单（必须在维护窗口双击 stop.bat/start.bat）
涉及底层通信链路、启动阶段静态常量冻结或进程代码加载，**修改后必须重启**：

| 配置项 / 场景 | 读取消费者函数与源码位置 | 为什么必须重启才生效（技术依据） |
|---|---|---|
| 全局实盘门禁：`REAL_MAX_DAILY_NOTIONAL` / `REAL_MAX_ORDER_NOTIONAL` | `Settings.load` (`src/futu_quant/config.py:722-729`) | **仅在账户无专有配置时的全局兜底**。全局 Settings 随进程拉起加载一次并冻结，运行期不重新执行 `Settings.load()` |
| 全局实盘环境开关：`FUTU_TRADING_ENV` / `REAL_TRADING_ENABLED` | `Settings.load` (`src/futu_quant/config.py:716-720`) | 启动时锁死实盘/模拟网关上下文，禁止运行时动态改变进程级 trading_env |
| `ENV_SETTINGS` | `refresh_runtime_environment` (`src/futu_quant/config.py:626-655`) | 注入到 `os.environ`，但在生产 Worker 主循环中**不会周期性主动调用 `Settings.load(refresh=True)`**，因此全局 Settings 不自动刷新 |
| `FUTU_OPEND_HOST` / `FUTU_OPEND_PORT` | `FutuPaperBroker.__init__` (`src/futu_quant/broker.py:115`) | OpenD TCP 通道在 Worker 启动时建立长连接，运行中不重连新端口 |
| `USMarketCalendar` 交易日历实例 | `market_calendar.py:6` (`src/futu_quant/market_calendar.py:6`) | 静态类加载缓存，节假日日历在服务拉起时单例构建 |
| 依赖注入与代码更新（.py / .bat） | 全局模块导入 | Python 解释器在内存中常驻已加载 bytecode，不重启绝对无法装载新逻辑 |

---

## 三、生产环境重启防线与严密解耦步骤

针对 G6（启动时通过只读连接加载 `ENV_SETTINGS`）改动，制定严密上线与防回退纪律：

### 1. 回滚基线记录
- **回滚 Tag**：`v2026.11.01-6`（提交 `433fb20`）；
- 若重启发生异常，立即回退该 Tag 并重新启动。

### 2. 重启前必须执行的预检命令（生产工作目录与生产 venv）
在 `E:\Thisyi\trade` 目录下使用生产 Python 执行下列单行命令：
```bash
E:\Thisyi\trade\.venv\Scripts\python.exe -c "import sqlite3, json; from futu_quant.config import Settings; c = sqlite3.connect('file:data/trading.db?mode=ro', uri=True); db_w = json.loads(c.execute('SELECT value_json FROM runtime_config WHERE config_key = ?', ('ENV_SETTINGS',)).fetchone()[0]).get('WATCHLIST'); s = Settings.load(refresh=True); print('SQLite ENV_SETTINGS:', db_w); print('Settings.load:      ', ','.join(s.watchlist)); assert db_w == ','.join(s.watchlist); print('PRECHECK PASS: SQLite权威配置与Settings.load完全一致，启动路径安全！')"
```
**放行条件**：输出 `PRECHECK PASS` 且无异常栈，证明启动链路确实读取了 SQLite 权威配置且无报错。

### 3. 停盘维护窗口严密解耦操作顺序
**严禁将服务重启与数据清理/迁移混在同一操作中**，严格按三步分阶段执行：
1. **第一阶段：服务重启与观测**：
   - 执行上述预检命令通过；
   - 桌面双击 `stop.bat`，等待 5 秒确认旧进程完全退出；
   - 桌面双击 `start.bat` 拉起新服务；
   - 观察 5 分钟，确认两租户 Worker 心跳正常（`HEALTHY`），无异常日志。
2. **第二阶段：独立执行 `task_runs` 14 天受控清理与空间回收**：
   - 严禁调用会连删 10 张表的 `store.cleanup()`；
   - 生产备份后，由 Ops 执行专用 `task_runs` DELETE（预计清理 205,932 行，保留每个 task_id 最新行，保留 RUNNING 行）；
   - 执行 `VACUUM` 回收 1.08 GB 物理空间。
3. **第三阶段：执行 G8 三阶段迁移演练**：
   - 运行 `python scripts/migrate_v25_task_runs_tenant_id.py --db data/trading.db` 完成加列、回填与索引构建。

---

## 四、Phase 5 架构与治理缺口综合台账（四栏标准）

对系统中已识别的技术债与治理缺口建立四栏标准化跟踪台账：

| 缺口 ID | 现状描述 (Current State) | 风险评级与分析 (Risk Analysis) | 计划治理方案 (Remediation Plan) | 是否本批修 (Fixed in Phase 5?) |
|---|---|---|---|---|
| **G1** | `VOLATILITY_CAMPAIGNS:{tenant}` 由雷达与自愈高频调用 `set_runtime_config` 直写库 | MEDIUM：单份数十 KB 并产生巨量 `config_versions` 历史，加剧数据库膨胀 | 纳入直写白名单；长期将持仓活动独立沉淀至持久化业务表，彻底剥离配置表 | **已治理** (固化直写白名单) |
| **G2** | `risk_breaker_mode:{tenant}` 熔断自愈逻辑直接修改 `system_settings` | LOW：绕过发布单，无法通过发布单进行审批流追溯 | 纳入紧急自愈直写白名单，直写处补充合法租户白名单校验 | **已治理** (固化直写白名单) |
| **G3** | `strategy_enabled:{tenant}:{strategy}` UI 页面调用 `strategy_toggle` 直写 `system_settings` | LOW：页面直接切库，绕过发布单基线与审批防覆盖 | 纳入 UI 直写白名单，直写前显式调用 `validate_instance_key` 守卫 | **已治理** (固化直写白名单) |
| **G4** | `strategy_execution_mode:{tenant}:{strategy}` 漂移自愈降级直接写 `system_settings` | LOW：自动降级绕过发布单 | 纳入紧急风控直写白名单，确保紧急降级零阻塞秒级生效 | **已治理** (固化直写白名单) |
| **G5** | `volatility_new_entries_paused:{tenant}` 页面一键暂停开仓直接写 `system_settings` | LOW：绕过发布单 | 纳入页面控制直写白名单，当前已有租户非空 fail-closed 守卫 | **已治理** (固化直写白名单) |
| **G6** | 原 `_load_runtime_value` 内 `TradingStore()` 无参自建生产连接 | HIGH：违反“库函数不得自建默认生产连接”红线，读配置路径存在写入隐患 | 重构为 `_get_active_store`，单次 load 严格只建 1 次只读连接复用，零反向写库 | **已修复** (v2026.11.01-7 已合入) |
| **G7** | `core_satellite_cash_yield_enabled` spec 声明 `system_settings`，实际落表 `runtime_config` | LOW：Spec 元数据声明与实际落表不一致 | 修正 `_specs_data.json` 为 `runtime_config`，并在 registry 显式排除草稿白名单 | **已修复** (v2026.11.01-7 已合入) |
| **G8** | `task_runs` 表缺少 `tenant_id` 字段 | MEDIUM：多租户 Worker 并行调度时任务归属不可判，死 PID 判定存在租户交叉 | 编写三阶段解耦迁移脚本 + 运行时双写过渡，生产待窗口执行 | **已就绪** (代码与脚本已合入) |
