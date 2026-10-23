# 停机窗口检查清单（stop → 迁移 A → 回读 → start）

> **本次窗口范围（2026-10-23 修订：迁移 A 已与 #1 第 3/4 步合并为同批）**
> - **含**：迁移 A（`migrate_volatility_campaigns_tenant_scope.py --apply`）· 迁移 B 第 ② 层种子（`risk_limits:futu_paper_sandbox`）· #1 第 3/4 步切流 · 雷达 `PYTHONUNBUFFERED=1`
> - **不含**：#2 雷达扇出（依赖 #1，排在其后）
>
> 归口：Ops。顺序以 Migrator 的迁移清单为准。
> 原则：**任一步不符 → 停止，不启动，走回滚。**
> **重启前必须全量回归绿**（窗口要重启加载新代码，绿了才开窗口）。

## 窗口时间表（2026-10-23 修订版）

| 时间 | 事项 | 谁 |
| :--- | :--- | :--- |
| 16:30–16:45 | 收尾 `risk.py` 回撤那 1 条红 | Coder |
| 16:45–19:30 | **#1 一整段**：D 批 → 写点 9 处 → 读点 17 处 → Tester 守卫 | Coder + Tester |
| **19:30** | **代码硬截止**（过点顺延明天，不赶工） | all |
| 19:30–19:50 | 全量回归（全仓冻结） | Chief |
| **19:50–20:20** | **停服窗口：迁移 A** | Chief + Migrator |
| 20:20–21:30 | 起服核验 + 缓冲 | Ops |
| 21:30 | 开盘，观察一个完整周期 | all |

---

## 0. 前置条件（不满足就不开工）

- [ ] 休市时段（美股收盘后 / 开盘前），**无在飞订单**
- [ ] **实盘开仓开关已确认**：`volatility_new_entries_paused:futu_real_master` 的**当前值**（**用生产解析函数 `run_gamma_strategy.volatility_new_entries_paused(store, broker)` 判，不要读全局键**）—— 全局键 `volatility_new_entries_paused=true` 只是**兜底**，不代表生效值
- [ ] 三账 MATCHED（**DB 直读**）：`reconciliation_reports` 今日非 MATCHED = 0；`recovery_blocked = false`
- [ ] **#1 第 1 步的等价守卫已定向绿**（见 §3.1）
- [ ] **全量回归已绿**（守卫夹住整表指纹）
- [ ] 记录迁移前指纹：
  - `runtime_config` 全表 `(config_key, version, value_json)` 的 sha256
  - `system_settings` 全表 `(key, value)` 的 sha256
  - `VOLATILITY_CAMPAIGNS` 的 `(version, sha256)` 与条数
- [ ] 现有备份有效：`data/backups/manifest-<date>.json` 存在且 `database_integrity == "ok"`

## 1. 停止

- [ ] 双击 `stop.bat`（级联杀所有活跃 Worker，按 PID 强杀，不留孤儿）
- [ ] `data/trading-service*.pid` 全部消失
- [ ] `Get-Process python*` 中无本项目 Worker（注意 `pythonw` 与 `python` 两种映像）
- [ ] 雷达 `data/volatility-radar.pid` 消失
- [ ] `.lock` 文件不再被占用（能打开库即通过）

## 2. 备份（**必须在停机后做**，否则备份抓到一半在飞的事务）

- [ ] 用 `store.backup()`（SQLite backup API），**不要用 `shutil.copy2`**
      （WAL 锁会报 `WinError 33`）
- [ ] 校验 `data/backups/manifest-<date>.json` 的 `database_integrity == "ok"`
- [ ] 记录备份文件路径 + sha256

## 3. 迁移 B（risk_limits 账户级）

- [ ] `scripts/migrate_risk_limits_to_accounts.py --apply`
- [ ] 回读：`real_account_config:*` 的 `risk` 段已写入，**10 个字段齐全**
- [ ] 回读：**顶层 `max_order_notional` 仍是 2500**（未被迁移改写）
- [ ] 回读：`RISK_LIMITS_ENV_FALLBACK` 审计计数**不再增长**

### 3.1 补模拟租户第二层种子（`risk_limits:futu_paper_sandbox`）

- [ ] 用**当前生效值**灌入（零行为变化）—— 迁移 B 的配套，缺了它模拟侧仍会 env 回落
- [ ] 回读：键存在且值与灌入前生效值一致
- [ ] 回读：`RISK_LIMITS_ENV_FALLBACK` 中 `租户=futu_paper_sandbox` 的计数停止增长

## 4. #1 第 1 步（代码随窗口重启加载；**零行为改动**）

- [ ] 新签名已落盘：`config.load_volatility_campaigns(tenant_id=None)` /
      `save_volatility_campaigns(..., tenant_id=None)` / `store.campaigns()` 接受租户键
