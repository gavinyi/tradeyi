# 测试环境运行手册（全流程验证用）

> 用户要求（2026-10-24）：测试环境必须能跑**全部功能**的全流程验收，用**模拟盘账号**；
> 且**绝不碰生产库**。本手册记录怎么把测试区跑起来、怎么验证、怎么停。

## 1. 与生产的隔离（三条硬边界）

| 维度 | 生产 | 测试区 |
|---|---|---|
| 目录 | `E:\Thisyi\trade`（master） | `E:\Thisyi\trade-test`（test） |
| 数据库 | `trade\data\trading.db` | `trade-test\data\trading.db` |
| Streamlit 端口 | 8501 | **8601** |
| 租户 | `futu_real_master` + `futu_paper_sandbox` | **只跑 `futu_paper_sandbox`（SIMULATE）** |
| 环境变量 | `TRADEYI_ENV` 不设 | `TRADEYI_ENV=test` |

`scripts/test_env.py` 会**拒绝**在生产目录或开发 worktree 里启动（`assert_not_production`），
并且只把 DB 指向测试工作区内。

## 2. 启动 / 状态 / 停止

```powershell
cd E:\Thisyi\trade-test

# 初始化测试库（正式 schema + 全部版本化迁移；不写任何生产数据）
python scripts\test_env.py start --dry-run   # 先看会拉起什么
python scripts\test_env.py start             # 初始化 DB 并拉起 worker/雷达/UI

python scripts\test_env.py status            # 看 DB 路径、端口、PID 文件
python scripts\test_env.py stop              # 按 PID 文件停止（只动测试区）
```

启动后：
- UI：<http://127.0.0.1:8601>
- 测试库：`trade-test\data\trading.db`（第一次 `start` 时由 `TradingStore.initialize()` 建好）

## 3. 全流程验收清单（照着核）

| # | 项目 | 判据 |
|---|---|---|
| 1 | 迁移 | `schema_migrations` 含 1–14；`candidate_exclusions`/`ml_retrain_runs` 表存在；`audit_events.actor` 列 + `idx_audit_actor` 存在 |
| 2 | 服务 | worker/雷达/UI 三个 PID 文件存在，进程命令行含对应脚本名 |
| 3 | 心跳/任务 | `strategy_heartbeats` 新鲜；`task_runs` 最近状态 SUCCESS |
| 4 | 告警链路 | 告警投递中心能看到记录与 `delivered` 状态；投递探针 6 小时一次 |
| 5 | 候选质量 | 候选质量中心能显示硬排除/软降权明细与 P50/P95 |
| 6 | ML 状态 | ML 模型状态页可打开，显示「默认规则打分」；配置区可保存并按租户生效 |
| 7 | 页面 | `python scripts\check_streamlit_pages.py` → 43+ 页 0 错 0 警告 |
| 8 | 隔离 | 全流程跑完，生产库 `runtime_config` 指纹不变（生产区跑全量那轮才有该证据）|

## 4. 注意

- **不要在测试区 seed `data/trading.db` 后再去做生产指纹比对**：那会让守卫拿测试库和
  自己比，看起来"未改动"，其实什么都没验（Reviewer 明确要求）。
- 测试区跑完要看的是「功能通不通」，不是「策略赚不赚」；下单走 SIMULATE。
- 与生产共用 OpenD（127.0.0.1:11111）；**Futu 客户端需要已登录 SIMULATE 账户**。
- 生产进程只能由桌面双击 `start.bat` 启动；测试区用 `scripts/test_env.py`，互不干扰。
