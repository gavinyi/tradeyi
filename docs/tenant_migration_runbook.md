# 租户化迁移 Runbook 与回滚剧本（停 → 迁移 → 起）

> 版本：2026-10-23 草案（Builder 起草，待 Chief/Reviewer 评审）
> 适用：**P0-7 参数租户化**（迁移 B）+ **campaign 分区租户化**（迁移 A）的**同一个停服窗口**
> 性质：**不可逆生产写**。本文件存在的意义是：**关键时刻不靠记忆操作。**

---

## 0. 一句话原则

**窗口内每一步都有「通过判据」，判据不是"没报错"，而是可核对的输出/退出码/不变量。**
任一步不满足判据 → **立即停手，走第 4 节回滚**，不"先起服务看看"。

---

## 1. 涉及的两条迁移与它们的真实接口

| | 迁移 A（campaign 分区） | 迁移 B（风险参数） |
| :--- | :--- | :--- |
| 脚本 | `scripts/migrate_volatility_campaigns_tenant_scope.py` | `scripts/migrate_risk_limits_to_accounts.py` |
| 写什么 | `runtime_config` 的 `VOLATILITY_CAMPAIGNS:{tenant_id}` | `system_settings` 的 `real_account_config:{account_id}` 的 **`risk` 段** |
| 旧键 | `VOLATILITY_CAMPAIGNS`（`runtime_config`） | 无（`risk` 段是**新增**子键，不改顶层） |
| 默认模式 | dry-run | dry-run |
| 落库开关 | `--apply` | `--apply` |
| 备份 | `--apply` 时自动做（`--skip-backup` 可跳过） | 同左 |
| 备份目录 | `data/qa/`（`--backup-dir` 可改） | 同左 |
| 磁盘阈值 | `--min-free-gb`，默认 **4.0 GB** | 同左 |
| 报告 | `data/qa/<时间戳>_volatility_campaigns_tenant_scope_{dryrun\|apply}.md` | `data/qa/<时间戳>_risk_limits_to_accounts_{dryrun\|apply}.md` |

**共同前置**：`$env:PYTHONPATH="src"`，用 `.venv\Scripts\python.exe` 跑。

### 1.1 退出码（判据的核心）

| 退出码 | 迁移 A | 迁移 B |
| :--- | :--- | :--- |
| **0** | 成功（含 dry-run） | 成功（含 dry-run） |
| **2** | 归属冲突：同一 code 在旧键里出现多次 | — |
| **3** | 旧键在写入前消失 → 中止 | — |
| **4** | 无前缀旧键内容发生变化 → 立即检查 | `RISK_*` 生效值在迁移过程中变化 → 立即检查 |
| **5** | 残留核查发现未平的虚拟持仓/批次 → 禁止 `--apply` | — |

> **只有 0 是通行证。** 其他任何码都进第 4 节。

---

## 2. 窗口前（T-30 分钟起，服务仍在跑）

### 2.1 备份校验

