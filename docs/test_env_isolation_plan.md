# 测试环境隔离（Tier 1）—— 实施计划草案

- 上游：`docs/test_environment_isolation_design.md`（已定稿）+ 待补 PRD / 实施方案
- 本文件：Chief 出的**执行计划草案 + 6 个决策点的默认裁决**（Architect 可反驳，反驳需给理由）
- 硬约束：**不触碰生产运行时**（生产目录、生产库 `data/trading.db`、生产锁均不动）；生产相关改动仍等收盘后

## 一、6 个决策点（Chief 裁决草案）

| # | 问题 | 裁决 | 理由 |
|---|---|---|---|
| 1 | 199 个未提交改动如何固化且**不换文件** | 在**当前目录**执行 `git checkout -b trade-live`（`test` 已在 b7c1be7，与 `main` 同 commit，**切分支不改变任何文件**）→ `git add -A && git commit -m "snapshot: 2026-10-23 生产运行态"`。**全程不 checkout 到不同 commit**，文件内容不变 | 低风险；提交只写 `.git`，不动工作区文件 |
| 2 | 生产目录的分支归属 | 原设计「生产永远停 main」与「提交到 test」自相矛盾。改为：**生产目录停在专用分支 `trade-live`**（内容=当前运行代码）；`main` 保留基线 b7c1be7 作回滚点；`test` 分支只给测试区用 | 生产目录不换内容，分支名仅作标签 |
| 3 | worktree 位置 | `E:\Thisyi\trade-test`（与生产同级、同盘，267GB 可用） | 跨盘会拖慢 `git worktree` 与 pytest I/O |
| 4 | venv | 测试区**新建独立 venv**（`uv sync --extra dev`），不复用生产 `.venv` | 两区并发跑 pytest 时复用 venv 会有 `.pyc`/`uv` 锁竞争；独立 venv 是硬隔离 |
| 5 | OpenD | 共享生产 OpenD `127.0.0.1:11111`，**只读行情、禁止下单**（`TRADEYI_ENV=test` + 桩 broker） | 第二台 OpenD 需第二账户，本期不做（Tier 2） |
| 6 | 生产库拒连守卫 | `store.py` 初始化时：`TRADEYI_ENV=test` 且解析出的 DB == 生产 `data/trading.db` → **直接抛错拒绝启动**；配守卫测试（干净路径放行 / 脏路径拒绝，变异证红） | 用户要求「混淆生产库必须报错且可读」 |

## 二、执行步骤（过闸 + 用户确认后执行）

| 步 | 动作 | 判据 | 责任人 |
|---|---|---|---|
| S1 | 生产目录：`checkout -b trade-live`（同 commit）→ `add -A` → `commit` | 提交前后 `git status` 仅少了未提交项，**工作区文件 mtime/内容不变**；服务不重启、无异常 | Chief |
| S2 | `git worktree add E:\Thisyi\trade-test test` | `git worktree list` 出现两条；两区 `git status` 互不影响 | Chief |
| S3 | 测试区：`uv sync --extra dev` 独立 venv | `pytest --collect-only` 可跑；生产 `.venv` 未被改 | Chief |
| S4 | 测试区独立配置：`TRADEYI_ENV=test`、UI 8502、独立 `data/` | 启动不抢 8501；独立 `.lock`/`.pid` 落在测试区 `data/` | Coder |
| S5 | 生产库拒连守卫 + 守卫测试 | 指着生产 DB 启动 → 拒绝并给可读错误；变异测试转红 | Coder |
| S6 | 验收（设计 §6 四条） | ①测试区跑全量 pytest，生产库 sha 前后不变 ②两区锁不冲突 ③`TRADEYI_ENV=test` 连生产库被拒 ④生产目录分支与工作树未被测试操作改动 | Tester + Reviewer |

## 三、回滚

- 撤 worktree：`git worktree remove E:\Thisyi\trade-test`；分支 `trade-live` 可保留（内容等于生产运行态，删分支不影响工作区文件）；
- 完全回到现状：`git checkout main`（**只在确认主窗口已停、不再跑代码时**做）；
- 全程不删任何数据：生产 `data/` 只读不改。

## 四、验收后即生效的能力

隔离完成后：**开发/测试可随时开工，不再受交易时段限制**（改的是 `trade-test`，生产目录与生产库不被触碰）；生产部署仍走「全量回归绿 + Reviewer 签 + 收盘窗口」。