- [ ] 键构造复用 `tenant.campaigns_config_key`，不另拼
- [ ] **等价守卫绿**：同一份数据，「旧调用方式」与「新签名 `tenant_id=None`」
      读到的键、写回的键、返回内容**逐个等价**
- [ ] 回读：`VOLATILITY_CAMPAIGNS` 仍 8 条、每条带 `max_position_loss_usd = 300.0`、
      `version` 未变

## 5. 回读比对（启动前）

- [ ] `runtime_config` 全表指纹与迁移前对比：**一字不变**（迁移 B 与 #1 第 1 步都不动它）
- [ ] `system_settings` 全表指纹：**只应新增迁移涉及的键**，其他键一字不变
- [ ] 任一项不符 → **停止，不启动，走回滚**

## 6. 启动

- [ ] 双击 `start.bat`
- [ ] 确认 `PYTHONUNBUFFERED=1` 生效：启动后 60 秒内日志文件有新行
- [ ] 日志落点正确：
      - 雷达 → `data/futu-logs/futu-radar.log`
      - Worker → `data/service-futu_real_master.log` / `service-futu_paper_sandbox.log`

## 7. 启动后验证

- [ ] 两个 Worker PID 与 `.pid` 文件一致；`.lock` 被持有
- [ ] **雷达 CPU 有增长、日志有新行**（本次修复的核心目标：不再零输出）
- [ ] 下一轮 `volatility_long_gamma` 读到 8 条 campaign
- [ ] `reconciliation_reports` 新写入且 MATCHED
- [ ] 有事件时 `alerts` 新增且 `delivered = 1`
- [ ] 飞书告警通道端到端探针通过
- [ ] 两个租户的 `RISK_LIMITS_ENV_FALLBACK` 计数**均停止增长**
- [ ] `VOLATILITY_CAMPAIGNS` version/条数/内容与窗口前一致（#1 第 1 步零行为改动的运行期证据）

## 8. 回滚

- [ ] **手术式**：删掉本次新增的键（秒级）—— 命令见 Migrator 清单
- [ ] **兜底**：`stop.bat` → 备份当前状态 → 用 `data/backups/trading-<date>.db`
      覆盖 `data/trading.db`（**覆盖后必须删 `-wal`/`-shm`**）→ `start.bat`
- [ ] 回滚后核对 `VOLATILITY_CAMPAIGNS` version/条数、`risk` 段是否已清除

---

## 不在本次窗口（留待后续）

| 项 | 为什么不能进 |
| :--- | :--- |
| **迁移 A**（campaigns 租户键） | 读点 17 处仍是全局键，单独 apply 会写一份没人读的快照；必须与 #1 第 3/4 步同批 |
| **#1 第 3/4 步**（写点 9 处传租户、读点 17 处切租户读） | 真正切流；与迁移 A 有硬顺序，先做会激活"写进去当场被剪枝删掉"的 bug |

**迁移 A 的输入已就绪**：服务每轮把**券商侧持仓**写进 `broker_snapshots` 表，
键为 `positions_futuhk_real` 与 `positions_sim_primary`（含 `code / qty / acc_id`），
实测 2026-10-23T08:30:36Z（北京 16:30）刚刷新过 —— **不需要新探针**。
窗口内读这两个键前，先校验 `captured_at` 在 5 分钟内，过期 fail-closed 拒绝 apply。

---

## 已知风险 / 踩过的坑

| 坑 | 说明 |
| :--- | :--- |
| **`stop.bat` 会 `pause`** | 脚本末尾（和失败分支）有 `pause`，**双击运行没问题，但非交互执行会卡住等按键**。窗口内请人工双击，不要脚本化 |
| **条件 UAC 弹窗** | 普通权限杀不干净时会自动提权重试（弹一次 UAC）。Worker 是管理员权限启动时才会触发，届时点「是」 |
| 备份方式 | 必须 `store.backup()`；`shutil.copy2` 打不开 WAL 锁住的生产库（`WinError 33`） |
| 取库快照分析 | 必须连 `-wal`/`-shm` 一起看，否则高 churn 表（`reconciliation_reports`）读不出来 |
| temp 目录 | 大表 `GROUP BY` 需要 temp b-tree；temp 目录不可写时报 `unable to open database file`。加 `PRAGMA temp_store=MEMORY` |
| `stop.bat` 身份校验 | **已修（06:35:06）**：`:pid_identity_ok` 验映像名 + 命令行标记，`IDENTITY_FOREIGN` 时不盲杀；`:is_alive` 复查同样验身份。窗口内仍建议人工核对进程清单 |
| 日志缓冲 | 已加 `PYTHONUNBUFFERED=1`（`start.bat` / `launch_workers.py` / `deploy-windows.ps1`） |
| 心跳脏数据 | `strategy_heartbeats` 按 rowid 取「最新」会取到旧的 `STOPPED`；判存活要按 `updated_at` |
| **持仓口径** | **券商快照（`broker_snapshots`）≠ 意图账本（`order_intents` FILLED 净额）**。分配/对账一律用前者 |