```powershell
$env:PYTHONPATH="src"
# 1) 看最新备份与 manifest
Get-ChildItem data\qa\trading.db.bak-*.db | Sort-Object LastWriteTime -Descending | Select-Object -First 3
Get-ChildItem data\qa\trading.db.bak-*.manifest.json | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

**通过判据**：
- [ ] 存在**本次窗口专用**的备份（时间戳在窗口开始前 15 分钟内）
- [ ] 对应 `.manifest.json` 里 `quick_check == "ok"`、`page_count > 0`、`size_bytes > 0`
- [ ] 记录 `sha256`（回滚后要比对）

### 2.2 磁盘余量

```powershell
Get-PSDrive E | Select-Object Used, Free
```

**通过判据**：`Free` ≥ **8 GB**（脚本自身阈值是 4 GB，留一倍余量给回滚时的临时文件）。

### 2.3 dry-run 输出必须与留档一致
窗口前各跑一次 dry-run，把报告路径记下来：

```powershell
.\.venv\Scripts\python.exe scripts\migrate_volatility_campaigns_tenant_scope.py
.\.venv\Scripts\python.exe scripts\migrate_risk_limits_to_accounts.py
```

**通过判据**：
- [ ] 迁移 A：`[迁移] DRY-RUN 结束，未写库`，退出码 0；报告已落盘
- [ ] 迁移 A：**无 `⛔ 存在未平风险`**（若有 → **禁止进窗口**，先人工平仓）
- [ ] 迁移 A：无 `同一 code 出现多次`
- [ ] 迁移 B：`DRY-RUN 结束，未写库`，退出码 0；报告已落盘
- [ ] **两份 dry-run 报告已归档**（飞书/本目录），窗口内的输出要与它们**逐项一致**

> **口径差异不阻塞**：迁移 A 报告里"设计 11 条 / 实测 N 条"是**已登记的口径差异**，按 `DESIGN_DOC_EXPECTED_CAMPAIGNS` 常量留痕。但**实测条数与留档必须一致** —— 不一致说明库在窗口前被人动过。

### 2.4 生成窗口前基准（`--baseline` 用）

第 5 节的三项判据（旧键逐字节不变 / env 回落不增长 / 期权开仓裁定值）需要**窗口前的基准值**，否则只能 SKIP。

基准文件 `data/qa/pre_window_baseline.json` 的字段：

```json
{
  "legacy_campaigns_sha256": "<VOLATILITY_CAMPAIGNS 原文的 SHA256>",
  "legacy_campaigns_bytes": 12345,
  "env_fallback_count": 2,
  "entry_paused": {"futu_real_master": "true", "futu_paper_sandbox": "false"},
  "account_configs": {"281756481476169109": "<real_account_config 原文>"}
}
```

**注意**：`data/qa/pre_window_baseline.json` 当前那份是**本次起草时的快照**，
**窗口前必须重新生成**（库可能已变）。生成时把 `entry_paused` 的两个值改成
**当时的首席裁定值**，不要照抄文件里的旧值。

---

## 3. 窗口内（逐步，每步有判据）

### 3.1 停服

```bat
stop.bat
```

**通过判据**：
- [ ] 退出码 0，四步全 OK
- [ ] 输出含 `[OK] 所有租户 Worker 已确认停止`
- [ ] **无 `[SKIP] ... 不是我们的进程`**、**无 `[WARN] ... 身份无法确认`**（有则说明 PID 判定存疑 → 停下人工确认，**不盲杀**）
- [ ] `data\trading-service-futu_real_master.pid`、`trading-service-futu_paper_sandbox.pid`、`volatility-radar.pid` 已清理
- [ ] 进程表无残留 python（`process_utils.pid_alive(pid, python_only=True)` 口径）

> **铁律**：迁移期间**不得有旧 Worker 在跑** —— 旧代码会再写无前缀旧键，迁移的不变量会被破坏。

### 3.2 迁移 A（campaign 分区）

```powershell
.\.venv\Scripts\python.exe scripts\migrate_volatility_campaigns_tenant_scope.py --apply
```

**通过判据**：
- [ ] 退出码 **0**
- [ ] 输出含 `[迁移] 备份完成：...` 且 `quick_check=ok`
- [ ] 输出含 `[迁移] 不变量通过：VOLATILITY_CAMPAIGNS 逐字节不变（N 字节）`
- [ ] 输出含 `[迁移] APPLY 报告已覆盖落盘：...`
- [ ] **记录这份报告的落盘路径**（回滚时要引用）

### 3.3 迁移 B（风险参数）

```powershell
.\.venv\Scripts\python.exe scripts\migrate_risk_limits_to_accounts.py --apply
```

**通过判据**：
- [ ] 退出码 **0**
- [ ] 输出含 `备份完成：...` 且 `quick_check=ok`
- [ ] 输出含 `不变量通过：RISK_* 生效值逐项不变（仍作兜底）`
- [ ] 输出含 `APPLY 报告已覆盖落盘：...`

### 3.4 起服务

```bat
start.bat
```

**通过判据**：
- [ ] 退出码 0，逐租户 `[n/n] Starting` 全部出现且**不重复**
- [ ] `data\trading-service-futu_real_master.pid`、`data\trading-service-futu_paper_sandbox.pid`、`data\volatility-radar.pid` 均生成
- [ ] **连续跑两次 `start.bat`，第二次不得出现 `Starting`**（防"以为死了又起一个"）

### 3.5 窗口内禁止事项

- ❌ 不得 `--skip-backup`（除非已有同窗口内的有效备份且经 Chief 同意）
- ❌ 不得在迁移 A 与 B 之间插入其他写库操作
- ❌ 不得在判据未全部打勾前起服务

---

## 4. 失败回滚剧本

**触发条件**：3.1–3.4 任一步的判据未满足，或退出码非 0。

### 4.1 回滚基准

**用迁移 A 的备份**（`data/qa/trading.db.bak-<A的时间戳>.db`）—— 它是在**任何写入之前**做的，恢复它可同时回退 A 与 B。

> 迁移 B 自己也会做备份，但那份是**在 A 写入之后**做的，恢复它只会回退 B、留下 A 的写入。**回滚一律用 A 的备份。**

### 4.2 回滚步骤

```powershell
# 1) 确认没有 Worker 在跑（回滚期间服务必须停着）
stop.bat

