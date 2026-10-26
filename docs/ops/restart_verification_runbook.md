# 重启后核验 Runbook（通用，2026-10-27 起）

> 归口：Ops。触发场景：任何 `stop.bat` → `start.bat`（含发版重启、回滚重启、日常重启）。
> 原则：**四项冒烟任一不过 → 不继续观察，先按 §5 回滚**。
> 关联：停机窗口前置动作见 [stop_start_window_checklist.md](stop_start_window_checklist.md)；巡检项见 [patrol_runbook.md](patrol_runbook.md)。

---

## 1. 停机前（30 秒确认）

- 已在群里说明「我重启一下」（预期变更，Ops 不当故障追查）。
- 无在飞订单（`order_intents` 最近 5 分钟无新建 `PENDING`）。
- 记录重启前基线（任选其一持久化）：
  - `schema_migrations` 最高版本（当前应为 **20**）；
  - `audit_events` 中最后一条 `MIGRATION_APPLIED` 的 `id`；
  - `data/*.pid` 的 PID 清单。

## 2. 启动后四项冒烟（按顺序，约 1 分钟）

### ① 进程与 PID 一致

- 8 个进程：2 Worker × 2 租户（`pythonw`）+ 2 雷达（`python`）+ 2 Streamlit。
- `data/trading-service-<tenant>.pid`、`data/volatility-radar.pid` 的 PID 必须与活进程一致，且 `StartTime` 晚于本次重启时刻。
- 注意 `python.exe` 与 `pythonw.exe` 两种映像都要看。

### ② 心跳

- `strategy_heartbeats`：`trading_service:futu_real_master`、`trading_service:futu_paper_sandbox`、`trading_service` 均 `HEALTHY`，`updated_at` 在 2 分钟内。
- `option_history` 心跳 `DEGRADED` **属预期**（如实降级，不是故障）：文案形如「采集 n 个合约；可用 m，降级 k」。若变成「缺失 n」才需要查采集。
- `volatility_scanner` 心跳要有新的「已扫描 X 个标的」文案。

### ③ 无新 `MIGRATION_APPLIED`

- `schema_migrations` 最高版本与重启前基线一致（当前 **20**）。
- `audit_events` 中 `MIGRATION_APPLIED` 的 `id` 不得大于重启前基线 id。
- **出现新行 = 未经授权的迁移被触发 → 立即停止观察，按 §5 回滚并上报**（2026-10-26 00:06:44 事故形态：Worker 子进程继承 env 后提前迁移）。

### ④ 雷达确实在跑（**必看，别只看 .pid**）

- 雷达日志 `data/futu-logs/futu-radar.log`（stdout 与 SDK 日志同一文件）在启动后 2 分钟内有新行。
- 雷达进程 `StartTime` 晚于重启时刻且仍是活进程。
- 若 `.pid` 存在但进程不在，或日志零输出 → 继续按 §3 判定。

## 3. 雷达专项：待迁移库 exit 2 与「先起后退」

### 3.1 待迁移库上雷达不会自迁

- `scripts/run_radar_service.py:185-189`：雷达进程**没有迁移授权**（授权只在 Worker 进程内，见 `migrations.authorize_migrations`）。
- 若库里有 pending 迁移，雷达**不会自行迁移**，而是打印中文报错
  `波动率雷达无法启动（数据库待迁移）：…` 并以 **exit code 2** 退出，且不写 `.pid`。
- 处置：说明库确有待迁移项 → 先让 Worker 完成迁移（Worker 启动路径有授权），再确认雷达被补起；**不要**为了雷达去设置任何迁移授权环境变量（env 授权已废除，设置无效且属违规路径）。

### 3.2 `start.bat` 并行拉起 → 雷达可能先起后退，由 Worker 补起

- `start.bat` 并行启动 Worker 与雷达；若雷达先启动时库还没迁移完，就会按 §3.1 退出。
- Worker 启动后会调用雷达控制入口（`scripts/run_service.py:1576-1578` 的 `radar_control.start()`）；成功即补起雷达，失败则写
  `operational_events`：`volatility_scanner` / `AUTO_START` / `DEGRADED`（附 error）。
- 因此**重启后必须核对两件事**：
  1. 雷达进程真的在跑（不能只看 `.pid`，见 §2④）；
  2. 若在跑，确认它不是「上一次遗留的旧进程」——比对 `StartTime` 与本次重启时刻。
- 若出现 `AUTO_START/DEGRADED` 事件且雷达仍不在跑：把该事件的 `error` 原文连同雷达日志尾部一起上报，不要反复重启碰运气。

## 4. 数据面核对（可选，发版重启时必做）

- 新权益快照：`equity_snapshots` 最近 5 分钟内有新行（两个 env 各一条）。
- `mode=shadow` 未被重置；`risk_state` 行数与重启前一致（正常为 0 行）。
- 对账：`reconciliation_reports` 新一轮为 `MATCHED`；出现 `MISMATCH` 立即上报（不可静默）。

## 5. 回滚（任一项不过）

1. `stop.bat`；
2. 退 tag：`git checkout <上一版 tag>`（或 `git revert` 发版 commit）；
3. `start.bat`；
4. 重新跑 §2 四项冒烟，并在群里报「已回滚 + 现象 + 证据」。

## 6. 已知坑

| 坑 | 说明 |
| :--- | :--- |
| 只核 PID 文件 | `.pid` 可能是上一次遗留；必须同时核 `StartTime` 与进程存活 |
| 只看心跳 HEALTHY | 包装器可能把 DEGRADED 覆盖（已修）；仍要单独看 `option_history` 与 `volatility_scanner` 的 message 原文 |
| 用 python 打开生产库 | 部署后、重启前**禁止**在生产目录跑任何会构造 `TradingStore()` 的脚本（会触发待迁移检查甚至迁移） |
| 雷达「在跑」但无输出 | 看 `data/futu-logs/futu-radar.log` 是否有新行；零输出等于没在采集 |