# 2) 记录当前库的指纹，便于事后追溯
(Get-FileHash data\trading.db -Algorithm SHA256).Hash

# 3) 用 A 的备份覆盖
Copy-Item data\qa\trading.db.bak-<A的时间戳>.db data\trading.db -Force

# 4) 校验恢复结果
Get-Content data\qa\trading.db.bak-<A的时间戳>.db.manifest.json
```

**通过判据**：
- [ ] 恢复后 `trading.db` 的 SHA256 == manifest 里的 `sha256`
- [ ] `runtime_config` 的 `VOLATILITY_CAMPAIGNS` **逐字节等于**迁移前留档的旧键原文
- [ ] `runtime_config` 里**不存在** `VOLATILITY_CAMPAIGNS:{tenant}` 键
- [ ] `system_settings` 的 `real_account_config:*` 里**不存在** `risk` 段
- [ ] `system_settings` 的 `real_account_config:281756481476169109` 内容与留档一致（顶层 `max_order_notional=2500` / `max_daily_notional=5000` 未被改动）

### 4.3 回滚后是否允许起服务

**允许，但必须同时满足**：
1. 4.2 的判据全部打勾；
2. **用窗口开始前的那份代码起服务** —— DB 与代码必须匹配。
   （新代码对旧键有读侧兜底，理论上也能跑；但回滚场景下**不要用新代码**，避免"DB 回退了、代码没回退"制造新的不一致。）
3. 期权开仓红线状态**回到窗口前的裁定值**（不因回滚而改变）。

### 4.4 回滚后必须做的留痕

- [ ] 把失败那一步的**完整 stdout/stderr** 存档到 `data/qa/`
- [ ] 在 `data/qa/` 写一份回滚记录：触发步骤、退出码、恢复的备份路径、SHA256 比对结果
- [ ] **在群里报**：失败步骤 + 退出码 + 回滚是否成功（不要静默回滚）

---

## 5. 起服务后必核清单（≥8 项）

**直接跑可执行脚本（只读，不写库/不起停进程）**：

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe data\qa\verify_after_migration.py --baseline data/qa/pre_window_baseline.json
# 机器可读：加 --json
```

**退出码**：`0` = 无 FAIL；`1` = 有 FAIL；`2` = 用法错误。**WARN/SKIP 不算失败，但必须逐条看。**

逐项打勾，**任何一项 FAIL 都要在群里报**，不要"先观察一下"。

| # | 检查项 | 判据 | 命令/位置 |
| :--- | :--- | :--- | :--- |
| 1 | 两个 Worker 存活 | 实盘 `trading-service-futu_real_master.pid`、模拟 `trading-service-futu_paper_sandbox.pid` 存在且进程活着 | `process_utils.pid_alive` 口径 |
| 2 | 雷达存活 | `data\volatility-radar.pid` 存在且进程活着；**日志确实在写**（当前已知缺口：re-exec 后重定向丢失） | 看 `volatility-radar.log` 尾部时间戳 |
| 3 | 策略心跳 | `strategy_heartbeats` 有新行写入（近 5 分钟内），且两个 Worker 对应行 `status='HEALTHY'` | `SELECT strategy_id, status, MAX(updated_at) FROM strategy_heartbeats GROUP BY strategy_id` |
| 4 | 对账连续 MATCHED | 近 3 份 `reconciliation_reports` 全部 `status='MATCHED'`，`difference=0` | 按 `created_at` 倒序查 |
| 5 | 租户键已存在 | `runtime_config` 有 `VOLATILITY_CAMPAIGNS:{tenant}`；`system_settings` 的 `real_account_config:*` 有 `risk` 段 | 见 §2.3 的键名 |
| 6 | **无前缀旧键零写入方** | 旧键 `VOLATILITY_CAMPAIGNS` 内容**与迁移前留档逐字节相同**；代码里无任何写入方 | `SELECT value_json FROM runtime_config WHERE config_key='VOLATILITY_CAMPAIGNS'` |
| 7 | env 回落不再增长 | `system_settings` 的 `risk_limits_env_fallback_count` **不再增长**（迁移后应恒为迁移前的值） | `SELECT value FROM system_settings WHERE key='risk_limits_env_fallback_count'` |
| 8 | 期权开仓状态符合当前裁定 | 实盘 `volatility_new_entries_paused:futu_real_master` 与模拟 `volatility_new_entries_paused:futu_paper_sandbox` 的值**与 Chief 当前裁定一致** | 红线未解除时实盘必须为 `true` |
| 9 | 无 P0-7 配置不完整告警 | 无新增 `RISK_LIMITS_CONFIG_INCOMPLETE` / `RISK_LIMITS_ENV_FALLBACK` 审计 | `audit_events` 按时间倒序 |
| 10 | 服务日志无异常 | 两个 Worker 日志尾部无 `ERROR` / `Traceback` | 各自日志文件尾部 |

---

## 6. 已知缺口与注意事项

1. **`real_account_config:*` 在 `system_settings`，`VOLATILITY_CAMPAIGNS*` 在 `runtime_config`** —— 两套设置表，**这是"一个概念两个地方"**。写查询时必须确认表名，别混。
2. **`risk_limits_env_fallback_count` 迁移前已是 2**（06:36 那次重启产生）。迁移后它**不应再增长**；若增长，说明仍有账户/租户没配好，风控在读 env。
3. **`risk` 段的 `max_entry_notional` / `max_entry_daily_notional`** 是**风控引擎**（`RiskEngine`）参数；账户**顶层**的 `max_order_notional` / `max_daily_notional` 是**实盘门禁**（`real_trading.check_real_trade`）参数。**两者数值不同是设计，不是冲突**（实测：门禁 2500/5000，风控 100000/100000）。
4. **迁移 B 只写 REAL 且 `is_active=True` 的账户** —— 模拟账户的参数走第二层 `risk_limits:{tenant_id}`，本脚本不写它。
5. **雷达日志路由**：当前 re-exec 后 `start /b` 的重定向会丢失（Ops 在处理）。核第 2 项时若日志没动，**先确认进程活着**再判断。

---

## 7. 速查

```powershell
$env:PYTHONPATH="src"

# 窗口前
.\.venv\Scripts\python.exe scripts\migrate_volatility_campaigns_tenant_scope.py
.\.venv\Scripts\python.exe scripts\migrate_risk_limits_to_accounts.py

# 窗口内
stop.bat
.\.venv\Scripts\python.exe scripts\migrate_volatility_campaigns_tenant_scope.py --apply
.\.venv\Scripts\python.exe scripts/migrate_risk_limits_to_accounts.py --apply
start.bat

# 回滚
stop.bat
Copy-Item data\qa\trading.db.bak-<A的时间戳>.db data\trading.db -Force
start.bat
```

**窗口内任何一步的退出码不是 0 → 停手 → 第 4 节。**
